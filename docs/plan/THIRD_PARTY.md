# Third-party code, libraries and tools — licence audit

Project licence: **MIT**. Checked 2026-10-19 via the GitHub API (repo
licence field) and PyPI metadata. Re-check before adopting anything.

Legend — **Use**: dependency or vendored file · **Adapt**: re-implement
an algorithm with attribution (MIT/BSD only) · **Run**: call as a separate
program, never link/copy · **Idea**: read for design only, no code ·
**No**: rejected.

## Alignment / audio

| Project | Licence | Verdict | What for |
|---|---|---|---|
| [ADSync](https://github.com/JohnnyTheCoder1/ADSync) | MIT | **Adapt** | candidate lattice + synthetic pass-through candidates, Viterbi offset decode, segment vetting, confidence factors ([03](03-confidence-report.md), [05](05-piecewise-alignment.md)). Attribution comment in code + entry here. |
| [describealign](https://github.com/julbean/describealign) | GPL-3.0 | **Idea** | piecewise rate alignment concept; benchmark reference. No code copied. |
| [audalign](https://github.com/benfmiller/audalign) | MIT | No (for now) | fingerprint/correlation aligners; our onset envelope already beats generic correlation on narrated mixes. Possible pairing fallback if 06 pass 2 proves weak. |
| [SyncSink](https://github.com/JorenSix/SyncSink) | AGPL-3.0 | **No** | Java, AGPL. |
| [align-videos-by-sound](https://github.com/align-videos-by-sound/align-videos-by-sound) | no licence file detected | **No** | unlicensed = all rights reserved; unmaintained since 2019. |
| [ffmpeg-normalize](https://github.com/slhck/ffmpeg-normalize) | MIT (PyPI licence_expression; GitHub API shows NOASSERTION) | Idea | loudness-match option (08) — the 2-pass `loudnorm` recipe is documented ffmpeg usage; no need for the package. |
| [ffmpeg-python](https://github.com/kkroening/ffmpeg-python) | Apache-2.0 | No | unmaintained since 2024; our command builders are simple lists. |

## App / packaging

| Project | Licence | Verdict | What for |
|---|---|---|---|
| numpy | BSD-3 | Use | existing |
| imageio-ffmpeg | BSD-2 (wrapper) | Use | existing; ships the ffmpeg binary below |
| fastapi | MIT | Use | existing |
| uvicorn | BSD-3 | Use | existing |
| python-multipart | Apache-2.0 | Use | missing from requirements today ([01](01-installer-and-deps.md)) |
| qrcode | BSD | Use | existing (optional) |
| [pywebview](https://github.com/r0x0r/pywebview) 6.x | BSD-3 | **Use** | desktop shell ([07](07-desktop-app.md)) |
| pythonnet 3.2 | MIT | Use (transitive) | pywebview Windows backend |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) 6.x | GPL-2.0+ with bootloader exception | **Use (build tool)** | exception explicitly allows bundling apps under any licence |
| [uPlot](https://github.com/leeoniya/uPlot) | MIT | **Use (vendored)** | drift/overlay charts ([04](04-drift-plot.md)); keep its LICENSE next to the files |
| [uv](https://github.com/astral-sh/uv) | Apache-2.0 / MIT | Optional | faster bootstrap if present on PATH ([01](01-installer-and-deps.md)); not vendored |
| scipy | BSD-3 | Optional | only if `linear_sum_assignment` is needed in pairing (06); default plan avoids it |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | MIT | No | stdlib `difflib` is enough for name tie-breaks |
| [Nuitka](https://github.com/Nuitka/Nuitka) | AGPL-3.0 (GitHub) | **No** | — |
| PySide6 / PyQt6 | LGPL-3 / GPL-3 | No | second UI codebase; pywebview reuses the web UI |
| [qbittorrent-api](https://github.com/rmartin16/qbittorrent-api) | MIT | **No** (dropped in iteration 7) | torrent hand-off not built |
| [audiovault-downloader](https://github.com/cartertemm/audiovault-downloader) | no licence file | **No** | own WebView approach instead (mirrors Android `VaultActivity`) |

## External programs (Run only)

| Program | Licence | Notes |
|---|---|---|
| ffmpeg 7.1 "essentials" (gyan.dev, bundled via imageio-ffmpeg) | **GPLv3** (`--enable-gpl --enable-version3`) | aggregation with our MIT app is fine; when redistributing the desktop folder include the GPLv3 text + build/source pointer ([07 §7.4](07-desktop-app.md)). Has nvenc/qsv/amf encoders, `atempo`, `rubberband`, `scale_cuda`. |
| MKVToolNix | GPL-2.0 | hand-off button only |
| HandBrake / HandBrakeCLI | GPL-2.0 | hand-off button only |
| qBittorrent | GPL-2.0 | Web API calls only |

## AI audio description ([10](10-ai-audio-description.md))

Model weights are downloaded at runtime, never committed or bundled.

| Project | Licence | Verdict | What for |
|---|---|---|---|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) (`libmtmd`) | MIT | **Use** (A1, fetched at build, commit `7951f60`) | on-device VLM runtime (`android/app/src/main/cpp`); `llama-server` as separate exe for PC path |
| llama.cpp prebuilt `llama-server` (Vulkan, Windows) | MIT | **Run** (PC path, separate exe) | Qwen3-VL-4B on the GTX 1650; Vulkan preferred over the CUDA build, whose `cudart`/`cublas` DLLs are NVIDIA-proprietary (free, not open) |
| Qwen3-VL-4B-Instruct | Apache-2.0 | **Use (weights, PC)** | PC describer |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) (`onnxruntime-android` 1.22.0) | MIT | **Use** (A2) | pyannote segmentation; later Kokoro, optional Florence-2/TransNetV2 |
| [pyannote segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) via [sherpa-onnx export](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-segmentation-models) | MIT (CNRS) / export script Apache-2.0 | **Use** (A2, int8 ONNX in `assets/`) | speech/dialogue detection for narration gaps |
| [Silero VAD](https://github.com/snakers4/silero-vad) | MIT | No (tested A2) | misses film dialogue under music; kept as a `gaps_ref.py` comparison |
| Qwen3-VL-2B-Instruct | Apache-2.0 | **Use (weights)** | "Best" describer |
| [SmolVLM2](https://github.com/huggingface/smollm) 256M/500M/2.2B | Apache-2.0 | **Use (weights)** | "Fast" describer |
| InternVL3/3.5-1B, Moondream 2, Florence-2 | MIT / Apache-2.0 / MIT | Optional | alternatives |
| [Kokoro-82M](https://github.com/hexgrad/kokoro) | Apache-2.0 | **Use (weights)** | natural voice (optional download) |
| [misaki](https://github.com/hexgrad/misaki) lexicons, CMUdict | Apache-2.0 / BSD | **Use (data)** | dictionary G2P for Kokoro without espeak-ng |
| [microsoft/ai-audio-descriptions](https://github.com/microsoft/ai-audio-descriptions) | MIT | **Adapt** | GenerateAD/RenderAD split, WebVTT cues, fit + duck + mix |
| [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | BSD-3 | **Idea + dev reference** (A3) | adaptive-ratio cut idea (own implementation); `tools/shots_ref.py ref` uses it (+ opencv-python, Apache-2.0) as a dev-only reference, not shipped |
| Windows SAPI (`System.Speech`) | part of Windows | **Use (OS API)** | PC TTS until Kokoro (A6); called through PowerShell, nothing bundled |
| [TransNetV2](https://github.com/soCzech/TransNetV2) | MIT | Optional | learned shot detection |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | MIT | Optional | dialogue transcript for names/context |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Apache-2.0, TTS links espeak-ng | **No** (linked) | pulls GPL-3.0 espeak-ng into the APK |
| [espeak-ng](https://github.com/espeak-ng/espeak-ng) | GPL-3.0 | Run (PC only) | phonemizer as separate exe/dll for `kokoro-onnx`; never linked on Android |
| [Piper](https://github.com/OHF-Voice/piper1-gpl) | GPL-3.0 (old rhasspy/piper MIT, archived) | No | — |
| Qwen2.5-VL-3B | Qwen Research licence | **No** | non-open licence |
| Gemma 3n (MediaPipe / LiteRT-LM) | Gemma Terms of Use | **No** (owner may waive) | runtime is Apache, weights are not open-licensed |
| LFM2-VL | LFM Open License | No | revenue-gated |
| lanad22/VideoDescribe, AutoAD*, UniAD | none / NOASSERTION | Idea | research pipelines |

## Attribution format (for adapted code)

```python
# Adapted from ADSync (https://github.com/JohnnyTheCoder1/ADSync),
# src/adsync/align/warp_decode.py, MIT License, Copyright (c) 2026
# Anilo. Re-implemented for onset envelopes; see
# docs/plan/THIRD_PARTY.md.
```

Plus a `THIRD_PARTY_NOTICES.txt` at repo root (iteration 4) containing
the full MIT licence text of every adapted source, and shipped inside the
desktop build folder.
