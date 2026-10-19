# 07 — Shutdown button + desktop app (Iterations 1 & 6)

## 7.1 Stop-server button (Iteration 1, web app)

- `POST /api/shutdown`:
  - **Only from localhost** (`request.client.host in {"127.0.0.1", "::1"}`)
    — a phone on the LAN must not be able to kill the PC's server. LAN
    clients get 403 and the button is hidden for them (`GET /api/info`
    returns `{"can_shutdown": bool}`).
  - If jobs are running → 409 with the job list; UI asks "N jobs running
    — stop anyway?" and retries with `?force=1` (kills their ffmpeg
    children).
  - Sets `server.should_exit = True` on the `uvicorn.Server` instance
    (switch `uvicorn.run(app, ...)` to an explicit
    `uvicorn.Server(uvicorn.Config(...))` so we hold a reference), after
    returning the response (FastAPI `BackgroundTasks`).
- UI: "Stop server & close" button. Then `window.close()` — browsers only
  allow it for script-opened tabs, so always replace the page with a
  "Server stopped — you can close this tab" screen first.
- Port is released when uvicorn exits; the `.bat` window closes (drop the
  trailing `pause` on clean exit: `if errorlevel 1 pause`).
- `jobs/` cleanup on exit: delete finished job folders older than 24 h
  (outputs already downloaded) — keep unfinished ones.

## 7.2 Desktop shell: pywebview (Iteration 6)

Choice: **pywebview** (BSD-3, active, 6k★) wrapping the existing FastAPI
UI — one UI codebase for PC desktop, PC browser and phones. Uses the
Edge WebView2 runtime that ships with Windows 10/11.

Rejected:
- PySide6/PyQt (LGPL/GPL; second UI codebase to maintain; +150 MB).
- Electron/Tauri (Node/Rust toolchains for a Python app).
- Nuitka (now AGPL-3.0).

Design:
- `desktop.py` entry point: start uvicorn on `127.0.0.1:<free port>` in a
  thread → `webview.create_window("AD Patcher", url, width=1000,
  height=760)` → `webview.start()`. Closing the window stops uvicorn
  (same path as 7.1) — no orphaned server.
- Optional "Share to phone" toggle in the desktop UI: rebinds to
  `0.0.0.0:8756` and shows the QR code in the window (today the QR is
  printed in the terminal only).
- **Native file pickers instead of uploads.** In desktop mode the UI
  calls `window.pywebview.api.pick_files()` (pywebview JS bridge →
  `create_file_dialog`) and gets real local paths; new endpoint
  `POST /api/patch_local {video, ad, options}` (localhost-only) runs on
  the files in place. No multi-GB copy into `jobs/` → saves a full read
  + write of the video (big speed win for the desktop path). Output goes
  next to the video (or chosen folder), like the CLI.
- Drag-and-drop files onto the window: pywebview exposes full paths on
  drop events (`pywebviewFullPath`) on Windows.
- Browser mode keeps uploads (phones).

## 7.3 AudioVault window (Iteration 6, optional)

Mirror Android `VaultActivity` (plain WebView + user's own login, no
scraping, no stored credentials):
- Second pywebview window → `https://audiovault.net`.
- `webview.settings["ALLOW_DOWNLOADS"] = True` → downloads land in the
  user's Downloads folder; watch that folder (poll every 2 s while the
  window is open) for a new audio file and auto-fill the AD slot.
- Cookies persist via pywebview `private_mode=False` +
  `storage_path=%LOCALAPPDATA%\ADPatcher\webview`.
- No credential handling by the app; the user logs in in the window.

## 7.4 Packaging: PyInstaller (Iteration 6)

- PyInstaller (GPL-2.0 + bootloader exception → output may be MIT).
  Supports Python 3.14 as of 6.16; pywebview's Windows backend needs
  `pythonnet` (MIT, supports 3.14 since 3.1).
- **One-folder** build (`--onedir`), not one-file: one-file unpacks
  ~100 MB to `%TEMP%` on every launch (slow, and AV false positives).
- Include: `imageio_ffmpeg/binaries/ffmpeg-*.exe` (`--collect-data
  imageio_ffmpeg`), uPlot static files, app icon (reuse Android icon
  artwork at 256 px `.ico`).
- `desktop.spec` committed; `build/` and `dist/` gitignored. Build stays
  local — **no GitHub release uploads** unless the owner asks.
- Size estimate: Python ~15 MB + numpy ~30 MB + ffmpeg ~80 MB +
  pywebview/pythonnet ~10 MB ≈ 140 MB folder, ~60 MB zipped.
- Antivirus: unsigned PyInstaller exes sometimes trigger Defender
  heuristics. Mitigations: onedir (helps a lot), build the bootloader from
  source if needed. Code signing costs money → out of scope (zero-cost
  constraint).
- Optional installer: Inno Setup (free, custom permissive licence) to
  make Start-menu shortcuts. Not needed for v0.9; zip the folder.

### ffmpeg licence compliance when redistributing

The bundled binary is gyan.dev's **essentials build, configured
`--enable-gpl --enable-version3`** → GPLv3. Shipping it inside our
folder is "mere aggregation" (separate executable, called as a
subprocess) — fine for an MIT app, but we must:
- include ffmpeg's licence text (`licenses/ffmpeg-GPLv3.txt`),
- state the version + where the exact source is
  (gyan.dev publishes the build config and links upstream source),
- not claim ffmpeg is MIT. Documented in [THIRD_PARTY.md](THIRD_PARTY.md).

## 7.5 Python version

Dev venv is 3.14. Pin the desktop build to the same minor version the
deps declare support for (pywebview classifiers list 3.13; test on 3.14
first, fall back to 3.13 via `py -3.13 -m venv .venv-build` if
pythonnet/WebView2 integration misbehaves).

## Verification

- 7.1: localhost button stops server, port free (`netstat -ano |
  findstr 8756` empty); phone gets no button and 403 on direct POST.
- 7.2: patch a 20 GB file via local paths → no copy in `jobs/`, timing
  ≈ CLI timing.
- 7.4: copy `dist/ADPatcher/` to a second PC without Python → runs,
  patches the harness files.
