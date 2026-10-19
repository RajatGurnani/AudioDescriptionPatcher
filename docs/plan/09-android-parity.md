# 09 — Android parity (rides along with Iterations 3 & 4)

Rule from CLAUDE.md: alignment math changes land in **both**
`adpatch.py` and `android/.../Aligner.kt`. This file lists exactly what
crosses over per iteration and what deliberately doesn't.

| Iteration | PC change | Android action |
|---|---|---|
| 0 harness | `tools/harness.py` | none; Android verified by exporting the harness media to the phone and comparing `a`, `b` with PC output (manual, `adb push`) |
| 1 installer | bat/requirements | none |
| 2 speed | parallel envelopes, chunked decode, cache | **already has** chunk-parallel decode (`onsetEnvelopeParallel`, v0.2.0) — PC is catching up. Port the envelope cache (key on URI + size + lastModified, `cacheDir`) |
| 3 confidence | `signals` + score + warnings | port score function verbatim (pure math); show band badge in `MainActivity` result + `LogsActivity` |
| 3 plot | uPlot web charts | optional Canvas drift scatter in `LogsActivity` |
| 4 piecewise | lattice + Viterbi + pieces + filter graph | port lattice + Viterbi + piece vetting into `Aligner.kt`; `Patcher.kt` resampler looks up `b_k` per output time; gap fill with original audio when it's decodable (already decoded for copy check) |
| 5 batch | folder pairing + queue | out of scope on Android (SAF folder access + multi-hour jobs on a phone = poor UX); revisit later |
| 6 desktop | pywebview + PyInstaller | n/a |
| 7 output options | stream/codec options | subset only: keep/drop original audio (exists), AD bitrate. MediaMuxer can't write mkv/srt — no subtitle injection |

## Shared test vectors

> **Implemented (iteration 4) differently from below:** a JVM unit test
> (`android/app/src/test/.../AlignerParityTest.kt`) runs `Aligner` on
> envelopes exported by `tools/parity_export.py` and compares with
> Python's results — no device or debug intent needed for the math.
> Device-only parts (decode, `Patcher`) still need a run on the phone.

To keep the two implementations honest without a cross-language test
runner:
- The harness writes, per case, the stage-1 and stage-2 intermediates
  (`a` after coarse, after fine, `b`, per-segment matches) to
  `harness_results.json`.
- A tiny debug intent in the Android app (`adb shell am start ... --es
  video <path> --es ad <path> --ez dump true`) writes the same
  intermediates to the app's files dir; `adb pull` and diff with a
  10 ppm / 5 ms tolerance.
- Keep numeric constants (SR, HOP, grids, thresholds, λ values) in one
  table in ARCHITECTURE.md — the single source of truth both files cite.

## Known intentional differences (document, don't "fix")

- Android stretch = linear resample (pitch shifts by drift %), PC =
  `atempo` (pitch preserved). Unchanged.
- Android output is MP4 only (MediaMuxer).
- Android AAC CSD is hand-crafted (0x12 0x10) — unaffected by any of this.
