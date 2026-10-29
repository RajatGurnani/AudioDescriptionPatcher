# 0004 — Try piecewise when one offset's sync wobbles (> 120 ms)

Date: 2026-10-29 · Status: Accepted (PC); Android pending

## Context

Piecewise alignment ran only when the linear fit left ≥ 10 % of its
30 s segments unmatched. But linear segments search ±5 s around the
line, so a cut of a few seconds still "matches" and shows up only as a
large residual. On real films (BENCHMARKS.md, real films) Little Shop
of Horrors kept one offset with p95 residual 189 ms — a ~0.9 s shift for
10 minutes — and scored 0.56 "check". Even when piecewise did run, it
had to beat the linear coverage, which can be 1.0 in this case, so it
could never win.

## Decision

- Trigger piecewise also when the linear p95 residual > `PW_WOBBLE_MS`
  (120 ms, the same level the confidence warning uses).
- In that case accept pieces when they beat the best single offset at
  piecewise precision (`best_single`, ±0.5 s) by 10 % of windows,
  instead of the linear coverage.
- Option `split_small_cuts` (default on; UI "fix small cuts", CLI
  `--no-split-small-cuts`) restores the old behaviour exactly.

## Consequences

- Little Shop: 3 pieces, p95 113 ms, 0.56 → 0.82; the other 12 films
  and Black Panther unchanged; synthetic harness unchanged.
- Risk (review): a slow sub-second wander can now become a few pieces
  with small jumps at the joins instead of one offset with a wobble. The
  toggle is the escape hatch.
- Does not fix sources whose timebase wanders continuously (Beat the
  Devil, House on Haunted Hill, Night of the Living Dead): that needs a
  warp mode — parked.
- Not yet mirrored in `Aligner.kt` (Android paused); parity row in
  plan 12.
