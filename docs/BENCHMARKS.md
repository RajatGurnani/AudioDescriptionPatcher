# Benchmarks vs prior art (2026-08-14)

Three tools, two test pairs, identical measurement: per-window
cross-correlation lag between the output's AD track and the source
video's own audio (ground-truth timeline). |lag| ≤ 50 ms counts as in
sync for AD narration.

Tools: **this project** (adpatch), **[describealign](https://github.com/julbean/describealign)**
v2.1.1 (`--stretch_audio`), **[ADSync](https://github.com/JohnnyTheCoder1/ADSync)**
@ 6-commit initial release (mode auto).

## Round 1 — Big Buck Bunny, synthetic AudioVault-style AD
(ground truth known: speed ×1.042 PAL drift + 4s trim + TTS narration mix)

| Tool | mean lag | max lag | windows >50ms | notes |
|---|---|---|---|---|
| **adpatch (ours)** | **13.8 ms** | **23.6 ms** | **0/9** | recovered drift 41,994 ppm vs 42,000 true |
| describealign | 25.9 ms | 47.2 ms | 0/9 | correctly flagged "likely undescribed media" |
| ADSync | 130.3 ms | 332.6 ms | 6/9 | drift ladder missed (est. 37,080 ppm), warp then invented 3 phantom discontinuities |

## Round 2 — Avatar TWOW trailer, real studio AD (remixed mix, same cut)

| Tool | mean lag | max lag | windows >50ms | notes |
|---|---|---|---|---|
| ADSync | **5.8 ms** | **9.2 ms** | 0/6 | drift 0.9 ppm, warp conf 92%, clean |
| **adpatch (ours)** | 14.9 ms | 28.6 ms | 0/6 | ~20 ms constant bias vs ADSync's offset estimate |
| describealign | — | — | DNF | `RuntimeError: Alignment failed, are the input files mismatched?` |

## Read

- **Ours is the only tool that stayed in sync on both rounds.** Its
  weakness is refusing (with a warning) different-cut files rather than
  fixing them.
- **ADSync's warp is the best mechanism for same-cut real AD and the
  only path to different-cut support**, but its drift detection anchors
  on raw audio and collapses under large speed drift — the exact failure
  our envelope speed-grid scan (stage 1) was built to avoid. It then
  "explains" the residual drift as phantom edit discontinuities with
  audible (100-300 ms) consequences.
- **describealign** sits between: decent on the easy case, hard crash on
  the remixed one.

## v2 direction

Best-of-both: keep our stage 1 (onset-envelope speed-grid — the robust
drift detector) and feed its residual into an ADSync-style
Viterbi/PCHIP warp stage (MIT licensed, attribution required) instead of
our current refuse-and-warn, gated so exact-linear fits never enter the
warp path.
