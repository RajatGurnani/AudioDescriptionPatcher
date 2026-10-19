# 01 — "Runs anywhere" installer + dependencies (Iteration 1, v0.4.1)

## Root cause of the failure on another PC

1. `requirements.txt` lists only `numpy` and `imageio-ffmpeg`. `app.py`
   also imports `fastapi`, `uvicorn` and needs `python-multipart` for
   `File(...)`/`Form(...)` uploads; the QR code uses `qrcode` (optional,
   wrapped in `try`).
2. `start_app.bat` runs `.venv\Scripts\python.exe` directly — on a fresh
   clone the venv doesn't exist, so it fails before any message is shown.
3. Dev venv is Python 3.14; nothing documents a minimum version.

## 1.1 Requirements split

```
requirements.txt          # engine + web app (runtime)
  numpy>=2.0
  imageio-ffmpeg>=0.6
  fastapi>=0.115
  uvicorn>=0.30
  python-multipart>=0.0.9
  qrcode>=7.4

requirements-desktop.txt  # iteration 6 only
  -r requirements.txt
  pywebview>=6.0
  pyinstaller>=6.16
```

- Lower bounds only (no hard pins) for the runtime file; the desktop
  build gets a `pip freeze` lock written next to the build output (local,
  not committed) for reproducibility.
- Optional: `requirements-dev.txt` for harness extras (none needed today).

## 1.2 Self-bootstrapping `start_app.bat` (and `patch.bat`)

Shared helper `tools\ensure_env.bat`, called by both launchers:

1. Find Python: `py -3` (Windows launcher) → `python` → fail with a clear
   message + link to python.org (and note "tick *Add to PATH*").
2. Check version ≥ 3.10 (`python -c "import sys; sys.exit(sys.version_info < (3,10))"`).
3. If `.venv\Scripts\python.exe` missing → `python -m venv .venv`.
4. Install/refresh deps when `requirements.txt` is newer than a stamp
   file `.venv\.req-stamp` (or the stamp is missing):
   `.venv\Scripts\python -m pip install -r requirements.txt` then write
   the stamp. Avoids a pip run on every launch.
5. Sanity import: `python -c "import numpy, imageio_ffmpeg, fastapi, uvicorn, multipart"`
   — on failure print which one and keep the window open (`pause`).
6. `imageio_ffmpeg` downloads nothing at runtime on Windows (binary ships
   in the wheel) — verify, since an offline PC would otherwise fail
   silently.

Alternative considered: **uv** (Apache-2.0/MIT) — `uv venv` + `uv pip
sync` is 10–50× faster and can even fetch a Python itself
(`uv python install`), which solves "no Python installed". Downside: one
more binary to obtain. Plan: support it opportunistically — if `uv` is on
PATH use it, else fall back to stdlib venv+pip. Don't vendor uv.exe.

## 1.3 Clear startup errors in `app.py`

- Wrap imports in `app.py`: on `ImportError` print
  "missing package X — run start_app.bat (it installs dependencies)".
- Port already in use (second launch) → detect, print "already running —
  open http://localhost:8756", and open the browser instead of crashing.

## 1.4 Shutdown button (pulled forward from 07)

See [07-desktop-app.md §7.1](07-desktop-app.md). Small and
self-contained; ships here.

## 1.5 Docs

- README quick start: "double-click `start_app.bat` — first run installs
  everything (needs internet once)".
- USAGE.md: troubleshooting section (no Python, firewall prompt, port in
  use, offline install via `pip download` on another PC).

## Verification

- Fresh clone into a new folder, delete any `.venv`, run
  `start_app.bat` → installs, starts, UI loads.
- Second run → no pip activity (stamp works).
- Edit `requirements.txt` → next run reinstalls.
- Rename python on PATH temporarily → friendly message, window stays open.
- A Windows Sandbox session (built into Win10 Pro/Enterprise only — this
  host is Home, so use a second PC or a VM) for the true clean-machine
  test.
