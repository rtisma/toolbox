#!/usr/bin/env python3
"""slideshow.py - build an mp4 slideshow from a directory of images (and videos).

Subcommands, meant to be run in order:

  check          Report which system dependencies are installed.
  generate-list  Build an ordered CSV list of media paths + capture
                 dates (sorted by filename, descending, or randomized;
                 optionally deduplicated by exact file content). Edit
                 the resulting file freely before moving on.
  annotate       (optional) Burn each item's capture date (from the
                 CSV, or file mtime if blank) into its bottom-right
                 corner. Works on still images and video clips.
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
--annotate fall back to the file's mtime. start_time/end_time trim a
video clip (ignored for still images); generate-list pre-fills them to
the clip's full range (00:00:00 to its duration) so you can just crop
the numbers down by hand. If you blank start_time back out while
end_time is set, it defaults back to 00:00:00; if you blank end_time
back out while start_time is set, the clip runs to its natural end. A
row whose filename starts with '#' is treated as a comment and
skipped.

Dependencies: ffmpeg (and ffprobe) on PATH for `render`/`generate-list`;
exiftool on PATH for `generate-list`; ffmpeg and a running docker
daemon for `annotate` (the actual text burn-in runs inside a small
Docker container, because Homebrew's ffmpeg build lacks the drawtext
filter). Commands that need dependencies check them first; pass
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
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

DOCKER_IMAGE = "slideshow-ffmpeg-drawtext"

DOCKERFILE = """\
FROM debian:bookworm-slim

RUN apt-get update \\
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \\
    && rm -rf /var/lib/apt/lists/*
"""

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


def check_docker_cli() -> tuple[bool, str]:
    path = shutil.which("docker")
    return (path is not None, path or "not found on PATH")


def check_docker_daemon() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return (False, "docker CLI not found")
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return (False, "daemon not reachable (is Docker running?)")
    return (result.returncode == 0, "running" if result.returncode == 0 else "daemon not reachable (is Docker running?)")


# (name, check function, commands that require it)
CHECKS = [
    ("ffmpeg", check_ffmpeg, {"render", "annotate", "generate-list"}),
    ("exiftool", check_exiftool, {"generate-list"}),
    ("docker CLI", check_docker_cli, {"annotate"}),
    ("docker daemon", check_docker_daemon, {"annotate"}),
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

def mtime_str(path: str, fmt: str) -> str:
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime(fmt)


def drawtext_filter(ts: str, font_size: int) -> str:
    # drawtext treats ':' as an option separator; escape it in the value.
    escaped_ts = ts.replace(":", r"\:")
    return (
        f"drawtext=text='{escaped_ts}':x=w-tw-20:y=h-th-20:"
        f"fontsize={font_size}:"
        f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        f"fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=8"
    )


def annotate_cmd(src: Path, dest: Path, ts: str, font_size: int, start: str, end: str) -> str:
    vf = drawtext_filter(ts, font_size)
    if is_video(str(src)):
        trim = ""
        if start:
            trim += f" -ss {shlex.quote(start)}"
        if end:
            trim += f" -to {shlex.quote(end)}"
        return "ffmpeg -y -i {src}{trim} -vf {vf} -c:v libx264 -crf 18 -preset fast -c:a copy -movflags +faststart {dest} -loglevel error".format(
            src=shlex.quote(str(src)), trim=trim, vf=shlex.quote(vf), dest=shlex.quote(str(dest)),
        )
    return "ffmpeg -y -i {src} -vf {vf} -frames:v 1 -q:v 2 {dest} -loglevel error".format(
        src=shlex.quote(str(src)), vf=shlex.quote(vf), dest=shlex.quote(str(dest)),
    )


def build_docker_image() -> None:
    with tempfile.TemporaryDirectory() as build_dir:
        dockerfile_path = Path(build_dir) / "Dockerfile"
        dockerfile_path.write_text(DOCKERFILE)
        subprocess.run(
            ["docker", "build", "-q", "-t", DOCKER_IMAGE, "-f", str(dockerfile_path), build_dir],
            check=True, stdout=subprocess.DEVNULL,
        )


def run_annotation(list_path: Path, out_dir: Path, date_format: str, font_size: int) -> tuple[list[tuple[str, str, str, str, str]], int]:
    """Burn dates (and, for videos, trim to start_time/end_time) into copies
    of every real entry in list_path.

    Returns (rows, count) where rows is [(dest_path, exif_datestamp, "", "", ""), ...]
    -- filename_datestamp/start/end come back blank because the resolved date
    and trim are already baked into the output file -- suitable for writing
    a new CSV list or feeding directly into render.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    build_docker_image()

    entries = read_list_csv(list_path)
    mount_dirs = {out_dir}
    batch_lines = []
    out_rows = []

    for filename, exif_ds, filename_ds, start, end in entries:
        src = Path(filename).resolve()
        mount_dirs.add(src.parent)

        ts = exif_ds or filename_ds
        if not ts:
            ts = mtime_str(str(src), date_format)
            print(f"No capture date for {src}, using file mtime", file=sys.stderr)

        start, end = resolve_trim(start, end)
        if (start or end) and not is_video(str(src)):
            print(f"Ignoring start_time/end_time for non-video item {src}", file=sys.stderr)
            start, end = "", ""

        dest = out_dir / src.name
        batch_lines.append(annotate_cmd(src, dest, ts, font_size, start, end))
        out_rows.append((str(dest), ts, "", "", ""))

    mounts = []
    for d in mount_dirs:
        mounts += ["-v", f"{d}:{d}"]

    batch_script = "\n".join(batch_lines) + "\n"
    subprocess.run(
        ["docker", "run", "--rm", "-i", *mounts, DOCKER_IMAGE, "sh"],
        input=batch_script, text=True, check=True,
    )

    return out_rows, len(out_rows)


def cmd_annotate(args: argparse.Namespace) -> None:
    ensure_deps("annotate", args.skip_checks)

    list_path = Path(args.list)
    out_dir = Path(args.output_dir).resolve()
    out_list_path = Path(args.output_list) if args.output_list else list_path.with_suffix(".annotated.csv")

    rows, n = run_annotation(list_path, out_dir, args.date_format, args.font_size)

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
        with tempfile.TemporaryDirectory(prefix="slideshow-annotate-") as tmp_dir:
            rows, _n = run_annotation(Path(args.list), Path(tmp_dir), args.date_format, args.font_size)
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
        description="Check ffmpeg, exiftool and docker (CLI + daemon) and report status for each.",
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
            "Burn exif_datestamp/filename_datestamp from a CSV list (falling back "
            "to file mtime if both are blank) into the bottom-right corner of a "
            "copy of each item. Images get a single annotated frame; videos are "
            "re-encoded with the date burned into every frame and audio "
            "preserved, trimmed to start_time/end_time if those columns are set "
            "(ignored for still images). The text burn-in runs inside a small "
            "Docker container, since Homebrew's ffmpeg build lacks the drawtext "
            "filter."
        ),
        epilog="Example: slideshow.py annotate -l list.csv -O ~/Pictures/trip_annotated",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_annotate.add_argument("-l", "--list", required=True, help="CSV list (filename,exif_datestamp,filename_datestamp,start_time,end_time)")
    p_annotate.add_argument("-O", "--output-dir", required=True, help="Directory to write annotated copies into")
    p_annotate.add_argument("-o", "--output-list", help="Output CSV list file (default: <list>.annotated.csv)")
    p_annotate.add_argument("-F", "--date-format", default="%Y-%m-%d", help="strftime date-only format for the mtime fallback (default: %%Y-%%m-%%d -> 2024-01-31)")
    p_annotate.add_argument("-s", "--font-size", type=int, default=28, help="Font size (default: 28)")
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
    p_render.add_argument("-F", "--date-format", default="%Y-%m-%d", help="strftime date-only format for the mtime fallback (default: %%Y-%%m-%%d -> 2024-01-31; only used with --annotate)")
    p_render.add_argument("-s", "--font-size", type=int, default=28, help="Font size for the burned-in date (only used with --annotate, default: 28)")
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
