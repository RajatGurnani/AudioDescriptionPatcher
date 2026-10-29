# 0001 — Stage hard-drive videos on the SSD; one reader per HDD

Date: 2026-10-29 · Status: Accepted

## Context

A 39-episode batch (2 GB 1080p, ~50 min each) on an external HDD ran at
4–5 MB/s: several analyses and the mux read the same drive at once and
the head seeks between them (one sequential reader gets 100–150 MB/s).
The batch PC itself has no HDD, so the fix has to detect one.

## Decision

- `is_rotational()` asks Windows for the device seek penalty
  (`IOCTL_STORAGE_QUERY_PROPERTY`, `StorageDeviceSeekPenaltyProperty`);
  `ADPATCH_FORCE_HDD` fakes it for tests.
- `disk_slot()`: one reader per HDD at a time.
- Scans extract only the analysis audio track (first English one) to
  the SSD scratch (`%LOCALAPPDATA%\ADPatcher\scratch`), not the video.
- `patch()` copies an HDD video to the scratch in one sequential pass,
  analyzes + muxes there, copies the result back to `.partial` and
  renames. Option `staging: auto | off`.
- Scratch keeps 2 GB free (`scratch_reserve`); stale job folders of dead
  processes are removed at startup (`clean_scratch`).

Rejected: read-ahead prefetch of the next episode — measured 6 % slower.

## Consequences

- Needs ~2.3× the video size free on the SSD while an item runs;
  otherwise it falls back to working in place.
- Two extra sequential copies per HDD video, cheap next to seeking.
- Tested by simulation only (`tools/cleanup_test.py` with
  ADPATCH_FORCE_HDD); no real HDD on the dev machine.
