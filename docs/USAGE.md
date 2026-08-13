# Usage Guide

## What you need

1. A video file (`.mp4`, `.mkv`, `.avi`, … anything ffmpeg reads).
2. Its audio-described soundtrack — an audio file (usually `.mp3`) that
   contains the movie's full audio **with narration mixed in**, e.g. from
   [audiovault.net](https://audiovault.net). It does not need to start at
   the same moment or run at the same speed as your video; that's what
   this tool fixes.

## Option 1 — PC, drag and drop

1. One-time setup:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
2. Select the video and the AD file in Explorer (both at once) and drag
   them onto `patch.bat`.
3. Read the sync report it prints (see "Reading the sync report" below).
4. Play the new `<video name>.AD.mkv`. The AD track is the default;
   switch back to the original audio in your player's audio-track menu.

CLI flags (via `adpatch.py`): `-o OUTPUT` to choose the output path,
`--replace` to drop the original audio track entirely.

## Option 2 — phone (or any browser) via the web app

1. Run `start_app.bat` on the PC. Allow Python through the Windows
   firewall if prompted.
2. On the phone: scan the QR code printed in the terminal (or type the
   `http://<ip>:8756` URL). Phone and PC must be on the same Wi-Fi.
3. Choose the video and the AD file, tap **Patch it**, wait, download.

The PC does all processing; the phone just uploads/downloads. Nothing is
sent to the internet.

## Option 3 — Android app (fully on-phone, offline)

1. Install `ADPatcher.apk` from the Releases tab
   (Settings will ask you to allow installing from unknown sources).
2. Open **AD Patcher**, pick the video, pick the AD file, tap **Patch it**,
   choose where to save the output MP4.
3. The screen stays on and the CPU stays awake while it works; progress
   and the sync report appear on screen. A 2-hour movie takes several
   minutes depending on the phone.

## Reading the sync report

Every run prints one line per checked segment:

```
   12.3m ->   12.5m   score 0.54       +1 ms
```

- `score` — how confidently that part of the AD audio was found in the
  video's audio. Above ~0.3 is a solid match; `<- weak match` rows mean
  that segment couldn't be located (silence, credits, or wrong cut).
- residual (`+1 ms`) — how far that segment is from the fitted alignment.
  Single-digit milliseconds = tight sync.

One or two weak rows (usually the credits) are harmless. If **most** rows
are weak, the tool says so — the AD file likely belongs to a different
cut (director's cut, different regional release, or an "extended audio
description" that pauses the video). The output will still be written,
but listen before keeping it.

## Troubleshooting

- **"could not tell which input is the video"** — both files look like
  videos (or neither does). Pass the video first, AD second.
- **Phone can't reach the web app** — same Wi-Fi? Windows firewall
  blocking Python? Try `http://<PC-ip>:8756` typed manually.
- **Android app says original audio can't be copied** — the source
  video's audio codec (e.g. Opus in a webm) doesn't fit in MP4; the
  output then has only the AD track. Use the PC version (MKV output) to
  keep both.
- **Output is out of sync anyway** — check the sync report; if it looked
  good but playback drifts, please open an issue with the report text.
