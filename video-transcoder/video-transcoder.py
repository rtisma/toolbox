#!/usr/bin/env python3
"""Re-encode a video (or a whole directory of them) to H.265 (default) or H.264
with a sane bitrate for its resolution.

Uses CRF (quality-based) encoding rather than a fixed bitrate, capped with
-maxrate/-bufsize as a safety ceiling (not a target) so a busy scene can't
balloon the file. The cap is picked from the source's own resolution (or
--height, if you're also downscaling): H.264 uses YouTube's published
upload-bitrate guidance directly (a size-conscious ceiling); H.265's cap
is set generously (~3.5x H.264/2) so it only binds on genuinely pathological
content, since CRF -- not the cap -- should be doing the quality work.

Output files are named <basename>.converted.mp4 (myfile.mov -> myfile.converted.mp4).
Accepts one or more inputs, each a video file or a directory of them, in any
mix -- e.g. "clip1.mov clip2.mov ./videos/". Whenever that resolves to more
than one file, --jobs controls how many run at once, and a JSON report with
per-file space savings and VMAF scores is written. Each in-flight file gets
a live progress bar (percent/time/speed) in the terminal; concurrent
conversions each get their own line, stacked.

VMAF scoring (on by default) requires ffmpeg to be built with libvmaf; this
is checked up front, before any conversion starts, and the script exits
with an error (not a silent skip) if it's missing — pass --no-compare to
proceed without it. Use "check-libvmaf" to check this directly, and
"compare" to VMAF-score a file you've already converted, without
re-running any conversion.

--target-vmaf auto-discovers --crf (and a derived --maxrate) instead of
using the fixed defaults: it extracts a few short sample clips from across
one input, binary-searches CRF against them for the highest value (smallest
file) that still meets the target VMAF, then measures the tuned CRF's
actual bitrate to derive a sane --maxrate. When converting more than one
file, this runs once, against the first one, and the result is applied to
the rest — not re-tuned per file — since files from the same recording
session/device are usually similar enough that one file's tuned settings
are a good estimate for the rest.

Three subcommands: "convert" (the default — implied if the first argument
isn't "convert", "compare", or "check-libvmaf"), "compare", "check-libvmaf".

    python3 video-transcoder.py input.mov
    python3 video-transcoder.py input.mov --codec h264 --height 480
    python3 video-transcoder.py input.mov -o out.mp4 --crf 25 --dry-run
    python3 video-transcoder.py input.mov --no-compare
    python3 video-transcoder.py ./videos/ --jobs 3
    python3 video-transcoder.py ./videos/ --jobs 3 --report ./videos/report.json
    python3 video-transcoder.py clip1.mov clip2.mov ./more-videos/ ./even-more-videos/
    python3 video-transcoder.py ./videos/ --retries 2 --slack-bot-token xoxb-... --slack-channel my-channel
    python3 video-transcoder.py compare input.mov input.converted.mp4
    python3 video-transcoder.py check-libvmaf
    python3 video-transcoder.py /mnt/nfs/videos/clip.mov --nfs
    python3 video-transcoder.py ./videos/ --target-vmaf 93
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROGRESS_FIELDS = {
    "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
    "out_time", "dup_frames", "drop_frames", "speed", "progress",
}

VIDEO_EXTENSIONS = {
    ".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm",
    ".wmv", ".flv", ".mpg", ".mpeg", ".m2ts", ".ts",
}

VMAF_RATINGS = [
    (93, "near-transparent, no perceptible loss for most viewers"),
    (80, "good, minor loss visible on close inspection"),
    (60, "noticeable degradation"),
    (0, "poor"),
]

# kbps caps, keyed by vertical resolution, standard frame rate (<=48fps).
# H.264 values are YouTube's recommended SDR upload bitrates -- a size-conscious ceiling
# tuned for typical, already-fairly-clean streaming source. H.265 values are a generous
# (~3.5x H.264/2) safety ceiling, not a target: with CRF-based encoding, this should only
# ever bind on genuinely pathological content (heavy grain/noise, extreme motion), not
# routinely clip the bitrate CRF actually wants. A cap that binds routinely is a bug --
# it silently overrides the CRF quality target, which is what drove VMAF scores as low
# as 50-60 on complex/grainy source (e.g. old camcorder footage) before this was raised.
BITRATE_KBPS = {
    "h264": {2160: 45000, 1440: 16000, 1080: 8000, 720: 5000, 480: 2500, 360: 1000, 240: 500},
    "h265": {2160: 80000, 1440: 30000, 1080: 16000, 720: 8000, 480: 4000, 360: 2000, 240: 1000},
}
HIGH_FPS_MULTIPLIER = 1.5  # applied above 48fps
DEFAULT_CRF = {"h264": 23, "h265": 20}  # h265: lower than h264's 23 -- x265's CRF scale runs
                                          # a few points "harsher" than x264's at the same number
DEFAULT_PRESET = {"h264": "slow", "h265": "slow"}
ENCODER = {"h264": "libx264", "h265": "libx265"}

CRF_SEARCH_RANGE = {"h264": (15, 32), "h265": (12, 32)}  # bounds for --target-vmaf's binary search
TUNE_MAXRATE_HEADROOM = 1.5  # multiplier applied to the tuned CRF's observed sample bitrate


class ProbeError(RuntimeError):
    pass


class DiskSpaceError(RuntimeError):
    pass


NFS_LOCAL_DIR = Path(tempfile.gettempdir()) / "video-transcoder"


def check_disk_space(output_dir, required_bytes):
    """One-shot check: does output_dir's filesystem have at least required_bytes free
    right now? Call this once up front with the combined size of everything about to
    be written, before starting any conversion -- not per file mid-run, since by the
    time a later file starts, earlier ones may already be consuming the space this
    check accounted for."""
    existing = next((p for p in (output_dir, *output_dir.parents) if p.exists()), Path(output_dir.anchor or "/"))
    free = shutil.disk_usage(existing).free
    if free < required_bytes:
        raise DiskSpaceError(
            f"not enough free space for {output_dir}: {human_size(free)} free, "
            f"need at least {human_size(required_bytes)} (combined size of the source file(s) -- "
            "a worst-case estimate, since converted output is almost always smaller)"
        )


def probe_video(path):
    if not shutil.which("ffprobe"):
        raise ProbeError("ffprobe not found on PATH — install ffmpeg")
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise ProbeError(f"no video stream found in {path}")
    stream = streams[0]
    num, _, den = stream["avg_frame_rate"].partition("/")
    fps = float(num) / float(den) if den and float(den) != 0 else float(num)
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return int(stream["height"]), fps, duration


def pick_bitrate_cap(codec, height, fps):
    table = BITRATE_KBPS[codec]
    bucket = next((h for h in sorted(table, reverse=True) if h <= height), min(table))
    cap = table[bucket]
    if fps > 48:
        cap = int(cap * HIGH_FPS_MULTIPLIER)
    return cap


def rate_vmaf(score):
    return next(text for threshold, text in VMAF_RATINGS if score >= threshold)


def format_time(seconds):
    if seconds is None:
        return "?:??:??"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def render_bar(pct, width=24):
    filled = int(width * min(max(pct, 0.0), 100.0) / 100)
    return "#" * filled + "-" * (width - filled)


def estimate_eta(elapsed, duration, speed):
    if duration is None or elapsed is None:
        return None
    try:
        speed_val = float(speed)
    except (TypeError, ValueError):
        return None
    if speed_val <= 0:
        return None
    return max(0.0, duration - elapsed) / speed_val


def format_progress_line(name, pct, elapsed, duration, speed):
    label = f"{name:<24.24}"
    if pct is not None:
        eta = estimate_eta(elapsed, duration, speed)
        return (f"{label} [{render_bar(pct)}] {pct:5.1f}% "
                f"{format_time(elapsed)}/{format_time(duration)} {speed}x ETA {format_time(eta)}")
    return f"{label} {format_time(elapsed)} elapsed {speed}x"


def _parse_ffmpeg_time(value):
    if not value:
        return None
    try:
        hours, minutes, secs = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(secs)
    except ValueError:
        return None


def run_ffmpeg_with_progress(cmd, duration, on_tick=None):
    """Run an ffmpeg command built with -progress pipe:1 -nostats, calling
    on_tick(pct, elapsed, speed) as each progress block arrives (pct is None
    if duration is unknown). Returns (returncode, log_text) where log_text is
    everything ffmpeg printed that wasn't a progress key=value line."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    log_lines = []
    snapshot = {}
    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        if sep and key in PROGRESS_FIELDS:
            snapshot[key] = value
            if key == "progress":
                if on_tick:
                    elapsed = _parse_ffmpeg_time(snapshot.get("out_time")) or 0.0
                    speed = snapshot.get("speed", "0x").rstrip("x") or "0"
                    pct = min(100.0, elapsed / duration * 100) if duration else None
                    on_tick(pct, elapsed, speed)
                snapshot = {}
        else:
            log_lines.append(line)
    proc.wait()
    return proc.returncode, "\n".join(log_lines)


class ProgressBoard:
    """Renders one live-updating progress bar per in-flight file (multiple bars
    stack via ANSI cursor moves when several conversions run concurrently).
    Falls back to periodic plain-text lines when stdout isn't a terminal."""

    def __init__(self):
        self.interactive = sys.stdout.isatty()
        self.lock = threading.Lock()
        self.order = []
        self.lines = {}
        self.drawn = 0
        self._last_plain = {}

    def _erase(self):
        if self.drawn:
            sys.stdout.write(f"\x1b[{self.drawn}A")
            for _ in range(self.drawn):
                sys.stdout.write("\x1b[2K\n")
            sys.stdout.write(f"\x1b[{self.drawn}A")
        self.drawn = 0

    def _draw(self):
        for key in self.order:
            sys.stdout.write("\x1b[2K" + self.lines[key] + "\n")
        sys.stdout.flush()
        self.drawn = len(self.order)

    def update(self, key, text):
        with self.lock:
            if not self.interactive:
                now = time.monotonic()
                if now - self._last_plain.get(key, 0.0) < 3.0:
                    return
                self._last_plain[key] = now
                print(text)
                return
            if key not in self.lines:
                self.order.append(key)
            self.lines[key] = text
            self._erase()
            self._draw()

    def finish(self, key, final_line):
        with self.lock:
            if not self.interactive:
                print(final_line)
                return
            self._erase()
            if key in self.lines:
                self.order.remove(key)
                del self.lines[key]
            sys.stdout.write(final_line + "\n")
            self._draw()


_libvmaf_available = None  # lazily probed once, then cached


def ffmpeg_has_libvmaf():
    global _libvmaf_available
    if _libvmaf_available is None:
        _libvmaf_available = False
        if shutil.which("ffmpeg"):
            result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
            _libvmaf_available = "libvmaf" in result.stdout
    return _libvmaf_available


def run_vmaf(reference, distorted, duration=None, on_tick=None):
    """Compare `distorted` against `reference` and return the pooled mean VMAF score.
    If given, on_tick(pct, elapsed, speed) is called as ffmpeg reports progress
    (pct is None if duration is unknown)."""
    if not shutil.which("ffmpeg"):
        raise ProbeError("ffmpeg not found on PATH — install ffmpeg")
    if not ffmpeg_has_libvmaf():
        raise ProbeError("this ffmpeg build lacks libvmaf (rebuild with --enable-libvmaf, or pass --no-compare)")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        log_path = Path(tmp.name)
    try:
        filter_graph = (
            "[0:v]setpts=PTS-STARTPTS[dist];"
            "[1:v]setpts=PTS-STARTPTS[ref];"
            "[dist][ref]scale2ref=flags=bicubic[dist2][ref2];"
            f"[dist2][ref2]libvmaf=log_fmt=json:log_path={log_path}"
        )
        cmd = ["ffmpeg", "-y", "-i", str(distorted), "-i", str(reference), "-lavfi", filter_graph,
               "-progress", "pipe:1", "-nostats", "-f", "null", "-"]
        returncode, log_text = run_ffmpeg_with_progress(cmd, duration, on_tick)
        if returncode != 0:
            raise ProbeError(f"vmaf comparison failed: {_error_tail(log_text, 300)}")
        data = json.loads(log_path.read_text())
        return data["pooled_metrics"]["vmaf"]["mean"]
    finally:
        log_path.unlink(missing_ok=True)


def human_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def default_output_path(input_path, output_dir=None):
    return (output_dir or input_path.parent) / f"{input_path.stem}.converted.mp4"


def discover_videos(directory):
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and not p.stem.endswith(".converted")
    )


def gather_files(inputs):
    """Expand a mix of file and directory inputs into a flat, deduplicated file list,
    in the order given (directories contribute their sorted contents in place)."""
    files, seen = [], set()
    for p in inputs:
        for f in (discover_videos(p) if p.is_dir() else [p]):
            if f not in seen:
                seen.add(f)
                files.append(f)
    return files


def describe_inputs(inputs, limit=3):
    names = [str(p) for p in inputs]
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", and {len(names) - limit} more"


def pick_sample_starts(duration, count):
    """Evenly space `count` sample start times across the middle 80% of `duration`,
    avoiding likely intro/outro (black frames, fades, credits) at either end."""
    if not duration or duration <= 0 or count <= 1:
        return [max((duration or 0.0) * 0.1, 0.0) for _ in range(max(count, 1))]
    margin = duration * 0.1
    usable = max(duration - 2 * margin, 0.0)
    return [margin + usable * i / (count - 1) for i in range(count)]


def extract_sample(input_path, start, duration, dest):
    """Fast stream-copy extraction of a short clip for tuning; no re-encode."""
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(input_path), "-t", f"{duration:.2f}",
           "-c", "copy", "-avoid_negative_ts", "make_zero", str(dest)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def encode_sample(sample_path, dest, codec, crf, preset, maxrate_kbps=None):
    cmd = ["ffmpeg", "-y", "-i", str(sample_path), "-an", "-c:v", ENCODER[codec], "-preset", preset, "-crf", str(crf)]
    if maxrate_kbps:
        cmd += ["-maxrate", f"{maxrate_kbps}k", "-bufsize", f"{maxrate_kbps * 2}k"]
    cmd.append(str(dest))
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def measure_kbps(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=bit_rate,duration", "-of", "default=noprint_wrappers=1"]
        + [str(path)],
        capture_output=True, text=True,
    )
    info = dict(line.partition("=")[::2] for line in result.stdout.strip().splitlines())
    try:
        return float(info["bit_rate"]) / 1000
    except (KeyError, ValueError):
        pass
    try:
        return path.stat().st_size * 8 / float(info["duration"]) / 1000
    except (KeyError, ValueError, ZeroDivisionError):
        return None


def auto_tune_quality(input_path, args, duration, log=print):
    """Binary-search for the highest CRF (smallest file) that still hits args.target_vmaf,
    using short sample clips from across input_path, then derive a maxrate from the
    tuned CRF's observed sample bitrate. Returns (crf, maxrate_kbps, info_dict)."""
    lo, hi = CRF_SEARCH_RANGE[args.codec]
    tmp_dir = Path(tempfile.mkdtemp(prefix="vt-tune-"))
    try:
        starts = pick_sample_starts(duration, args.tune_samples)
        cap = max(duration - args.tune_sample_duration, 0.0) if duration else None
        starts = [max(0.0, min(s, cap)) if cap is not None else s for s in starts]

        sample_paths = []
        for i, start in enumerate(starts):
            dest = tmp_dir / f"sample{i}.mkv"
            if extract_sample(input_path, start, args.tune_sample_duration, dest):
                sample_paths.append(dest)
        if not sample_paths:
            log("  could not extract any sample clips -- falling back to the default CRF")
            return DEFAULT_CRF[args.codec], None, {"tuned": False, "reason": "no samples extracted"}
        log(f"  extracted {len(sample_paths)} sample clip(s) (~{args.tune_sample_duration:.0f}s each)")

        def score_sample(sample_path, tag, crf):
            """Encode one sample at `crf` and VMAF-score it against itself; runs in a worker thread."""
            candidate = tmp_dir / f"{tag}_{sample_path.stem}.mkv"
            try:
                if not encode_sample(sample_path, candidate, args.codec, crf, args.preset):
                    return None
                try:
                    return run_vmaf(reference=sample_path, distorted=candidate)
                except ProbeError:
                    return None
            finally:
                candidate.unlink(missing_ok=True)

        def bitrate_sample(sample_path, tag, crf):
            """Encode one sample at `crf` and measure its bitrate; runs in a worker thread."""
            candidate = tmp_dir / f"{tag}_{sample_path.stem}.mkv"
            try:
                if not encode_sample(sample_path, candidate, args.codec, crf, args.preset):
                    return None
                return measure_kbps(candidate)
            finally:
                candidate.unlink(missing_ok=True)

        workers = max(1, min(len(sample_paths), args.jobs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            trials, best = [], None
            for generation in range(1, args.tune_generations + 1):
                crf = (lo + hi) // 2
                tag = f"g{generation}"
                futures = [pool.submit(score_sample, sample_path, tag, crf) for sample_path in sample_paths]
                scores = [f.result() for f in futures]
                scores = [s for s in scores if s is not None]
                if not scores:
                    log(f"  generation {generation}/{args.tune_generations}: CRF {crf} -- all sample encodes failed, stopping")
                    break
                aggregate = min(scores)  # worst-of-samples: a floor guarantee, not an average
                trials.append((crf, aggregate))
                meets_target = aggregate >= args.target_vmaf - args.tune_tolerance
                log(f"  generation {generation}/{args.tune_generations}: CRF {crf} -> worst-sample VMAF {aggregate:.1f} "
                    f"({len(scores)} sample(s) concurrently, {'meets' if meets_target else 'below'} target {args.target_vmaf:.1f})")
                if meets_target and (best is None or crf > best[0]):
                    best = (crf, aggregate)
                if abs(aggregate - args.target_vmaf) <= args.tune_tolerance:
                    break
                if meets_target:
                    lo = crf + 1  # target met -- try a higher CRF (smaller file) next
                else:
                    hi = crf - 1  # target missed -- need a lower CRF (higher quality)
                if lo > hi:
                    break

            if best is None:
                best = min(trials, key=lambda t: t[0]) if trials else (CRF_SEARCH_RANGE[args.codec][0], None)
            chosen_crf, achieved = best

            futures = [pool.submit(bitrate_sample, sample_path, "final", chosen_crf) for sample_path in sample_paths]
            bitrates = [kbps for kbps in (f.result() for f in futures) if kbps]
        maxrate_kbps = int(max(bitrates) * TUNE_MAXRATE_HEADROOM) if bitrates else None

        return chosen_crf, maxrate_kbps, {
            "tuned": True,
            "target_vmaf": args.target_vmaf,
            "achieved_vmaf": achieved,
            "generations_run": len(trials),
            "trials": trials,
            "samples": len(sample_paths),
            "maxrate_kbps": maxrate_kbps,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def build_command(input_path, output_path, args, target_height, fps):
    if args.uncapped:
        cap_kbps = None
    elif args.maxrate:
        cap_kbps = args.maxrate
    else:
        cap_kbps = pick_bitrate_cap(args.codec, target_height, fps)

    cmd = ["ffmpeg", "-y", "-i", str(input_path)]
    if args.height:
        cmd += ["-vf", f"scale=-2:{args.height}"]
    cmd += ["-c:v", ENCODER[args.codec], "-preset", args.preset, "-crf", str(args.crf)]
    if cap_kbps is not None:
        cmd += ["-maxrate", f"{cap_kbps}k", "-bufsize", f"{cap_kbps * 2}k"]
    cmd += [
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
    ]
    if args.codec == "h265":
        cmd += ["-tag:v", "hvc1"]  # QuickTime/Apple HEVC-in-MP4 compatibility
    cmd += ["-progress", "pipe:1", "-nostats"]
    cmd.append(str(output_path))
    return cmd, cap_kbps


def process_one(input_path, output_path, args, report=None):
    """Transcode input_path -> output_path and return a result dict describing the outcome.
    If given, report(text) is called with a live-updating status line for this file."""
    entry = {"file": str(input_path), "output": str(output_path), "codec": args.codec, "crf": args.crf, "preset": args.preset}
    try:
        source_height, fps, duration = probe_video(input_path)
    except ProbeError as err:
        return {**entry, "status": "error", "error": str(err)}

    target_height = args.height or source_height
    cmd, cap_kbps = build_command(input_path, output_path, args, target_height, fps)
    entry["command"] = " ".join(cmd)
    entry["source_height"] = source_height
    entry["target_height"] = target_height
    entry["maxrate_kbps"] = cap_kbps

    if args.dry_run:
        dry_entry = {**entry, "status": "dry-run"}
        if args.retries:
            dry_entry["retry_policy"] = f"would retry up to {args.retries} time(s) on failure"
        if args.nfs:
            dry_entry["nfs_note"] = f"would read from NFS, write locally to {output_path.parent}"
        return dry_entry

    output_path.parent.mkdir(parents=True, exist_ok=True)

    max_attempts = args.retries + 1
    log_text = ""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and report:
            report(f"{input_path.name:<24.24} retrying (attempt {attempt}/{max_attempts})...")

        def on_tick(pct, elapsed, speed, _attempt=attempt):
            if report:
                line = format_progress_line(input_path.name, pct, elapsed, duration, speed)
                if max_attempts > 1:
                    line += f"  [attempt {_attempt}/{max_attempts}]"
                report(line)

        returncode, log_text = run_ffmpeg_with_progress(cmd, duration, on_tick if report else None)
        if returncode == 0:
            break
        output_path.unlink(missing_ok=True)  # drop any partial output so a re-run retries this file
        if attempt == max_attempts:
            return {**entry, "status": "error", "error": log_text.strip()[-2000:], "attempts": attempt}
    entry["attempts"] = attempt

    original_bytes = input_path.stat().st_size
    converted_bytes = output_path.stat().st_size
    entry.update({
        "status": "ok",
        "original_bytes": original_bytes,
        "converted_bytes": converted_bytes,
        "savings_bytes": original_bytes - converted_bytes,
        "savings_percent": round((1 - converted_bytes / original_bytes) * 100, 2) if original_bytes else 0.0,
    })

    if args.compare:
        def on_vmaf_tick(pct, elapsed, speed):
            if report:
                report(format_progress_line(input_path.name, pct, elapsed, duration, speed) + "  [vmaf]")

        try:
            score = run_vmaf(reference=input_path, distorted=output_path, duration=duration,
                              on_tick=on_vmaf_tick if report else None)
            entry["vmaf"] = round(score, 2)
            entry["vmaf_rating"] = rate_vmaf(score)
        except ProbeError as err:
            entry["vmaf_error"] = str(err)

    return entry


def summarize_entry(entry):
    if entry["status"] == "error":
        attempts_note = f" (failed after {entry['attempts']} attempts)" if entry.get("attempts", 1) > 1 else ""
        return f"[error] {entry['file']}: {_error_tail(entry['error'], 300)}{attempts_note}"
    if entry["status"] == "dry-run":
        line = f"[dry-run] {entry['file']} -> {entry['output']}"
        notes = [n for n in (entry.get("retry_policy"), entry.get("nfs_note")) if n]
        if notes:
            line += f" ({'; '.join(notes)})"
        return line
    parts = [
        f"[ok] {entry['file']} -> {entry['output']}",
        f"{human_size(entry['original_bytes'])} -> {human_size(entry['converted_bytes'])} ({entry['savings_percent']:+.1f}%)",
    ]
    if entry.get("attempts", 1) > 1:
        parts.append(f"succeeded after {entry['attempts']} attempts")
    if "vmaf" in entry:
        parts.append(f"VMAF {entry['vmaf']:.2f} ({entry['vmaf_rating']})")
    elif "vmaf_error" in entry:
        parts.append(f"VMAF error: {entry['vmaf_error']}")
    return " | ".join(parts)


def _error_tail(error_text, limit=200):
    """Pull the root-cause line out of an ffmpeg/ffprobe error: the first line
    mentioning "error" (ffmpeg logs cascade top-down from the real failure into
    generic thread-termination noise), falling back to the last non-empty line."""
    lines = [line.strip() for line in error_text.strip().splitlines() if line.strip()]
    if not lines:
        return error_text.strip()[:limit]
    for line in lines:
        if "error" in line.lower():
            return line[:limit]
    return lines[-1][:limit]


def build_batch_slack_message(args, label, results, report_path):
    dry = [r for r in results if r["status"] == "dry-run"]
    if dry:
        settings = f"codec `{args.codec}` · CRF `{args.crf}` · jobs `{args.jobs}`"
        if args.retries:
            settings += f" · retries `{args.retries}`"
        lines = [
            f":test_tube: *video-transcoder* — DRY RUN — `{label}`",
            f"*{len(dry)} file(s)* would be converted ({settings})",
            "",
        ]
        lines.extend(f"• `{Path(r['file']).name}` → `{Path(r['output']).name}`" for r in dry)
        return "\n".join(lines)

    ok = [r for r in results if r["status"] == "ok"]
    errors = [r for r in results if r["status"] == "error"]
    total_saved = sum(r["savings_bytes"] for r in ok)
    total_original = sum(r["original_bytes"] for r in ok)
    savings_pct = (total_saved / total_original * 100) if total_original else 0.0
    vmaf_scores = [r["vmaf"] for r in ok if "vmaf" in r]

    icon = ":white_check_mark:" if not errors else ":warning:"
    headline = f"*{len(ok)} converted* · *{len(errors)} failed* · *{human_size(total_saved)} saved* ({savings_pct:.1f}%)"
    if vmaf_scores:
        headline += f" · avg VMAF *{sum(vmaf_scores) / len(vmaf_scores):.2f}*"
    lines = [f"{icon} *video-transcoder* — `{label}`", headline]

    if errors:
        lines.append("")
        lines.append("*Failed:*")
        for e in errors:
            attempts_note = f" _(after {e['attempts']} attempts)_" if e.get("attempts", 1) > 1 else ""
            lines.append(f"• `{Path(e['file']).name}`{attempts_note} — {_error_tail(e['error'])}")

    settings = f"Codec: `{args.codec}` · CRF: `{args.crf}` · Jobs: `{args.jobs}`"
    if args.retries:
        settings += f" · Retries: `{args.retries}`"
    lines += ["", settings, f"Report: `{report_path}`"]
    return "\n".join(lines)


def build_single_slack_message(input_path, entry, args):
    if entry["status"] == "dry-run":
        msg = f":test_tube: *video-transcoder* — DRY RUN — `{input_path.name}` → `{Path(entry['output']).name}`"
        msg += f"\nCodec: `{args.codec}` · CRF: `{args.crf}`"
        if args.retries:
            msg += f" · would retry up to `{args.retries}` time(s) on failure"
        return msg

    if entry["status"] == "error":
        attempts_note = f" after {entry['attempts']} attempt(s)" if entry.get("attempts", 1) > 1 else ""
        return f":x: *video-transcoder* — `{input_path.name}` FAILED{attempts_note}\n```{_error_tail(entry['error'], 300)}```"

    lines = [
        f":white_check_mark: *video-transcoder* — `{input_path.name}`",
        f"{human_size(entry['original_bytes'])} → {human_size(entry['converted_bytes'])} (*{entry['savings_percent']:+.1f}%*)",
    ]
    if "vmaf" in entry:
        lines.append(f"VMAF *{entry['vmaf']:.2f}* ({entry['vmaf_rating']})")
    elif "vmaf_error" in entry:
        lines.append(f"VMAF error: {entry['vmaf_error']}")
    if entry.get("attempts", 1) > 1:
        lines.append(f"Succeeded after {entry['attempts']} attempts")
    return "\n".join(lines)


def notify_slack(bot_token, channel, message):
    payload = json.dumps({"channel": channel, "text": message}).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {bot_token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        if not body.get("ok"):
            print(f"warning: slack notification failed: {body.get('error', 'unknown error')}", file=sys.stderr)
    except urllib.error.URLError as err:
        print(f"warning: slack notification failed: {err}", file=sys.stderr)


def run_batch(files, args, output_dir):
    board = ProgressBoard()

    def task(f):
        key = str(f)
        entry = process_one(f, default_output_path(f, output_dir), args, report=lambda text: board.update(key, text))
        board.finish(key, summarize_entry(entry))
        return entry

    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(task, f) for f in files]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda e: e["file"])
    return results


KNOWN_SUBCOMMANDS = {"convert", "compare", "check-libvmaf"}


def build_parser():
    parser = argparse.ArgumentParser(prog="video-transcoder.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    convert = subparsers.add_parser(
        "convert", help="convert a video file or a directory of them (default when no subcommand is given)"
    )
    convert.add_argument("inputs", type=Path, nargs="+",
                          help="one or more source video files and/or directories of video files")
    convert.add_argument("-o", "--output", type=Path,
                          help="output file when a single file is the only input, or output directory otherwise "
                               "(multiple inputs, or any directory input) (default: alongside each source, "
                               "named <basename>.converted.mp4)")
    convert.add_argument("-c", "--codec", choices=["h265", "h264"], default="h265")
    convert.add_argument("--height", type=int, help="downscale to this height (e.g. 480, 720); default: keep source resolution")
    convert.add_argument("--crf", type=int, help="override default CRF (h264: 23, h265: 20); lower = higher quality")
    convert.add_argument("--preset", help="override default encoder preset (h264: slow, h265: slow)")
    convert.add_argument("--maxrate", type=int,
                          help="override the auto-computed -maxrate safety ceiling, in kbps (default: picked by "
                               "resolution; see BITRATE_KBPS). Raise this if VMAF is still low on demanding "
                               "content even after lowering --crf -- the cap may be binding")
    convert.add_argument("--uncapped", action="store_true",
                          help="drop the -maxrate/-bufsize ceiling entirely; CRF alone controls bitrate with no "
                               "limit. Strongest quality guarantee, but can produce very large files on complex "
                               "content. Mutually exclusive with --maxrate")
    convert.add_argument("--target-vmaf", type=float, nargs="?", const=93.0, default=None,
                          help="auto-discover --crf (and a derived --maxrate) by sampling short clips from across "
                               "one input and binary-searching for the CRF that hits this VMAF (bare flag defaults "
                               "to 93.0). When converting more than one file, tuning runs once against the first "
                               "one and the result is reused for the rest -- not re-tuned per file. Requires "
                               "libvmaf. Mutually exclusive with --crf/--maxrate/--uncapped")
    convert.add_argument("--tune-generations", type=int, default=6,
                          help="max binary-search iterations for --target-vmaf (default: 6)")
    convert.add_argument("--tune-samples", type=int, default=3,
                          help="number of sample clips tested per generation for --target-vmaf (default: 3)")
    convert.add_argument("--tune-sample-duration", type=float, default=6.0,
                          help="duration in seconds of each --target-vmaf sample clip (default: 6.0)")
    convert.add_argument("--tune-tolerance", type=float, default=1.0,
                          help="acceptable VMAF deviation from --target-vmaf before the search stops (default: 1.0)")
    convert.add_argument("--dry-run", action="store_true", help="print the planned ffmpeg command(s) without running them")
    convert.add_argument("--compare", action=argparse.BooleanOptionalAction, default=True,
                          help="after encoding, compute the VMAF score against the source (default: on; use --no-compare to skip)")
    convert.add_argument("-j", "--jobs", type=int, default=4,
                          help="number of files to convert concurrently when converting more than one (default: 4)")
    convert.add_argument("-r", "--report", type=Path,
                          help="path for the JSON report when converting more than one file "
                               "(default: conversion-report.json in the current directory)")
    convert.add_argument("--retries", type=int, default=0, help="retry a failed conversion this many times (default: 0)")
    convert.add_argument("--slack-bot-token", default=os.environ.get("SLACK_BOT_TOKEN"),
                          help="Slack bot token for a completion notification (default: $SLACK_BOT_TOKEN); "
                               "notification is disabled unless both this and --slack-channel are set")
    convert.add_argument("--slack-channel", default=os.environ.get("SLACK_CHANNEL"),
                          help="Slack channel name or ID to notify on completion (default: $SLACK_CHANNEL); "
                               "notification is disabled unless both this and --slack-bot-token are set")
    convert.add_argument("--nfs", action="store_true",
                          help="treat input as being on NFS: read it from there but write output to local disk "
                               f"instead of alongside the source (default local dir: {NFS_LOCAL_DIR}; override with "
                               "-o), checking free space on the destination first")

    compare = subparsers.add_parser("compare", help="score an already-converted file's VMAF against its original")
    compare.add_argument("original", type=Path, help="the original, unconverted source file")
    compare.add_argument("converted", type=Path, help="the converted output file to score")

    subparsers.add_parser("check-libvmaf", help="check whether the ffmpeg on PATH has libvmaf support")

    return parser, convert, compare


def parse_args(argv):
    # bare `video-transcoder.py input.mov ...` (no subcommand) implicitly means `convert`
    if not argv or (argv[0] not in KNOWN_SUBCOMMANDS and argv[0] not in ("-h", "--help")):
        argv = ["convert", *argv]

    parser, convert, compare = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "convert":
        for p in args.inputs:
            if not p.exists():
                convert.error(f"input path not found: {p}")
        if args.jobs < 1:
            convert.error("--jobs must be >= 1")
        if args.retries < 0:
            convert.error("--retries must be >= 0")
        if args.maxrate and args.uncapped:
            convert.error("--maxrate and --uncapped are mutually exclusive")
        if args.maxrate is not None and args.maxrate < 1:
            convert.error("--maxrate must be >= 1")
        if args.target_vmaf is not None:
            if args.crf is not None or args.maxrate is not None or args.uncapped:
                convert.error("--target-vmaf can't be combined with --crf/--maxrate/--uncapped "
                               "(it discovers them automatically)")
            if args.tune_samples < 1:
                convert.error("--tune-samples must be >= 1")
            if args.tune_generations < 1:
                convert.error("--tune-generations must be >= 1")
            if args.tune_sample_duration <= 0:
                convert.error("--tune-sample-duration must be > 0")
        if args.crf is None:
            args.crf = DEFAULT_CRF[args.codec]
        if args.preset is None:
            args.preset = DEFAULT_PRESET[args.codec]
    elif args.subcommand == "compare":
        if not args.original.exists():
            compare.error(f"original file not found: {args.original}")
        if not args.converted.exists():
            compare.error(f"converted file not found: {args.converted}")

    return args


def require_libvmaf_or_exit(suggest_no_compare=True):
    if not ffmpeg_has_libvmaf():
        lines = ["error: this ffmpeg build lacks libvmaf, so VMAF comparison can't run."]
        if suggest_no_compare:
            lines.append("  - to convert without VMAF checking, add --no-compare")
        lines.append("  - to add libvmaf support, install a build that has it "
                      "(e.g. https://github.com/BtbN/FFmpeg-Builds, the *-gpl static builds)")
        print("\n".join(lines), file=sys.stderr)
        sys.exit(1)


def run_compare(args):
    require_libvmaf_or_exit(suggest_no_compare=False)
    board = ProgressBoard()
    key = str(args.converted)
    try:
        _, _, duration = probe_video(args.original)

        def on_tick(pct, elapsed, speed):
            board.update(key, format_progress_line(args.converted.name, pct, elapsed, duration, speed))

        score = run_vmaf(reference=args.original, distorted=args.converted, duration=duration, on_tick=on_tick)
    except ProbeError as err:
        board.finish(key, f"error: {err}")
        return 1
    board.finish(key, f"VMAF: {score:.2f} ({rate_vmaf(score)})")
    return 0


def run_check_libvmaf():
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 1

    if ffmpeg_has_libvmaf():
        print(f"libvmaf: available ({ffmpeg_path})")
        return 0

    print(f"libvmaf: NOT available ({ffmpeg_path})\n"
          "  - install a build that has it (e.g. https://github.com/BtbN/FFmpeg-Builds, the *-gpl static builds)")
    return 1


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.subcommand == "compare":
        return run_compare(args)
    if args.subcommand == "check-libvmaf":
        return run_check_libvmaf()

    if args.compare or args.target_vmaf is not None:
        require_libvmaf_or_exit(suggest_no_compare=args.target_vmaf is None)

    if bool(args.slack_bot_token) != bool(args.slack_channel):
        missing = "--slack-channel" if args.slack_bot_token else "--slack-bot-token"
        print(f"warning: Slack notification disabled -- {missing} not set", file=sys.stderr)

    is_single_file = len(args.inputs) == 1 and args.inputs[0].is_file()

    if is_single_file:
        files, tune_target = None, args.inputs[0]
    else:
        files = gather_files(args.inputs)
        if not files:
            print(f"no video files found in {describe_inputs(args.inputs)}")
            return 0
        tune_target = files[0]

    if args.target_vmaf is not None:
        if args.dry_run:
            print(f"--target-vmaf {args.target_vmaf}: would tune CRF/maxrate using {tune_target} "
                  f"({args.tune_samples} sample(s), up to {args.tune_generations} generation(s)) and apply "
                  "the result to " + ("this file" if is_single_file else "the rest"))
        else:
            print(f"tuning quality using {tune_target} (target VMAF {args.target_vmaf}, up to "
                  f"{args.tune_generations} generation(s), {args.tune_samples} sample(s))...")
            _, _, tune_duration = probe_video(tune_target)
            crf, maxrate_kbps, tune_info = auto_tune_quality(tune_target, args, tune_duration)
            if tune_info.get("tuned"):
                rate_desc = f"maxrate={maxrate_kbps}kbps" if maxrate_kbps else "uncapped"
                print(f"tuned: CRF={crf}, {rate_desc} (worst-sample VMAF {tune_info['achieved_vmaf']:.1f} "
                      f"after {tune_info['generations_run']} generation(s))")
            else:
                print(f"tuning failed ({tune_info.get('reason', 'unknown')}) -- using default CRF={crf}")
            args.crf, args.maxrate, args.uncapped = crf, maxrate_kbps, False
        args.target_vmaf = None  # resolved -- downstream treats crf/maxrate as if passed explicitly

    if not is_single_file:
        label = describe_inputs(args.inputs)
        output_dir = args.output or (NFS_LOCAL_DIR if args.nfs else None)
        first = args.inputs[0]
        default_report_dir = first if first.is_dir() else first.parent
        report_path = args.report or (default_report_dir / "conversion-report.json")

        if args.nfs:
            print(f"--nfs: reading from {label}, writing converted files locally to {output_dir}")
            if not args.dry_run:
                total_bytes = sum(f.stat().st_size for f in files)
                try:
                    check_disk_space(output_dir, total_bytes)
                except DiskSpaceError as err:
                    print(f"error: {err}", file=sys.stderr)
                    return 1
        if not args.dry_run and output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)

        print(f"converting {len(files)} file(s) from {label} with {args.jobs} concurrent job(s)")
        results = run_batch(files, args, output_dir)
        report_path.write_text(json.dumps(results, indent=2))

        ok = [r for r in results if r["status"] == "ok"]
        errors = [r for r in results if r["status"] == "error"]
        dry = [r for r in results if r["status"] == "dry-run"]
        if dry:
            print(f"dry-run: {len(dry)} file(s) would be converted; nothing was written")
        else:
            total_saved = sum(r["savings_bytes"] for r in ok)
            print(f"done: {len(ok)} converted, {len(errors)} failed, {human_size(total_saved)} saved total")
        print(f"report written to {report_path}")

        if args.slack_bot_token and args.slack_channel:
            notify_slack(args.slack_bot_token, args.slack_channel,
                         build_batch_slack_message(args, label, results, report_path))

        return 1 if errors else 0

    input_path = args.inputs[0]
    output_path = args.output or default_output_path(input_path, NFS_LOCAL_DIR if args.nfs else None)
    if args.nfs:
        print(f"--nfs: reading {input_path}, writing converted output locally to {output_path}")
        if not args.dry_run:
            try:
                check_disk_space(output_path.parent, input_path.stat().st_size)
            except DiskSpaceError as err:
                print(f"error: {err}", file=sys.stderr)
                return 1
    board = ProgressBoard()
    key = str(input_path)
    entry = process_one(input_path, output_path, args, report=lambda text: board.update(key, text))
    board.finish(key, summarize_entry(entry))
    if args.report:
        args.report.write_text(json.dumps([entry], indent=2))
        print(f"report written to {args.report}")

    if args.slack_bot_token and args.slack_channel:
        notify_slack(args.slack_bot_token, args.slack_channel, build_single_slack_message(input_path, entry, args))

    return 0 if entry["status"] in ("ok", "dry-run") else 1


if __name__ == "__main__":
    sys.exit(main())
