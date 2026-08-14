# Plan — immich-google-descriptor-syncer

Implementation plan for [SPEC.md](SPEC.md), with the state each step actually reached.
Written after the fact from the session that produced the script; the sequencing is what
was followed, not a proposal.

## Done

- [x] **Survey the data before designing.** Counted media files vs sidecars, read a
      sample sidecar to learn its shape (`title` == exact filename, `description`),
      counted empty descriptions, confirmed the album-level `metadata.json` is a
      different thing, checked for an `immich` CLI and `IMMICH_*` env vars (neither
      present). Result: 139 media, 138 sidecars, 123 described, 15 empty, no CLI.
- [x] **Settle the one design question that changes the code** — what to do when an
      asset already has a description. Answer: skip and report, no `--force`.
- [x] **Get the API right from the spec, not from memory.** The docs site fetches were
      not usable, so the OpenAPI JSON was pulled from the immich-app repo and read
      directly. Two findings changed the implementation without changing the approved
      behaviour: `GET /api/albums/{id}` no longer returns `assets`, so
      `POST /api/search/metadata` is paged as the asset source; and the current
      description lives at `exifInfo.description`, not top level.
- [x] **Write the script** — one file, stdlib only: `Immich` client, `load_sidecars`,
      `resolve_album_id`, `existing_description`, `report_section`, `main`.
- [x] **Verify sidecar loading against the real album.** Imported the module and ran
      `load_sidecars('.')`: 123 described, 15 blank, 0 collisions, awkward filenames
      intact.
- [x] **Verify behaviour against a fake client.** Subclassed `Immich` with canned
      responses covering a case-mismatched filename, an asset that already has a
      description, two album assets sharing a filename, an album asset with no sidecar,
      and a write that raises. Confirmed: case-insensitive match works, conflicts and
      duplicates are skipped, one failed write does not abort the others, the run exits
      `1` when anything failed, and `--dry-run` performs no writes.
- [x] **Fix what that test exposed.** A mismatched album flooded the report with 100+
      lines, so the two informational lists were capped at 40 with an "... and N more"
      tail. Cap verified.
- [x] **Move into the toolbox repo** as
      `toolbox/immich-google-descriptor-syncer/immich-google-descriptor-syncer.py`, and
      update the usage docstring for the new name and location (`--dir` is now required
      in practice, since the default `.` is the repo directory rather than the album).

## Remaining

- [ ] **Live dry run.** Needs `IMMICH_URL` and `IMMICH_API_KEY`. This is the only real
      check on the wire format; everything above it is offline. Read the report,
      especially "sidecars with no matching album asset" and the conflict list.
- [ ] **Apply for real** (drop `--dry-run`) and spot-check one photo in the Immich UI.
- [ ] **Write a real README.** The `README.md` here is still the one-line scratch note
      from the original album directory.

## Deliberately not done

- No `--force` / overwrite path — see the decision in the spec. Adding one later means
  revisiting that decision, not just adding a flag.
- No test file. A one-shot utility with no framework in the repo; verification was done
  by importing the module and substituting a fake client. If this grows a second
  behaviour, that ad-hoc fake is the thing to promote into a real test.
- No retry or rate-limit handling on writes. 123 sequential `PUT`s against a personal
  server did not warrant it; a failed write is reported and the run continues, so a
  re-run picks up whatever failed.
