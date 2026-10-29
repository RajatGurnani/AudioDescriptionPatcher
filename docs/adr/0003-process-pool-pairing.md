# 0003 — Pairing scores in worker processes, not threads

Date: 2026-10-29 · Status: Accepted

## Context

Find-pairs was slow on a 16-thread PC. `content_score()` is numpy FFT
work that mostly holds the GIL: a thread pool gave only 1.5×.

## Decision

`_score_pairs()` uses a `ProcessPoolExecutor`, in-process below 24
pairs (spawn cost). Candidates are also tiered: same episode code first,
then same season + unnumbered AD files — not all N × M.

## Consequences

- 3× faster pairing on the measured batch.
- Every entry point that can reach it needs a `__main__` guard, and the
  frozen desktop exe needs `multiprocessing.freeze_support()`
  (`desktop.py`).
- TV-show case: 16 comparisons for 13 episodes instead of 169.
