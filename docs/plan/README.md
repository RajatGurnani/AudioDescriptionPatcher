# Roadmap: v0.4 → v1.0 (PC engine + web/desktop app)

Status: iterations **0–5 done** (2026-10-19); 6+ pending. Results
per iteration in [BENCHMARKS.md](../BENCHMARKS.md). Written
2026-10-19 after a review of the feature wishlist (GPU, desktop app,
track editing, size limits, torrents, AudioVault, waveforms, speed).

## Guiding decisions

1. **Build only what nobody else does well: alignment + muxing.**
   Track surgery (MKVToolNix), size/resolution re-encodes (HandBrake),
   and torrenting (qBittorrent) are mature free tools — we hand off to
   them instead of rebuilding them. See
   [08-output-options-and-handoffs.md](08-output-options-and-handoffs.md).
2. **GPU does not speed up the current pipeline.** Video is stream-copied
   and the alignment math runs on a ~1M-sample envelope (milliseconds).
   Time goes to container reads + audio decode + output write. GPU only
   matters for optional video re-encode (bundled ffmpeg already has
   `h264_nvenc` / `hevc_nvenc`; dev PC GTX 1650 supports both, not AV1).
3. **Measure before optimizing.** Iteration 0 adds per-phase timings and
   a reproducible synthetic test harness; every later iteration reports
   before/after numbers against it.
4. **License policy (project is MIT):** copy/adapt code only from
   MIT/BSD/Apache/ISC sources, with attribution in
   [THIRD_PARTY.md](THIRD_PARTY.md). GPL/AGPL projects are used only as
   *separate executables* (ffmpeg, HandBrakeCLI, mkvmerge) or as idea
   references — never copied. Full table in THIRD_PARTY.md.
5. **Local-only workflow:** build outputs, exes, temp media and caches
   never leave the machine. Deliverables are git commits + pushes only —
   no release uploads, hosted pages or published artifacts unless the
   owner explicitly asks.
6. **Python ↔ Kotlin parity:** any change to alignment math lands in
   `adpatch.py` *and* `Aligner.kt` in the same iteration
   ([09-android-parity.md](09-android-parity.md)).

## Iterations

Each iteration ends with: harness run green, docs updated, one commit
(or a small series) pushed. Version numbers are the PC engine's; Android
keeps its own numbering.

| Iter | Version | Theme | Steps | Exit criteria |
|---|---|---|---|---|
| 0 | v0.4.0 | Baseline | [00](00-baseline-and-harness.md) | harness script + phase timings committed; baseline numbers recorded in BENCHMARKS.md |
| 1 | v0.4.1 | "Runs anywhere" | [01](01-installer-and-deps.md), shutdown button from [07](07-desktop-app.md) | fresh Windows box with only Python → `start_app.bat` works; web UI can stop the server |
| 2 | v0.5.0 | Speed | [02](02-speed.md) | analysis phase ≥1.5× faster on harness + one real movie; alignment unchanged (Δa < 20 ppm, Δb < 10 ms) |
| 3 | v0.6.0 | Trust | [03](03-confidence-report.md), [04](04-drift-plot.md), [09](09-android-parity.md) | JSON sync report + confidence score + drift plot in web UI |
| 4 | v0.7.0 | Different cuts | [05](05-piecewise-alignment.md), [09](09-android-parity.md) | synthetic cut-edit test (removed 40 s scene) aligns every piece < 50 ms; linear cases unchanged |
| 5 | v0.8.0 | Batch | [06](06-batch-and-pairing.md) | folder of N movies + N AD files auto-paired correctly (incl. renamed files), queued, resumable |
| 6 | v0.9.0 | Desktop app | [07](07-desktop-app.md) | `ADPatcher.exe` (one-folder, local build) runs on a machine with no Python |
| 7 | v1.0.0 | Output options | [08](08-output-options-and-handoffs.md) | track keep/drop, external SRT, AD codec/bitrate; HandBrake/MKVToolNix hand-off buttons |

Ordering rationale:
- 0 before everything: no test suite exists; the harness is the safety net.
- 1 is cheap and unblocks other users immediately.
- 2 before 4/5: batch and piecewise both multiply analysis work; the
  envelope cache from 2 makes pairing (5) nearly free.
- 3 before 4: piecewise alignment needs the confidence machinery to decide
  when to split, and the plot to debug it.
- 6 after 5: package once the feature surface is stable (fewer exe
  rebuilds).
- 7 last: mostly UI + ffmpeg flags; lowest value per hour.

## Not doing (and why)

| Wish | Decision |
|---|---|
| GPU-accelerated alignment (CuPy etc.) | math is ~ms; adds a 500 MB dependency for no gain |
| In-app torrent client / torrent search | qBittorrent exists; search over piracy indexers is a legal risk. At most: "open magnet in qBittorrent" via its Web API (MIT client lib) — see 08 |
| Own track editor UI | MKVToolNix GUI is better than anything we'd build; offer "open in MKVToolNix" |
| Own size/res transcoder UI | HandBrake does this with NVENC + target-size; offer hand-off. A single "fit to N GB" preset is in 08 as optional |
| Fork describealign | GPL-3.0 — incompatible with copying into an MIT repo; ideas only |
| Fork ADSync wholesale | MIT, but heavier (scipy, librosa-style features) and lost round 1 of our benchmark; we adopt specific algorithms (Viterbi lattice, monotone chain vetting) instead |
| Nuitka for the exe | now AGPL-3.0 on GitHub; PyInstaller (GPL + bootloader exception) is fine for MIT apps |

## File index

- [00-baseline-and-harness.md](00-baseline-and-harness.md) — timings, synthetic media generator, accuracy checker
- [01-installer-and-deps.md](01-installer-and-deps.md) — requirements, self-bootstrapping bat
- [02-speed.md](02-speed.md) — parallel envelopes, chunked decode, envelope cache, mux I/O
- [03-confidence-report.md](03-confidence-report.md) — confidence score, JSON report, warnings
- [04-drift-plot.md](04-drift-plot.md) — drift + envelope-overlay plots
- [05-piecewise-alignment.md](05-piecewise-alignment.md) — different-cut support
- [06-batch-and-pairing.md](06-batch-and-pairing.md) — folder mode, content-based pairing
- [07-desktop-app.md](07-desktop-app.md) — shutdown button, pywebview shell, PyInstaller build
- [08-output-options-and-handoffs.md](08-output-options-and-handoffs.md) — track/codec options, external tools
- [09-android-parity.md](09-android-parity.md) — what must be mirrored in Kotlin
- [THIRD_PARTY.md](THIRD_PARTY.md) — every reused library/algorithm with license + verdict
