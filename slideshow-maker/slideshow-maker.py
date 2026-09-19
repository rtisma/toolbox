#!/usr/bin/env python3
"""slideshow.py - build an mp4 slideshow from a directory of images (and videos).

Subcommands, meant to be run in order:

  check          Report which system dependencies are installed.
  generate-list  Build an ordered CSV list of media paths + capture
                 dates (sorted by filename, descending, or randomized;
                 optionally deduplicated by exact file content). Edit
                 the resulting file freely before moving on.
  annotate       (optional) Burn each item's capture date from the
                 CSV into its bottom-right corner; items with no date
                 are copied through unannotated. Works on still images
                 and video clips.
  render         Render an mp4 from a list, N seconds per image, with
                 an optional crossfade between images. Pass --annotate
                 to burn dates in as part of rendering.

The list is a CSV with five columns: filename (required),
exif_datestamp, filename_datestamp, start_time, end_time (all
optional). Only the date matters here, not the time of day.
exif_datestamp comes from exiftool; if that finds nothing,
generate-list falls back to an 8-digit YYYYMMDD run in the filename
(e.g. "20191028" -> October 28th 2019) and puts it in
filename_datestamp instead -- the two are mutually exclusive, and a
row with both set is an error. If both are blank, annotate/render
--annotate burn nothing in for that item (it's trimmed, if a video
with start_time/end_time set, or otherwise copied through unchanged).
start_time/end_time trim a video clip (ignored for still images);
generate-list pre-fills them to
the clip's full range (00:00:00 to its duration) so you can just crop
the numbers down by hand. If you blank start_time back out while
end_time is set, it defaults back to 00:00:00; if you blank end_time
back out while start_time is set, the clip runs to its natural end. A
row whose filename starts with '#' is treated as a comment and
skipped.

Dependencies: ffmpeg (and ffprobe) on PATH for `render`/`generate-list`;
exiftool on PATH for `generate-list`; ffmpeg on PATH *with the drawtext
filter compiled in* for `annotate` (Homebrew's default ffmpeg formula
lacks it -- `check` reports this separately from plain ffmpeg presence,
and its message includes the fix: `brew install ffmpeg-full && brew
link --overwrite ffmpeg-full` on macOS, `sudo apt-get install ffmpeg`
on Ubuntu/Debian, which normally has it already). Commands that need
dependencies check them first; pass
--skip-checks to bypass that.

Note: render's per-item duration/crossfade logic assumes every list
entry is a still image. A video entry can be annotated, but rendering
a list that mixes videos with images is not yet supported.
"""

import argparse
import csv
import glob
import hashlib
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg"}

LIST_HEADER = ["filename", "exif_datestamp", "filename_datestamp", "start_time", "end_time"]

# 8 digits not adjacent to another digit, e.g. "IMG_20191028_143022.jpg" -> "20191028".
FILENAME_DATE_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


# --- dependency checks ----------------------------------------------------

def check_ffmpeg() -> tuple[bool, str]:
    path = shutil.which("ffmpeg")
    return (path is not None, path or "not found on PATH")


def check_exiftool() -> tuple[bool, str]:
    path = shutil.which("exiftool")
    return (path is not None, path or "not found on PATH")


def check_ffmpeg_drawtext() -> tuple[bool, str]:
    """annotate burns text in via ffmpeg's drawtext filter, which needs
    ffmpeg built with libfreetype. Homebrew's default ffmpeg formula lacks
    it; Ubuntu/Debian's apt package normally has it already."""
    if shutil.which("ffmpeg") is None:
        return (False, "ffmpeg not found on PATH")
    result = subprocess.run(["ffmpeg", "-h", "filter=drawtext"], capture_output=True, text=True)
    if "Unknown filter" in result.stdout or "Unknown filter" in result.stderr:
        system = platform.system()
        if system == "Darwin":
            fix = "brew install ffmpeg-full && brew link --overwrite ffmpeg-full"
        elif system == "Linux":
            fix = "sudo apt-get update && sudo apt-get install -y ffmpeg"
        else:
            fix = "install an ffmpeg build with libfreetype (--enable-libfreetype)"
        return (False, f"ffmpeg was built without the drawtext filter (needs libfreetype) -- fix: {fix}")
    return (True, "available")


# (name, check function, commands that require it)
CHECKS = [
    ("ffmpeg", check_ffmpeg, {"render", "annotate", "generate-list"}),
    ("exiftool", check_exiftool, {"generate-list"}),
    ("ffmpeg drawtext filter", check_ffmpeg_drawtext, {"annotate"}),
]


def run_all_checks() -> bool:
    """Print a full dependency report. Returns True if everything is OK."""
    all_ok = True
    for name, fn, _commands in CHECKS:
        ok, detail = fn()
        all_ok = all_ok and ok
        status = "OK" if ok else "MISSING"
        print(f"[{status}] {name}: {detail}")
    return all_ok


def ensure_deps(command: str, skip: bool) -> None:
    """Check only the dependencies a given command needs; exit with a clear error if any are missing."""
    if skip:
        return
    failures = []
    for name, fn, commands in CHECKS:
        if command not in commands:
            continue
        ok, detail = fn()
        if not ok:
            failures.append(f"{name}: {detail}")
    if failures:
        lines = "\n".join(f"  - {f}" for f in failures)
        sys.exit(
            f"error: missing dependencies for '{command}':\n{lines}\n"
            f"Run 'slideshow.py check' for the full report, or pass --skip-checks to bypass."
        )


def cmd_check(args: argparse.Namespace) -> None:
    ok = run_all_checks()
    if not ok:
        sys.exit(1)


# --- CSV list I/O -----------------------------------------------------------

def write_list_csv(path: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(LIST_HEADER)
        for row in rows:
            writer.writerow(row)


def read_list_csv(path: Path) -> list[tuple[str, str, str, str, str]]:
    """Return (filename, exif_datestamp, filename_datestamp, start_time,
    end_time) for every real (non-comment) row. Verifies files exist.
    exif_datestamp and filename_datestamp are mutually exclusive -- a row
    with both set is an error, since it's ambiguous which one to trust."""
    if not path.is_file():
        sys.exit(f"error: list file not found: {path}")
    rows = []
    with path.open(newline="") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if not row:
                continue
            filename = row[0].strip()
            if i == 0 and filename.lower() == "filename":
                continue
            if not filename or filename.startswith("#"):
                continue
            exif_ds = row[1].strip() if len(row) > 1 else ""
            filename_ds = row[2].strip() if len(row) > 2 else ""
            start = row[3].strip() if len(row) > 3 else ""
            end = row[4].strip() if len(row) > 4 else ""
            if exif_ds and filename_ds:
                sys.exit(
                    f"error: {filename} has both exif_datestamp and filename_datestamp set; "
                    f"only one may be present"
                )
            if not os.path.isfile(filename):
                sys.exit(f"error: missing file: {filename}")
            rows.append((filename, exif_ds, filename_ds, start, end))
    if not rows:
        sys.exit(f"error: no entries found in {path}")
    return rows


def resolve_trim(start: str, end: str) -> tuple[str, str]:
    """Apply the blank-default rule: a blank start paired with a set end
    becomes 00:00:00; a blank end paired with a set start stays blank
    (meaning "run to the clip's natural end" -- no -to arg needed)."""
    start = start.strip()
    end = end.strip()
    if not start and end:
        start = "00:00:00"
    return start, end


def scale_pad_filter(resolution: str) -> str:
    width, height = resolution.split("x")
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )


# --- generate-list -----------------------------------------------------------

def exif_datestamp(path: str, fmt: str) -> str:
    """Try DateTimeOriginal then CreateDate via exiftool. Returns '' if neither
    is present (some containers, e.g. mp4/QuickTime, use an all-zero sentinel
    for "unset" rather than omitting the tag, so that's treated as absent too).
    """
    for tag in ("-DateTimeOriginal", "-CreateDate"):
        raw = subprocess.run(
            ["exiftool", "-s3", tag, path], capture_output=True, text=True,
        ).stdout.strip()
        if not raw or raw.startswith("0000:00:00"):
            continue
        result = subprocess.run(
            ["exiftool", "-s3", tag, "-d", fmt, path],
            capture_output=True, text=True,
        )
        ts = result.stdout.strip()
        if ts:
            return ts
    return ""


def filename_datestamp(path: str, fmt: str) -> str:
    """Look for an 8-digit YYYYMMDD run in the filename (e.g. "20191028" ->
    October 28th 2019) and return it formatted per fmt. Returns '' if no
    8-digit run in the name parses as a valid date."""
    name = Path(path).stem
    for match in FILENAME_DATE_RE.finditer(name):
        try:
            dt = datetime.strptime(match.group(1), "%Y%m%d")
        except ValueError:
            continue
        return dt.strftime(fmt)
    return ""


def video_duration_str(path: str) -> str:
    """Return the video's duration as HH:MM:SS (whole seconds, floored)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", path],
        capture_output=True, text=True, check=True,
    )
    total_seconds = int(float(result.stdout.strip()))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def file_sha256(path: str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dedupe_files(files: list[str]) -> list[str]:
    """Drop files whose content exactly matches one already kept (first
    occurrence wins), reporting each skip to stderr."""
    seen_hashes: dict[str, str] = {}
    deduped = []
    for f in files:
        digest = file_sha256(f)
        original = seen_hashes.get(digest)
        if original is not None:
            print(f"Skipping duplicate of {original}: {f}", file=sys.stderr)
            continue
        seen_hashes[digest] = f
        deduped.append(f)
    return deduped


def cmd_generate_list(args: argparse.Namespace) -> None:
    ensure_deps("generate-list", args.skip_checks)

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        sys.exit(f"error: not a directory: {input_dir}")

    patterns = [p.strip() for p in args.glob.split(",") if p.strip()]
    files: list[str] = []
    seen = set()
    for pattern in patterns:
        for f in glob.glob(str(input_dir / pattern)):
            if os.path.isfile(f) and f not in seen:
                seen.add(f)
                files.append(f)

    if not files:
        sys.exit(f"error: no files matched '{args.glob}' in {input_dir}")

    if args.dedupe:
        files = dedupe_files(files)
        if not files:
            sys.exit(f"error: no files left after deduplication in {input_dir}")

    if args.random:
        random.shuffle(files)
    else:
        files.sort(key=lambda f: os.path.basename(f), reverse=True)

    rows = []
    for f in files:
        exif_ds = exif_datestamp(f, args.date_format)
        filename_ds = "" if exif_ds else filename_datestamp(f, args.date_format)
        if is_video(f):
            rows.append((f, exif_ds, filename_ds, "00:00:00", video_duration_str(f)))
        else:
            rows.append((f, exif_ds, filename_ds, "", ""))

    out_path = Path(args.output)
    write_list_csv(out_path, rows)
    print(f"Wrote {len(rows)} entries to {out_path}")


# --- annotate ------------------------------------------------------------

# Checked in order; first one that exists on disk wins. Only macOS/Linux --
# this tool has no Windows target.
DEFAULT_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def default_font_file() -> str:
    for candidate in DEFAULT_FONT_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return ""


def drawtext_filter(ts: str, font_size: int, font_file: str) -> str:
    # drawtext treats ':' as an option separator; escape it in the value.
    escaped_ts = ts.replace(":", r"\:")
    parts = [f"drawtext=text='{escaped_ts}'", "x=w-tw-20", "y=h-th-20", f"fontsize={font_size}"]
    if font_file:
        parts.append(f"fontfile={font_file}")
    parts += ["fontcolor=white", "box=1", "boxcolor=black@0.5", "boxborderw=8"]
    return ":".join(parts)


def annotate_cmd(src: Path, dest: Path, ts: str, font_size: int, font_file: str, start: str, end: str) -> list[str]:
    vf = drawtext_filter(ts, font_size, font_file) if ts else None
    if is_video(str(src)):
        cmd = ["ffmpeg", "-y", "-i", str(src)]
        if start:
            cmd += ["-ss", start]
        if end:
            cmd += ["-to", end]
        if vf:
            cmd += ["-vf", vf]
        cmd += [
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "copy", "-movflags", "+faststart",
            str(dest), "-loglevel", "error",
        ]
        return cmd
    # images only reach here with ts set -- the no-date/no-trim case is a
    # plain copy, handled before this is called.
    return ["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-frames:v", "1", "-q:v", "2", str(dest), "-loglevel", "error"]


def run_annotation(list_path: Path, out_dir: Path, font_size: int, font_file: str) -> tuple[list[tuple[str, str, str, str, str]], int]:
    """Burn dates (and, for videos, trim to start_time/end_time) into copies
    of every real entry in list_path. If an item has neither exif_datestamp
    nor filename_datestamp, no text is burned in -- it's trimmed (if a video
    with start_time/end_time set) or otherwise copied through unchanged.

    Returns (rows, count) where rows is [(dest_path, exif_datestamp, "", "", ""), ...]
    -- filename_datestamp/start/end come back blank because the resolved date
    (if any) and trim are already baked into the output file -- suitable for
    writing a new CSV list or feeding directly into render.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = read_list_csv(list_path)
    out_rows = []

    for filename, exif_ds, filename_ds, start, end in entries:
        src = Path(filename).resolve()
        ts = exif_ds or filename_ds

        start, end = resolve_trim(start, end)
        if (start or end) and not is_video(str(src)):
            print(f"Ignoring start_time/end_time for non-video item {src}", file=sys.stderr)
            start, end = "", ""

        dest = out_dir / src.name
        if not ts and not start and not end:
            print(f"No capture date for {src}; copying without annotation", file=sys.stderr)
            shutil.copy2(src, dest)
        else:
            if not ts:
                print(f"No capture date for {src}; trimming without annotation", file=sys.stderr)
            subprocess.run(annotate_cmd(src, dest, ts, font_size, font_file, start, end), check=True)
        out_rows.append((str(dest), ts, "", "", ""))

    return out_rows, len(out_rows)


def cmd_annotate(args: argparse.Namespace) -> None:
    ensure_deps("annotate", args.skip_checks)

    list_path = Path(args.list)
    out_dir = Path(args.output_dir).resolve()
    out_list_path = Path(args.output_list) if args.output_list else list_path.with_suffix(".annotated.csv")
    font_file = args.font_file if args.font_file is not None else default_font_file()

    rows, n = run_annotation(list_path, out_dir, args.font_size, font_file)

    write_list_csv(out_list_path, rows)
    print(f"Annotated {n} items into {out_dir}; wrote {out_list_path}")


# --- render ---------------------------------------------------------------

def read_list(path: Path) -> list[str]:
    return [row[0] for row in read_list_csv(path)]


def render_files(files: list[str], args: argparse.Namespace) -> None:
    n = len(files)
    scale_pad = scale_pad_filter(args.resolution)

    if args.fade == 0:
        concat_lines = []
        for f in files:
            concat_lines.append(f"file '{f}'")
            concat_lines.append(f"duration {args.duration}")
        concat_lines.append(f"file '{files[-1]}'")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write("\n".join(concat_lines) + "\n")
            concat_path = fh.name
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path,
                    "-vf", scale_pad,
                    "-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset,
                    "-pix_fmt", "yuv420p", "-vsync", "vfr",
                    args.output,
                ],
                check=True,
            )
        finally:
            os.unlink(concat_path)
    else:
        clip_len = args.duration + args.fade
        inputs = []
        for f in files:
            inputs += ["-loop", "1", "-t", str(clip_len), "-i", f]

        filter_parts = [f"[{i}:v]{scale_pad}[v{i}];" for i in range(n)]
        cur = "v0"
        offset = 0.0
        for i in range(1, n):
            offset += args.duration
            out_label = f"x{i}"
            filter_parts.append(
                f"[{cur}][v{i}]xfade=transition=fade:duration={args.fade}:offset={offset}[{out_label}];"
            )
            cur = out_label
        filter_complex = "".join(filter_parts).rstrip(";")

        subprocess.run(
            [
                "ffmpeg", "-y", *inputs,
                "-filter_complex", filter_complex,
                "-map", f"[{cur}]",
                "-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset,
                "-pix_fmt", "yuv420p",
                args.output,
            ],
            check=True,
        )

    fade_msg = f", {args.fade}s crossfade" if args.fade else ""
    print(f"Wrote {args.output} ({n} images, {args.duration}s each{fade_msg})")


def cmd_render(args: argparse.Namespace) -> None:
    ensure_deps("render", args.skip_checks)
    if args.annotate:
        ensure_deps("annotate", args.skip_checks)
        font_file = args.font_file if args.font_file is not None else default_font_file()
        with tempfile.TemporaryDirectory(prefix="slideshow-annotate-") as tmp_dir:
            rows, _n = run_annotation(Path(args.list), Path(tmp_dir), args.font_size, font_file)
            files = [row[0] for row in rows]
            if not files:
                sys.exit(f"error: no entries found in {args.list}")
            render_files(files, args)
    else:
        files = read_list(Path(args.list))
        render_files(files, args)


# --- CLI -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slideshow.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser(
        "check", help="Report which system dependencies are installed",
        description="Check ffmpeg, exiftool, and whether ffmpeg has the drawtext filter, and report status for each.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_check.set_defaults(func=cmd_check)

    p_gen = sub.add_parser(
        "generate-list", help="Build an ordered CSV list of media paths + capture dates",
        description=(
            "Build an ordered CSV list (filename,exif_datestamp,filename_datestamp,"
            "start_time,end_time) of media paths. exif_datestamp is extracted via "
            "exiftool (DateTimeOriginal, then CreateDate); if neither is present, "
            "an 8-digit YYYYMMDD run in the filename (e.g. \"20191028\") is tried "
            "instead and put in filename_datestamp -- at most one of the two is "
            "ever set. Only the date is kept, not the time of day. For video "
            "files, start_time/end_time are pre-filled to the clip's full range "
            "(00:00:00 to its duration) so you can crop the numbers down by hand; "
            "left blank for still images. --dedupe drops any file whose content "
            "exactly matches one already kept."
        ),
        epilog=(
            "Examples:\n"
            "  slideshow.py generate-list -i ~/Pictures/trip -o list.csv -R\n"
            "  slideshow.py generate-list -i ~/Pictures/trip -g \"*.jpg,*.mp4\" -o list.csv\n"
            "  slideshow.py generate-list -i ~/Pictures/trip -o list.csv --dedupe"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_gen.add_argument("-i", "--input-dir", required=True, help="Directory of images/videos")
    p_gen.add_argument("-g", "--glob", default="*.jpg", help="Comma-separated glob(s), e.g. \"*.jpg,*.mp4\" (default: *.jpg)")
    p_gen.add_argument("-o", "--output", default="list.csv", help="Output CSV list file (default: list.csv)")
    p_gen.add_argument("-R", "--random", action="store_true", help="Randomize order (default: sort by filename, descending)")
    p_gen.add_argument("-D", "--dedupe", action="store_true", help="Skip files whose content exactly matches one already kept (first occurrence wins)")
    p_gen.add_argument("-F", "--date-format", default="%Y-%m-%d", help="exiftool/strftime date-only format, e.g. %%Y-%%m-%%d is year-month-day (default: %%Y-%%m-%%d -> 2024-01-31)")
    p_gen.add_argument("--skip-checks", action="store_true", help="Skip the dependency check before running")
    p_gen.set_defaults(func=cmd_generate_list)

    p_annotate = sub.add_parser(
        "annotate", help="Burn each item's capture date into its bottom-right corner",
        description=(
            "Burn exif_datestamp/filename_datestamp from a CSV list into the "
            "bottom-right corner of a copy of each item; items with both blank "
            "are copied through unannotated (still trimmed, for a video with "
            "start_time/end_time set). Images get a single annotated frame; "
            "videos are re-encoded with the date burned into every frame and "
            "audio preserved, trimmed to start_time/end_time if those columns "
            "are set (ignored for still images). Uses ffmpeg's drawtext filter "
            "directly, so it needs a build with libfreetype (see `check`)."
        ),
        epilog="Example: slideshow.py annotate -l list.csv -O ~/Pictures/trip_annotated",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_annotate.add_argument("-l", "--list", required=True, help="CSV list (filename,exif_datestamp,filename_datestamp,start_time,end_time)")
    p_annotate.add_argument("-O", "--output-dir", required=True, help="Directory to write annotated copies into")
    p_annotate.add_argument("-o", "--output-list", help="Output CSV list file (default: <list>.annotated.csv)")
    p_annotate.add_argument("-s", "--font-size", type=int, default=28, help="Font size (default: 28)")
    p_annotate.add_argument("--font-file", default=None, help="Path to a TTF/TTC font file (default: auto-detect a system font)")
    p_annotate.add_argument("--skip-checks", action="store_true", help="Skip the dependency check before running")
    p_annotate.set_defaults(func=cmd_annotate)

    p_render = sub.add_parser(
        "render", help="Render an mp4 slideshow from an image list",
        description="Render an mp4 from a CSV list of image paths, N seconds per image.",
        epilog=(
            "Examples:\n"
            "  slideshow.py render -l list.csv -o out.mp4 -d 2 -f 0.5\n"
            "  slideshow.py render -l list.csv -o out.mp4 -d 2 --annotate"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_render.add_argument("-l", "--list", required=True, help="CSV list (filename,exif_datestamp,filename_datestamp,start_time,end_time)")
    p_render.add_argument("-o", "--output", required=True, help="Output mp4 path")
    p_render.add_argument("-d", "--duration", type=float, default=2, help="Seconds per image (default: 2)")
    p_render.add_argument("-r", "--resolution", default="1920x1080", help="Output resolution WxH (default: 1920x1080)")
    p_render.add_argument("-f", "--fade", type=float, default=0, help="Crossfade duration in seconds (default: 0 = hard cut)")
    p_render.add_argument("-c", "--crf", type=int, default=18, help="x264 CRF, lower = better/larger (default: 18)")
    p_render.add_argument("-p", "--preset", default="slow", help="x264 preset (default: slow)")
    p_render.add_argument("-a", "--annotate", action="store_true", help="Burn each photo's capture date into its bottom-right corner before rendering")
    p_render.add_argument("-s", "--font-size", type=int, default=28, help="Font size for the burned-in date (only used with --annotate, default: 28)")
    p_render.add_argument("--font-file", default=None, help="Path to a TTF/TTC font file (only used with --annotate; default: auto-detect a system font)")
    p_render.add_argument("--skip-checks", action="store_true", help="Skip the dependency check before running")
    p_render.set_defaults(func=cmd_render)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: command failed ({e.cmd}): exit code {e.returncode}")


if __name__ == "__main__":
    main()
