# 13 — Standalone PC app (no upload/download)

> **Status: CLOSED (2026-10-28).** Step 5 (silent start) found and
> fixed: the mkv had ~16 s of video before any audio (ffmpeg's 10 s
> interleave leeway) - now `-max_interleave_delta` 60 s (e343aa7),
> confirmed by the owner. Fit-to-size review fixes (cfd3346) and a
> "best picture for the size" priority (f9d2b39) followed.
>
> Earlier: steps 2, 3, 4, 6 done. Step 1 (drop the
> local server for js_api calls) skipped as YAGNI: the desktop app's
> server listens on 127.0.0.1 only and the page already works on paths
> in place, so nothing user-visible would change. Step 2 shipped as
> Cancel + Play; output-folder setting / name collisions skipped (in
> place is the expected behaviour; batch has -o). Tests: cleanup_test.py (partial files,
> job sweeper, cancel), options_test fit cases (incl. lossless + subs),
> desktop_smoke (Play/Cancel/no Download).
>
> Planned 2026-10-26. Owner priority: develop fully on PC
> first; Android paused. Target machine: Ryzen 250 + RTX 5060 Laptop
> (8 GB, NVENC HEVC/AV1).

## Why

On the PC, the web UI makes a round trip that serves no purpose: it uploads
the video to `jobs/<id>/` on the same disk, processes it there, and then
the user downloads the output again. That means a copy of every file on
the way in and another on the way out. `jobs/` is only cleaned when
`app.py` starts (`clean_old_jobs`, > 24 h) and never by the desktop
app, so outputs pile up. The desktop shell (`desktop.py`) already
patches files in place through `/api/patch_local`, but it still runs a
local FastAPI server and shares the upload UI.

## Goal

One Windows app (`ADPatcher.exe` + `desktop.py` from source):
- inputs are real paths (picker, drag-drop, batch folders); nothing is
  copied in
- the output is written next to the video (or to a chosen folder), with
  "Show in folder" / "Open"
- no `jobs/` folder, no upload, no download, no network port
- temp files (piecewise renders, fit-to-size passes) live in a
  per-job temp dir that is deleted when the job ends, on success,
  failure or cancel

The phone/web UI (`app.py` on 0.0.0.0) stays as an *optional* separate
mode for phones on Wi-Fi; it is not part of the PC app.

## Prerequisite

Merge `claude/ai-ad-generation-c6685c` first. That branch adds a
"Generate one with AI" button to the same UI, and reworking the UI
before the merge would conflict.

## Steps

### 1. Bridge without a server
- `desktop.py`: replace the uvicorn server with pywebview `js_api`
  methods: `start_patch(video, ad, opts)`, `job_status(id)`,
  `cancel(id)`, `probe(path)`, `options()/presets()`, `batch_*`. The
  methods call the same functions the FastAPI routes call today. Move
  the route bodies into plain functions in a new `service.py`, shared
  by both `app.py` and `desktop.py`, so nothing is duplicated.
- Page: one `api()` helper in INDEX_HTML that calls
  `window.pywebview.api.*` in desktop mode and `fetch()` otherwise.
  Progress uses polling (as now) or `evaluate_js` pushes.
- Keep `_private` attribute names on `Api` (pywebview walks the public
  ones and causes COM spam).
- Exit: the app works with no port open (`netstat` shows nothing
  listening), and `tools\desktop_smoke.py` passes hidden.

### 2. Desktop UI without upload/download
- Desktop mode hides the upload progress bar, the "Download" link and
  the phone hints.
- Output-location setting: "next to video" (default) / chosen folder,
  remembered in `desktop.json`. Name collision → `name.AD (2).mkv`,
  never overwrite.
- When a job finishes: "Show in folder", "Play" (opens in the default
  player), plus the sync report link.
- Cancel kills the ffmpeg process tree and deletes the partial output.

### 3. Temp-file hygiene (all modes)
- Each job gets `tempfile.mkdtemp(prefix="adpatch-")`, removed in a
  `finally` block.
- Web mode (optional server): delete the job folder once its download
  finishes, plus an hourly sweep for jobs older than 6 h (not only at
  startup).
- Startup sweep of stale `%TEMP%\adpatch-*` left by crashed runs older
  than 24 h.

### 4. Fit-to-size that actually fits
Today's bug: a "2 GB" target gives files over 2 GB. Causes:
- kept audio tracks without a `kb/s` line in `ffmpeg -i` are costed at
  640 kb/s (`adpatch.py:1378`); TrueHD/DTS-HD MA really run 3–5 Mb/s
- subtitles and attachments aren't counted
- VBR with `maxrate` 1.5× and no check of the result

Fix:
- Measure every kept stream's real size: for stream copy, read the packet
  sizes with `ffmpeg -i in -map 0:N -c copy -f null -`, which reports
  the byte count. Cache per file.
- Budget = target × 0.97 − audio − subs − 1 MB of mux overhead per
  hour.
- NVENC: `-rc vbr -multipass fullres` with `-maxrate` 1.2×. After the
  encode, if the file is still over the target, re-encode at
  `rate × target/actual × 0.98` (at most one retry), and say so in
  the notes.
- If the target is impossible with lossless audio kept, offer "convert
  original audio to EAC3 640k" in the error text.
- Exit: `options_test.py` gets fit cases with a fake 4 Mb/s lossless
  track plus subtitles; the output is ≤ target in every case.

### 5. "Audio silent until I skip back" bug
Reported against the previous update and not yet reproduced. Plan:
- Get the file, the player and the container from the owner. Dump
  each stream's first packets with
  `ffmpeg -i out -map 0:a:0 -c copy -f framecrc -` and look at the
  start PTS, any edit list (mp4) and the codec delay.
- Likely causes, in order:
  1. The AD track starts at PTS > 0, from `adelay` padding with
     `asetpts`, or `-avoid_negative_ts` shifting things. Some players
     wait at the start instead of playing the leading silence.
  2. An mp4 edit list with an empty edit, from aac priming, or
     `aac_mf` writing no priming while the muxer still adds an edit.
  3. The voice-boost extra track or the piecewise `amix` starting later
     than the video.
- Fix direction: every audio track starts at exactly PTS 0 (leading
  silence is real samples, not a timestamp gap), plus
  `-avoid_negative_ts make_zero` and `-fflags +genpts` where needed.
- Exit: a harness check that asserts the first audio packet's PTS
  is ≤ 1 frame for every output track, in mp4 and in mkv, for all
  cases (positive offset, negative offset, piecewise, voice boost).

### 6. Package + launchers
- `start_app.bat` launches the desktop app; `start_web.bat` (new) runs
  the optional phone server.
- PyInstaller onedir build, local only (no upload or release).
- Exit: the exe runs hidden with `ADPATCH_DESKTOP_HIDDEN=1` and patches
  a harness pair in place, leaving no `jobs/` folder and no
  `%TEMP%\adpatch-*`.

## Order

5 (bug, needs a sample) can run in parallel. Then 3 → 4 → 1 → 2 → 6.
Hygiene and fit-size are small fixes that ship value immediately. The
bridge rework (1, 2) is the biggest change.

## Not doing

- A native UI (WinUI/.NET/Qt): the HTML UI already works in pywebview,
  and the owner said no.
- Dropping the phone web mode entirely: it's cheap to keep as a
  separate launcher.
