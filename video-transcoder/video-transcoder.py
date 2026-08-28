#!/usr/bin/env python3
"""Re-encode a video (or a whole directory of them) to H.265 (default) or H.264
with a sane bitrate for its resolution.

Uses CRF (quality-based) encoding rather than a fixed bitrate, capped with
-maxrate/-bufsize so a busy scene can't balloon the file. The cap is picked
from the source's own resolution (or --height, if you're also downscaling)
using YouTube's published upload-bitrate guidance as the H.264 baseline,
halved for H.265 (HEVC typically matches H.264 quality at ~50% of the bits).

Output files are named <basename>.converted.mp4 (myfile.mov -> myfile.converted.mp4).
When given a directory, every video file in it is converted — set --jobs to
control how many run at once — and a JSON report with per-file space savings
and VMAF scores is written. Each in-flight file gets a live progress bar
(percent/time/speed) in the terminal; concurrent conversions each get their
own line, stacked.

    python3 video-transcoder.py input.mov
    python3 video-transcoder.py input.mov --codec h264 --height 480
    python3 video-transcoder.py input.mov -o out.mp4 --crf 25 --dry-run
    python3 video-transcoder.py input.mov --no-compare
    python3 video-transcoder.py ./videos/ --jobs 3
    python3 video-transcoder.py ./videos/ --jobs 3 --report ./videos/report.json
    python3 video-transcoder.py ./videos/ --retries 2 --slack-webhook https://hooks.slack.com/services/...
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
# H.264 values are YouTube's recommended SDR upload bitrates; H.265 is ~50% of that.
BITRATE_KBPS = {
    "h264": {2160: 45000, 1440: 16000, 1080: 8000, 720: 5000, 480: 2500, 360: 1000, 240: 500},
    "h265": {2160: 22000, 1440: 8000, 1080: 4000, 720: 2500, 480: 1200, 360: 600, 240: 300},
}
HIGH_FPS_MULTIPLIER = 1.5  # applied above 48fps
DEFAULT_CRF = {"h264": 23, "h265": 28}
DEFAULT_PRESET = {"h264": "slow", "h265": "medium"}
ENCODER = {"h264": "libx264", "h265": "libx265"}


class ProbeError(RuntimeError):
    pass


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


def run_vmaf(reference, distorted):
    """Compare `distorted` against `reference` and return the pooled mean VMAF score."""
    if not shutil.which("ffmpeg"):
        raise ProbeError("ffmpeg not found on PATH — install ffmpeg")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        log_path = Path(tmp.name)
    try:
        filter_graph = (
            "[0:v]setpts=PTS-STARTPTS[dist];"
            "[1:v]setpts=PTS-STARTPTS[ref];"
            "[dist][ref]scale2ref=flags=bicubic[dist2][ref2];"
            f"[dist2][ref2]libvmaf=log_fmt=json:log_path={log_path}"
        )
        cmd = ["ffmpeg", "-y", "-i", str(distorted), "-i", str(reference), "-lavfi", filter_graph, "-f", "null", "-"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise ProbeError(f"vmaf comparison failed: {result.stderr.strip()[-2000:]}")
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


def build_command(input_path, output_path, args, target_height, fps):
    cap_kbps = pick_bitrate_cap(args.codec, target_height, fps)
    cmd = ["ffmpeg", "-y", "-i", str(input_path)]
    if args.height:
        cmd += ["-vf", f"scale=-2:{args.height}"]
    cmd += [
        "-c:v", ENCODER[args.codec],
        "-preset", args.preset,
        "-crf", str(args.crf),
        "-maxrate", f"{cap_kbps}k",
        "-bufsize", f"{cap_kbps * 2}k",
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
        return dry_entry

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
        if report:
            report(f"{input_path.name:<24.24} computing VMAF...")
        try:
            score = run_vmaf(reference=input_path, distorted=output_path)
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
        if entry.get("retry_policy"):
            line += f" ({entry['retry_policy']})"
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


def build_batch_slack_message(args, input_dir, results, report_path):
    dry = [r for r in results if r["status"] == "dry-run"]
    if dry:
        settings = f"codec `{args.codec}` · CRF `{args.crf}` · jobs `{args.jobs}`"
        if args.retries:
            settings += f" · retries `{args.retries}`"
        lines = [
            f":test_tube: *video-transcoder* — DRY RUN — `{input_dir}`",
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
    lines = [f"{icon} *video-transcoder* — `{input_dir}`", headline]

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


def notify_slack(webhook_url, message):
    payload = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
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


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="source video file, or a directory of video files")
    parser.add_argument("-o", "--output", type=Path,
                         help="output file for a single input, or output directory for a directory input "
                              "(default: alongside each source, named <basename>.converted.mp4)")
    parser.add_argument("-c", "--codec", choices=["h265", "h264"], default="h265")
    parser.add_argument("--height", type=int, help="downscale to this height (e.g. 480, 720); default: keep source resolution")
    parser.add_argument("--crf", type=int, help="override default CRF (h264: 23, h265: 28)")
    parser.add_argument("--preset", help="override default encoder preset (h264: slow, h265: medium)")
    parser.add_argument("--dry-run", action="store_true", help="print the planned ffmpeg command(s) without running them")
    parser.add_argument("--compare", action=argparse.BooleanOptionalAction, default=True,
                         help="after encoding, compute the VMAF score against the source (default: on; use --no-compare to skip)")
    parser.add_argument("-j", "--jobs", type=int, default=4, help="number of files to convert concurrently in directory mode (default: 4)")
    parser.add_argument("-r", "--report", type=Path, help="path for the JSON report in directory mode (default: <directory>/conversion-report.json)")
    parser.add_argument("--retries", type=int, default=0, help="retry a failed conversion this many times (default: 0)")
    parser.add_argument("--slack-webhook", default=os.environ.get("SLACK_WEBHOOK_URL"),
                         help="Slack incoming webhook URL for a completion notification (default: $SLACK_WEBHOOK_URL)")
    args = parser.parse_args(argv)

    if not args.input.exists():
        parser.error(f"input path not found: {args.input}")
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    if args.retries < 0:
        parser.error("--retries must be >= 0")
    if args.crf is None:
        args.crf = DEFAULT_CRF[args.codec]
    if args.preset is None:
        args.preset = DEFAULT_PRESET[args.codec]
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.input.is_dir():
        files = discover_videos(args.input)
        if not files:
            print(f"no video files found in {args.input}")
            return 0

        output_dir = args.output or args.input
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.report or (args.input / "conversion-report.json")

        print(f"converting {len(files)} file(s) from {args.input} with {args.jobs} concurrent job(s)")
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

        if args.slack_webhook:
            notify_slack(args.slack_webhook, build_batch_slack_message(args, args.input, results, report_path))

        return 1 if errors else 0

    output_path = args.output or default_output_path(args.input)
    board = ProgressBoard()
    key = str(args.input)
    entry = process_one(args.input, output_path, args, report=lambda text: board.update(key, text))
    board.finish(key, summarize_entry(entry))
    if args.report:
        args.report.write_text(json.dumps([entry], indent=2))
        print(f"report written to {args.report}")

    if args.slack_webhook:
        notify_slack(args.slack_webhook, build_single_slack_message(args.input, entry, args))

    return 0 if entry["status"] in ("ok", "dry-run") else 1


if __name__ == "__main__":
    sys.exit(main())
