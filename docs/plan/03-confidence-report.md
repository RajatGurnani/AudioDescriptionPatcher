# 03 — Confidence score + structured sync report (Iteration 3, v0.6.0)

> **Outcome (2026-10-19):** done as planned, with breakpoints re-tuned
> on the harness (peak → 1 at 0.18, uniqueness at 1.65, duration band
> ±3 %; see ARCHITECTURE.md §4). `speed_margin` and `inlier_frac` are
> reported but not scored. Harness gained `hard` (remixed + loud
> narration → good) and `cut` (40 s scene cut → check + "different
> edit" warning) cases. Android ported (compiles; not yet run on the
> phone). Report files are opt-in (`--report`).

## Today

`fit_alignment()` returns `report = [(ad_sec, video_sec, score,
residual_ms), ...]`; `patch()` counts `weak` (score ≤ 0.12) and sets
`suspect = weak > len/2`. Text-only; the web UI shows the log lines.

Gaps:
- No single number a user (or batch mode) can sort/filter on.
- Doesn't distinguish "weak because quiet scene" from "weak because wrong
  position".
- Stage-1 peak sharpness (how decisively the speed scan won) is computed
  and thrown away.
- Residual spread isn't summarized (a fit can have strong scores but a
  ±200 ms wobble = audible).

## 3.1 Signals to collect (all already computed or nearly free)

| Signal | Source | Meaning |
|---|---|---|
| `peak` | stage-1 best normalized correlation | global match strength |
| `peak_ratio` | best peak / 2nd-best peak at a lag ≥ 2 s away (same factor) | uniqueness; ~1.0 = ambiguous (repetitive music) |
| `speed_margin` | best score − best score of factors ≥ 0.2 % away | how decisive the drift estimate is |
| `coverage` | fraction of segments with score > MIN_SEGMENT_SCORE | how much of the runtime is verified |
| `resid_p95_ms` | 95th percentile of `|residual_ms|` over confident segments | audible wobble |
| `inlier_frac` | segments kept after outlier rejection / confident | consistency |
| `dur_ratio` | video duration / (a × AD duration) | far from 1 → different cut |

## 3.2 Score

Weighted product, each factor mapped to 0..1 with documented
breakpoints (start from these, tune on the harness + real set):

```
c_peak     = clamp((peak - 0.05) / 0.25)
c_unique   = clamp((peak_ratio - 1.1) / 0.9)
c_cover    = coverage
c_resid    = clamp(1 - (resid_p95_ms - 40) / 160)    # 40 ms → 1, 200 ms → 0
c_dur      = clamp(1 - (abs(dur_ratio - 1) - 0.02) / 0.08)
confidence = c_peak^0.25 * c_unique^0.15 * c_cover^0.3 * c_resid^0.2 * c_dur^0.1
```

Bands: ≥ 0.75 "good", 0.45–0.75 "check", < 0.45 "likely wrong".
Product (not sum) so one catastrophic factor sinks the score.

Inspiration: ADSync `align/confidence.py` (MIT) uses the same
multi-factor idea (anchor quality, coverage, duration ratio, residual
p95) — adapt its factor list and warnings; our weights are tuned to our
envelope features, not copied.

## 3.3 Warnings (human-readable, attached to the result)

- `different_cut`: `dur_ratio` off by > 3 % and coverage < 0.7
  → "AD file is probably a different edit. Iteration 4 handles this."
- `ambiguous`: `peak_ratio < 1.2` → "repetitive audio; offset may be
  wrong by a whole loop".
- `wobble`: `resid_p95_ms > 120` → "sync drifts ±X ms in places".
- `extended_ad`: coverage < 0.3 and AD longer than video by > 5 %
  → existing extended-description message.
- `unrelated`: peak < 0.08 → "these files don't share a soundtrack".

## 3.4 Structured result

`patch()` returns (additive, backward compatible):

```json
{
  "output": "...", "a": 1.0427, "b": 2.013,
  "confidence": 0.86, "band": "good", "warnings": [],
  "signals": {"peak": 0.41, "peak_ratio": 3.2, ...},
  "segments": [{"ad": 30.0, "video": 33.2, "score": 0.62,
                "residual_ms": 4.1, "used": true}, ...],
  "timings": {...}
}
```

Also written as `<output>.sync.json` next to the output **only when the
user enables "save report"** (default off — avoid clutter in movie
folders). CLI: `--report` flag.

## 3.5 UI

- Web: a coloured badge (good / check / likely wrong) + warnings list
  above the log; log stays collapsible.
- CLI: one summary line `confidence 0.86 (good)` before the segment
  table.

## 3.6 Android

Same signals exist in `Aligner.kt`; port the score function verbatim
(pure math) — see [09](09-android-parity.md).

## Verification

- Harness: all linear cases ≥ 0.75; `wrong` < 0.45 with `unrelated`;
  `cut` (pre-iteration-4) gets `different_cut`.
- Real set: record scores; sanity-check bands against listening tests.
