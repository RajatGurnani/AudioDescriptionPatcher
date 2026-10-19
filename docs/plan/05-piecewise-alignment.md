# 05 — Piecewise alignment for different cuts (Iteration 4, v0.7.0)

> **Outcome (2026-10-19):** done; design as below with two changes found
> while building it: (1) instead of synthetic pass-through candidates,
> candidates are clustered into offset families and every window is
> scored at every family; (2) hand-over points prefer the latest tie and
> the renderer cuts pieces at the next piece's start, which is what
> makes insertions come out right. Gap fill with original audio on PC;
> Android leaves gaps silent. Harness cases cut / insert / adbreaks /
> cut_pal pass; linear cases stay 1 piece. Android parity verified by a
> JVM unit test on identical envelopes (≤ 0.3 ppm, ≤ 0.1 ms, same
> pieces); on-device rendering not yet verified.

## Problem

The model `video_time = a·ad_time + b` assumes one continuous edit. Real
mismatches it can't fix (today they're only *flagged* as suspect):
- TV rip AD with ad-break gaps or a scene the home release adds/removes.
- Different studio logo sequence *mid*-file (rare), recap at the start
  of an episode, extended/theatrical cut differences.

Benchmarks: describealign (GPL, idea only) and ADSync (MIT) both handle
this; ADSync's approach is the one we adapt.

## Model

Piecewise linear with a **shared speed** `a` (speed is a property of the
source frame rate, constant across the file) and per-piece offsets:

```
video_time = a·ad_time + b_k     for ad_time in [s_k, e_k)
```

Pieces may leave gaps in video time (video has content the AD lacks) or
overlap (AD has content the video lacks — that AD audio is dropped).
Linear-only files are the K = 1 special case and must produce
bit-identical results to v0.6.

## Algorithm

### Stage 1 — unchanged
Global speed scan → `a`, coarse `b0`. The speed scan still wins on
cut-edited files because most of the runtime shares one offset family;
if the largest piece is < 40 % of the runtime, stage 1 may lock onto a
wrong `a` — mitigation: fall back to `a` ∈ {1, PAL, NTSC specials} and
keep the best total path score (below).

### Stage 2a — candidate lattice (adapted from ADSync `candidate_lattice.py`, MIT)
- Stretch AD envelope by `a` (as today).
- Windows: 20 s long, 10 s hop across the whole AD (2 h → ~720 windows).
- For each window, normalized cross-correlation against the video
  envelope:
  - **local** search ±5 s around the previous best offset (cheap), and
  - **global** search over the full video at 20 Hz pooled resolution for
    every 6th window (or any window whose local score < 0.15) — finds
    jumps of minutes.
- Keep top K = 3 peaks per window (non-max suppression 1 s) →
  `(offset, score)` candidates.
- Windows with no candidate > 0.08: inject a synthetic "pass-through"
  candidate carrying the neighbours' offset at low score (ADSync's
  trick — lets the path bridge quiet stretches instead of jumping to
  noise).

### Stage 2b — Viterbi decode (adapted from ADSync `warp_decode.py`, MIT)
- States: candidates per window. Emission = score.
- Transition cost between consecutive windows' offsets o₁→o₂:
  - |Δ| < 50 ms → 0
  - 50 ms ≤ |Δ| < 1 s → λ_small·|Δ| (drift wobble is not real; discourage)
  - |Δ| ≥ 1 s → λ_jump (constant: a cut costs the same regardless of size)
- λ_jump chosen so a jump needs ~3 consecutive windows (30 s) of
  better evidence to be worth it — stops single noisy windows from
  creating phantom cuts (exactly ADSync's failure in our round-1
  benchmark: it "explained" residual drift as 3 phantom cuts).
- O(N·K²) — trivial.

### Stage 2c — pieces
- Split the decoded path where |Δoffset| ≥ 1 s.
- Vet pieces (ADSync `warp_fit.py` `_vet_and_bridge` idea): a piece needs
  ≥ 30 s and ≥ 3 real (non-synthetic) windows; otherwise merge into the
  neighbour it agrees with better.
- Per piece: the existing weighted line fit with outlier rejection
  (current stage-2 code, run per piece with `a` fixed or ±200 ppm free).
- **Boundary refinement:** around each cut, slide a 5 s probe window in
  0.1 s steps; at each step score both pieces' offsets; the switch point
  is where the better offset changes. Then snap to the nearest
  low-energy frame of the AD envelope (±0.5 s) so the splice lands in a
  quiet moment.

### Speed fallback
Compute the total path score for the stage-1 `a`; if confidence is
"likely wrong", rerun 2a–2c for the special ratios and keep the best.

## Rendering (ffmpeg)

One `-filter_complex` with K branches:

```
[1:a]asplit=K[s1]...[sK];
[s1]atrim=start=S1:end=E1,asetpts=PTS-STARTPTS,atempo=1/a,
    afade=t=in:d=0.01,afade=t=out:st=..:d=0.01,adelay=D1:all=1[p1];
...
[p1]...[pK]amix=inputs=K:normalize=0:dropout_transition=0[ad]
```

- Pieces don't overlap in output time, so `amix normalize=0` = splice.
- 10 ms fades kill clicks at splices.
- **Gaps** (video content with no AD): default silence; option
  "fill gaps with original audio" — add the original track for the gap
  ranges into the same amix (`[0:a:0]atrim=...,adelay=...`). Recommended
  default ON: dead air in an AD track is jarring.
- K capped at 64 (filter graph size); beyond that → warn "too
  fragmented, probably wrong files".
- Linear case (K = 1) keeps today's exact command.

Why not ADSync's PCHIP warp + WSOLA renderer: it renders audio in
Python (needs scipy, full decode at 48 kHz, block processing). Our
piecewise-constant-speed model maps 1:1 onto ffmpeg filters, keeps
`atempo` pitch preservation, and adds no dependencies. PCHIP warp is
only needed for continuously varying speed (tape transfers) — not a
case we've seen. Revisit if the harness or users show it.

## Report / UI

- Result gains `pieces: [{ad_start, ad_end, video_start, offset,
  windows, score}]`.
- Drift plot ([04](04-drift-plot.md)) shows piece boundaries as vertical
  lines and the offset map.
- Log: `3 pieces: 0:00–41:12 (+2.01 s), 41:12–1:23:40 (−38.96 s), ...`

## Android

`Patcher.kt` already resamples per output sample with
`inputPos = ((j/rate − b)/a)·in_rate`; piecewise = look up `b_k` by
output time (binary search over piece boundaries), output silence (or
original audio) in gaps. Lattice + Viterbi port to `Aligner.kt` — see
[09](09-android-parity.md).

## Verification

Harness additions (from [00](00-baseline-and-harness.md)):
- `cut`: 40 s removed from the AD at 5:00 → 2 pieces, each < 20 ms.
- `insert`: 25 s of foreign audio inserted into the AD at 3:00 → that
  AD audio dropped, 2 pieces.
- `adbreaks`: 4 × 3 min gaps → 5 pieces.
- `cut+pal`: cut + PAL drift together.
- **No phantom cuts:** all linear cases still produce K = 1
  (regression guard against ADSync's round-1 failure).
- Output-verification lag ≤ 20 ms in every window outside a ±1 s
  margin around splice points.
