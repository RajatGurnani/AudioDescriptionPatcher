# 0007 — Find pairs, single patch and batch never overlap

Date: 2026-10-30 · Status: Accepted

## Context

A user cancelled a job, ran Find pairs and resumed the batch while work
was still in flight; some outputs ended with broken audio. Nothing
stopped a single patch, a scan and a batch from running together, so
two of them could write the same `.partial` output, and they fought for
the same disks and CPU.

## Decision

`app._busy()` refuses (409 with a reason shown in the UI) any new
single patch, scan, batch start or resume while another of them runs —
including a batch still finishing its in-flight items after Stop.
Check and claim happen under `start_lock`. Batch items can be cancelled
one by one or all at once (`Queue.cancel`), and every patch writes a
log file (`%LOCALAPPDATA%\ADPatcher\logs`) so the next incident can be
diagnosed from evidence.

## Consequences

- No more interleaved writers from the app. The CLI and a second app
  instance are not covered (rare; would need a cross-process lock).
- The broken-audio incident itself wasn't reproduced; the overlap is
  the likely cause, and the job logs will show it if it recurs.
