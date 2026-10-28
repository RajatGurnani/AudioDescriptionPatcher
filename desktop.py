#!/usr/bin/env python3
"""Desktop app: the web UI in its own window (pywebview + Edge WebView2).

    .venv\\Scripts\\python desktop.py        (or ADPatcher.exe when built)

Runs the same FastAPI app as app.py, but on 127.0.0.1 only and a free
port, inside a native window - closing the window stops everything. In
the window the page switches to desktop mode:
  - native file dialogs that remember the last folder separately for
    videos and AD files (stored in %LOCALAPPDATA%\\ADPatcher\\desktop.json);
  - files dropped onto the window arrive as real paths;
  - jobs patch the files in place (no upload copy) and write the output
    next to the video, like the CLI; "Show in folder" reveals it;
  - an AudioVault window (your own login); audio saved to the Downloads
    folder from it is picked up as the AD file automatically.
Phones keep using start_app.bat (app.py), which serves the LAN.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

APPDATA = os.path.join(os.environ.get("LOCALAPPDATA") or
                       os.path.expanduser("~"), "ADPatcher")
os.environ.setdefault("ADPATCH_NO_BROWSER", "1")
os.environ.setdefault("ADPATCH_JOBS_DIR", os.path.join(APPDATA, "jobs"))

import uvicorn  # noqa: E402
import webview  # noqa: E402
from webview.dom import DOMEventHandler  # noqa: E402

import adpatch  # noqa: E402
import app as web  # noqa: E402
import batch  # noqa: E402

SETTINGS = os.path.join(APPDATA, "desktop.json")
VIDEO_TYPES = ("Video files (" + ";".join(
    "*" + e for e in sorted(adpatch.VIDEO_EXTS)) + ")", "All files (*.*)")
AUDIO_TYPES = ("Audio description (" + ";".join(
    "*" + e for e in sorted(batch.AUDIO_EXTS)) + ")", "All files (*.*)")


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    os.makedirs(APPDATA, exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)


def downloads_dir():
    """The user's Downloads folder (it can be moved, so ask the shell)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Explorer\User Shell Folders") as k:
            path = winreg.QueryValueEx(
                k, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
            return os.path.expandvars(path)
    except OSError:
        return os.path.join(os.path.expanduser("~"), "Downloads")


class Api:
    """Exposed to the page as window.pywebview.api.*"""

    def __init__(self):
        self._window = None
        self._settings = load_settings()
        self._vault = None

    def pick_file(self, slot):
        """Native open dialog starting in the slot's last folder."""
        key = f"last_dir_{slot}"
        res = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            directory=self._settings.get(key, ""),
            file_types=VIDEO_TYPES if slot == "video" else AUDIO_TYPES)
        if not res:
            return None
        path = res[0]
        self._settings[key] = os.path.dirname(path)
        save_settings(self._settings)
        return self.file_info(path)

    def pick_folder(self, key="last_dir_batch"):
        res = self._window.create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=self._settings.get(key, ""))
        if not res:
            return None
        self._settings[key] = res[0]
        save_settings(self._settings)
        return res[0]

    def file_info(self, path):
        try:
            return {"path": path, "name": os.path.basename(path),
                    "size": os.path.getsize(path),
                    "dir": os.path.isdir(path)}
        except OSError:
            return None

    def reveal(self, path):
        """Open Explorer with the file selected."""
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def play(self, path):
        """Open the file in the default video player."""
        os.startfile(os.path.normpath(path))

    def open_audiovault(self):
        if self._vault is not None:
            return
        self._vault = webview.create_window(
            "AudioVault - save audio to Downloads", "https://audiovault.net",
            width=1100, height=800)
        self._vault.events.closed += self._vault_closed
        threading.Thread(target=self._watch_downloads, args=(self._vault,),
                         daemon=True).start()

    def _vault_closed(self):
        self._vault = None

    def _watch_downloads(self, vault):
        """While the AudioVault window is open, pick up audio files that
        appear in Downloads (once their size stops changing)."""
        folder, since = downloads_dir(), time.time()
        seen, sizes = set(), {}
        while self._vault is vault:
            time.sleep(2)
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for n in names:
                p = os.path.join(folder, n)
                if p in seen or os.path.splitext(n)[1].lower() \
                        not in batch.AUDIO_EXTS:
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_mtime < since:
                    continue
                if sizes.get(p) == st.st_size and st.st_size > 0:
                    seen.add(p)
                    self._window.evaluate_js(
                        f"adpDropPaths({json.dumps([p])}, 'AudioVault')")
                sizes[p] = st.st_size


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def busy():
    """Names of jobs still working (single jobs + batch)."""
    names = [j["name"] for j in web.jobs.values()
             if j["status"] in ("queued", "running")]
    if web._batch_running():
        names += [os.path.basename(it["output"])
                  for it in web.batch_state["queue"].items
                  if it["status"] in ("pending", "running")]
    return names


def main(selftest=None):
    """Run the app; `selftest(window, api)` (tools/desktop_smoke.py) is
    started on a thread once the page has loaded."""
    os.makedirs(web.JOBS_DIR, exist_ok=True)
    # test hooks: fixed port, and a hidden (never shown) window for
    # headless checks - tools/desktop_smoke.py and the built-exe check
    port = int(os.environ.get("ADPATCH_DESKTOP_PORT") or free_port())
    hidden = bool(os.environ.get("ADPATCH_DESKTOP_HIDDEN"))
    server = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1",
                                           port=port, log_level="warning"))
    web.server = server
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    api = Api()
    win = webview.create_window(
        "AD Patcher", f"http://127.0.0.1:{port}/?desktop=1", js_api=api,
        width=1000, height=860, min_size=(420, 520), hidden=hidden)
    api._window = win

    def on_drop(e):
        paths = [f.get("pywebviewFullPath") for f in
                 e.get("dataTransfer", {}).get("files", [])]
        paths = [p for p in paths if p]
        if paths:
            win.evaluate_js(f"adpDropPaths({json.dumps(paths)})")

    def on_loaded():
        win.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        if selftest:
            threading.Thread(target=selftest, args=(win, api),
                             daemon=True).start()

    def on_closing():
        running = busy()
        if running and not win.create_confirmation_dialog(
                "Jobs still running",
                f"{len(running)} job(s) still running:\n" +
                "\n".join(running[:8]) + "\n\nQuit anyway?"):
            return False   # keep the window open

    win.events.loaded += on_loaded
    win.events.closing += on_closing
    # downloads only matter for the AudioVault window (a Save dialog
    # opens on the user's Downloads folder)
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.start(private_mode=False,
                  storage_path=os.path.join(APPDATA, "webview"))

    # window closed: stop the server; running jobs have ffmpeg children
    # that would outlive us - take down the whole tree
    if busy():
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                       capture_output=True)
    server.should_exit = True


if __name__ == "__main__":
    sys.exit(main())
