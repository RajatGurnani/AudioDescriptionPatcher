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
        h = os.path.join(tempfile.gettempdir(), "adpatch-options")
        video = shutil.copy(os.path.join(h, "movie.mkv"), work)
        ad = shutil.copy(os.path.join(h, "movie_AD.mp3"), work)
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
        # track picker: keep both audio tracks, drop the French subtitle
        boxes = js("[...document.querySelectorAll('#o_tracks input')]"
                   ".map(c => c.dataset.t + c.dataset.i + ':' + c.checked)")
        # size breakdown of the chosen video (measured, a few seconds)
        for _ in range(40):
            bd = js("(document.getElementById('o_breakdown') || {})"
                    ".textContent") or ""
            if bd.startswith("In this file"):
                break
            time.sleep(0.25)
        check("track size breakdown", bd.startswith("In this file") and
              "Video" in bd and "Audio" in bd and "Subtitles" in bd, bd)
        # (default subtitles = English only: the French one starts unticked)
        check("track picker lists 2 audio + 2 subs", boxes ==
              ["audio0:true", "audio1:false", "subtitle0:true",
               "subtitle1:false"], str(boxes))
        js("document.querySelector('#o_tracks input[data-t=audio][data-i=\"1\"]').checked = true;"
           "document.querySelector('#o_tracks input[data-t=subtitle][data-i=\"1\"]').checked = false;"
           "O('o_codec').value = 'opus'; syncOptionsUi()")
        js("go.click()")
        stage = ""
        for _ in range(120):
            time.sleep(1)
            stage = js("stage.textContent") or ""
            if stage.startswith("Done") or "⚠" in stage:
                break
        check("job done", stage.startswith("Done"), stage)
        time.sleep(1.5)
        out = os.path.join(work, "movie.AD.mkv")
        check("output next to video", os.path.exists(out))
        import adpatch
        st = adpatch.probe_streams(out)
        got = [(x["type"][0], x["codec"], x["language"]) for x in st
               if x["type"] in ("audio", "subtitle")]
        check("picked tracks in output", got == [
            ("a", "opus", "eng"), ("a", "pcm_s16le", "eng"),
            ("a", "ac3", "spa"), ("s", "subrip", "eng")], str(got))
        check("Show in folder", js("!document.getElementById('reveal').hidden")
              is True)
        check("Play button", js("!document.getElementById('play').hidden")
              is True)
        check("Cancel hidden after the job",
              js("document.getElementById('cancel').hidden") is True)
        check("no Download link", js("document.getElementById('dl').hidden")
              is True)
        badge = js("document.getElementById('badge').textContent") or ""
        piece = js("document.getElementById('align').textContent") or ""
        check("verdict", "In sync" in badge, f"{badge} | {piece}")
        # hand-off: a stand-in "MKVToolNix" (.bat) records what it got
        check("MKVToolNix button",
              js("!document.getElementById('h_mkvtoolnix').hidden") is True)
        js("O('h_mkvtoolnix').click()")
        for _ in range(20):
            time.sleep(0.5)
            if os.path.exists(MARKER):
                break
        got = open(MARKER).read().strip() if os.path.exists(MARKER) else ""
        check("hand-off launched with the output", got.strip('"') == out,
              got)
        # dropping a folder: lands in the batch tab, in the right zone
        folder = os.path.dirname(video)
        win.evaluate_js(f"adpDropPaths({json.dumps([folder])})")
        for _ in range(40):
            z = js("JSON.stringify(zones)") or ""
            if folder.replace("\\", "\\\\") in z:
                break
            time.sleep(0.25)
        check("dropped folder -> batch tab, Videos zone",
              js("zones.video.includes(" + json.dumps(folder) + ")") is True
              and js("document.querySelector('main').classList"
                     ".contains('wide')") is True, z)
        result["done"] = True
    except Exception as e:   # report, then close the window anyway
        check("selftest crashed", False, repr(e))
    finally:
        win.destroy()


MARKER = os.path.join(tempfile.gettempdir(), "adpatch-handoff.txt")

if __name__ == "__main__":
    os.environ["ADPATCH_DESKTOP_HIDDEN"] = "1"   # headless: never shown
    # isolated app data, and a fake MKVToolNix that writes its argument
    os.environ["LOCALAPPDATA"] = os.path.join(tempfile.gettempdir(),
                                              "adpatch-smoke-appdata")
    fake = os.path.join(tempfile.gettempdir(), "fake-mkvtoolnix.bat")
    with open(fake, "w") as f:
        f.write(f'@echo %~1> "{MARKER}"\n')
    if os.path.exists(MARKER):
        os.remove(MARKER)
    os.environ["ADPATCH_MKVTOOLNIX"] = fake
    import desktop
    t0 = time.time()
    desktop.main(selftest=selftest)
    check("window closed cleanly", result["done"],
          f"{time.time() - t0:.0f}s")
    print("PASS" if not result["fails"] else f"FAIL: {result['fails']}")
    sys.exit(1 if result["fails"] else 0)
