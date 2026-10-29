# Architecture

## The problem

An AD file (audiovault.net etc.) is the movie's full soundtrack with
narration mixed into natural pauses. It shares nearly all audio content
with the video — but typically not its timeline:

- different start point (trimmed studio logos, rip differences),
- speed drift (a 25 fps PAL source plays 4.27% faster than a
  23.976 fps film source),
- occasionally both.

So the whole task reduces to estimating the linear map

```
video_time = a * ad_time + b
```

then remuxing: correct the AD audio by `a` (tempo) and `b` (delay/trim)
and attach it to the untouched video stream.

## Alignment pipeline (identical in Python and Kotlin)

### 1. Onset envelopes, not waveforms

Both audio streams are decoded to mono 8 kHz and reduced to an
"onset-strength envelope": energy per 10 ms frame → log → frame-to-frame
difference → negative values clipped → z-normalized. 100 values per
second.

Why onsets: the AD file has narration *added on top* and may be encoded,
EQ'd and leveled differently. Raw waveform correlation degrades badly
under those differences; the pattern of loudness *increases* (dialogue
starts, doors, music hits) survives them. It also shrinks a 3-hour movie
to ~1M floats, making global FFT correlation cheap.

### 2. Speed scan (the critical step)

Cross-correlating the envelopes directly only works when `a = 1`. With
even 1% drift, early content and late content need different shifts, and
every correlation peak smears into noise — this is the first thing that
failed in testing, in both a synthetic PAL test and any drifted real rip.

So stage 1 grid-searches the speed factor on 20 Hz pooled envelopes:
for each candidate `a` in 0.95 … 1.05 (step 0.002, then refined at step
0.0002 around the winner), time-stretch the AD envelope by `a`, FFT
cross-correlate against the video envelope, and score the highest peak
(normalized). The true factor aligns onsets across the entire runtime
and wins decisively. The winning lag gives an initial `b`.

The video envelope's FFT is computed once and reused across all ~70
candidates.

### 3. Segment refinement

Stretch the full-rate AD envelope by the winning factor — segments are
now internally drift-free — then match ~16 segments of 30 s spread over
the runtime (windowed normalized cross-correlation, ±5 s search,
parabolic sub-frame peak interpolation). A weighted linear fit through
(segment position → matched position), with outlier rejection
(3× median residual), yields the final `a` and `b` with millisecond-level
residuals.

The per-segment scores and residuals are also the **sync report** shown
to the user: it is direct evidence the alignment holds across the whole
movie, and its failure mode (most segments weak) is how wrong-cut AD
files are detected.

### 4. Confidence

`confidence()` (mirrored in `Aligner.confidence()`) folds the fit's
evidence into one 0..1 score — a weighted *product*, so one bad factor
sinks it:

| signal | from | factor → 1 at | weight |
|---|---|---|---|
| `peak` | stage-1 best normalized correlation | ≥ 0.18 | 0.25 |
| `peak_ratio` | best peak ÷ best peak ≥ 2 s away | ≥ 1.65 | 0.15 |
| `coverage` | share of segments scoring > 0.15 | 1.0 | 0.3 |
| `resid_p95_ms` | 95th pct. \|residual\| of fitted segments | ≤ 40 ms (0 at 200) | 0.2 |
| `dur_ratio` | video length ÷ (a × AD length) | within ±3 % | 0.1 |

Bands: good ≥ 0.75, check ≥ 0.45, else likely wrong. Calibrated on the
synthetic harness (correct pairs 0.97–1.0, a 40 s scene cut 0.67,
unrelated audio 0.0) — **not yet on real movies**; revisit the
breakpoints once real pairs have been measured.

### 5. Mux

- **PC (ffmpeg):** `atempo=1/a` (pitch-preserving) unless |a−1| < 5e-5;
  `adelay` for b>0 or `atrim` for b<0; `-c:v copy`; AD resampled to
  48 kHz and encoded AAC 192k as default track — Windows' `aac_mf`
  (≈5× faster than ffmpeg's `aac`, zero priming) with ffmpeg `aac` as
  fallback; for that one, mkv outputs start the AD 1024/48000 s early
  to cancel the priming delay that mkv, unlike mp4's edit list, doesn't
  trim; original audio copied as track 2; subtitles copied
  when the container allows (with automatic retry without them). Output
  MKV.
- **Android (MediaCodec/MediaMuxer):** video samples copied; original
  audio copied if MP4-compatible (AAC/AC3/EAC3); AD decoded and
  resampled in one pass — output frame `j` reads input position
  `((j/rate − b)/a) · in_rate` with linear interpolation — then
  AAC-encoded. Resampling shifts pitch by |a−1| (≤ a few %); acceptable
  on mobile, and exactly zero in the common no-drift case. Output MP4.

### 5b. Output options

`resolve_options()` merges `DEFAULT_OPTIONS` with the caller's (CLI
flags, presets, the app's panel); `build_mux_cmd()` turns them into maps
and codecs using `probe_streams()` (parsed `ffmpeg -i`): kept original
audio copied (converted to AAC only where mp4 can't hold it), subtitles
copied (mkv) or converted to mov_text (mp4, image subs dropped),
external subtitles as extra inputs. AD codec priming that mkv doesn't
trim, measured at sample level: ffmpeg `aac` 1024, `ac3`/`eac3` 256,
`libopus` and `aac_mf` 0 (mp4's edit list trims all of them).
Fit-to-size: video bitrate = (N GB × 8 × 0.985 − audio bits) ÷ duration,
encoder = first working of hevc_nvenc / hevc_qsv / hevc_amf / libx265
(1-frame probe), VBR with 1.5× maxrate; checked before analysis starts.
Everything returns `notes` that the UI and CLI show.

### 6. Different edits: piecewise alignment

When the linear fit leaves ≥ 10 % of its segments unmatched while the
files clearly share a soundtrack — or (PC, option `split_small_cuts`,
default on) its p95 residual exceeds 120 ms, the signature of cuts of a
few seconds that still match inside the 5 s segment search — `_fit_piecewise()` (mirrored in
`Aligner.fitPiecewise`) tries `video = a·ad + b_k` per piece — one
speed, one offset per piece. Adapted from ADSync's lattice + Viterbi
(MIT, THIRD_PARTY_NOTICES.txt):

1. 20 s windows every 10 s; each gets its top-3 video offsets from a
   global 20 Hz normalized cross-correlation.
2. Offsets seen in ≥ 3 windows (± 0.3 s) become **families**; every
   window is then scored at every family (±0.5 s local search) — real
   evidence in every lattice cell.
3. Viterbi picks one family per window; switching costs 1.5× the median
   window score, so a cut needs ~3 windows of better evidence. Runs
   shorter than 3 windows merge into a neighbour. Accepted only if it
   explains ≥ 10 % more windows than any single offset — linear files
   stay linear (harness: no phantom cuts). In the wobble case the
   reference is the best single offset at piecewise precision (±0.5 s),
   not the linear coverage, which counted matches up to 5 s off the line
   ([ADR 0004](adr/0004-split-on-wobbly-sync.md)).
4. Hand-over points: coarse (4 s probes) then fine (1 s probes, 50 ms
   steps) "evidence before vs after" maximisation. Probes matching
   neither offset count as zero and ties go to the latest point, so
   inserted material stays with the earlier piece…
5. …and rendering cuts each piece where the next one starts in the
   video (`plan_pieces()`), dropping it. Holes get the original audio
   (PC). One slope + per-piece intercepts are fitted jointly.

ffmpeg renders it as one graph: per piece `atrim → atempo → 10 ms
fades → adelay`, original-audio gap fills, `amix normalize=0` (pieces
never overlap) and `asetpts=N/SR/TB` (mp4 rejects amix's timestamp-less
last frame with ffmpeg's aac). Android's resampler jumps to each piece's
AD position and outputs silence in gaps.

Harness: cut / insert / adbreaks / cut+PAL all within 0.3 s on the
hand-over and ≤ 20 ms on every 5 s window of the output.

## Known limitation: extended audio description

Some AD productions (common in short-form/educational content, e.g.
W3C's Perspectives videos) pause the video while long descriptions play.
The AD timeline then has content the video timeline simply doesn't have,
and **no** linear (or even piecewise-shift) audio-only fix exists — the
video itself would need frozen-frame insertions. The sync report flags
these ("most segments matched poorly"). Movie-length AD tracks are
normally standard (same-runtime) description and align fine.

## Repo layout

```
adpatch.py       PC engine + CLI (envelope / fit_alignment / patch)
app.py           FastAPI web UI wrapping adpatch.patch (PC + phone)
batch.py         folder mode: discover -> content pairing -> resumable queue
desktop.py       desktop shell: pywebview window over the same FastAPI app
desktop.spec     PyInstaller one-folder build (dist/ and build/ are local)
static/          uPlot (vendored), app icon (tools/make_icon.py)
licenses/        GPLv3 text + ffmpeg notice shipped with the exe
patch.bat        drag-and-drop wrapper
start_app.bat    web app launcher
android/         Kotlin app: AudioEngine (decode+envelope),
                 Aligner (port of fit_alignment), Patcher (MediaMuxer),
                 MainActivity (UI, wake lock, progress)
tools/harness.py synthetic accuracy + timing harness (ground truth known)
tools/batch_test.py  synthetic pairing + queue/resume test
tools/parity_export.py  envelopes + results for the Kotlin parity test
docs/            this file + usage guide; docs/plan/ = roadmap
```

## Batch, disks and parallelism (PC)

- Pairing scores candidate pairs in worker processes (`_score_pairs`,
  in-process below 24 pairs): numpy FFT work holds the GIL, so threads
  gave 1.5×, processes 3× ([ADR 0003](adr/0003-process-pool-pairing.md)).
- `Queue.run` keeps up to 3 items in analyze/align at once (1 below 6
  CPU threads) and lets one write at a time (`write_lock`): the mux and
  any re-encode are disk/GPU bound, alignment is CPU bound
  ([ADR 0002](adr/0002-batch-pipeline-one-writer.md)).
- Hard drives: `is_rotational()` asks Windows for the seek penalty;
  `disk_slot()` allows one reader per HDD (parallel reads seek: ~5 MB/s
  each vs 100–150 MB/s for one). HDD videos are copied to the SSD scratch,
  worked on there and copied back; scans extract only the analysis audio
  track to the scratch ([ADR 0001](adr/0001-ssd-staging-for-hard-drives.md)).

All decisions: [docs/adr](adr/README.md).

## Numbers that matter

- envelope: 8 kHz mono, hop 80 → 100 Hz, ~10 ms alignment resolution
  (+ parabolic interpolation below that). AD sync tolerance is ~100 ms,
  so this is comfortable.
- speed grid: ±5% covers PAL (±4.27%) with margin; 0.0002 fine step ≈
  1.4 s max error over 2 h before stage 2 tightens it.
- segment threshold 0.15 (min to use), 0.12 (weak flag) — chosen from
  real tests: correct matches score 0.3–0.98, wrong-position noise
  < 0.1.
