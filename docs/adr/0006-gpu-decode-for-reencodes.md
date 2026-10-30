# 0006 — Decode and scale on the GPU for NVENC re-encodes

Date: 2026-10-30 · Status: Accepted

## Context

During batch re-encodes the GPU sat at ~40 % while the CPU was at
100 %: ffmpeg decoded and scaled 1080p on the CPU, competing with the
alignment of the next items, so NVENC was starved.

## Decision

When the encoder is `hevc_nvenc`: `-hwaccel cuda -hwaccel_output_format
cuda` on the video input and `scale_cuda` instead of `scale`
(`hw_decode()`, `_scale_filter()`). If the mux fails (input NVDEC
can't decode: codec, bit depth, chroma), retry once on the CPU
(`opts["_hwdec"] = False`).

## Consequences

- 120 s of 1080p → 720p HEVC: 9.7 s → 5.5 s wall; 1080p → 1080p
  unchanged (encoder-bound). CPU is left to the other batch items.
- A failing GPU decode costs one extra attempt before the CPU path.
- Running two encodes at once is not done; measure with the job logs
  first (two-phase / render-ahead batch also parked until measured).
