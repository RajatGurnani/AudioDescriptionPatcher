# 00 — Baseline timings + test harness (Iteration 0, v0.4.0)

## Why first

There is no test suite. Every later step (parallel decode, envelope
cache, piecewise alignment, pairing) can silently shift alignment by tens
of milliseconds. We need (a) a one-command accuracy check with known
ground truth and (b) per-phase timings so "faster" is a number.

## 0.1 Phase timings in `patch()`

- Wrap each phase in `time.perf_counter()`: `classify`, `envelope:video`,
  `envelope:ad`, `align`, `mux`.
- Log one summary line at the end:
  `timings: video-env 41.2s | ad-env 6.3s | align 0.8s | mux 38.9s | total 87.2s`
- Return them in the result dict as `timings` (seconds, float) — the web
  UI and harness read it.
- Also log input sizes + output size so MB/s can be derived
  (disk-bound vs CPU-bound diagnosis).

## 0.2 Synthetic media harness: `tools/harness.py`

Local-only script, not shipped in the exe. Generates media into
`%TEMP%/adpatch-harness/` (gitignored, never committed), runs
`adpatch.patch()`, checks results against ground truth.

Generation (pure bundled ffmpeg, no external downloads):

1. **Source soundtrack**: 10 min (configurable) of gated noise bursts
   with random spacing + a few tone sweeps — onset-rich, deterministic
   from a seed. `anoisesrc` + `volume` envelope driven by an `aevalsrc`
   expression, or numpy-generated wav.
2. **Video**: `lavfi color=c=black:s=320x180:r=24` + the soundtrack →
   `video.mkv` (tiny, fast).
3. **AD variants** from the same soundtrack:
   | case | transform | ground truth |
   |---|---|---|
   | `offset+` | `adelay=4000` | a=1, b=−4.0 |
   | `offset-` | `atrim=start=3.5` | a=1, b=+3.5 |
   | `pal` | `atempo=25/23.976` + `atrim=2` | a=25/23.976, b=+2 |
   | `ntsc` | `atempo=23.976/25` | a=23.976/25, b=0 |
   | `narration` | `pal` + mixed TTS-like bursts (filtered noise in gaps) at −3 dB | as `pal` |
   | `cut` (iter 4) | remove 40 s at 5:00 | two pieces, b jumps by 40 s |
   | `wrong` | unrelated seed | must be flagged suspect |
4. Encode AD as MP3 128k (matches AudioVault).

Checks per case:
- `|a − a_true| < 20 ppm`, `|b − b_true| < 10 ms`.
- Output verification (independent of the aligner's own report):
  decode output track 0 + video's original audio, cross-correlate
  9 windows across runtime, every `|lag| ≤ 20 ms`. Reuse the method from
  [BENCHMARKS.md](../BENCHMARKS.md) (it already defines this metric).
- `wrong` case: `result["suspect"] is True`.

Output: a table on stdout + `harness_results.json` (gitignored) with
timings, so iterations can diff.

## 0.3 Real-media spot set (local only)

`MoviesADTest/` is already gitignored. Record in BENCHMARKS.md (numbers
only, no media): one real movie + AD pair per category we have
(same-cut studio AD, PAL drift, different cut if available). Re-run after
iterations 2, 4, 5.

## 0.4 Baseline record

Append a "Baseline v0.4.0" section to `docs/BENCHMARKS.md`:
- harness: per-case accuracy + timings
- real: file sizes, per-phase seconds, MB/s for envelope:video and mux
- machine: CPU (16 threads), disk type for source/output, GPU unused

## Done when

- `python tools/harness.py` exits 0 on all linear cases on the current
  engine (proves the harness itself is right before we change anything).
- Baseline numbers committed.

## Risks

- Synthetic audio too "easy" → speedups that break real files pass the
  harness. Mitigation: the `narration` case + real spot set gate every
  speed change.
