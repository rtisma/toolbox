#!/usr/bin/env python3
"""Re-encode a video to H.265 (default) or H.264 with a sane bitrate for its resolution.

Uses CRF (quality-based) encoding rather than a fixed bitrate, capped with
-maxrate/-bufsize so a busy scene can't balloon the file. The cap is picked
from the source's own resolution (or --height, if you're also downscaling)
using YouTube's published upload-bitrate guidance as the H.264 baseline,
halved for H.265 (HEVC typically matches H.264 quality at ~50% of the bits).

    python3 video-transcoder.py input.mov
    python3 video-transcoder.py input.mov --codec h264 --height 480
    python3 video-transcoder.py input.mov -o out.mp4 --crf 25 --dry-run
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

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


def build_command(args, target_height, fps):
    cap_kbps = pick_bitrate_cap(args.codec, target_height, fps)
    cmd = ["ffmpeg", "-y", "-i", str(args.input)]
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
    cmd.append(str(args.output))
    return cmd, cap_kbps


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="source video file")
    parser.add_argument("-o", "--output", type=Path, help="output file (default: <input>_<codec>.mp4)")
    parser.add_argument("-c", "--codec", choices=["h265", "h264"], default="h265")
    parser.add_argument("--height", type=int, help="downscale to this height (e.g. 480, 720); default: keep source resolution")
    parser.add_argument("--crf", type=int, help="override default CRF (h264: 23, h265: 28)")
    parser.add_argument("--preset", help="override default encoder preset (h264: slow, h265: medium)")
    parser.add_argument("--dry-run", action="store_true", help="print the ffmpeg command without running it")
    args = parser.parse_args(argv)

    if not args.input.exists():
        parser.error(f"input file not found: {args.input}")
    if args.output is None:
        args.output = args.input.with_name(f"{args.input.stem}_{args.codec}.mp4")
    if args.crf is None:
        args.crf = DEFAULT_CRF[args.codec]
    if args.preset is None:
        args.preset = DEFAULT_PRESET[args.codec]
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        source_height, fps = probe_video(args.input)
    except ProbeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    target_height = args.height or source_height
    cmd, cap_kbps = build_command(args, target_height, fps)

    print(f"source: {source_height}p @ {fps:.2f}fps -> target: {target_height}p, "
          f"codec={args.codec}, crf={args.crf}, preset={args.preset}, maxrate={cap_kbps}k")
    print(" ".join(cmd))
    if args.dry_run:
        return 0

    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
