# Handoff — Android voice boost (2026-10-19)

Branch: `claude/voice-boost` (based on main 9703825). Not on main because
the Patcher.kt hook depends on the sync session's uncommitted Patcher.kt
work (`encoderDelay()` loopback, `EncoderSink(delay)`), which isn't in
any commit yet. Plan/rationale: [docs/plan/11-voice-boost.md](../plan/11-voice-boost.md).

## Built

- Owner decision: voice boost = **additional** audio track (original audio
  + boost). AD track (default, a:0) and original copy (a:1) untouched by
  default; option to also boost the AD track. Live overlay: not built
  (feasibility in plan 11, bottom).
- `android/.../VoiceBoost.kt` — streaming zero-latency DSP (low-shelf,
  mid/side centre emphasis, 2.5 kHz presence, speech-detector ducking of
  out-of-band mid + side, RMS compressor, -1 dBFS limiter), `Options`
  (SharedPreferences `voice_boost`, default OFF / extra track on / AD off),
  `writeTrack()` (decode video audio → stereo downmix → resample if needed
  → boost → AAC via Patcher.EncoderSink with priming compensation).
- UI: "Voice boost" card in `activity_main.xml` + `MainActivity.setupVoiceBoost()`
  (Off/Low/High toggle, two checkboxes, saved on change).
- Patcher hook: **not in the branch's Patcher.kt** — it's
  [voice-boost-patcher-hook.patch](voice-boost-patcher-hook.patch), made
  against the working tree *including* the sync session's edits
  (internal modifiers, `EncoderSink(fx)`, `mux(boost=Options.load(ctx))`,
  `writeAdTrack(boostLevel)`, one extra writer thread). The shared
  working tree on main still has it applied (uncommitted).
- Test: `android/app/src/test/.../VoiceBoostTest.kt` (JVM; `-Dvb.out=DIR`
  writes WAVs + report; build.gradle.kts passes `vb.out` through).
- Docs: plan 11, THIRD_PARTY (RBJ cookbook adapted; RNNoise/DeepFilterNet/
  DTLN evaluated, not used), USAGE (Android step 4), CLAUDE.md, plan README.

## PC measurement (passed)

Synthetic 30 s: formant voice (centre) 10 dB under music + pink noise;
exact stems via shadow processing (stem-sum error < 1e-7). VBR gain:

| background | level | speech band | full band | voice level |
|---|---|---|---|---|
| wide stereo | low | +2.8 dB | +4.9 dB | +0.7 dB |
| wide stereo | high | +3.6 dB | +8.4 dB | +1.5 dB |
| mono | low | +1.0 dB | +4.2 dB | +0.4 dB |
| mono | high | +1.4 dB | +7.2 dB | +1.2 dB |

No clipping (peak ≤ 0.67). APK assembled fine (with the sync session's
working-tree changes + hook).

## Pending

1. **On-device run on the CMF (not done)** — the phone was busy with the
   sync session's test runs (picker open) the whole time.
2. Merge into main once the sync session commits its Patcher.kt.

## Next steps (exact)

1. After the sync session commits Patcher.kt to main:
   `git merge refs/heads/claude/voice-boost` (or cherry-pick), then
   `git apply docs/handoff/voice-boost-patcher-hook.patch` if Patcher.kt
   on main doesn't already have the hook (the working tree may already
   carry it — `git apply --check -R` tells you), build, commit, push.
2. `cd android && gradle testReleaseUnitTest --tests '*VoiceBoostTest*'`
   (JAVA_HOME = Android Studio jbr; gradle 8.9 from a scratchpad dist).
3. `gradle assembleRelease`; check `adb -s 00116646S015275 shell getprop
   ro.product.model` = A015; `adb -s 00116646S015275 install -r
   app/build/outputs/apk/release/app-release.apk` (MSYS_NO_PATHCONV=1).
   Device driving (sync session's method): scratchpad `ui.py` (uiautomator
   dump + adb input tap, pinned to the serial) + `run.sh <AD name in
   Download/ADPatcherTest> <out.mp4>` in
   `%TEMP%\claude\D--PERSONAL-AudioDescriptionPatcher\cde144db-be47-4a22-8f9d-1bcef4959cd5\scratchpad`
   (may be cleaned up; it taps pickAd → file → goButton → save name →
   button1, waits for "writing output:" in logcat). Set the voice boost
   card first (vbHigh, vbTrack).
4. On the phone: Voice boost = High, extra track on; patch
   `/sdcard/Download/BigBuckBunny.mp4` + `BigBuckBunny_AD.mp3`. Expect log
   line `voice boost: high (extra track)` and `voice-boost track done`.
5. Pull the output; with ffmpeg on PC check 3 audio streams, equal
   durations, and a:2 vs a:1 cross-correlation lag ≈ 0 ms (sync); listen.
   Repeat once with "Also boost the AD track" to check AD path.
6. Record results in plan 11 + bump versionCode/versionName.

## Open questions

- MediaMuxer can't name tracks — players show "Track 3". OK, or reorder?
- Should the boosted track replace the original copy when the original
  codec can't be copied into MP4 (today: AD-only output then; the boosted
  track still gets added since it's decoded + re-encoded)?
- Live overlay (DynamicsProcessing on players that broadcast audio
  sessions: VLC/MX yes, YouTube/Netflix no) — wanted as a follow-up?
- In-band centred music can't be separated by DSP; ML separation only if
  real-film listening says Low/High isn't enough.
