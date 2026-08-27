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
and VMAF scores is written.

    python3 video-transcoder.py input.mov
    python3 video-transcoder.py input.mov --codec h264 --height 480
    python3 video-transcoder.py input.mov -o out.mp4 --crf 25 --dry-run
    python3 video-transcoder.py input.mov --no-compare
    python3 video-transcoder.py ./videos/ --jobs 3
    python3 video-transcoder.py ./videos/ --jobs 3 --report ./videos/report.json
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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
        "-show_entries", "stream=width,height,avg_frame_rate",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed: {result.stderr.strip()}")
    streams = json.loads(result.stdout).get("streams") or []
    if not streams:
        raise ProbeError(f"no video stream found in {path}")
    stream = streams[0]
    num, _, den = stream["avg_frame_rate"].partition("/")
    fps = float(num) / float(den) if den and float(den) != 0 else float(num)
    return int(stream["height"]), fps


def pick_bitrate_cap(codec, height, fps):
    table = BITRATE_KBPS[codec]
    bucket = next((h for h in sorted(table, reverse=True) if h <= height), min(table))
    cap = table[bucket]
    if fps > 48:
        cap = int(cap * HIGH_FPS_MULTIPLIER)
    return cap


def rate_vmaf(score):
    return next(text for threshold, text in VMAF_RATINGS if score >= threshold)


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
    cmd.append(str(output_path))
    return cmd, cap_kbps


def process_one(input_path, output_path, args):
    """Transcode input_path -> output_path and return a result dict describing the outcome."""
    entry = {"file": str(input_path), "output": str(output_path), "codec": args.codec, "crf": args.crf, "preset": args.preset}
    try:
        source_height, fps = probe_video(input_path)
    except ProbeError as err:
        return {**entry, "status": "error", "error": str(err)}

    target_height = args.height or source_height
    cmd, cap_kbps = build_command(input_path, output_path, args, target_height, fps)
    entry["command"] = " ".join(cmd)
    entry["source_height"] = source_height
    entry["target_height"] = target_height
    entry["maxrate_kbps"] = cap_kbps

    if args.dry_run:
        return {**entry, "status": "dry-run"}

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)  # drop any partial output so a re-run retries this file
        return {**entry, "status": "error", "error": result.stderr.strip()[-2000:]}

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
        try:
            score = run_vmaf(reference=input_path, distorted=output_path)
            entry["vmaf"] = round(score, 2)
            entry["vmaf_rating"] = rate_vmaf(score)
        except ProbeError as err:
            entry["vmaf_error"] = str(err)

    return entry


def summarize_entry(entry):
    if entry["status"] == "error":
        return f"[error] {entry['file']}: {entry['error']}"
    if entry["status"] == "dry-run":
        return f"[dry-run] {entry['file']} -> {entry['output']}"
    parts = [
        f"[ok] {entry['file']} -> {entry['output']}",
        f"{human_size(entry['original_bytes'])} -> {human_size(entry['converted_bytes'])} ({entry['savings_percent']:+.1f}%)",
    ]
    if "vmaf" in entry:
        parts.append(f"VMAF {entry['vmaf']:.2f} ({entry['vmaf_rating']})")
    elif "vmaf_error" in entry:
        parts.append(f"VMAF error: {entry['vmaf_error']}")
    return " | ".join(parts)


def run_batch(files, args, output_dir):
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(process_one, f, default_output_path(f, output_dir), args): f for f in files}
        for future in as_completed(futures):
            entry = future.result()
            print(summarize_entry(entry))
            results.append(entry)
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
    args = parser.parse_args(argv)

    if not args.input.exists():
        parser.error(f"input path not found: {args.input}")
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
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
        total_saved = sum(r["savings_bytes"] for r in ok)
        print(f"done: {len(ok)} converted, {len(errors)} failed, {human_size(total_saved)} saved total")
        print(f"report written to {report_path}")
        return 1 if errors else 0

    output_path = args.output or default_output_path(args.input)
    entry = process_one(args.input, output_path, args)
    print(summarize_entry(entry))
    if args.report:
        args.report.write_text(json.dumps([entry], indent=2))
        print(f"report written to {args.report}")
    return 0 if entry["status"] in ("ok", "dry-run") else 1


if __name__ == "__main__":
    sys.exit(main())
