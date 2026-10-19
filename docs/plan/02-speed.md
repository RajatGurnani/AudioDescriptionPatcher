# 02 — Speed (Iteration 2, v0.5.0)

Prerequisite: baseline timings from [00](00-baseline-and-harness.md).
Every change below is measured against it and must keep alignment
within Δa < 20 ppm, Δb < 10 ms of the baseline on the harness.

## Where the time goes (expected — confirm with 0.1)

| Phase | Bound by | Today |
|---|---|---|
| video envelope | **disk read** of the whole container (ffmpeg demuxes every packet to reach the audio) + audio decode | sequential, single ffmpeg |
| AD envelope | MP3 decode (CPU, small file) | runs *after* video, sequential |
| align | numpy FFTs on ~1M floats | < 1 s |
| mux | disk read of the whole video + write of the whole output + AAC encode of AD | single ffmpeg |

Consequence: for a 20 GB remux on an HDD, video-envelope and mux are
each one full read (~150 MB/s → ~2+ min each). On an SSD with a 2 GB
file, decode CPU matters more.

## 2.1 Parallel envelopes (video ‖ AD) — low risk, do first

- In `patch()`, run `onset_envelope(video)` and `onset_envelope(ad)` in a
  `concurrent.futures.ThreadPoolExecutor(max_workers=2)`.
- Works despite the GIL: the heavy work is in the ffmpeg child process
  and in numpy (releases the GIL); the Python loop only reads pipes.
- Logs from both threads go through `log`, prefixed `[video]` / `[AD]`.
- Expected: total ≈ max(video, AD) instead of sum → ~1.2–1.5×.
- Exceptions: `future.result()` re-raises in the caller; cancel the other
  future (kill its ffmpeg) on failure.

## 2.2 Chunk-parallel decode of one file (port of Android v0.2.0)

Android already does this (`AudioEngine.onsetEnvelopeParallel`): split
the timeline into W ranges, decode each with its own decoder, stitch the
energy arrays on the global 100 Hz grid.

Python version:
- Duration from `ffmpeg -i` stderr (`Duration: hh:mm:ss.xx`) — already
  parsed style in `probe_has_video`; add `probe_duration()`.
- W workers (default `min(4, os.cpu_count()//2)`, configurable), each:
  `ffmpeg -ss <start> -t <len+overlap> -i file -map 0:a:0 -ac 1 -ar 8000 -f f32le -`
  (input-side `-ss` = fast seek).
- **Seam handling:** input seeking lands on a packet boundary, not the
  exact sample. Decode each chunk with 2 s of overlap on the left, then
  locate the exact seam by cross-correlating the overlap's energy frames
  against the previous chunk's tail (lag search ±1 s at 100 Hz), drop
  the duplicate frames. Android avoids this with pts anchoring — ffmpeg
  can do the same with `-copyts` + reading `pts_time` via
  `-af ashowinfo` (heavier); the overlap-correlation method is simpler
  and self-checking (low correlation at a seam → fall back to sequential
  decode and log a warning).
- **When it helps:** CPU-bound decode (SSD, DTS-HD/TrueHD/FLAC tracks —
  lossless decode is expensive). **When it hurts:** HDD (parallel seeks
  thrash). Rule: enable only if the file is on an SSD *or* W is set
  explicitly. Detecting SSD on Windows: `Get-PhysicalDisk` MediaType via
  PowerShell is fragile — instead, auto-tune: time the first 30 s chunk
  sequentially, compare MB/s with a 2-worker probe, pick the faster.
  Keep it simple at first: config flag `decode_workers` (default 1 on
  first ship, flip default after real-world numbers).
- Expected: 2–3× on CPU-bound decode, ~1× (or worse) on HDD.

## 2.3 Envelope cache

- Key: `sha1(abspath + size + mtime_ns + ENVELOPE_VERSION)` where
  `ENVELOPE_VERSION` bumps whenever SR/HOP/onset formula changes.
- Value: `float32` `.npy` (2 h film = 720k frames ≈ 2.9 MB).
- Location: `%LOCALAPPDATA%\ADPatcher\cache\envelopes\` (not the repo,
  not `jobs/`, which the web app clears). LRU-prune to 500 MB.
- Benefits: re-runs with different output options (iteration 7), retries
  after a mux error, batch pairing (iteration 5: pairing pass envelopes
  are reused by the alignment pass — the full-length pairing case costs
  nothing extra).
- Web-app uploads are temp copies (new path each time) → key on
  `size + first/last 1 MB hash` instead of path for uploads, so a
  re-uploaded file still hits.

## 2.4 Don't decode what we don't need

- Add `-vn -sn -dn` to the envelope ffmpeg command: ffmpeg still has to
  read the packets, but skips setting up decoders/parsers for other
  streams. Small win, zero risk.
- `-threads 1` per decoder when chunk-parallel (avoid oversubscription).
- Multi-audio-track files: we use `0:a:0`. Pick the track ffmpeg marks
  `(default)` and in English if present; log which one. (Correctness more
  than speed, but cheap here.)

## 2.5 Mux phase

- Output on a different physical drive than the source halves HDD head
  contention — document it; add an "output folder" option (web/desktop).
- AD encode: native `aac` at 192k is already fast; the cost is the full
  video read/write which is unavoidable with stream copy.
- `atempo` vs `rubberband`: keep `atempo` (fast, good quality for ±5 %);
  `rubberband` (available in the bundled build) only if users report
  artefacts.
- Progress: parse `-progress pipe:1` (`out_time_ms`) and report % to the
  UI (no speedup, but the long phase stops looking frozen).

## 2.6 Seek-sampled analysis — deferred

Decoding only sampled windows would cut the video-envelope read, but
stage 1 (global speed scan) needs the full-length envelope to separate
drift candidates, and piecewise alignment (iteration 4) needs full
coverage to find cut points. Revisit only for the *pairing* pre-pass
([06 §6.2](06-batch-and-pairing.md), iteration 5), where a few minutes per file is enough.

## Verification

- Harness: all linear cases still pass; Δa, Δb vs baseline within limits.
- Timings: record before/after for harness + one real SSD file + one
  real HDD file in BENCHMARKS.md.
- Kill test: cancel a job mid-envelope → no orphan ffmpeg processes
  (Task Manager / `tasklist | findstr ffmpeg`).
