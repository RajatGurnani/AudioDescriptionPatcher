# 11 — Voice boost (dialogue clarity), Android

Status: **built** (Android 0.4.x, 2026-10-19). Owner's choice: voice boost as
an *additional* track; AD + original tracks untouched by default; optional
"also boost the AD track". Live overlay: see bottom.

Goal: make speech + AD narration easier to hear over music/effects,
like YouTube's "Voice Boost". Strength setting: off / low / high.

## How others do it

| Approach | Who | Notes |
|---|---|---|
| ML voice/background separation, raise voice, duck rest | YouTube Voice Boost (2025-26, playback-only, original untouched; ASMR complaints about flattening) | details only from secondary sources |
| Center-channel emphasis (mid/side): dialogue is mixed to center | TV "dialogue enhance", Dolby Voice/Dialogue Enhancer, VLC "stereo → center" tricks | free, cheap; useless on mono / downmixed AD MP3s (most AudioVault files are stereo though) |
| Dialogue-band EQ: low-shelf cut <150 Hz, presence lift 1–4 kHz | every soundbar "voice" mode, Android `Equalizer` | cheap, always helps intelligibility a bit; boosts any music in band too |
| Dynamic range compression + limiter | "night mode", Netflix/Prime "reduce loud sounds", Android `DynamicsProcessing` (API 28) / `LoudnessEnhancer` | lifts quiet speech, tames explosions |
| Speech-gated ducking (VAD → lower non-speech bands while voice present) | smart TVs, broadcast "dialogue intelligence" | needs a speech detector; energy+spectral-flatness VAD is enough |
| ML denoise | [RNNoise](https://github.com/xiph/rnnoise) (BSD-3, C, 48 kHz, tiny), [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet) (MIT/Apache-2.0 dual, ONNX ~8 MB), [DTLN](https://github.com/breizhn/DTLN) (MIT, TFLite) | trained to *remove* noise, not music → on film mixes they gate/warble music and can eat narration under music. Needs NDK (RNNoise) or onnxruntime-android (MIT, +~10-15 MB APK). True voice/music separation models (Demucs etc.) are far too slow for a phone on a 2 h film. |

Android platform effects (`DynamicsProcessing`, `Equalizer`,
`LoudnessEnhancer`) only attach to an **audio session of a live player**.
This app has no player; third-party players (VLC, MX, Gallery) don't
expose sessions; global session 0 is deprecated. → no use for file
output.

## Modes

| Mode | What | Cost | Fit |
|---|---|---|---|
| **A. While patching** | DSP on the AD PCM right before AAC encode (AD is re-encoded anyway → zero extra encode cost). Hook = one call in `Patcher.writeAdTrack` before `sink.feed()`; all logic in new `VoiceBoost.kt` | small; ~1 line in Patcher + param plumbing | **best** — AD track = what blind/low-vision users listen to |
| B. Boost existing video | new action: decode original audio → `VoiceBoost` → AAC, copy video, mux | medium; new job type + UI; original audio no longer stream-copied | nice-to-have, reuses A's class |
| C. Live playback | AudioEffect on a session | needs own player | **no** (see above) |

Original track (a:1) stays a bit-exact copy in mode A — boosting it
would add a decode+encode pass and change Patcher's copy path.

## Recommendation

1. Mode **A** now, pure Kotlin DSP, no dependencies, no licence issues:
   - mid/side: keep mid, scale side (low 0.7, high 0.45) — center emphasis
   - high-pass ~100 Hz on side only + low-shelf −3/−6 dB <150 Hz
   - peaking +3/+6 dB at ~2.5 kHz (Q≈0.8) — presence
   - speech-band VAD (300–3400 Hz energy vs full-band, smoothed) →
     duck non-speech-band energy 2/5 dB while speech present
   - RMS compressor (ratio 2:1 / 3:1, 10 ms attack, 200 ms release) +
     peak limiter at −1 dBFS so the lift never clips
   - Biquads from the RBJ Audio-EQ Cookbook (public formulas).
2. Mode B later as its own small iteration if wanted (same class).
3. ML (RNNoise/DeepFilterNet) **not now**: wrong training target for
   film mixes, NDK/ONNX weight; revisit only if DSP measurements are weak.

## Verification plan

- PC synthetic clip: speech-like bursts (formant-filtered pulse trains,
  syllable-rate AM 4 Hz) over music (chords + drums) / pink noise, wide
  stereo background, centered "voice". Known voice-only + background-only
  stems → measure SNR in 300–3400 Hz band before/after, by running the
  same chain (JVM unit test on VoiceBoost.kt, so the measured code = the
  shipped code).
- On-device: patch a clip on the CMF with low/high; check log + output
  plays, duration/sync unchanged (DSP is sample-aligned, no latency).

## Built (owner decision 2026-10-19)

- UI card "Voice boost": Off (default) / Low / High + "Add extra track:
  original audio + voice boost" (default on) + "Also boost the AD track"
  (default off). Saved in SharedPreferences `voice_boost`; Patcher reads it.
- Output (MP4): a:0 AD (default, unboosted unless opted in), a:1 original
  (bit-exact copy), a:2 original + voice boost (AAC 192k, stereo; 5.1
  downmixed with centre at 0.707). Extra track is written on its own
  thread in parallel with the AD chain; same AAC priming compensation.
- Code: `VoiceBoost.kt` (DSP + `writeTrack`); Patcher hook = internal
  modifiers, `EncoderSink(fx)`, `mux(boost=...)`, one extra thread.
- DSP final: low-shelf 150 Hz (-3/-6 dB), side x0.6/x0.35, presence
  +4/+7 dB @2.5 kHz, speech detector = centre 300-3400 Hz energy vs its
  own slow floor (falls 50 ms, rises 3 s; 3-9 dB above -> 0..1), ducks
  out-of-band mid + side by 4/8 dB while speech, RMS compressor 2:1/3:1
  at -28/-30 dB, makeup +5.5/+8.5 dB, limiter -1 dBFS. No look-ahead.

## Measured (PC, `VoiceBoostTest`, same Kotlin code)

Synthetic 30 s clip: formant voice (centre) 10 dB under music+pink noise.
Exact stems via shadow processing (stem sum error < 1e-7).
VBR = voice-to-background energy ratio; band = 300-3400 Hz.

| background | level | band VBR gain | full-band VBR gain | voice level |
|---|---|---|---|---|
| wide stereo | low | +2.8 dB | +4.9 dB | +0.7 dB |
| wide stereo | high | +3.6 dB | +8.4 dB | +1.5 dB |
| mono | low | +1.0 dB | +4.2 dB | +0.4 dB |
| mono | high | +1.4 dB | +7.2 dB | +1.2 dB |

Run: `gradle testReleaseUnitTest --tests '*VoiceBoostTest*' -Dvb.out=DIR`
(DIR gets WAVs off/low/high per case + report). Limits: in-band
background (music inside 300-3400 Hz, centred) can't be separated by
DSP - that needs ML source separation.

## On-device (CMF Phone 1, main 655e02e / app 0.4.1, 2026-10-21)

Harness cut+PAL pair (10 min). Lag measured on the PC: 5 s windows vs
the input video's own audio.

| Run | Tracks | AD sync (piece 1 / piece 2) | Boosted track | Levels (mean / peak) |
|---|---|---|---|---|
| Off | AD + original | −0.1 / −4.4 ms | — | AD −22.1 / −0.2 dB |
| High + extra track | AD + original + boosted original | −0.1 / −4.4 ms | +0.2 ms over all 119 windows | original −21.1 / 0.0, boosted −25.2 / −0.7 dB |
| High + boost AD | AD (boosted) + original | +0.1 / −3.5 ms | — | AD −25.8 / −0.8 dB |

Logs: `voice boost: high (AD track)` and `voice-boost track done`; the
extra track added ~165 s to the 10-min render (377 s vs 212 s). No
clipping, and the boost doesn't raise loudness. The test audio is
synthetic bursts, so clarity on real dialogue still needs a listen.
MediaMuxer flags every audio track as default; players usually take the
first (the AD).

## Live overlay / realtime (owner question)

Possible as a follow-up, with caveats: a foreground service that
attaches `DynamicsProcessing` (API 28+) to players that broadcast
`ACTION_OPEN_AUDIO_EFFECT_CONTROL_SESSION` (VLC, MX Player, many music
apps; *not* YouTube/Netflix/Chrome) plus a quick-settings tile/overlay
toggle; global session 0 is deprecated and unreliable. DynamicsProcessing
is per-channel EQ + multiband compressor + limiter - no mid/side and no
speech-gated ducking - so it gets roughly the EQ/compression part of
the above (~half the full-band gain, less in band). Capturing other
apps' audio (AudioPlaybackCapture) can't replace what they play, so our
own DSP can't run live on third-party players.
