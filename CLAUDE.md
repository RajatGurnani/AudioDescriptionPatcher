# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working rules (all sessions, incl. worktrees + subagents)

- **Work queue:** treat all work as a queue of todos. User tweaks/changes/corrections → append to the queue; don't break the queue to jump to them. Only stop when the queue is 100% clear: every item `[x]` or `BLOCKED:`. Items the user put in the queue count as "told"; side-effectful steps (push, deploy, delete, send) still get confirmed.
- **Queue file:** `D:\PERSONAL\AudioDescriptionPatcher\TODO.md`, always this absolute path, even from a worktree (`git rev-parse --show-toplevel` gives the worktree there, not main). Git-excluded, never committed. Read it before starting work. Re-read it right before each change, and change only your own line(s) with Edit, never a whole-file Write (other sessions edit it too).
- **Queue format:** `- [ ] item (owner: X)`; done → `- [x]`; blocked → `- [ ] BLOCKED: reason — item (owner: X)`. X = `main` (the session in the main checkout), `user`, or the linked session's short name (e.g. `android-generate-ad-for`). Items owned by `user` are always `BLOCKED: waiting user` (they never hold up the stop test).
- **Mistakes:** no apologies. Mistake = gap or issue in instruction files → name the gap + suggest the fix. Solution oriented.
- **YAGNI:** don't overengineer; never expand scope without permission. Suggest scope improvements when they make sense. All work must be justifiable. Judge where the project sits on "quick and dirty" ↔ "production grade" and match engineering to it.
- **Reply budget:** a mistake-gap note and a scope suggestion get at most 1 line each, counted inside the 8-line cap. A scope suggestion only when it changes what the user decides; unsure → cut.
- **Review:** after making changes, spin up a subagent on a cheap model with minimal context to review them with fresh eyes. Report its findings only; fix only when told. Skip for one-line or docs-only edits.

## Project Goal

A tool that "patches" a regular video file with its audio-described (AD) version: given a video and an AD audio file (e.g., an MP3 from audiovault.net), align the AD audio to the video and mux it in as the default audio track.

## Commands

```
# setup: start_app.bat (desktop) / start_web.bat (phones) / patch.bat
# self-bootstrap via tools\ensure_env.bat
# (creates .venv, pip/uv installs when requirements.txt changes)
.venv\Scripts\pip install -r requirements.txt

# CLI (inputs in any order; auto-detects which is the video)
.venv\Scripts\python adpatch.py <video> <ad_audio> [-o out] [--replace]

# web app (PC browser + phones on same Wi-Fi), port 8756
# ADPATCH_NO_BROWSER=1 skips auto-opening the browser (tests)
# POST /api/shutdown: localhost only; 409 if jobs run, ?force=true kills tree
.venv\Scripts\python app.py

# AI audio description for videos without AD (docs/plan/10, P1):
# local VLM via llama-server (Vulkan) + pyannote gaps + SAPI TTS
.venv\Scripts\pip install -r requirements-ai.txt
.venv\Scripts\python adgen.py --setup [--model 4b]
.venv\Scripts\python adgen.py <video> [-o out] [--model auto|2b|4b|8b|cpu]

# desktop app (pywebview window over app.py's FastAPI, 127.0.0.1 only)
.venv\Scripts\python desktop.py
# headless smoke test (hidden window, never shown) - test GUI this way
.venv\Scripts\python tools\desktop_smoke.py
# local exe build -> dist\ADPatcher\ADPatcher.exe (never upload/release)
.venv\Scripts\pip install -r requirements-desktop.txt
.venv\Scripts\pyinstaller desktop.spec --noconfirm
# exe headless check: ADPATCH_DESKTOP_HIDDEN=1 ADPATCH_DESKTOP_PORT=8799

# Android APK (needs Gradle 8.9 on PATH or a downloaded dist;
# JDK 17 from Android Studio: set JAVA_HOME to
# "C:\Program Files\Android\Android Studio\jbr")
# native VLM lib (docs/plan/10) also needs NDK 27.2 + CMake 3.22+ (with
# ninja.exe in its bin/): android/local.properties keys adp.ndkPath,
# cmake.dir; llama.cpp is fetched by CMake at a pinned commit
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
  3. different edits: if linear coverage < 0.9 (or, option
     `split_small_cuts` default on, linear p95 residual > PW_WOBBLE_MS
     120 — PC only, Aligner.kt not yet), `_fit_piecewise()`
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
- Output options: `DEFAULT_OPTIONS` / `resolve_options()` /
  `BUILTIN_PRESETS` + presets.json; `build_mux_cmd()` returns
  (cmd, notes). Per-codec mkv priming in `AD_CODECS` (ac3/eac3 256,
  opus 0) + `AD_ENCODERS` (aac_mf 0, aac 1024). Fit-to-size picks
  `video_encoder()` (NVENC → QSV → AMF → x265). Test:
  `tools\options_test.py` (streams, per-codec sync, loudness, size).
- ffmpeg comes from `imageio_ffmpeg.get_ffmpeg_exe()` (pip-bundled binary,
  no ffprobe available — stream detection parses `ffmpeg -i` stderr).

## Other components

- `batch.py` — folder mode (`adpatch.py batch <folders> | --resume`, web
  Batch card, localhost-only /api/batch/*): discover → `parse_episode()`
  (SxxEyy / 1x02 / Episode N + season folder) → candidates tiered same
  episode → same season → all (15 % duration gate) → `content_score()`
  = coverage (20 s segments searched over the whole video, counted only
  when 2+ agree on an offset and each peak stands clear of noise; shared
  intros/recaps stay low) → greedy one-to-one with uniqueness (else
  "check"; episode codes / names only break ties; double-episode AD →
  note, not patched; pairing runs in worker processes) → `Queue` persisted atomically to
  %LOCALAPPDATA%\ADPatcher\queue.json, resumable; Queue.run keeps up to 3
  items in flight (analyze/align) with one writer (`write_lock`).
  Disks (adpatch.py): `is_rotational()` (seek-penalty query, env
  ADPATCH_FORCE_HDD for tests), `disk_slot()` = one reader per HDD,
  `analysis_envelope()` pulls the analysis track to the SSD scratch,
  `patch()` stages HDD videos on the scratch (copy_in/copy_out). Test:
  `tools\batch_test.py`.
- `desktop.py` — pywebview shell: `?desktop=1` page mode, js_api `Api`
  (native dialogs with per-slot remembered folders in
  %LOCALAPPDATA%\ADPatcher\desktop.json, reveal, AudioVault window + Downloads
  watcher), Python-side drop handler → `adpDropPaths()` with real paths,
  jobs via POST /api/patch_local (localhost, in place, no upload). Keep
  Api attributes `_private` (pywebview walks public ones → COM spam).
- `app.py` — FastAPI wrapper around `adpatch.patch()`: multipart upload →
  background thread → poll `/api/jobs/<id>` → download. Inline single-page
  UI (mobile-first). Jobs live in `jobs/<id>/`; uploads deleted after run.
- `android/` — package `com.fuzzypyjamas.adpatcher` (renamed from
  com.renderedideas.adpatcher in 0.4.1 — a separate app id, so the old
  install has to be removed by hand). Standalone Kotlin port, NO ffmpeg: `AudioEngine`
  (MediaCodec decode → same onset envelope), `Aligner` (same two-stage
  fit, JTransforms FFT), `Patcher` (MediaMuxer: video samples copied, AD
  re-encoded AAC with offset+resample applied; AAC csd-0 crafted by hand
  (0x12 0x10) so the muxer can start before encoder output). Android
  stretch = linear resample (pitch shifts by drift %, unlike ffmpeg
  atempo). minSdk 26 (multi-track MediaMuxer). Alignment math changes must
  be mirrored in BOTH adpatch.py and Aligner.kt.
  Feature parity matrix: docs/plan/12-feature-parity.md (keep it current).
  Voice boost exists on both sides: PC `voice_boost_graph()` (options
  voice_boost / voice_boost_track / voice_boost_ad, CLI --voice-boost,
  test `toolsoiceboost_test.py`) mirrors the Android one.
  `VoiceBoost.kt` (Android): optional extra track = original audio +
  dialogue-clarity DSP, or applied to the AD via `EncoderSink(fx)`; off by
  default, prefs `voice_boost`. Measure: `VoiceBoostTest` (docs/plan/11).

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
