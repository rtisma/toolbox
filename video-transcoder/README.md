# video-transcoder

Re-encode a video to H.265 (default) or H.264 with a bitrate cap sized to its
actual resolution, instead of guessing a number. Uses CRF (quality-based)
encoding with `-maxrate`/`-bufsize` so busy scenes can't balloon the file.

Requires `ffmpeg`/`ffprobe` on `PATH`, built with `libvmaf` (for the VMAF
comparison). No Python dependencies beyond the standard library.

## Usage

```bash
python3 video-transcoder.py input.mov
python3 video-transcoder.py input.mov --codec h264 --height 480
python3 video-transcoder.py input.mov -o out.mp4 --crf 25 --dry-run
python3 video-transcoder.py input.mov --no-compare
```

| Flag | Default | Notes |
|------|---------|-------|
| `-o/--output` | `<input>_<codec>.mp4` | |
| `-c/--codec` | `h265` | `h265` or `h264` |
| `--height` | source height | downscale (e.g. `720`, `480`, `360`) |
| `--crf` | `23` (h264) / `28` (h265) | lower = higher quality/bigger file |
| `--preset` | `slow` (h264) / `medium` (h265) | encoder speed/efficiency tradeoff |
| `--dry-run` | off | print the ffmpeg command without running it |
| `--compare`/`--no-compare` | on | after encoding, score the output against the source with VMAF |

By default, after encoding the script runs a VMAF (Netflix's perceptual
quality metric) comparison between the output and the original source,
scaling the output back up to the source's resolution first if it was
downscaled. Pass `--no-compare` to skip it. VMAF is 0-100: `>=93`
near-transparent, `>=80` good (minor loss on close inspection), `>=60`
noticeable degradation, below that poor.

The bitrate cap is picked from the target resolution using YouTube's
published SDR upload-bitrate guidance as the H.264 baseline (2160p 45Mbps,
1440p 16Mbps, 1080p 8Mbps, 720p 5Mbps, 480p 2.5Mbps, 360p 1Mbps, 240p
500kbps), halved for H.265 since HEVC typically matches H.264 quality at
~50% of the bitrate. Caps are bumped 1.5x above 48fps.
