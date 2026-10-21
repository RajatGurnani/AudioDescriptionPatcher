# 12 — PC ↔ Android feature parity (2026-10-21)

PC = CLI + web app + desktop app (`adpatch.py`). Android = `android/`
(com.fuzzypyjamas.adpatcher 0.5.0). Alignment math is mirrored and
JVM-tested (`AlignerParityTest`).

| Feature | PC | Android | Notes |
|---|---|---|---|
| Linear alignment (speed + offset) | ✓ | ✓ | parity test |
| Piecewise alignment (different edits) | ✓ | ✓ | phone splice ≤ 4.4 ms |
| Confidence score + warnings | ✓ | ✓ | |
| Envelope cache | ✓ | ✓ | Android since 0.5.0 |
| Voice boost (dialogue clarity) | ✓ | ✓ | PC = ffmpeg graph with the same parameter table (`voice_boost_graph`); measured with `tools/voiceboost_test.py` |
| AD bitrate / keep or drop original | ✓ | ✓ | Android 128k/192k/256k |
| Resume an interrupted job | batch queue | ✓ | |
| AudioVault in-app | desktop | ✓ | |
| Gap fill with original audio (pieces) | ✓ | ✗ (silence) | Android would need to decode the original too |
| Container choice, subtitle keep/add, track picks | ✓ | ✗ | MediaMuxer writes MP4 only, no subtitles |
| AD codec (Opus/AC-3/E-AC-3) | ✓ | ✗ | MediaCodec AAC only |
| Fit-to-size re-encode (GPU) | ✓ | ✗ | not sensible on a phone |
| Loudness match | ✓ | ✗ | candidate: compute from decoded energy |
| Batch + content pairing | ✓ | ✗ | out of scope on phone (plan 09) |
| Presets, drift charts, report export | ✓ | ✗ | Android shows the verdict line only |
| Hand-offs (MKVToolNix / HandBrake) | desktop | n/a | |

Rule: any new feature lands on both sides or gets a row here explaining
why not.
