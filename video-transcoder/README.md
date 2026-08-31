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
python3 video-transcoder.py ./videos/ --retries 2 --slack-bot-token xoxb-... --slack-channel my-channel

# score a file you already converted, without converting anything
python3 video-transcoder.py compare input.mov input.converted.mp4

# check whether the ffmpeg on PATH has libvmaf support
python3 video-transcoder.py check-libvmaf

# source is on an NFS mount: read from there, write output to local disk
python3 video-transcoder.py /mnt/nfs/videos/clip.mov --nfs

# don't know the right CRF for this content? auto-discover it (see "Auto-tuning quality" below)
python3 video-transcoder.py ./videos/ --target-vmaf 93
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
| `--crf` | `23` (h264) / `20` (h265) | lower = higher quality/bigger file |
| `--preset` | `slow` (h264) / `slow` (h265) | encoder speed/efficiency tradeoff |
| `--maxrate` | auto by resolution | override the `-maxrate` safety ceiling in kbps |
| `--uncapped` | off | drop `-maxrate`/`-bufsize` entirely; CRF alone controls bitrate |
| `--target-vmaf` | off (bare flag: `93.0`) | auto-discover `--crf`/`--maxrate` for this VMAF target (see "Auto-tuning quality" below) |
| `--dry-run` | off | print the planned ffmpeg command(s) without running them |
| `--compare`/`--no-compare` | on | after encoding, score the output against the source with VMAF |
| `-j/--jobs` | `4` | directory mode: how many files to convert concurrently |
| `-r/--report` | `<directory>/conversion-report.json` | directory mode: where to write the JSON report |
| `--retries` | `0` | retry a failed conversion this many times before giving up |
| `--slack-bot-token` | `$SLACK_BOT_TOKEN` | Slack bot token; notification is disabled unless this and `--slack-channel` are both set |
| `--slack-channel` | `$SLACK_CHANNEL` | Slack channel name or ID; notification is disabled unless this and `--slack-bot-token` are both set |
| `--nfs` | off | read from the source but write output to local disk instead (see "Reading from NFS" below) |

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

If `--slack-bot-token` and `--slack-channel` (or `$SLACK_BOT_TOKEN`/`$SLACK_CHANNEL`)
are both set, a formatted summary is posted there when the run finishes —
status emoji, converted/failed counts, total space saved, average VMAF, a
bullet per failed file with its root-cause error line, and the
codec/CRF/jobs/retries settings used. Single-file runs post a one-line
result (or the error, in a code block, on failure) instead. A failed
Slack post only prints a warning; it never fails the conversion. If only
one of the two is set, notification is disabled with a one-line warning
explaining which one is missing — the bot token needs `chat:write` scope
and must be invited to the target channel.

`--dry-run` combines with both `--retries` and `--slack-bot-token`/`--slack-channel`
so you can test the retry policy and Slack formatting/connectivity without
doing any real encoding — the message is clearly marked `:test_tube: DRY RUN`
and lists what *would* be converted instead of real results:

```
:test_tube: *video-transcoder* — DRY RUN — `./videos`
*3 file(s)* would be converted (codec `h265` · CRF `28` · jobs `4` · retries `2`)

• `clip1.mov` → `clip1.converted.mp4`
• `clip2.mov` → `clip2.converted.mp4`
• `clip3.mov` → `clip3.converted.mp4`
```

The `-maxrate`/`-bufsize` cap is a safety ceiling, not a target — CRF does
the actual quality work. It's picked from the target resolution: H.264 uses
YouTube's published SDR upload-bitrate guidance directly (2160p 45Mbps,
1440p 16Mbps, 1080p 8Mbps, 720p 5Mbps, 480p 2.5Mbps, 360p 1Mbps, 240p
500kbps) — a size-conscious ceiling, appropriate since H.264's default CRF
(23) is already a fairly efficient setting. H.265's cap is set far more
generously (2160p 80Mbps, 1440p 30Mbps, 1080p 16Mbps, 720p 8Mbps, 480p
4Mbps, 360p 2Mbps, 240p 1Mbps) so it only binds on genuinely pathological
content (heavy grain/noise, extreme motion) rather than routinely
overriding the CRF quality target — a cap that binds routinely on ordinary
complex content (e.g. old camcorder footage) is what drove VMAF scores as
low as 50-60 before this was raised. Caps are bumped 1.5x above 48fps.

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

The VMAF pass gets the same live bar, tagged `[vmaf]` so you can tell it
apart from encoding — and the standalone `compare` subcommand shows one too,
since a VMAF comparison on a large file can take a while with otherwise zero
output (it's decoding both videos frame-by-frame under the hood).

## Reading from NFS

```bash
python3 video-transcoder.py /mnt/nfs/videos/clip.mov --nfs
python3 video-transcoder.py /mnt/nfs/videos/ --nfs --jobs 4
python3 video-transcoder.py /mnt/nfs/videos/clip.mov --nfs -o /scratch/clip.converted.mp4
```

`--nfs` reads the source from wherever it is (NFS or otherwise) but writes
the converted output to local disk instead of alongside the source —
useful because writing (especially the `-movflags +faststart` rewrite at
the end of encoding) is much slower and less reliable over NFS than local
disk. Without `-o`, output goes to a shared local temp directory
(`$TMPDIR/video-transcoder`, e.g. `/tmp/video-transcoder`); `-o` overrides
this the same way it does normally (output file for a single input, output
directory for a directory input).

Before writing, it checks free space on the destination filesystem against
the source file's size (a safe upper bound, since converted output is
almost always smaller) and fails that file with a clear error — no ffmpeg
run, no partial file — if there isn't enough room, rather than running out
of disk mid-encode.

In directory mode with `--jobs > 1`, several files reserve space at once.
A per-file check against a fresh OS free-space reading each time isn't
enough on its own — two files could each see the same free space a moment
apart and both pass, even though combined they don't fit. A shared,
lock-protected ledger tracks bytes already claimed by conversions that are
still in flight (i.e. haven't necessarily finished writing yet) and
subtracts that from the OS-reported free space before approving the next
file, releasing the claim once that file's conversion finishes (success or
failure). It's still a snapshot-based check, not a filesystem-level
reservation — actual usage can differ slightly from a file's declared size
— but it prevents the concurrent-approval race, not just a single-file one.

## Auto-tuning quality

```bash
python3 video-transcoder.py input.mov --target-vmaf 93
python3 video-transcoder.py ./videos/ --target-vmaf 90 --tune-samples 4 --tune-generations 8
```

Picking a CRF by hand means guessing, converting, checking VMAF, and
adjusting — exactly the loop `--target-vmaf` automates. Instead of using
the fixed `--crf`/`--maxrate` defaults, it:

1. Extracts a few short sample clips (default: 3, 6s each — `--tune-samples`/
   `--tune-sample-duration`) via fast stream-copy from evenly-spaced points
   across the middle 80% of the input (skipping likely intro/outro).
2. Binary-searches CRF: each generation (default: up to 6 —
   `--tune-generations`) encodes every sample at one candidate CRF and takes
   the **worst** (not average) VMAF across samples — a floor guarantee, not
   just a typical case. If that meets the target (within `--tune-tolerance`,
   default 1.0), the search tries a higher CRF (smaller file) next;
   otherwise a lower one (higher quality). This converges on the highest
   CRF — smallest file — that still meets the target everywhere sampled.
3. Once CRF converges, re-encodes the samples at that CRF once more to
   measure their actual bitrate, and derives `--maxrate` from it (1.5x
   headroom) instead of the fixed per-resolution table.

The discovered CRF/maxrate are then used for the real conversion, exactly
as if you'd passed `--crf`/`--maxrate` yourself — mutually exclusive with
those flags for that reason. Requires `libvmaf` (checked up front, like
`--compare`) regardless of whether `--no-compare` is also set, since the
search itself depends on VMAF even if you don't want a final check.

**In directory mode, tuning runs once, against the first file** — not
per file. This is deliberate: comprehensively tuning every file in a
100-file batch would mean 100x the sampling/search cost. If the files come
from the same recording session or device (the common case for a batch),
one file's tuned settings are a reasonable estimate for the rest. If your
batch is genuinely heterogeneous, tune each file individually instead:
`--target-vmaf` on a single file at a time, or pick the settings it finds
and pass them explicitly per subset via `--crf`/`--maxrate`.

`--dry-run` skips the actual search (it's real encode-and-compare work,
not free) and just prints what tuning would target and reuse.

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
