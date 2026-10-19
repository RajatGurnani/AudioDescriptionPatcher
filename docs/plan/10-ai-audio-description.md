# 10 — AI audio description (generate AD where none exists)

Status: **A1 done** (2026-10-19) — VLM runs in-app on the CMF; next A2.
Scope: Android app first (`com.fuzzypyjamas.adpatcher`); a PC-assisted
path reuses the same design. Constraints: open-source, zero cost, local
models only, licences per [THIRD_PARTY.md](THIRD_PARTY.md).

Target device measured over adb: CMF Phone 1 = MediaTek **MT6878
(Dimensity 7300)**, 8 cores (4×A78 + 4×A55), Mali-G615, **5.3 GB RAM
visible**, Android SDK 36, 39 GB free. Dev PC: GTX 1650 4 GB.

## 1. What the feature has to do

1. Find *where* narration may go: gaps in dialogue (not just silence —
   film gaps are full of music/effects).
2. Find *what* to describe: shots/scenes changing during or before a gap.
3. Describe the frames in a short, present-tense, objective sentence that
   fits the gap (≈ 2.5–3 words/s spoken).
4. Speak it (TTS), duck the original mix under it, encode, and mux as the
   default audio track (original kept as a:1) — the part we already do.

Steps 1, 4 are deterministic engineering. Step 3 decides quality and
speed; step 2 decides how many VLM calls we make.

## 2. Prior art (automatic AD / video → narration)

| Project | Licence | What it is | Use for us |
|---|---|---|---|
| [microsoft/ai-audio-descriptions](https://github.com/microsoft/ai-audio-descriptions) | MIT | GenerateAD → WebVTT draft per narration window → human edit → RenderAD (TTS, fit to window, duck, mix). | **Adapt** the shape: WebVTT cue file as the editable intermediate; fit/duck/mix stage. Its models are Azure GPT + Azure Speech (paid cloud) → replace both. |
| [lanad22/VideoDescribe](https://github.com/lanad22/VideoDescribe) ([paper](https://arxiv.org/abs/2602.01390)) | none on GitHub → all rights reserved | Scenes → ASR → VLM per scene → fit into silence gaps; inline vs. "extended" (pause video) AD. Local path = Qwen2.5-VL-72B (CUDA). | **Idea** only. Confirms the pipeline; 72B is far beyond our hardware. |
| [nayan-20-01/audio-description](https://github.com/nayan-20-01/audio-description) | check | Lectures: Silero VAD pauses > 1.5 s → OCR → LLM sentence → TTS fitted to gap. | Idea: VAD-gap + length-fitting loop. |
| AutoAD I/II/III, AutoAD-Zero (Oxford VGG), UniAD (Ant, CVPR'25), MM-Narrator | research; mostly no licence / NOASSERTION; MM-Narrator needs GPT-4 | Movie AD SOTA: character bank (face → name), context of previous ADs, "when to describe" models. | **Idea**: (a) pass previous AD lines as context, (b) two-stage "describe densely → condense to 1 sentence that fits", (c) character names are what makes AD feel professional — later iteration. |
| DescribePro (arXiv 2508.01092) | paper | Human-AI AD editor; timing via pydub silence + Silero VAD + PySceneDetect. | Confirms tool choice for steps 1–2. |
| YouDescribe, KinoSync | service / app | Human-written AD. | Not applicable. |

No existing open project does this **on a phone**; nobody ships a local,
zero-cost movie-length AD generator. The pieces all exist.

## 3. Vision-language models (step 3)

All run through **llama.cpp `libmtmd`** (MIT, has `examples/llama.android`
Kotlin/JNI sample) unless noted. Phone timings are **estimates** from
public numbers on faster SoCs, scaled; iteration 1 measures the real ones.

| Model | Licence | Size (Q4/Q8 + mmproj) | Phone fit (5.3 GB) | Est. CMF s/frame (encode + ~30 tok) | Quality for AD |
|---|---|---|---|---|---|
| SmolVLM2-256M-Video | Apache-2.0 | ~0.3 GB | trivial | ~2–4 s | weak; generic ("a man in a room") |
| **SmolVLM2-500M-Video** | Apache-2.0 | ~0.6 GB | easy | ~4–8 s | ok captions, sometimes wrong details; supports multi-frame/video prompts |
| InternVL3-1B / 3.5-1B | MIT / Apache-2.0 | ~1 GB | easy | ~6–12 s | better than SmolVLM-500M |
| **Qwen3-VL-2B-Instruct** | Apache-2.0 | ~1.6 GB (Q4_K_M + Q8 mmproj) | ok | ~15–30 s (dynamic resolution → cap image to ~448 px) | best small model for scene/action description; Arm ships it as an Android demo |
| SmolVLM2-2.2B | Apache-2.0 | ~1.8 GB | ok | ~15–30 s | good, below Qwen3-VL-2B |
| Qwen2.5-VL-3B | **Qwen Research licence** (3B only) | — | — | — | **No** (not open licence) |
| Gemma 3n E2B (LiteRT-LM/MediaPipe) | **Gemma Terms of Use** (custom, gated) | ~3 GB | tight | S24 Ultra: 16 tok/s decode | **No** under "fully open-source" rule; strong otherwise — owner may waive |
| LFM2-VL | LFM Open License (revenue-gated) | — | — | — | No |
| Moondream 2 | Apache-2.0 | ~1.7 GB | ok | similar to 2B | good short captions; llama.cpp support is legacy-only |
| Florence-2 base/large (ONNX Runtime) | MIT | 0.23/0.77 B | easy | ~1–3 s | literal captions, no reasoning/context; fast fallback / object tags |

Runtimes considered: **llama.cpp** (MIT; widest VLM coverage, GGUF,
CPU NEON fine on A78; Vulkan on Mali is unreliable → CPU first),
MLC-LLM (Apache; fewer VLMs), LiteRT-LM/MediaPipe (Apache runtime but
model choice ≈ Gemma), ONNX Runtime (MIT; Florence-2, Kokoro, Silero),
ExecuTorch (BSD; heavier build). Pick **llama.cpp** for the VLM,
**ONNX Runtime** for the small models.

### Measured on the CMF (A1, 2026-10-19)

In-app via `VlmDescriber` (JNI, llama.cpp `7951f60`, CPU, 4 threads,
`armv8.2-a+dotprod+fp16`), Sintel trailer frames (CC-BY) scaled to
512×288, model kept loaded, greedy decoding, prompt = AD instruction
(≤ 15 words):

| Model | Load | Per frame (encode+prefill / gen) | PSS | Sample line (dragon frame) |
|---|---|---|---|---|
| SmolVLM2-500M-Video Q8_0 + mmproj Q8_0 | 0.7 s | **5.3 s** (4.9 s / 0.4 s, 177 prompt tok) | **1.1 GB** | "A hand pointing at a flying creature in the sky." |
| Qwen3-VL-2B-Instruct Q4_K_M + mmproj Q8_0, `image_max_tokens=256` | 5.4 s | **8.7 s** (6.4 s / 2.2 s, 188 prompt tok) | **2.5–2.7 GB** | "A large, winged creature soars through a warm, orange sky, its form partially obscured by a hand reaching out below." |

- Full 854×480 frame: Smol 7.5 s, Qwen 13.1 s → always downscale to
  512 px wide first.
- 8 threads is *slower* than 4 (A55 little cores drag): Smol 9.1 vs
  7.4 s encode in the CLI test. Keep 4.
- MemAvailable stayed ~3 GB with Qwen loaded; no crash. Battery temp
  31 → 36 °C over ~25 short CLI runs; sustained-load throttling is
  measured in A4.
- Qwen is clearly better (names objects right: dragon vs. "bat", sword
  vs. "hammer") but wordier → word budget + rewrite step matter.

Budget: 2 h movie, ~1 description per 20–40 s of film → 200–350 calls
→ **Qwen ≈ 30–50 min, Smol ≈ 20–30 min** on the CMF, plus decode/TTS.
Default = Qwen3-VL-2B ("Best"), SmolVLM2-500M as "Fast" / low-RAM
fallback (< 4 GB devices).
Acceptable for a "start it and come back" job; offer a model picker
(Fast = SmolVLM2-500M, Best = Qwen3-VL-2B).

Model files are **downloaded on first use** (HuggingFace, ~0.6–1.6 GB)
into app storage, not bundled in the APK.

## 4. TTS (step 4)

| Option | Licence | Phone speed (Dimensity 7300) | Notes |
|---|---|---|---|
| **Android `TextToSpeech`** (system engine) | OS API, no bundling | faster than real time; `synthesizeToFile()` gives WAV | zero size, zero licence risk, many languages; voice quality depends on the installed engine (Google TTS is good). **Default.** |
| **Kokoro-82M** via ONNX Runtime | weights Apache-2.0 | ~RTF 1.0–1.5 on mid-range (3rd-party reports) → ~10 min of speech for a movie's AD ≈ 10–15 min | best open voice. **Phonemizer is the catch**: the usual path is espeak-ng (**GPL-3.0**). Use **dictionary G2P** instead: misaki's lexicons (Apache-2.0) / CMUdict (BSD) + simple fallback for OOV words. English only at first. |
| sherpa-onnx (Kokoro/Piper/VITS) | Apache-2.0 *but* its TTS build statically links espeak-ng (GPL-3.0) | same | **No** as a linked library under our MIT/no-GPL-linking rule. |
| Piper | engine now GPL-3.0 (piper1-gpl); old rhasspy/piper MIT archived; voices per-model | fast (RTF ≪ 1) | No for linking; Kokoro sounds better anyway. |

Plan: ship with system TTS; add Kokoro (+ dictionary G2P) as an
optional "natural voice" download in a later iteration.

## 5. Pause + shot detection (steps 1–2)

- **Speech vs. non-speech:** [Silero VAD](https://github.com/snakers4/silero-vad)
  (MIT, ~2 MB ONNX, supports **8 kHz**) → reuses the existing 8 kHz mono
  decode in `AudioEngine`. Gap = no speech ≥ 1.5 s (tunable), merged
  across < 0.3 s blips, trimmed 0.2 s each side. Fallback without ONNX:
  energy + spectral-flatness heuristic on the same PCM.
- **Shot cuts:** decode video via MediaCodec to a small surface
  (ImageReader, ~64×36 luma) at ~2–4 fps sampled frames, colour-histogram
  + luma-diff cut detector (PySceneDetect's ContentDetector idea, BSD —
  re-implement). Fast path: keyframe-only decode (encoders put I-frames
  at most cuts). TransNetV2 (MIT, ONNX) = optional accuracy upgrade.
- **Which frames to send:** for each gap, the 1–3 most recent shots
  overlapping or just before it → middle frame of each (Qwen3-VL and
  SmolVLM2 accept multiple images). Skip gaps where nothing changed
  since the last description.

## 6. Writing the line (prompting)

- System prompt from AD guidelines (DCMP/ACB): present tense, third
  person, describe what is seen, no interpretation, no "we see", no
  camera talk, names only once known.
- Pass the gap length → word budget (`floor(seconds × 2.6)`), plus the
  previous 2–3 descriptions for continuity.
- If TTS duration > gap: ask once for a shorter rewrite, else speed up
  TTS ≤ 1.15×, else drop. Never overlap dialogue.
- Optional context: dialogue transcript of the scene (whisper.cpp, MIT,
  tiny/base model) or embedded subtitles → helps names + plot. Later
  iteration; costs time.

## 7. Output / mixing

Reuse the existing mux path: video copied, AD track = original audio
with narration mixed in and the original ducked −10 dB (30 ms
attack, 300 ms release), AAC, default track; original kept as a:1. Also
write the cue file (`.ad.vtt`) next to the output so the user can edit
lines and re-render without re-running the VLM (Microsoft's
GenerateAD/RenderAD split).

New code goes in **new files** (`describe/` package: `GapFinder`,
`ShotDetector`, `Describer` (JNI to llama.cpp), `Narrator` (TTS),
`AdMixer`). `AudioEngine.kt` / `Patcher.kt` / `Aligner.kt` are being
changed by another session; we only call their public functions and
coordinate if a change there is needed.

## 8. On-device vs. PC-assisted

| | On-device (CMF) | PC-assisted (GTX 1650) |
|---|---|---|
| VLM | Qwen3-VL-2B Q4 / SmolVLM2-500M, CPU | Qwen3-VL-2B fully on GPU, or Qwen3-VL-4B Q4 partly offloaded; via `llama-server` (MIT, separate exe) |
| Est. 2 h movie | 30 min – 2 h | ~5–15 min |
| TTS | system TTS / Kokoro | Kokoro (`kokoro-onnx`, MIT; espeak-ng as separate exe/dll = allowed) |
| Fit | needs ~2–3 GB free RAM, model download | phone uses the existing web app on Wi-Fi |

Recommendation: build the **pipeline once, on-device first** (the ask),
keep model calls behind a `Describer` interface so a PC backend
(`llama-server` on the LAN) can be added later with no other changes.
`adpatch.py` gets the same pipeline in Python for the harness-style
testing and the desktop app.

## 8a. Resume from any point (required)

The app can be killed mid-movie (OOM, thermal, user, reboot), so every
stage checkpoints and is idempotent. Job dir per input, keyed by a
content hash (same idea as `cached_envelope()`), plus `manifest.json`
holding model id, prompt version, TTS voice and stage status:

| Stage | Checkpoint | On restart |
|---|---|---|
| gaps (VAD) | `gaps.json` | skip if present |
| shots | `shots.json` | skip if present |
| describe | `.ad.vtt` + `cues.jsonl`, one line appended + fsync **per cue** | continue after last cue id |
| TTS | `tts/<cue>.wav` per cue, written to temp then renamed | synthesize only missing/stale clips |
| mix + mux | output written to `.part`, renamed when done | redo this stage only (minutes, no AI) |

Changing model/prompt/voice invalidates only the affected stage (a cue
edited in `.ad.vtt` re-renders its TTS clip only). `PatchService`
re-queues unfinished jobs from `JobStore` on next launch; worst-case
loss on a kill = the one cue in flight. Exit check (A4/A5): kill the
app with `adb shell am force-stop` mid-describe and mid-TTS → resumed
output identical to an uninterrupted run.

## 8b. Getting AD audio from existing AD apps

Checked 2026-10-19: [MovieReading](https://www.moviereading.com/)
(Universal Multimedia Access, IT), [Earcatch](https://apps.apple.com/app/id1021220730)
(NL/BE), Actiview → now Spectrum Access (Charter, US), KinoSync, and
Netflix/Disney+ AD.

- All of them are **second-screen players**: you download a
  **narration-only** track inside the app, it listens through the
  mic, syncs by audio fingerprint, and plays the narration in your
  headphones while the film plays elsewhere.
- **No export, no public API, no public catalogue**; tracks are
  licensed from distributors and kept in app-private storage. Netflix
  and Disney+ AD exists only inside their DRM streams.
- Pulling files out of these apps would mean bypassing app protection
  and/or breaking their terms + the distributor's copyright → **out of
  scope; we won't do it.**
- Legit routes: (1) ask the operator for a licence/partnership (Earcatch
  and MovieReading work with distributors, unlikely for a hobby app);
  (2) users keep using those apps *alongside* playback — no patching
  needed; (3) [YouDescribe/YuWA](https://github.com/youdescribe-sfsu/You-Described-We-Archived)
  publishes human AD clips for YouTube videos under CC BY-NC-ND 4.0
  (non-commercial, no derivatives → mixing them into a new file is
  likely a "derivative"; idea/eval data only).
- Useful takeaway: if a user *legitimately* has a **narration-only**
  file (own recording, describer's own work), the A5 mixer can take it
  instead of TTS output: align it with the existing engine, then duck
  and mix. That would be a small extra input mode, not a new pipeline.

## 9. Iteration plan (Android, app 0.5.x)

Each iteration ends with a headless check on the PC or a run on the CMF
(serial `00116646S015275`, model checked first), docs updated, commit +
push. Test clips: 2–3 short public-domain/CC films (e.g. Blender open
movies, CC-BY) kept local.

| Iter | Theme | Exit criteria |
|---|---|---|
| A1 ✅ | **Spike: llama.cpp on the CMF** — build `libmtmd` for arm64 via NDK, JNI wrapper, load SmolVLM2-500M + Qwen3-VL-2B, caption one bitmap | measured s/frame + peak RAM for both models recorded here; no crash on 5.3 GB — **done**, see §3 "Measured" |
| A2 | **Gaps** — Silero VAD (ONNX Runtime) on the existing 8 kHz decode → gap list; debug overlay in logs | on 3 test clips, ≥ 90 % of hand-marked gaps found, < 5 % of gaps overlap speech |
| A3 | **Shots + frame picking** — MediaCodec → small frames, cut detector, gap → frames | cut recall ≥ 85 % on test clips; 2 h movie scanned < 10 min |
| A4 | **Describe** — prompts, word budget, context, rewrite loop; per-cue checkpoint (8a) | lines fit their gaps; spot review of 30 lines: ≥ 80 % correct + useful; force-stop mid-run resumes at the next cue |
| A5 | **Speak + mix + mux** — system TTS → PCM, ducking, AAC, default track; UI "Generate AD" job in `PatchService` with progress + cancel | playable output on the CMF and VLC; no narration over dialogue; original track kept |
| A6 | **Natural voice** — Kokoro ONNX + dictionary G2P, optional download | RTF measured; A/B vs. system TTS |
| A7 | **Names + context (optional)** — whisper.cpp transcript / subtitles into the prompt | fewer "a man/a woman" in spot review |
| P1 | **PC path** — same pipeline in `adpatch.py` + `llama-server` on GPU; web/desktop "Generate AD" | 2 h movie < 15 min on dev PC |

A1 build notes: `android/app/src/main/cpp/` (CMake fetches llama.cpp
at a pinned commit + SHA-256, links `mtmd`+`llama` statically into
`libadpvlm.so`, 7 MB; APK 13 MB). `ndk.abiFilters = arm64-v8a`, so the
APK no longer installs on 32-bit-only phones — fine for now; revisit
(stub lib or dynamic feature) before a public release. Dev probe:
`describe/VlmProbeActivity` (adb-only via DUMP permission) reads
`files/models/*.gguf` + `files/frames/*.jpg`, logs to tag `ADP-VLM`.

Risks: RAM (Qwen3-VL-2B + decode buffers on 5.3 GB; fall back to
SmolVLM2), thermal throttling on hour-long runs (pace + resume from the
`.ad.vtt` checkpoint), hallucinated details (keep sentences short and
literal; editable cue file), espeak-ng GPL trap (dictionary G2P only).
