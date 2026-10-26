#!/usr/bin/env python3
"""Temp-file hygiene checks (plan 13 step 3).

    .venv\\Scripts\\python tools\\cleanup_test.py

1. adpatch.patch muxes into *.partial.* and renames on success: a good
   run leaves only the output; a failed mux (and Ctrl+C) leaves nothing.
2. app.clean_old_jobs: an upload job's folder goes 15 min after its
   download or after 6 h; running jobs and fresh folders stay.
3. a cancelled job ends "cancelled" with no output and no partial.
Exit 1 on any failure.
"""

import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import adpatch  # noqa: E402
import harness  # noqa: E402

os.environ["ADPATCH_NO_BROWSER"] = "1"


def leftovers(folder):
    return [n for n in os.listdir(folder) if ".partial" in n]


def main():
    fails = []
    work = os.path.join(tempfile.gettempdir(), "adpatch-cleanup")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    # tiny pair: 2 min soundtrack, AD delayed 1.5 s
    wav = os.path.join(work, "src.wav")
    harness.write_wav(wav, harness.soundtrack(5, 2.0))
    video = os.path.join(work, "clip.mkv")
    ad = os.path.join(work, "clip_ad.mp3")
    harness.run([adpatch.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=160x90:r=24", "-i", wav, "-shortest",
                 "-c:v", "libx264", "-preset", "ultrafast", "-c:a",
                 "pcm_s16le", video])
    harness.run([adpatch.FFMPEG, "-y", "-v", "error", "-i", wav, "-af",
                 "adelay=1500:all=1", "-c:a", "libmp3lame", ad])

    out = os.path.join(work, "clip.AD.mkv")
    adpatch.patch(video, ad, output=out, log=lambda *a: None)
    if not os.path.exists(out) or leftovers(work):
        fails.append(f"good run: output {os.path.exists(out)}, "
                     f"leftovers {leftovers(work)}")

    # failed mux: ffmpeg wrote part of the file, then exited 1
    real = adpatch.run_ffmpeg

    def broken(cmd, duration=None, progress=None):
        with open(cmd[-1], "wb") as f:
            f.write(b"half a file")
        return 1, "boom"
    adpatch.run_ffmpeg = broken
    out2 = os.path.join(work, "clip2.AD.mkv")
    try:
        adpatch.patch(video, ad, output=out2, log=lambda *a: None)
        fails.append("failed mux didn't raise")
    except RuntimeError:
        pass
    if os.path.exists(out2) or leftovers(work):
        fails.append(f"failed run left files: {leftovers(work)}")

    # interrupted mux (Ctrl+C mid-write)
    def interrupted(cmd, duration=None, progress=None):
        with open(cmd[-1], "wb") as f:
            f.write(b"half a file")
        raise KeyboardInterrupt
    adpatch.run_ffmpeg = interrupted
    try:
        adpatch.patch(video, ad, output=out2, log=lambda *a: None)
    except KeyboardInterrupt:
        pass
    if leftovers(work):
        fails.append(f"interrupted run left files: {leftovers(work)}")
    adpatch.run_ffmpeg = real

    # web job folders
    jobs_dir = os.path.join(work, "jobs")
    os.environ["ADPATCH_JOBS_DIR"] = jobs_dir
    import app
    app.JOBS_DIR = jobs_dir
    now = time.time()
    cases = {   # name: (job status, downloaded ago s, folder age h, kept?)
        "fresh": ("done", None, 0, True),
        "downloaded_now": ("done", 60, 0, True),
        "downloaded_old": ("done", 20 * 60, 0, False),
        "old": ("done", None, 7, False),
        "old_running": ("running", None, 7, True),
        "orphan_old": (None, None, 7, False),   # from an earlier run
    }
    for name, (status, dl, age_h, _) in cases.items():
        path = os.path.join(jobs_dir, name)
        os.makedirs(path)
        open(os.path.join(path, "out.mkv"), "wb").close()
        t = now - age_h * 3600
        os.utime(path, (t, t))
        if status:
            app.jobs[name] = {"status": status, "dir": path,
                              "output": os.path.join(path, "out.mkv")}
            if dl:
                app.jobs[name]["downloaded"] = now - dl
    app.clean_old_jobs()
    for name, (*_, kept) in cases.items():
        if os.path.isdir(os.path.join(jobs_dir, name)) != kept:
            fails.append(f"jobs/{name}: {'deleted' if kept else 'kept'}")
    if app.jobs["downloaded_old"]["output"] is not None:
        fails.append("cleaned job still points at its output")

    # cancel: the job ends "cancelled", no output, no partial
    out3 = os.path.join(work, "clip3.AD.mkv")
    app.jobs["c"] = {"status": "queued", "log": [], "output": None,
                     "error": None, "name": "x", "phase": None, "frac": 0.0,
                     "cancel": True}
    app.run_job("c", video, ad, out3, False, cleanup=False)
    if app.jobs["c"]["status"] != "cancelled" or os.path.exists(out3) or \
            leftovers(work):
        fails.append(f"cancel: status {app.jobs['c']['status']}, output "
                     f"{os.path.exists(out3)}, leftovers {leftovers(work)}")

    print("PASS" if not fails else "FAIL:\n  " + "\n  ".join(fails))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
