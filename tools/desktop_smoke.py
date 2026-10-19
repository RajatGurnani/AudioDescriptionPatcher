#!/usr/bin/env python3
"""Headless smoke test of the desktop window (desktop.py): runs the real
window hidden (never shown), drives it from Python, closes it.

    .venv\\Scripts\\python tools\\desktop_smoke.py

Needs the harness media (run tools/harness.py once). Checks: desktop mode
detected, paths dropped via adpDropPaths() fill both slots, a local job
runs to "Done" with a sync verdict and "Show in folder", no Stop button,
then the window closes cleanly. Exit 1 on failure.
"""

import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.stdout.reconfigure(errors="replace")   # emoji in stage texts

result = {"fails": [], "done": False}


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail
                                                  else ""))
    if not ok:
        result["fails"].append(name)


def selftest(win, api):
    try:
        work = os.path.join(tempfile.gettempdir(), "adpatch-desktop-smoke")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        h = os.path.join(tempfile.gettempdir(), "adpatch-harness")
        video = shutil.copy(os.path.join(h, "video_pcm_10m.mkv"), work)
        ad = shutil.copy(os.path.join(h, "ad4_cut_pal_10m.mp3"), work)
        time.sleep(1.5)
        js = win.evaluate_js
        check("desktop mode", js("DESKTOP && !!window.pywebview.api") is True)
        check("no Stop button", js("document.getElementById('stop').hidden")
              is True)
        check("AudioVault button",
              js("!document.getElementById('vault').hidden") is True)
        js(f"adpDropPaths({json.dumps([video, ad])})")
        time.sleep(1.5)
        got = js("[files.video && files.video.path, files.ad && files.ad.path]")
        check("drop fills slots", got == [video, ad], str(got))
        js("go.click()")
        stage = ""
        for _ in range(120):
            time.sleep(1)
            stage = js("stage.textContent") or ""
            if stage.startswith("Done") or "⚠" in stage:
                break
        check("job done", stage.startswith("Done"), stage)
        time.sleep(1.5)
        out = os.path.join(work, "video_pcm_10m.AD.mkv")
        check("output next to video", os.path.exists(out))
        check("Show in folder", js("!document.getElementById('reveal').hidden")
              is True)
        badge = js("document.getElementById('badge').textContent") or ""
        piece = js("document.getElementById('align').textContent") or ""
        check("verdict + pieces", "In sync" in badge and "2 pieces" in piece,
              f"{badge} | {piece}")
        result["done"] = True
    except Exception as e:   # report, then close the window anyway
        check("selftest crashed", False, repr(e))
    finally:
        win.destroy()


if __name__ == "__main__":
    os.environ["ADPATCH_DESKTOP_HIDDEN"] = "1"   # headless: never shown
    import desktop
    t0 = time.time()
    desktop.main(selftest=selftest)
    check("window closed cleanly", result["done"],
          f"{time.time() - t0:.0f}s")
    print("PASS" if not result["fails"] else f"FAIL: {result['fails']}")
    sys.exit(1 if result["fails"] else 0)
