# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Goal

A tool that "patches" a regular video file with its audio-described (AD) version: given a video and an AD audio file (e.g., an MP3 from audiovault.net), align the AD audio to the video and mux it in as the default audio track.

## Commands

```
# setup: start_app.bat / patch.bat self-bootstrap via tools\ensure_env.bat
# (creates .venv, pip/uv installs when requirements.txt changes)
.venv\Scripts\pip install -r requirements.txt

# CLI (inputs in any order; auto-detects which is the video)
.venv\Scripts\python adpatch.py <video> <ad_audio> [-o out] [--replace]

# web app (PC browser + phones on same Wi-Fi), port 8756
# ADPATCH_NO_BROWSER=1 skips auto-opening the browser (tests)
# POST /api/shutdown: localhost only; 409 if jobs run, ?force=true kills tree
.venv\Scripts\python app.py

# Android APK (needs Gradle 8.9 on PATH or a downloaded dist;
# JDK 17 from Android Studio: set JAVA_HOME to
# "C:\Program Files\Android\Android Studio\jbr")
cd android && gradle assembleRelease
# apk: android/app/build/outputs/apk/release/app-release.apk
adb install -r app-release.apk
```

No unit tests. Verify changes with the synthetic harness (exact ground
truth, checks the output file itself, exit 1 on failure):

```
.venv\Scripts\python tools\harness.py [--minutes 10] [--cases pal,ntsc] [--container mp4]
```

Kotlin parity (Aligner.kt vs fit_alignment on identical envelopes, JVM
unit test, skipped without the export):

```
.venv\Scripts\python tools\parity_export.py
cd android && gradle testReleaseUnitTest -Dparity.dir=%TEMP%\adpatch-parity
```

Never build truth with `atempo` (adds 5-20 ms WSOLA latency) or
non-integer `asetrate` (rounds) — the harness uses 23976<->25000 Hz
relabeling. Roadmap + per-iteration plans: docs/plan/README.md. An end-to-end web-app test = POST two files to /api/patch, poll
/api/jobs/<id>, GET download.

## Architecture (adpatch.py, single file)

- `onset_envelope()` — streams ffmpeg-decoded mono 8 kHz PCM, computes a
  100 Hz z-normalized onset-strength envelope (never holds full PCM).
- `fit_alignment()` — two stages, models `video_time = a*ad_time + b`:
  1. speed-factor grid scan (0.95–1.05 coarse→fine) on 20 Hz pooled
     envelopes: stretch AD envelope per candidate factor, FFT
     cross-correlate, pick sharpest peak. This is REQUIRED before any
     segment matching — with uncorrected drift, segment correlations
     smear and produce garbage fits (learned the hard way).
  2. stretch AD envelope by best factor, then 30 s `segment_match()`
     windows across the runtime → weighted linear fit with outlier
     rejection → final a, b + per-segment sync report.
  Also returns `signals` → `confidence()` = score, band
  (good/check/likely wrong), warnings. `patch(save_report=True)` /
  CLI `--report` writes .sync.json + .sync.svg; web UI draws drift +
  waveform-match charts with vendored uPlot (static/vendor/uplot, MIT)
  from `result["plot"]` via /api/jobs/<id>/envelopes.
  3. different edits: if linear coverage < 0.9, `_fit_piecewise()`
     (window lattice → offset families → Viterbi → vetted pieces →
     hand-over points → joint slope + per-piece intercepts). Returns
     `pieces`; `plan_pieces()` turns them into render segments + gaps;
     `_piecewise_graph()` splices in ffmpeg (gaps = original audio).
- `patch(..., log, progress)` — `progress(phase, frac)` with phases
  analyze/align/mux. Video + AD envelopes run in parallel threads via
  `cached_envelope()` (content-keyed .npy cache in
  %LOCALAPPDATA%\ADPatcher\cache; `ADPATCH_NO_CACHE=1` bypasses, the
  harness sets it unless `--cache`). Decode % from bytes vs `probe_duration()`, mux %
  from ffmpeg `-progress pipe:1` (`run_ffmpeg()`). All ffmpeg stderr is
  drained on a thread (`_drain`) — reading it after stdout deadlocks on
  Windows. CLI draws `ConsoleProgress` when stderr is a TTY.
- `build_mux_cmd()` — ffmpeg mux: `atempo=1/a` (skipped if |a-1|<5e-5),
  `adelay` (b≥0) or `atrim` (b<0); video `-c:v copy`; AD resampled to
  48 kHz, encoded with `ad_encoder()` (Windows `aac_mf`, 5x faster, no
  priming; fallback ffmpeg `aac`, whose 1024-sample priming is
  compensated in mkv only) as default track a:0, original audio copied as a:1 unless `--replace`;
  retries without subtitles if subtitle copy fails (e.g., mov_text→mkv).
- ffmpeg comes from `imageio_ffmpeg.get_ffmpeg_exe()` (pip-bundled binary,
  no ffprobe available — stream detection parses `ffmpeg -i` stderr).

## Other components

- `batch.py` — folder mode (`adpatch.py batch <folders> | --resume`, web
  Batch card, localhost-only /api/batch/*): discover → 15 % duration
  gate → `match_score()` (stage-1 speed-scan peak on cached envelopes)
  → greedy one-to-one assignment with uniqueness (else "check"; names
  only break ties) → `Queue` persisted atomically to
  %LOCALAPPDATA%\ADPatcher\queue.json, sequential, resumable. Test:
  `tools\batch_test.py`.
- `app.py` — FastAPI wrapper around `adpatch.patch()`: multipart upload →
  background thread → poll `/api/jobs/<id>` → download. Inline single-page
  UI (mobile-first). Jobs live in `jobs/<id>/`; uploads deleted after run.
- `android/` — standalone Kotlin port, NO ffmpeg: `AudioEngine`
  (MediaCodec decode → same onset envelope), `Aligner` (same two-stage
  fit, JTransforms FFT), `Patcher` (MediaMuxer: video samples copied, AD
  re-encoded AAC with offset+resample applied; AAC csd-0 crafted by hand
  (0x12 0x10) so the muxer can start before encoder output). Android
  stretch = linear resample (pitch shifts by drift %, unlike ffmpeg
  atempo). minSdk 26 (multi-track MediaMuxer). Alignment math changes must
  be mirrored in BOTH adpatch.py and Aligner.kt.

See docs/ARCHITECTURE.md for the full alignment rationale and tuned
thresholds; docs/USAGE.md for the user-facing guide.

## Key Domain Knowledge

## Key Domain Knowledge

- AD audio files (AudioVault etc.) are usually the **full movie soundtrack with narration mixed in**, not narration-only clips. So the core problem is *alignment + muxing*, not detecting silent gaps and inserting description snippets.
- Length mismatch between the video and the AD file typically comes from **different cuts/sources** (trimmed studio logos/intros, PAL 25fps speedup vs 24fps, ad breaks in TV rips), not from the narration adding time. Narration sits in natural pauses of the original mix.
- Therefore alignment may need more than a single global offset: piecewise offsets and/or tempo (time-stretch) correction to handle drift from framerate differences.

## Feasibility / Prior Art (researched 2026-08)

Proven approach; existing projects to study or build on:

- **KinoSync** (iOS app) — real-time audio-fingerprint sync of AD tracks to movies; closest existing product, but real-time playback rather than file patching.
- **[describealign](https://github.com/julbean/describealign)** (Python) — CLOSEST prior art, found late (2026-08): combines videos with AD audio files, piecewise rate alignment (handles mid-movie edit discontinuities our linear model can't), GUI+CLI, alignment plots. No mobile/web story. Benchmark against it; consider adopting its piecewise approach for v2 (check license first).
- **[ADSync](https://github.com/JohnnyTheCoder1/ADSync)** (Python, MIT, very new 2026-08) — CLI aligning fan AD tracks into MKV; four strategies incl. Viterbi warp + PCHIP for discontinuous edits, ~1-3 ms accuracy, confidence warnings. MIT = adoptable for our v2 warp stage. No web/mobile. Needs ffmpeg on PATH.
- **[audalign](https://github.com/benfmiller/audalign)** (Python, PyPI) — aligns audio files via fingerprinting, cross-correlation, and spectrogram correlation; returns offsets. Strong candidate as the core alignment library.
- **[SyncSink](https://github.com/JorenSix/SyncSink)** (Java) — fingerprint-based offset detection (~8ms accuracy) between a reference file and other recordings; also detects drift.
- **[align-videos-by-sound](https://github.com/align-videos-by-sound/align-videos-by-sound)** (Python, ffmpeg + scipy/numpy) — reports time offsets between files sharing a soundtrack.
- **[audiovault-downloader](https://github.com/cartertemm/audiovault-downloader)** — fetches AD files from audiovault.net; relevant for sourcing inputs.
- **ffmpeg** — final muxing step: apply offset (`-itsoffset`), optional `atempo` for speed correction, then `-map`/`-c:v copy` to add or replace the audio track without re-encoding video.

## Planned Pipeline

1. Extract audio from video (ffmpeg → wav/pcm).
2. Align AD file against extracted audio (fingerprint first; fall back to cross-correlation). Detect global offset + drift; if drift ≈ 4.1% suspect PAL speedup.
3. Correct AD timing (offset trim/pad; `atempo` or resample for drift).
4. Mux corrected AD audio into the video container as new/second audio track, video stream copied untouched.
5. Verify: spot-check correlation at several timestamps across the runtime.

## Environment Notes

- Windows 10 host; ffmpeg must be on PATH for any of the pipeline to work.
