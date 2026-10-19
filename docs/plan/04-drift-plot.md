# 04 — Drift plot + waveform match view (Iteration 3, v0.6.0)

> **Outcome (2026-10-19):** web charts with uPlot 1.6.32 (vendored,
> npm integrity verified), hand-written SVG for `--report`. Android
> drift chart not done (optional) — the verdict shows in the status
> line instead.

## Goal

Let a user *see* the sync before watching a 2-hour movie:
1. **Drift plot** — residual (ms) vs movie time, per segment, with the
   ±50 ms "in sync" band. Flat line near 0 = good. A step = cut edit.
   A slope = uncorrected drift.
2. **Envelope overlay** — the video's onset envelope and the *aligned*
   AD envelope, zoomable. Lets you eyeball "these peaks line up".
3. (Iteration 4) **Offset map** — video_time − ad_time across the
   runtime; piecewise steps visible.

## Data (no extra analysis needed)

- Drift points: `result["segments"]` from [03](03-confidence-report.md).
- Overlay: both envelopes are already in memory after alignment.
  Downsample for transport: max-pool to 10 Hz → 2 h = 72k points per
  series; send as `Float32Array` binary (`application/octet-stream`,
  ~290 KB each) rather than JSON.
- Aligned AD envelope: `_stretch(ad_env, a)` shifted by `b` — reuse
  existing helpers.
- Store per job in memory (`jobs[id]["plot"]`), endpoint
  `GET /api/jobs/{id}/plot/{drift|overlay}`.

## Rendering

Option A — **uPlot** (MIT, ~50 KB minified, fastest canvas charting lib,
handles 100k+ points). Vendor the minified JS + CSS into
`static/vendor/uplot/` with its LICENSE file (no CDN — the app must work
offline and on the LAN).

Option B — hand-written `<canvas>`/SVG (~150 lines). Zero deps, but zoom
and pan are fiddly.

**Decision: uPlot.** Zoom/pan/cursor for free; MIT; tiny. Recorded in
THIRD_PARTY.md.

Charts:
- Drift: scatter (segments) coloured by score (grey = unused/outlier),
  band ±50 ms shaded, y-axis clamped to ±500 ms with off-scale markers.
- Overlay: two lines (video above, AD mirrored below the axis —
  "butterfly" layout reads better than two overlapping lines), shared
  x zoom with the drift plot (uPlot `sync` cursor).

## Also as a static image (CLI + saved report)

When `--report` is on (see 03), write `<output>.sync.png`. Options:
- ffmpeg `showwavespic` can render waveforms but not our envelopes/
  residuals — no.
- matplotlib (PSF/BSD-style) = 30 MB dependency — no.
- **Write an SVG by hand** (`<polyline>` per series, ~60 lines of
  Python, no deps) → `<output>.sync.svg`. Opens in any browser.

## Mobile layout

Web UI is mobile-first: charts full-width, 180 px tall, overlay
collapsed behind a "Show waveform match" toggle (72k-point canvas on a
phone is fine with uPlot, but saves bandwidth when not needed).

## Android

Separate, optional: a simple Canvas view in `LogsActivity` drawing the
drift scatter only (data already in the job store). Not required for
iteration 3 exit.

## Verification

- Harness `pal` case: flat drift plot at ~0 ms.
- Deliberately corrupt `a` by +200 ppm in a debug flag → visible slope.
- `cut` case (iteration 4): visible step before the fix, flat after.
- Phone (CMF Phone 1) over LAN: plot loads < 2 s.
