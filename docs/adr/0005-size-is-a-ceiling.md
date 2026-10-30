# 0005 — Fit-to-size is a ceiling, not a target

Date: 2026-10-30 · Status: Accepted

## Context

"Fit to 2 GB" set the video bitrate from size ÷ duration, so a 720p and
a 1080p episode both came out at ~1.93 GB (2 GB × 0.97 margin): the
720p wasted bits it couldn't show, and every file was as big as allowed.

## Decision

For `hevc_nvenc` and `libx265`: encode at constant quality
(`QUALITY_CQ` 24; x265 CRF 23) with `-maxrate` = the size budget's
bitrate (`-bufsize` 2×). The budget only caps; the overshoot retry stays.
`hevc_qsv` / `hevc_amf` keep the average-bitrate target (no hardware
here to verify their quality-capped modes).
Speed priority drops NVENC multipass (kept for "best picture for the size").

## Consequences

- Test footage at a 0.3 GB cap: 0.155 GB (was ~0.28 GB).
- Hard scenes can't borrow bits beyond the cap, which costs a little
  quality versus an average target at the same size; the cap is what
  the user asked for.
