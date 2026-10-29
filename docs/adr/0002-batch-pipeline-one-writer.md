# 0002 — Batch pipeline: 3 items aligning, one writing

Date: 2026-10-29 · Status: Accepted

## Context

The batch queue patched one item at a time: CPU-bound analysis/alignment
of the next episode waited behind the disk/GPU-bound mux or re-encode of
the current one.

## Decision

`Queue.run(parallel=None)` keeps up to 3 items in flight (1 when the PC
has fewer than 6 CPU threads); `write_lock` lets one item mux at a time
(progress phase `wait` while queued for it). Per-item `log` and
`timings` are kept; `save()` is serialized and retries a dump that hits
a dict changed mid-write. Stop = start nothing new.

## Consequences

- Real films: 13 patches 579 s → 500 s (BENCHMARKS.md, real films).
- Peak scratch use grows with items in flight (ADR 0001 reserves space).
- `tools/batch_test.py` counts concurrent mux ffmpegs to prove one writer.
