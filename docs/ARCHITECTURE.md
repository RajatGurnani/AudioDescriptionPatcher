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

### 4. Mux

- **PC (ffmpeg):** `atempo=1/a` (pitch-preserving) unless |a−1| < 5e-5;
  `adelay` for b>0 or `atrim` for b<0; `-c:v copy`; AD encoded AAC 192k
  as default track; original audio copied as track 2; subtitles copied
  when the container allows (with automatic retry without them). Output
  MKV.
- **Android (MediaCodec/MediaMuxer):** video samples copied; original
  audio copied if MP4-compatible (AAC/AC3/EAC3); AD decoded and
  resampled in one pass — output frame `j` reads input position
  `((j/rate − b)/a) · in_rate` with linear interpolation — then
  AAC-encoded. Resampling shifts pitch by |a−1| (≤ a few %); acceptable
  on mobile, and exactly zero in the common no-drift case. Output MP4.

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
patch.bat        drag-and-drop wrapper
start_app.bat    web app launcher
android/         Kotlin app: AudioEngine (decode+envelope),
                 Aligner (port of fit_alignment), Patcher (MediaMuxer),
                 MainActivity (UI, wake lock, progress)
docs/            this file + usage guide
```

## Numbers that matter

- envelope: 8 kHz mono, hop 80 → 100 Hz, ~10 ms alignment resolution
  (+ parabolic interpolation below that). AD sync tolerance is ~100 ms,
  so this is comfortable.
- speed grid: ±5% covers PAL (±4.27%) with margin; 0.0002 fine step ≈
  1.4 s max error over 2 h before stage 2 tightens it.
- segment threshold 0.15 (min to use), 0.12 (weak flag) — chosen from
  real tests: correct matches score 0.3–0.98, wrong-position noise
  < 0.1.
