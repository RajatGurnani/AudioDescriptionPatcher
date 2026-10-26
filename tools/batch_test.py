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

    fails += parser_test()
    fails += show_test(work)
    print("\n" + ("PASS" if not fails else "FAIL:\n  " + "\n  ".join(fails)))
    sys.exit(1 if fails else 0)


PARSE_CASES = {   # name -> (season, episodes) or None
    "Show.Name.S01E02.720p.mkv": (1, (2,)),
    "show s1e2 webrip.mkv": (1, (2,)),
    "Show S01 E02.mkv": (1, (2,)),
    "Show.S10E120.mkv": (10, (120,)),
    "Show.S01E03.1080p.x265-GRP.mkv": (1, (3,)),
    "Show S01E05 - 720p.mkv": (1, (5,)),
    "Show Name - S01E01-E02 - Pilot (AD).mp3": (1, (1, 2)),
    "Show.S01E01E02.mkv": (1, (1, 2)),
    "Show S01E01-03.mkv": (1, (1, 2, 3)),
    "Show S00E01 Special.mkv": (0, (1,)),
    "Show 1x05.mkv": (1, (5,)),
    "Show - 2x10 - Title.mp3": (2, (10,)),
    "Show Name - Season 1 Episode 9.mp3": (1, (9,)),
    "Show/Season 3/Episode 4.mkv": (3, (4,)),
    "Show/S02/Show Ep 07.mp3": (2, (7,)),
    "Show/Extras/Show Episode 5.mkv": (None, (5,)),
    "Show/Extras/Show E05.mkv": None,          # bare E, no season: ambiguous
    "Show/Season 2/Show E05.mkv": (2, (5,)),
    "Show S01E05-12 Title.mkv": (1, (5,)),
    "Movie 2019 E-AC3 DDP5.1 x265.mkv": None,
    "Show.Dolby.E.5.1.mkv": None,
    "Movie (2019) 1080p x264.mkv": None,
    "Show 1920x1080.mkv": None,
    "Harbour Lights.mkv": None,
    "track_123.mp3": None,
}


def parser_test():
    fails = []
    for name, want in PARSE_CASES.items():
        got = batch.parse_episode(name)
        if got != want:
            fails.append(f"parse {name!r}: got {got}, want {want}")
    return fails


def show_test(work):
    """A whole show: 3 seasons x 8 episodes + a special. Every episode
    shares a 20 s theme intro and 15 s credits; episodes 2+ open with a
    30 s "previously on" cut from the previous episode. Checks:
      A  clean SxxEyy names, AD files in another folder -> all matched
         with ~one comparison per episode; one AD carries a 30 s ad
         break; season 2 AD is PAL sped up;
      C  missing AD (S02E03), extra AD (S04E01), double-episode AD
         (S03E07-E08) -> no match (with a note) / unused;
      B  the same AD files with meaningless names -> all matched by
         content alone."""
    root = os.path.join(work, "show")
    vids = os.path.join(root, "My Show")
    named = os.path.join(root, "AD named")
    rand = os.path.join(root, "AD random")
    for d in (named, rand):
        os.makedirs(d)
    intro = harness.soundtrack(500, 20 / 60)
    credits = harness.soundtrack(501, 15 / 60)
    rng = random.Random(11)
    ids = iter(rng.sample(range(1000, 9999), 60))
    want_a, want_b = {}, {}
    prev_body = None
    bodies = {}
    for s in (1, 2, 3):
        for e in range(1, 9):
            body = harness.soundtrack(1000 + s * 10 + e, 2.5)
            parts = [intro]
            if prev_body is not None:
                parts.append(prev_body[-30 * GEN_SR:])   # previously on
            parts += [body, credits]
            bodies[s, e] = np.concatenate(parts)
            prev_body = body
    special = np.concatenate([intro, harness.soundtrack(1999, 3.0)])

    def episode(s, e, wav_audio, folder):
        wav = os.path.join(work, f"show_{s}_{e}.wav")
        harness.write_wav(wav, wav_audio)
        video = os.path.join(folder, f"My Show S{s:02d}E{e:02d}.mkv")
        os.makedirs(folder, exist_ok=True)
        make_video(video, wav)
        return video, wav

    wavs = {}
    for (s, e), audio in bodies.items():
        video, wavs[s, e] = episode(s, e, audio,
                                    os.path.join(vids, f"Season {s}"))
        want_a[video] = want_b[video] = None
    sp_video, wavs[0, 1] = episode(0, 1, special,
                                   os.path.join(vids, "Specials"))
    want_a[sp_video] = want_b[sp_video] = None
    by_code = {os.path.basename(v)[8:14]: v for v in want_a}

    def ad(code, wav, filt="adelay=1500:all=1"):
        path = os.path.join(named, f"My Show - {code} - Title (AD).mp3")
        make_ad(path, wav, filt)
        return path

    for (s, e), wav in wavs.items():
        code = f"S{s:02d}E{e:02d}"
        if code == "S02E03" or (s == 3 and e in (7, 8)):
            continue                                # missing / double
        filt = harness.SPEEDUP if s == 2 else "adelay=1500:all=1"
        if code == "S01E04":                        # 30 s ad break at 90 s
            brk = os.path.join(work, "adbreak.wav")
            harness.write_wav(brk, harness.soundtrack(777, 0.5))
            path = os.path.join(named, f"My Show - {code} - Title (AD).mp3")
            ff("-i", wav, "-i", brk, "-filter_complex",
               "[0:a]asplit[p][q];[p]atrim=0:90,asetpts=PTS-STARTPTS[x];"
               "[q]atrim=start=90,asetpts=PTS-STARTPTS[y];"
               "[x][1:a][y]concat=n=3:v=0:a=1[o]", "-map", "[o]",
               "-c:a", "libmp3lame", "-b:a", "96k", path)
        else:
            path = ad(code, wav, filt)
        want_a[by_code[code]] = path
        copy = os.path.join(rand, f"{next(ids)}.mp3")
        shutil.copy(path, copy)
        want_b[by_code[code]] = copy
    # double-episode AD + an AD for an episode we don't have
    dbl = os.path.join(work, "dbl.wav")
    harness.write_wav(dbl, np.concatenate([bodies[3, 7], bodies[3, 8]]))
    dbl_ad = os.path.join(named, "My Show - S03E07-E08 - Two Parter (AD).mp3")
    make_ad(dbl_ad, dbl, "anull")
    extra = os.path.join(work, "extra_ep.wav")
    harness.write_wav(extra, harness.soundtrack(4001, 3.5))
    extra_ad = os.path.join(named, "My Show - S04E01 - Next Year (AD).mp3")
    make_ad(extra_ad, extra, "anull")

    fails = []
    for label, folders, want in (("A", [vids, named], want_a),
                                 ("B", [vids, rand], want_b)):
        logs = []
        rows, unused = batch.scan(folders, log=logs.append)
        print(f"\nshow case {label}: " + logs[-1])
        batch._print_rows(rows, unused)
        for r in rows:
            v = r["video"]
            if v not in want:
                fails.append(f"{label}: unexpected video {v}")
                continue
            exp = want[v]
            if r["ad"] != exp or (exp and r["status"] != "matched") or \
                    (not exp and r["status"] != "no match"):
                fails.append(f"{label} {os.path.basename(v)}: got "
                             f"{r['ad'] and os.path.basename(r['ad'])} "
                             f"({r['status']}), want "
                             f"{exp and os.path.basename(exp)}")
        n_scored = int(logs[-1].split()[1])
        if label == "A":
            notes = {os.path.basename(r["video"]): r["note"] for r in rows}
            for code in ("S03E07", "S03E08"):
                if "double" not in notes.get(f"My Show {code}.mkv", ""):
                    fails.append(f"A {code}: no double-episode note")
            for d in (dbl_ad, extra_ad):
                if d not in unused:
                    fails.append(f"A: {os.path.basename(d)} not unused")
            if n_scored > 2 * len(want):
                fails.append(f"A: {n_scored} comparisons for {len(want)} "
                             "videos (episode codes not used?)")
    return fails


if __name__ == "__main__":
    main()
