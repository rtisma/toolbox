# video-transcoder

Re-encode a video — or a whole directory of them — to H.265 (default) or
H.264 with a bitrate cap sized to its actual resolution, instead of guessing
a number. Uses CRF (quality-based) encoding with `-maxrate`/`-bufsize` so
busy scenes can't balloon the file.

Requires `ffmpeg`/`ffprobe` on `PATH`. `libvmaf` is required for VMAF
scoring (on by default) — if this ffmpeg build wasn't compiled with
`--enable-libvmaf` (common on Linux distro packages), the script checks
once up front, before doing anything else, and **exits with an error**
rather than converting without telling you; pass `--no-compare` to proceed
without VMAF. See "Checking for / adding libvmaf" below. No Python
dependencies beyond the standard library.

## Usage

```bash
python3 video-transcoder.py input.mov
python3 video-transcoder.py input.mov --codec h264 --height 480
python3 video-transcoder.py input.mov -o out.mp4 --crf 25 --dry-run
python3 video-transcoder.py input.mov --no-compare

# directory mode: convert every video in ./videos, 3 at a time
python3 video-transcoder.py ./videos/ --jobs 3
python3 video-transcoder.py ./videos/ --jobs 3 --report ./videos/report.json

# retry failures up to twice, and post a summary to Slack when done
python3 video-transcoder.py ./videos/ --retries 2 --slack-webhook https://hooks.slack.com/services/...

# score a file you already converted, without converting anything
python3 video-transcoder.py compare input.mov input.converted.mp4

# check whether the ffmpeg on PATH has libvmaf support
python3 video-transcoder.py check-libvmaf
```

Three subcommands: `convert` (conversion — the default, so `video-transcoder.py input.mov` is short for
`video-transcoder.py convert input.mov`), `compare`, and `check-libvmaf`. Run
`video-transcoder.py --help` to see all three, or `video-transcoder.py <subcommand> --help` for a
subcommand's own flags. The one edge case: if you have a file or directory literally named `convert`,
`compare`, or `check-libvmaf` in the current directory, you'll need the explicit form
(`video-transcoder.py convert ./compare`) since the bare form treats a first argument matching a
subcommand name as that subcommand.

Output files are always named `<basename>.converted.mp4` unless `-o` is
given — `myfile.mov` becomes `myfile.converted.mp4`.

| Flag | Default | Notes |
|------|---------|-------|
| `-o/--output` | alongside source, `<basename>.converted.mp4` | single input: output file path; directory input: output directory |
| `-c/--codec` | `h265` | `h265` or `h264` |
| `--height` | source height | downscale (e.g. `720`, `480`, `360`) |
| `--crf` | `23` (h264) / `28` (h265) | lower = higher quality/bigger file |
| `--preset` | `slow` (h264) / `medium` (h265) | encoder speed/efficiency tradeoff |
| `--dry-run` | off | print the planned ffmpeg command(s) without running them |
| `--compare`/`--no-compare` | on | after encoding, score the output against the source with VMAF |
| `-j/--jobs` | `4` | directory mode: how many files to convert concurrently |
| `-r/--report` | `<directory>/conversion-report.json` | directory mode: where to write the JSON report |
| `--retries` | `0` | retry a failed conversion this many times before giving up |
| `--slack-webhook` | `$SLACK_WEBHOOK_URL` | if set, post a completion summary to this Slack incoming webhook |

By default, after encoding the script runs a VMAF (Netflix's perceptual
quality metric) comparison between the output and the original source,
scaling the output back up to the source's resolution first if it was
downscaled. Pass `--no-compare` to skip it. VMAF is 0-100: `>=93`
near-transparent, `>=80` good (minor loss on close inspection), `>=60`
noticeable degradation, below that poor.

## Directory mode

Pass a directory instead of a file and every video in it (matched by
extension, non-recursive; files already named `*.converted.mp4` are skipped
so re-running is safe) is converted, up to `--jobs` at a time. A JSON report
is written with one entry per file — status, original/converted size,
space savings, and VMAF score:

```json
[
  {
    "file": "videos/clip1.mov",
    "output": "videos/clip1.converted.mp4",
    "status": "ok",
    "original_bytes": 104857600,
    "converted_bytes": 41943040,
    "savings_bytes": 62914560,
    "savings_percent": 60.0,
    "vmaf": 94.32,
    "vmaf_rating": "near-transparent, no perceptible loss for most viewers"
  }
]
```

A file that fails to convert gets `"status": "error"` with an `"error"`
message instead, and doesn't stop the rest of the batch. Pass `--retries N`
to retry a failing file up to `N` additional times (each retry deletes the
partial output first) before it's marked as failed; successful entries that
needed a retry get an `"attempts"` field.

If `--slack-webhook` (or `$SLACK_WEBHOOK_URL`) is set, a formatted summary is
posted there when the run finishes — status emoji, converted/failed counts,
total space saved, average VMAF, a bullet per failed file with its root-cause
error line, and the codec/CRF/jobs/retries settings used. Single-file runs
post a one-line result (or the error, in a code block, on failure) instead.
A failed Slack POST only prints a warning; it never fails the conversion.

`--dry-run` combines with both `--retries` and `--slack-webhook` so you can
test the retry policy and Slack formatting/connectivity without doing any
real encoding — the message is clearly marked `:test_tube: DRY RUN` and
lists what *would* be converted instead of real results:

```
:test_tube: *video-transcoder* — DRY RUN — `./videos`
*3 file(s)* would be converted (codec `h265` · CRF `28` · jobs `4` · retries `2`)

• `clip1.mov` → `clip1.converted.mp4`
• `clip2.mov` → `clip2.converted.mp4`
• `clip3.mov` → `clip3.converted.mp4`
```

The bitrate cap is picked from the target resolution using YouTube's
published SDR upload-bitrate guidance as the H.264 baseline (2160p 45Mbps,
1440p 16Mbps, 1080p 8Mbps, 720p 5Mbps, 480p 2.5Mbps, 360p 1Mbps, 240p
500kbps), halved for H.265 since HEVC typically matches H.264 quality at
~50% of the bitrate. Caps are bumped 1.5x above 48fps.

## Live progress

Each in-flight file gets its own progress bar in the terminal, driven by
ffmpeg's `-progress` output (percent, elapsed/total time, encode speed, and
an ETA derived from remaining source duration ÷ current speed):

```
clip1.mov                [###############---------]  62.9% 0:00:05/0:00:08 10.3x ETA 0:00:00
clip2.mov                [#######################-]  99.2% 0:00:07/0:00:08 11.3x ETA 0:00:00
```

Concurrent conversions (`--jobs > 1`) each get their own stacked line that
updates in place; as a file finishes, its bar is replaced by its final
one-line result and removed from the stack, so the board only ever shows
what's still running. A file being retried shows `[attempt N/M]`. When
stdout isn't a real terminal (piped to a file/log), it falls back to a plain
status line printed every few seconds per file instead of redrawing in place.

## Checking for / adding libvmaf

```bash
python3 video-transcoder.py check-libvmaf
```
Runs the same check the script does up front — prints whether `libvmaf` is
available (and which `ffmpeg` on `PATH` was checked), exit code 0 if so,
1 if not. Equivalent to `ffmpeg -hide_banner -filters | grep -i libvmaf`.

If your ffmpeg came from a distro package manager, the easiest fix is
usually a prebuilt static binary that
already includes it, e.g. the `*-gpl` builds from
https://github.com/BtbN/FFmpeg-Builds — copy `ffmpeg`/`ffprobe` from the
archive somewhere earlier on `PATH` than the distro package (`/usr/local/bin`
typically works). `libvmaf` is GPL-licensed, so you need a `-gpl` build, not
`-lgpl`. Building ffmpeg from source with `--enable-libvmaf --enable-gpl` is
the alternative if you need other custom build flags too.

## Comparing an already-converted file

```bash
python3 video-transcoder.py compare original.mov original.converted.mp4
```
Runs just the VMAF comparison — no conversion — against two existing files
and prints the score. Useful for checking a file converted before this
script had VMAF support, or one converted elsewhere with the same codec.
