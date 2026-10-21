# Handoff: AI audio description (2026-10-19, updated 2026-10-21)

> 2026-10-21: A2 resumed — pyannote chosen (th 0.4), `GapFinder.kt` +
> `GapFinderTest` done and matching the PC reference; results in doc 10
> "A2 results". Remaining: CMF run (speed, parity on device). The
> section below is the original 10-19 snapshot.

Branch `claude/ai-ad-generation-c6685c` (not merged to main). Plan +
research: [docs/plan/10-ai-audio-description.md](../plan/10-ai-audio-description.md).

## Done

- **Research** (doc 10): no open on-device AD generator exists. Picks:
  llama.cpp + Qwen3-VL-2B ("Best") / SmolVLM2-500M ("Fast"), Android
  system TTS (Kokoro later, dictionary G2P to avoid GPL espeak-ng),
  resume via per-cue checkpoints (§8a). AD apps (MovieReading,
  Earcatch, …) offer no export or API (§8b).
- **A1 ✅** (commit 96af797): `android/app/src/main/cpp/` (llama.cpp
  `mtmd` via JNI → `libadpvlm.so`), `describe/VlmDescriber.kt`, adb-only
  `describe/DevProbeActivity.kt`. Measured in-app on the CMF at 512 px:
  SmolVLM2-500M **5.3 s/frame, 1.1 GB**; Qwen3-VL-2B **8.7 s/frame,
  2.6 GB**. A 2 h movie ≈ 20–50 min of VLM time.
- **A2 in progress** (uncommitted work committed with this handoff):
  `tools/gaps_ref.py` = PC reference + scorer for dialogue gaps.
  - Ground truth = full mix vs. M&E stem (spectral, speech band),
    limited to subtitle lines. Clips: Tears of Steel (full mix + M&E
    `.aif` + `TOS-en.srt`) and Sintel (720p mkv + `sintel-m+e-st.flac`
    + embedded German subs), all CC-BY from download.blender.org.
  - **Silero VAD is not good enough on film mixes**: catches only
    41–48 of 76 ToS subtitle lines, 12–40 % speech recall on Sintel
    (any threshold, 8 or 16 kHz, centre extraction doesn't help).
  - **pyannote segmentation-3.0 (MIT, sherpa-onnx ONNX int8, 1.5 MB)**
    at 16 kHz, 10 s windows, 50 % overlap, threshold 0.5:
    ToS **PASS** (gap recall 93 %, 1/33 gaps overlapping speech);
    Sintel recall 100 %, but 3/26 gaps contain a short shouted word
    (model prob 0.33–0.47) → FAIL on the < 5 % rule.
  - Was about to sweep: max vs. mean over overlapping windows, 75 %
    overlap, threshold 0.3–0.5, guard 0.2–0.4 s. Then: third clip,
    Kotlin `GapFinder` (ONNX Runtime Android, own downmix + 16 kHz
    resample on top of `AudioEngine.decodeAudio`, no edits to
    AudioEngine), probe on CMF, compare to `gaps_ref.py` output.

## Next

1. Finish A2 per above; record numbers in doc 10; update THIRD_PARTY
   (pyannote MIT + sherpa-onnx export, onnxruntime-android MIT).
2. A3 shots → A4 describe (+ checkpoint/resume) → A5 TTS/mix/mux.

## Building the native lib

`android/local.properties` (gitignored, machine-specific):
```
sdk.dir=C:/Users/Rajat/AppData/Local/Android/Sdk
cmake.dir=<CMake 3.22+ dir whose bin/ also holds ninja.exe>
adp.ndkPath=C:/Program Files/Unity/Hub/Editor/6000.6.5f1/Editor/Data/PlaybackEngines/AndroidPlayer/NDK
```
- NDK must be **27.2.12479018** (`ndkVersion` in app/build.gradle.kts);
  the Unity 6000.6.5f1 copy is that version. No NDK in the Android SDK.
- CMake: `pip install cmake ninja` into any venv, copy `ninja.exe`
  into `.../site-packages/cmake/data/bin`, point `cmake.dir` at
  `.../cmake/data`. (Used a scratchpad venv this session — it is
  temporary and will be gone; recreate.)
- CMake downloads llama.cpp at pinned commit `7951f60` (SHA-256 in
  `cpp/CMakeLists.txt`) — first build needs network, ~5 min.
- `JAVA_HOME="C:/Program Files/Android/Android Studio/jbr"`, Gradle
  8.9, `cd android && gradle assembleRelease`. APK is arm64-only now.

## Model + data locations

- **Phone (CMF, serial 00116646S015275)**:
  `/sdcard/Android/data/com.fuzzypyjamas.adpatcher/files/models/`
  holds `Qwen3VL-2B-Instruct-Q4_K_M.gguf`,
  `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf`,
  `SmolVLM2-500M-Video-Instruct-Q8_0.gguf`,
  `mmproj-SmolVLM2-500M-Video-Instruct-Q8_0.gguf` (~2.1 GB total);
  `files/frames/` has 4 Sintel test frames. Deleted if the app is
  uninstalled. `/data/local/tmp/adpai/` has the CLI bench
  (`llama-mtmd-cli`, `bench.sh`, jpgs) — safe to delete.
- **Sources**: HF `Qwen/Qwen3-VL-2B-Instruct-GGUF`,
  `ggml-org/SmolVLM2-500M-Video-Instruct-GGUF` (Apache-2.0);
  pyannote ONNX: github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-segmentation-models;
  Silero: github.com/snakers4/silero-vad `src/silero_vad/data`.
- **PC**: models, clips and truth JSONs were in the session scratchpad
  (temporary). Re-download clips from download.blender.org
  (`demo/movies/ToS/`, `durian/movies/`).

## Probe

```
adb -s 00116646S015275 shell am start -n com.fuzzypyjamas.adpatcher/.describe.DevProbeActivity --es model Qwen3VL-2B-Instruct-Q4_K_M.gguf --es mmproj mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf
adb -s 00116646S015275 logcat -s ADP-VLM
```

## Device state

CMF has this branch's A1 build installed (version still 0.4.1, adds
the native lib + probe activity; the normal patch UI is unchanged).
Nothing else changed on the phone.

## Open questions for the owner

1. Default model: Qwen3-VL-2B (better, 2.6 GB RAM, ~9 s/frame) vs.
   SmolVLM2-500M (faster, 1.1 GB, more mistakes)? Plan: Qwen default,
   Smol for < 4 GB phones.
2. OK to switch the gap detector from Silero (planned) to pyannote
   segmentation (MIT, needs 16 kHz)?
3. Should narration avoid breaths/grunts/screams too, or only dialogue?
   (Currently: only subtitle-level dialogue counts as speech.)
4. APK became arm64-only — acceptable until a public release?
5. Gemma 3n is excluded by the open-licence rule — keep it excluded?
