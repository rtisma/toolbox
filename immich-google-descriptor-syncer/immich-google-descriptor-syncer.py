#!/usr/bin/env python3
"""Copy descriptions from Google Takeout sidecars onto assets in an Immich album.

Each photo exported by Google Takeout has a `<filename>.supplemental-metadata.json`
sidecar carrying the caption it had in Google Photos. Immich's importer does not
pick these up, so this script matches sidecars to album assets by filename and
writes the caption into the asset's description.

An asset that already has a description is never overwritten; those are listed as
conflicts in the final report.

    export IMMICH_URL=https://photos.example.com
    export IMMICH_API_KEY=...
    python3 immich-google-descriptor-syncer.py --album "Nikola MillWood 2026 - SK" \
        --dir "/path/to/the/takeout/album" --dry-run
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict

SIDECAR_SUFFIX = ".supplemental-metadata.json"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


class ImmichError(RuntimeError):
    pass


class Immich:
    def __init__(self, base_url, api_key):
        base = base_url.rstrip("/")
        self.base = base if base.endswith("/api") else base + "/api"
        self.api_key = api_key

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("x-api-key", self.api_key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace").strip()[:500]
            raise ImmichError(f"{method} {path} -> HTTP {err.code} {err.reason}: {detail}") from None
        except urllib.error.URLError as err:
            raise ImmichError(f"{method} {path} -> {err.reason}") from None
        return json.loads(raw) if raw else None

    def list_albums(self):
        return self._request("GET", "/albums")

    def get_album(self, album_id):
        return self._request("GET", "/albums/{}".format(album_id))

    def album_assets(self, album_id):
        """Return the album's assets, with exif (which carries the description).

        Older servers embed `assets` in the album response; newer ones dropped
        that field, so fall back to paging the metadata search.
        """
        album = self.get_album(album_id)
        embedded = album.get("assets")
        if embedded:
            return album, embedded

        assets, page = [], 1
        while page is not None:
            result = self._request(
                "POST",
                "/search/metadata",
                {"albumIds": [album_id], "withExif": True, "size": 1000, "page": page},
            )
            block = result["assets"]
            assets.extend(block.get("items") or [])
            next_page = block.get("nextPage")
            page = int(next_page) if next_page else None
        return album, assets

    def set_description(self, asset_id, description):
        self._request("PUT", "/assets/{}".format(asset_id), {"description": description})


def load_sidecars(directory):
    """Map lowercased filename -> (filename, description) for non-empty descriptions."""
    described, blank, collisions = {}, [], []
    try:
        names = sorted(os.listdir(directory))
    except OSError as err:
        raise ImmichError("cannot read --dir {}: {}".format(directory, err)) from None

    for name in names:
        if not name.endswith(SIDECAR_SUFFIX):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as err:
            print("warning: skipping unreadable sidecar {}: {}".format(name, err), file=sys.stderr)
            continue

        filename = data.get("title") or name[: -len(SIDECAR_SUFFIX)]
        description = (data.get("description") or "").strip()
        if not description:
            blank.append(filename)
            continue

        key = filename.lower()
        if key in described and described[key][1] != description:
            collisions.append(filename)
            continue
        described[key] = (filename, description)

    return described, blank, collisions


def resolve_album_id(client, wanted):
    if UUID_RE.match(wanted):
        return wanted
    albums = client.list_albums()
    matches = [a for a in albums if a.get("albumName") == wanted]
    if not matches:
        matches = [a for a in albums if (a.get("albumName") or "").lower() == wanted.lower()]
    if not matches:
        available = ", ".join(sorted(repr(a.get("albumName")) for a in albums)) or "(none)"
        raise ImmichError("no album named {!r}. Available: {}".format(wanted, available))
    if len(matches) > 1:
        ids = ", ".join(a["id"] for a in matches)
        raise ImmichError(
            "{} albums named {!r} ({}). Pass the album id instead.".format(len(matches), wanted, ids)
        )
    return matches[0]["id"]


def existing_description(asset):
    candidates = (asset.get("description"), (asset.get("exifInfo") or {}).get("description"))
    for value in candidates:
        if value and value.strip():
            return value.strip()
    return ""


def report_section(title, items, limit=None):
    if not items:
        return
    print("\n{} ({}):".format(title, len(items)))
    shown = items if limit is None else items[:limit]
    for item in shown:
        print("  - {}".format(item))
    if limit is not None and len(items) > limit:
        print("  ... and {} more".format(len(items) - limit))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Copy Google Takeout sidecar descriptions onto Immich album assets.",
    )
    parser.add_argument("--album", required=True, help="album name (exact, then case-insensitive) or album id")
    parser.add_argument("--dir", default=".", help="directory holding the media and sidecars (default: .)")
    parser.add_argument("--url", default=os.environ.get("IMMICH_URL"), help="Immich base URL (env: IMMICH_URL)")
    parser.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"), help="API key (env: IMMICH_API_KEY)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    args = parser.parse_args(argv)

    if not args.url:
        parser.error("missing Immich URL: set IMMICH_URL or pass --url")
    if not args.api_key:
        parser.error("missing API key: set IMMICH_API_KEY or pass --api-key")

    client = Immich(args.url, args.api_key)

    described, blank, collisions = load_sidecars(args.dir)
    if not described:
        print("no sidecars with a description found in {}".format(args.dir), file=sys.stderr)
        return 1

    album_id = resolve_album_id(client, args.album)
    album, assets = client.album_assets(album_id)
    print(
        "album {!r} ({}): {} assets; {} sidecars with a description in {}".format(
            album.get("albumName"), album_id, len(assets), len(described), os.path.abspath(args.dir)
        )
    )
    if args.dry_run:
        print("dry run - no changes will be written")

    by_key = defaultdict(list)
    for asset in assets:
        by_key[(asset.get("originalFileName") or "").lower()].append(asset)

    updated, conflicts, ambiguous, unmatched, failures = [], [], [], [], []

    for key in sorted(described):
        filename, description = described[key]
        candidates = by_key.get(key)
        if not candidates:
            unmatched.append(filename)
            continue
        if len(candidates) > 1:
            ambiguous.append("{} ({} assets: {})".format(filename, len(candidates), ", ".join(a["id"] for a in candidates)))
            continue

        asset = candidates[0]
        current = existing_description(asset)
        if current:
            conflicts.append("{}\n      immich: {}\n      sidecar: {}".format(filename, current[:120], description[:120]))
            continue

        if args.dry_run:
            updated.append(filename)
            continue
        try:
            client.set_description(asset["id"], description)
        except ImmichError as err:
            failures.append("{}: {}".format(filename, err))
        else:
            updated.append(filename)

    described_keys = set(described)
    no_description = sorted(
        {(a.get("originalFileName") or "?") for k, group in by_key.items() if k not in described_keys for a in group}
    )

    verb = "would update" if args.dry_run else "updated"
    print("\n=== summary ===")
    print("{}: {}".format(verb, len(updated)))
    print("skipped, description already set in Immich: {}".format(len(conflicts)))
    print("sidecars with an empty description: {}".format(len(blank)))
    print("sidecars with no matching album asset: {}".format(len(unmatched)))
    print("album assets with no sidecar description: {}".format(len(no_description)))
    if ambiguous:
        print("skipped, duplicate filenames in album: {}".format(len(ambiguous)))
    if failures:
        print("failed: {}".format(len(failures)))

    report_section("Skipped - description already set in Immich", conflicts)
    report_section("Sidecars with no matching album asset", unmatched, limit=40)
    report_section("Album assets with no sidecar description", no_description, limit=40)
    report_section("Skipped - duplicate filenames in album", ambiguous)
    report_section("Sidecars with duplicate filenames and conflicting descriptions", collisions)
    report_section("Failed", failures)

    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ImmichError as error:
        print("error: {}".format(error), file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
