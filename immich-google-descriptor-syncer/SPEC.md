# Spec — immich-google-descriptor-syncer

Copy photo captions from Google Takeout sidecar JSON files onto the matching assets
in an Immich album.

Status: implemented and verified offline. Never yet run against a live Immich server
(see [Verification](#verification)).

## Problem

Google Takeout exports each photo alongside a `<filename>.supplemental-metadata.json`
sidecar carrying the caption the photo had in Google Photos. Immich's importer does not
read these sidecars, so captions are lost on import. The photos are already in an Immich
album; only the descriptions are missing.

Reference data set (the album this was written for): 139 media files, 138 sidecars,
123 of which carry a non-empty description, 15 empty. Filenames include awkward cases
(`IMG_0782 2.HEIC`, `IMG_1688~2.JPG`) that must round-trip intact.

## Interface

Single file, Python 3, standard library only (`urllib`) — no install step.

```
export IMMICH_URL=https://photos.example.com
export IMMICH_API_KEY=...
python3 immich-google-descriptor-syncer.py \
    --album "Nikola MillWood 2026 - SK" \
    --dir "/path/to/the/takeout/album" \
    --dry-run
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--album` | required | Album name (exact match first, then case-insensitive) or album UUID |
| `--dir` | `.` | Directory holding the media and sidecars |
| `--url` | `$IMMICH_URL` | Immich base URL; `/api` is appended if absent |
| `--api-key` | `$IMMICH_API_KEY` | API key, sent as the `x-api-key` header |
| `--dry-run` | off | Do every read and match, write nothing |

Exit codes: `0` success, `1` one or more writes failed (or no described sidecars found),
`2` a fatal API/setup error, `130` interrupted.

## Decisions

**An existing description is never overwritten.** Chosen during design over the
alternatives "always overwrite" and "skip unless `--force`". There is deliberately no
`--force` flag; the sidecar is not treated as the source of truth. Conflicts are
reported side by side so they can be resolved by hand. Consequence: the script is
idempotent and safe to re-run.

**Match by filename, case-insensitively.** In Takeout data the sidecar's `title` field
is byte-identical to the media filename, and Immich stores it as `originalFileName`, so
this is a direct comparison. Case-insensitivity guards against a server or filesystem
that normalises case. A filename that matches more than one asset in the album is
ambiguous and is skipped rather than guessed at.

**Sidecars with an empty description are not writes.** They are counted, not listed —
15 of them in the reference album, and nothing can be done about them.

**Stdlib only.** A one-shot utility that must run anywhere `python3` exists.

## Flow

1. Scan `--dir` for `*.supplemental-metadata.json`. Build `lowercased filename →
   (filename, description)`, taking the filename from the sidecar's `title` and falling
   back to stripping the suffix. Skip empty descriptions. An unreadable or malformed
   sidecar prints a warning to stderr and is skipped — it does not abort the run. The
   album-level `metadata.json` has no matching suffix and is ignored for free.
2. Resolve the album. A UUID in `--album` is used directly; otherwise `GET /api/albums`
   and match on `albumName`, exact then case-insensitive. Zero matches is a fatal error
   that lists the available album names; more than one is a fatal error that lists the
   ids.
3. Fetch the album's assets with EXIF.
4. Match each described sidecar to an asset by the lowercased filename key.
5. If the asset already has a non-empty description, record a conflict and move on.
   Otherwise `PUT /api/assets/{id}` with `{"description": ...}`. A failed write is
   recorded and the run continues; the process exits `1` at the end.
6. Print the report.

## Immich API

Verified against Immich's published OpenAPI spec
(https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.json),
not from memory. Base path `/api`, auth via the `x-api-key` header.

- `GET /api/albums` — album list for name resolution.
- `GET /api/albums/{id}` — album info. **Current versions no longer return `assets`
  here.** The embedded list is used when present (older servers) and otherwise ignored.
- `POST /api/search/metadata` — the fallback asset source, paged with
  `{"albumIds": [id], "withExif": true, "size": 1000, "page": n}`, following
  `assets.nextPage` until it is null.
- `PUT /api/assets/{id}` with `{"description": "..."}` — the write.

An asset's current description may arrive as the top-level `description` or as
`exifInfo.description` depending on how it was fetched; both are read, first non-empty
wins.

## Report

A summary of counts, then a section per category that needs human eyes:

| Category | Listed? |
| --- | --- |
| updated / would update | count |
| skipped, description already set in Immich | full list, Immich vs sidecar side by side |
| sidecars with an empty description | count only |
| sidecars with no matching album asset | list, capped at 40 |
| album assets with no sidecar description | list, capped at 40 |
| skipped, duplicate filenames in album | full list, with asset ids |
| sidecars with duplicate filenames and conflicting descriptions | full list |
| failed writes | full list, with the error |

The two capped lists are informational and can each run to the full album size when the
album is wrong; the caps keep a mismatch from burying the summary. Everything requiring
action is uncapped.

## Verification

No test framework here and no live server available during development, so:

- **Verified** — sidecar loading against the real 138-file album; matching, conflict
  skip, duplicate skip, isolated write failure, and dry-run-writes-nothing against a
  fake client substituted for `Immich`; the report cap.
- **Not verified** — anything on the wire. The request and response shapes are
  spec-derived rather than observed.
- **The live check** is a `--dry-run` against the real album, reading the report before
  applying. `sidecars with no matching album asset: 123` means the album resolved but
  the filenames do not line up — inspect a few real `originalFileName` values before
  going further. Then apply and spot-check one photo in the Immich UI.
