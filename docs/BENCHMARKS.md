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

## Baseline v0.4.0 — synthetic harness (2026-10-19)

`tools/harness.py --minutes 10` (Ryzen 7 4800H, NVMe SSD). Ground truth
is exact: speed changes are sample-exact resamples (23976↔25000 Hz),
offsets are `adelay`/`atrim`. "lag max" is measured on the **output
file** (AD track vs source soundtrack, 9 windows), independent of the
aligner's own report.

| case | Δa ppm | Δb ms | lag max ms (mkv) | lag max ms (mp4) |
|---|---|---|---|---|
| offset +4 s | 0.0 | 0.0 | 0.0 | 0.0 |
| offset −3.5 s | 0.0 | 0.0 | 0.2 | 0.0 |
| PAL + 2 s trim | +3.5 | +0.5 | 12.8 | 13.4 |
| NTSC (slowdown) | +0.4 | −0.5 | 9.2 | 9.2 |
| PAL + narration | +2.5 | +0.7 | 12.1 | — |
| unrelated audio | flagged suspect ✓ | | | |

Timings per 10-min case: envelopes 0.1–0.3 s, align 0.1 s, mux ~17 s
(the mux dominates even on synthetic media; real-movie numbers pending —
no local real test pair at the moment).

Bugs the harness found and fixed in this iteration:
- **MKV outputs played the AD ~21–23 ms late** (64 ms at 16 kHz): the
  AAC encoder's 1024 priming samples are trimmed by mp4's edit list but
  not in mkv. Fix: AD is now resampled to a fixed 48 kHz and, for mkv,
  started 1024/48000 s earlier.
- **`onset_envelope()` could hang forever** when ffmpeg wrote > ~4 KB to
  stderr (pipe deadlock on Windows) — e.g. harmless dts warnings on some
  mp4 files. stderr is now drained on a thread.

Residual 9–13 ms on drift cases is `atempo`'s own WSOLA latency in the
mux (measured 5–20 ms depending on rate) — within AD tolerance, left as
is.
