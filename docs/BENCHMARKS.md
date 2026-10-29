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

## Iteration 2 — speed (v0.5.0, 2026-10-19)

Same machine. "realistic" = `tools/harness.py --realistic --minutes 20`:
2.5 GB mkv (raw 320x180 video ≈ 2 MB/s, 5.1 E-AC-3 640k audio), AD =
mono MP3 with PAL drift.

| | before | after | change |
|---|---|---|---|
| mux, 10-min harness | 17.0 s | 3.0–3.8 s | **5×** — AD encoder `aac` → `aac_mf` |
| mux, 20-min realistic | 36–58 s | 10–27 s | encoder + I/O (range = warm vs cold OS file cache) |
| analyze, 20-min realistic | 2.3+0.7 s sequential | 1.9–5.6 s wall (parallel) | ≈ max(video, AD) instead of sum |
| analyze, repeat run (cache hit) | same as first | 0.01 s | envelope cache |

Where the time went (profiled on the 10-min harness mux): AAC encoding
was ~95 % of the mux. ffmpeg's native `aac` runs ~36× realtime on this
CPU; Windows' Media Foundation `aac_mf` ~180×, `ac3` ~500×. `aac_mf`
also adds **no** priming delay (native `aac` adds 1024 samples that mkv
doesn't trim), so the mkv compensation is now per-encoder. Native `aac`
remains the automatic fallback (verified in sync, ~18 s mux).

Not done, by measurement: chunk-parallel decode of one file (plan 02
§2.2). Decoding 20 min of 5.1 E-AC-3 takes 2–6 s, so for a 2 h film
analysis is ~15–35 s against a mux that has to read and write the whole
file anyway. Revisit for lossless tracks (TrueHD / DTS-HD MA) if real
files show a decode-bound analysis.

Accuracy after the changes: all harness cases unchanged (Δa ≤ 3.5 ppm,
Δb ≤ 0.7 ms, output lag ≤ 13.4 ms; realistic ≤ 16.1 ms).

## Real films (2026-10-29)

`tools/realmedia_test.py`: 13 public-domain films (Internet Archive,
240p `_512kb.mp4` copies) with their AudioVault AD MP3s — real
different-source pairs, no ground truth beyond the fit's own residuals.
Media is local only (`.local/testmedia`, git-excluded). Envelope cache
off for every run below; same machine, runs one after another.

| | before (a785113) | after (66d78a6) | + small-cuts split (41903eb) |
|---|---|---|---|
| scan by content, 13 × 13 | 224 s | 181 s | 193 s |
| as a TV show (S01E01..) | 82 s | 89 s | 94 s |
| patch all 13 (batch queue) | 579 s | 500 s | 526 s |
| films paired correctly | 8 / 13 | 8 / 13 | 8 / 13 |
| verdict good / check / likely wrong | 2 / 3 / 8 | 2 / 3 / 8 | 3 / 2 / 8 |

"after" = HDD layer, batch pipeline (3 in flight, one writer),
process-pool pairing. Quality identical, ~15–20 % faster.

Findings, per film (from `fit_alignment` signals):

- **Little Shop of Horrors:** one offset left p95 residual 189 ms; a
  ~0.9 s shift from 28.9–38.9 min. Fixed by the small-cuts split
  (3 pieces, p95 113 ms, 0.56 → 0.82) — [ADR 0004](adr/0004-split-on-wobbly-sync.md).
- **Beat the Devil, House on Haunted Hill, Night of the Living Dead:**
  strong matches (segment scores up to 0.89, coverage 0.87–1.0) but the
  offset wanders smoothly by ±0.4–1.6 s across the film — not cuts, a
  wobbling timebase (likely the old web transcodes). Neither one offset
  nor a few pieces fit; needs a warp mode (parked).
- **Suddenly, Gulliver's Travels:** different cuts plus the same wobble
  (p95 240 / 727 ms).
- **McLintock!:** the AD MP3 is incomplete (21.7 min of a 118 min film)
  — bad download, not an engine issue.
- **Nosferatu:** silent film; the AD's score is a different music track
  (peak 0.03) — nothing shared to align on.
- **His Girl Friday:** "[TTS]" AD, 5 min longer, no shared soundtrack
  (segment scores ≤ 0.16) — likely a different source mix.
- All files carry real audio (mean −18 to −38 dB at 10 and 40 min), so
  none of the failures is silence.
- Black Panther: Wakanda Forever (Blu-ray, same-cut AD): 0.98 good,
  1 piece, identical with and without the split — no regression.
