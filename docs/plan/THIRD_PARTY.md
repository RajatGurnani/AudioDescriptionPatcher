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

## Voice boost (Android, [11](11-voice-boost.md))

| Project | Licence | Verdict | What for |
|---|---|---|---|
| [RBJ Audio-EQ Cookbook](https://www.w3.org/TR/audio-eq-cookbook/) | W3C note (public formulas) | **Adapt** | biquad coefficients in VoiceBoost.kt |
| [RNNoise](https://github.com/xiph/rnnoise) | BSD-3-Clause | No (for now) | noise suppressor; trained on noise, not music; needs NDK |
| [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet) | MIT / Apache-2.0 | No (for now) | ML speech enhancement via ONNX (+onnxruntime-android, MIT, ~10-15 MB) |
| [DTLN](https://github.com/breizhn/DTLN) | MIT | No (for now) | TFLite denoiser; same caveat |

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
