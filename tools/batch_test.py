#!/usr/bin/env python3
"""Synthetic test for batch.py: content-based pairing + resumable queue.

    .venv\\Scripts\\python tools\\batch_test.py

Builds a folder of tiny "movies" and AD files whose names say nothing
(%TEMP%/adpatch-batch), runs batch.scan() and checks every pair:
  - 5 movies with differently transformed AD (offset, PAL, NTSC, cut,
    narration), AD files randomly named;
  - two "episodes" sharing an identical 60 s intro;
  - a decoy AD (unrelated audio, similar length) -> must stay unpaired;
  - a video without any AD -> "no match".
Then runs a 2-job queue, interrupts it after job 1 and resumes. Exit 1 on
any failure.
"""

import os
import random
import shutil
import subprocess
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import adpatch  # noqa: E402
import batch  # noqa: E402
import harness  # noqa: E402

GEN_SR = harness.GEN_SR


def ff(*args):
    subprocess.run([adpatch.FFMPEG, "-y", "-v", "error", *args], check=True)


def make_video(path, wav):
    ff("-f", "lavfi", "-i", "color=c=black:s=160x90:r=24", "-i", wav,
       "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
       "-c:a", "pcm_s16le", path)


def make_ad(path, wav, filt):
    ff("-i", wav, "-af", filt, "-c:a", "libmp3lame", "-b:a", "96k", path)


def main():
    work = os.path.join(tempfile.gettempdir(), "adpatch-batch")
    shutil.rmtree(work, ignore_errors=True)
    media = os.path.join(work, "media")
    os.makedirs(media)
    rng = random.Random(7)
    names = iter(rng.sample(range(100, 999), 50))
    expected = {}   # video basename -> AD basename (None = no match)

    movies = {   # title: (seed, minutes, AD filter)
        "The Long Night (2019)": (11, 5.0, "adelay=3000:all=1"),
        "Harbour Lights": (12, 4.6, harness.SPEEDUP),
        "Paper Moons (2021) 1080p": (13, 5.4, harness.SLOWDOWN),
        "Glass River": (14, 4.8, "asplit[p][q];[p]atrim=0:120,asetpts=PTS-"
                                 "STARTPTS[x];[q]atrim=start=150,asetpts="
                                 "PTS-STARTPTS[y];[x][y]concat=n=2:v=0:a=1"),
        "Quiet Signals": (15, 5.2, "volume=0.7"),
    }
    for title, (seed, minutes, filt) in movies.items():
        wav = os.path.join(work, f"{seed}.wav")
        harness.write_wav(wav, harness.soundtrack(seed, minutes))
        make_video(os.path.join(media, title + ".mkv"), wav)
        ad = f"track_{next(names)}.mp3"
        if "asplit" in filt:
            ff("-i", wav, "-filter_complex", f"[0:a]{filt}[o]", "-map",
               "[o]", "-c:a", "libmp3lame", "-b:a", "96k",
               os.path.join(media, ad))
        else:
            make_ad(os.path.join(media, ad), wav, filt)
        expected[title + ".mkv"] = ad

    # two episodes sharing an identical 60 s intro
    intro = harness.soundtrack(21, 1.0)
    for ep, seed in (("S01E01", 22), ("S01E02", 23)):
        body = harness.soundtrack(seed, 4.0)
        wav = os.path.join(work, f"{ep}.wav")
        harness.write_wav(wav, np.concatenate([intro, body]))
        make_video(os.path.join(media, f"Show {ep}.mkv"), wav)
        ad = f"desc_{next(names)}.mp3"
        make_ad(os.path.join(media, ad), wav, "adelay=1500:all=1")
        expected[f"Show {ep}.mkv"] = ad

    # decoy AD (similar length to several movies) + a video with no AD
    wav = os.path.join(work, "decoy.wav")
    harness.write_wav(wav, harness.soundtrack(99, 5.0))
    make_ad(os.path.join(media, "decoy_ad.mp3"), wav, "anull")
    wav = os.path.join(work, "extra.wav")
    harness.write_wav(wav, harness.soundtrack(98, 3.0))
    make_video(os.path.join(media, "Bonus Extra.mkv"), wav)
    expected["Bonus Extra.mkv"] = None

    os.environ["ADPATCH_CACHE_DIR"] = os.path.join(work, "cache")
    rows, unused = batch.scan([media], log=print)
    batch._print_rows(rows, unused)

    fails = []
    for r in rows:
        v = os.path.basename(r["video"])
        got = os.path.basename(r["ad"]) if r["ad"] else None
        want = expected[v]
        if got != want or (want and r["status"] != "matched") or \
                (not want and r["status"] != "no match"):
            fails.append(f"{v}: got {got} ({r['status']}), want {want}")
    if "decoy_ad.mp3" not in [os.path.basename(d) for d in unused]:
        fails.append("decoy AD was paired")

    # queue: 2 jobs, stop after the first, resume
    qpath = os.path.join(work, "queue.json")
    pairs = [(r["video"], r["ad"]) for r in rows
             if r["status"] == "matched"][:2]
    outdir = os.path.join(work, "out")
    q = batch.Queue.create(pairs, outdir, path=qpath)
    q.run(log=lambda *a: None,
          on_update=lambda it: q.stop.set() if it["status"] == "done"
          else None)
    q2 = batch.Queue.load(qpath)
    states = [it["status"] for it in q2.items]
    if states != ["done", "pending"]:
        fails.append(f"after interrupt: {states}")
    q2.run(log=lambda *a: None)
    states = [it["status"] for it in batch.Queue.load(qpath).items]
    if states != ["done", "done"] or not all(
            os.path.exists(it["output"]) for it in q2.items):
        fails.append(f"after resume: {states}")
    # a new queue over existing outputs skips them
    q3 = batch.Queue.create(pairs, outdir, path=qpath)
    if [it["status"] for it in q3.items] != ["skipped", "skipped"]:
        fails.append("existing outputs not skipped")

    print("\n" + ("PASS" if not fails else "FAIL:\n  " + "\n  ".join(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
