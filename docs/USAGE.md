# Usage Guide

## What you need

1. A video file (`.mp4`, `.mkv`, `.avi`, … anything ffmpeg reads).
2. Its audio-described soundtrack — an audio file (usually `.mp3`) that
   contains the movie's full audio **with narration mixed in**, e.g. from
   [audiovault.net](https://audiovault.net). It does not need to start at
   the same moment or run at the same speed as your video; that's what
   this tool fixes.

## Setup (PC)

Install Python 3.10+ from python.org (tick *Add python.exe to PATH*).
That's all: the first run of `patch.bat` or `start_app.bat` creates
`.venv` and installs everything (needs internet once, ~30 s). Later runs
skip installation unless `requirements.txt` changed. If
[uv](https://github.com/astral-sh/uv) is on PATH it's used instead of
pip, and can even fetch Python itself when none is installed.

## Option 0 — desktop app (PC, recommended)

`.venv\Scripts\python desktop.py` — or `ADPatcher.exe` from a local build
(below). The same page as the web app, in its own window:

- **Choose video / Choose AD file** open Windows' file dialog, which
  remembers the last folder separately for videos and for AD files.
- **Drop** files anywhere on the window (both at once is fine).
- Files are patched **in place** — no upload copy of a multi-GB video;
  the output (`<video>.AD.mkv`) lands next to the video. **Show in
  folder** opens it in Explorer.
- **AudioVault** opens audiovault.net in a second window with your own
  login. Save an audio file to your **Downloads** folder there and it
  is picked up as the AD file automatically.
- Batch mode has **Add folder…** / **Choose output folder…** buttons.
- Closing the window quits; if jobs are still running it asks first.

Phones keep using `start_app.bat` (the web app serves the Wi-Fi).

**Building the exe (local only):**

```
.venv\Scripts\pip install -r requirements-desktop.txt
.venv\Scripts\pyinstaller desktop.spec --noconfirm
```

→ `dist\ADPatcher\ADPatcher.exe` (one folder, ~145 MB, ~60 MB zipped). Copy the
whole `dist\ADPatcher` folder to another Windows 10/11 PC — no Python
needed (the Edge WebView2 runtime ships with Windows). The folder
includes the licences of everything bundled (`_internal\licenses`,
including ffmpeg's GPLv3 notice). The exe is unsigned, so SmartScreen
may ask once ("More info → Run anyway").

What the target PC needs — all part of Windows itself, nothing to
install in the normal case:
- Windows 10 or 11, 64-bit.
- **Edge WebView2 runtime** — preinstalled on Windows 11 and on updated
  Windows 10. On older/LTSC Windows 10 without it the window can't
  open; install Microsoft's free "Evergreen" WebView2 runtime.
- .NET Framework 4.7.2+ (built into Windows 10 1803 and later) — used by
  the window toolkit.
- Media Foundation for the fast AAC encoder — missing on Windows "N"
  editions; the app then falls back to ffmpeg's own (slower) AAC
  encoder automatically.
Python, all libraries, ffmpeg and the Visual C++ runtime are inside the
folder.

## Option 1 — PC, drag and drop

1. Select the video and the AD file in Explorer (both at once) and drag
   them onto `patch.bat`.
   A progress bar shows each step (1/4 … 4/4) as it runs.
2. Read the sync report it prints (see "Reading the sync report" below).
3. Play the new `<video name>.AD.mkv`. The AD track is the default;
   switch back to the original audio in your player's audio-track menu.

CLI flags (via `adpatch.py`): `-o OUTPUT` to choose the output path,
`--replace` to drop the original audio track entirely.

## Option 2 — phone (or any browser) via the web app

1. Run `start_app.bat` on the PC. It opens the page in your browser.
   Allow Python through the Windows firewall if prompted.
2. On the phone: scan the QR code printed in the terminal (or type the
   `http://<ip>:8756` URL). Phone and PC must be on the same Wi-Fi.
3. Choose the video and the AD file — tap each box, or drag both files
   onto the page at once (they're sorted by type) — then **Patch it**.
   A progress bar shows upload speed, then each step (1/4 analyzing
   video … 4/4 writing output) with % and elapsed time; the full log is
   under "log".

On the PC in Chrome/Edge the file dialogs remember the last folder
separately for videos and for AD files, across sessions. (Phones and
Firefox use the standard picker, which can't be told where to start.)

The PC does all processing; the phone just uploads/downloads. Nothing is
sent to the internet.

To stop the server, press **Stop** on the page *on the PC* (phones don't
get the button). If jobs are still running it asks first, then ends them
too. Running `start_app.bat` again while the server is up just reopens
the page. Job folders older than 24 h are cleaned up on start.

## Option 4 — a whole folder at once (batch, PC)

Put movies and AD files in one or more folders — **names don't matter**;
each movie is matched to its AD file by comparing the audio itself.

- **Web app (on the PC's own browser):** the *Batch* card. Paste folder
  paths (one per line), optionally an output folder, **Find pairs**.
  Each row shows the movie, the best AD file with its match score, and
  ✔ matched / ⚠ check / – no match. Change any row's AD in its dropdown
  (unpaired AD files can be picked by hand), untick rows to skip,
  **Patch selected**. Jobs run one after another with progress and a
  sync verdict per movie. **Stop after current** pauses; the batch
  survives closing the app — reopen it and press **Resume batch**.
- **CLI:** `adpatch.py batch D:\Movies D:\AD [-o D:\Out] [--yes]` prints
  the pairing table and asks before starting (`--yes`: process the
  ✔ rows without asking). Ctrl+C stops; `adpatch.py batch --resume`
  continues.

How pairing decides: files whose lengths differ by more than 15 % are
never paired; the rest are scored by how well their audio lines up
(score ≥ 0.08 needed, and a pair must clearly beat every rival —
otherwise it's marked ⚠ check and preselected for you to confirm). File
names only break near-ties. Outputs that already exist are skipped.
Batch is PC-only (phones get the single-file page).

## Option 3 — Android app (fully on-phone, offline)

1. Install `ADPatcher.apk` from the Releases tab
   (Settings will ask you to allow installing from unknown sources).
2. Open **AD Patcher**, pick the video, pick the AD file, tap **Patch it**,
   choose where to save the output MP4.
3. The screen stays on and the CPU stays awake while it works; progress
   and the sync report appear on screen. A 2-hour movie takes several
   minutes depending on the phone.
4. Optional **Voice boost** (Off by default): Low / High adds a third
   audio track, "original audio + voice boost" (clearer dialogue: centre
   emphasis, presence EQ, background ducked while people speak,
   compression). The AD and original tracks are unchanged unless you
   tick **Also boost the AD track**. Pick the track in your player's
   audio menu (track 3).

## Output options

In the web app and the desktop app, open **Output options** above the
Patch button (the CLI has the same as flags — `adpatch.py --help`).
Choices are remembered; **Presets** store whole sets (built-in: Default,
TV-friendly MP4/AC-3, Archive MKV/Opus, Phone 720p ≤ 2 GB, AD only;
**Save as preset…** adds your own).

| Option | What it does |
|---|---|
| Container | MKV holds everything; MP4 plays on more TVs/phones — subtitles become mov_text, image subtitles (PGS/DVD) are dropped, audio MP4 can't hold (e.g. PCM, DTS) is converted to AAC. Each conversion is listed under the result. |
| Default audio | which track players start with: the AD or the original |
| AD codec / bitrate / channels | AAC (default), Opus (small, great quality), AC-3 / E-AC-3 (TV receivers) |
| Original audio | first track (default), all, or none (AD only) |
| Subtitles | keep all or drop |
| Track picks (desktop app) | after choosing the video, every audio and subtitle track is listed with a checkbox |
| Add subtitles | an external .srt/.ass/.vtt with its language |
| Match AD loudness | measures both and levels the AD to the original (±12 dB max; costs one extra decode of each) |
| Size | *fit to N GB* re-encodes the video (HEVC on the GPU when there is one — NVIDIA/Intel/AMD — else CPU) and optionally scales it down; N is an upper limit (simple footage comes out smaller). Refused for HDR video: use HandBrake. Slow compared to the default copy. |

CLI examples:

```
adpatch.py movie.mkv ad.mp3 --preset "TV-friendly (MP4, AC-3, all audio)"
adpatch.py movie.mkv ad.mp3 --keep-audio all --keep-subs none --add-subs extra.srt:spa:forced
adpatch.py movie.mkv ad.mp3 --container mp4 --fit-gb 2 --max-height 720
adpatch.py --list-streams movie.mkv
```

**Hand-offs (desktop app):** after a job, **Open in MKVToolNix** (full
track/chapter editing) and **Open in HandBrake** (advanced re-encodes)
appear when those free programs are installed in their default folders
(or set `ADPATCH_MKVTOOLNIX` / `ADPATCH_HANDBRAKE` to the exe).
HandBrake's window has no "open this file" switch, so Explorer also
opens with the file selected — drag it in.

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

One or two weak rows (usually the credits) are harmless.

Above the rows sits one verdict — `confidence 0.93 (good)` — plus plain
warnings when something's off:

| Verdict | Meaning |
|---|---|
| **good** (≥ 0.75) | in sync across the movie; just watch it |
| **check** (0.45–0.75) | probably mostly right — spot-check the warned part |
| **likely wrong** (< 0.45) | don't trust the output |

**Different edits** (the AD comes from a cut with scenes added or
removed, or ad breaks cut differently) are fixed automatically: the AD
is split into pieces, each placed at its own offset — the report says
e.g. `different edit: 3 pieces`, and dashed lines on the drift chart
mark where each piece starts. Parts of the video the AD doesn't have
play the original audio (PC) or silence (Android); AD material the video
doesn't have is dropped.

Warnings name the cause: files that don't share a soundtrack (wrong AD
file), a different edit that couldn't be fully matched, extended
audio description (pauses the video — can't be fixed by shifting),
repetitive audio (offset could be off by a loop), or sync wobble.
The output is always written; the verdict tells you whether to keep it.

**Seeing it:** the web app shows the verdict as a coloured badge with a
*drift chart* (one dot per 30 s segment; dots inside the green ±50 ms
band are in sync — a step means a cut, a slope means drift) and, under
"Show waveform match", the video's audio onsets above the aligned AD's
below: peaks should line up (drag to zoom). On the CLI, `--report` writes
`<output>.sync.svg` (the same two charts, opens in any browser) and
`<output>.sync.json` (all numbers). The Android app shows the verdict in
the final status line and the full report in Logs.

## Troubleshooting

- **"Python 3.10 or newer is required"** — install it from python.org
  with *Add python.exe to PATH* ticked, then rerun the .bat.
- **Setup failed / no internet on that PC** — on a PC with internet run
  `pip download -r requirements.txt -d wheels`, copy the `wheels` folder
  over, then `.venv\Scripts\pip install --no-index -f wheels -r requirements.txt`.
- **"port 8756 is in use by another program"** — something else holds
  the port; close it (or change `PORT` in `app.py`).

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
