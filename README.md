# AudioDescriptionPatcher

Give it a video and its audio-described (AD) audio file (e.g., from
audiovault.net) — it aligns the AD audio to the video automatically and
produces a new file with the AD track built in. Handles different start
offsets (trimmed logos/intros) and speed drift (PAL vs film sources), and
prints a per-segment sync report so you know the result is actually in
sync before you play it.

Three ways to use it:

| Interface | Where | Best for |
|---|---|---|
| `patch.bat` / `adpatch.py` | Windows CLI / drag-drop | quick one-offs on the PC |
| `start_app.bat` (web app) | browser on PC **or phone** (same Wi-Fi) | phone files, no install; **batch**: whole folders, auto-paired by audio (PC) |
| Android app (`android/`) | on the phone itself, offline | fully standalone mobile |

See [docs/USAGE.md](docs/USAGE.md) for the full walkthrough of all three,
and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the alignment
actually works.

## Quick start (PC)

Requires Python 3.10+. ffmpeg is bundled via pip — no separate install.
The first run of `patch.bat` / `start_app.bat` sets up `.venv` and
installs dependencies automatically (internet needed once).

Drag the video **and** the AD file together onto `patch.bat`, or:

```
patch.bat "Movie (2020).mp4" "Movie AD.mp3"
```

Order doesn't matter — it detects which is which. Output:
`Movie (2020).AD.mkv` — video stream untouched, AD audio as default
track, original audio as track 2.

## Quick start (phone via web app)

```
start_app.bat
```

It opens the page on this PC; scan the QR code it prints with your phone
(same Wi-Fi). Upload the two files, wait, download the patched video.
Files never leave your network. Stop the server with the **Stop** button
on the PC's page.

## Quick start (Android app)

Install the APK from the Releases tab (or build from `android/`). Pick
video, pick AD file, choose where to save — done. The app keeps the
device awake during long jobs and shows per-phase progress. Output is MP4
with the AD track first (default) and the original audio kept when its
codec allows.

## Limits worth knowing

- **Extended audio description isn't supported.** If the AD version
  pauses the video to fit long descriptions (common for short-form
  educational AD, e.g. W3C's videos), no time-shift can align it — that
  needs re-editing the video. The sync report detects this and warns
  ("most segments matched poorly"). Movie AD tracks are normally
  same-length mixes and work fine.
- The Android app time-stretch changes pitch by the drift amount (max a
  few %); the PC version uses ffmpeg `atempo`, which preserves pitch.
- **Different edits are handled** (scenes cut or added, differently cut
  ad breaks): the AD is spliced piece by piece. On PC, video stretches
  the AD doesn't cover get the original audio; on Android they're
  silent. If the verdict still says "likely wrong", the AD file is
  probably for another movie or an extended-description version.
