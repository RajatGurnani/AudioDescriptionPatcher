#!/usr/bin/env python3
"""Batch mode: pair movies with their AD files by audio content, then
patch every pair in a resumable queue.

Command line (also reachable as `adpatch.py batch ...`):
    batch.py <folder> [<folder> ...] [-o OUTDIR] [--yes] [--replace]
    batch.py --resume

File names don't matter: pairs are found by comparing the audio itself.
1. discover: every video and audio file under the folders (already
   patched *.AD.* outputs and clips under a minute are skipped);
2. duration gate: a pair is only considered if the lengths agree within
   15 % (covers PAL speedup, trimmed intros, cut or added scenes);
3. score: the alignment stage-1 speed scan on the two onset envelopes -
   its correlation peak says "same soundtrack" (~0.1-1) or not (~0.05);
4. assign one-to-one, best first; a pair is "matched" only if it clearly
   beats every competitor in its row and column, else "check" (the user
   confirms). File-name similarity only breaks near-ties.

Envelopes go through adpatch's cache, so the full alignment that follows
pairing doesn't decode anything again. The queue is saved to
%LOCALAPPDATA%\\ADPatcher\\queue.json after every step; an interrupted
batch resumes where it stopped.
"""

import argparse
import difflib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import adpatch

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".flac", ".ogg",
              ".opus", ".mka", ".wma", ".ac3", ".eac3", ".dts"}
MIN_DURATION = 60        # seconds; shorter files are samples/extras
DUR_TOLERANCE = 0.15     # duration ratio gate (PAL 4 % + cut/added scenes)
MIN_PAIR_SCORE = 0.08    # below: these files don't share a soundtrack
UNIQUENESS = 1.5         # best must beat any competitor by this factor
QUEUE_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "ADPatcher", "queue.json")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _is_output(name):
    return re.search(r"\.AD\.(mkv|mp4|m4v)$", name, re.I) is not None


def discover(folders, recursive=True):
    """(videos, ad_candidates): absolute paths found under `folders`."""
    files = []
    for folder in folders:
        folder = os.path.abspath(folder)
        if os.path.isfile(folder):
            files.append(folder)
            continue
        for root, dirs, names in os.walk(folder):
            files += [os.path.join(root, n) for n in names]
            if not recursive:
                break
    videos, ads = [], []
    for f in sorted(set(files)):
        name = os.path.basename(f)
        ext = os.path.splitext(name)[1].lower()
        if _is_output(name):
            continue
        if ext in adpatch.VIDEO_EXTS:
            videos.append(f)
        elif ext in AUDIO_EXTS:
            ads.append(f)
    return videos, ads


def probe_all(paths, workers=4):
    """{path: (duration_s, has_video)} via one `ffmpeg -i` per file."""
    def probe(p):
        return p, (adpatch.probe_duration(p) or 0.0,
                   adpatch.probe_has_video(p))
    with ThreadPoolExecutor(workers) as pool:
        return dict(pool.map(probe, paths))


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------

_NOISE = re.compile(
    r"\b(\d{3,4}p|x26[45]|h\.?26[45]|hevc|avc|aac|ac3|eac3|dts|ddp?5\.?1|"
    r"bluray|blu-ray|brrip|bdrip|web-?dl|webrip|hdrip|dvdrip|remux|hdr|"
    r"10bit|ad|audio ?description|described|audiovault|dv)\b", re.I)


def norm_title(path):
    """Comparable title: lower case, release noise removed (year kept)."""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    stem = re.sub(r"[._\-\[\]()]+", " ", stem)
    stem = _NOISE.sub(" ", stem)
    return " ".join(stem.split())


def name_similarity(a, b):
    return difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


def match_score(ad_env, vid_env):
    """Same-soundtrack score: stage-1 speed-scan peak (coarse + fine grid
    on 20 Hz envelopes), i.e. how well the AD lines up with the video at
    its best speed and offset."""
    pool = 5
    ad20, vid20 = adpatch._pool(ad_env, pool), adpatch._pool(vid_env, pool)
    a, _, _ = adpatch._speed_scan(ad20, vid20,
                                  np.arange(0.95, 1.0501, 0.002))
    _, _, peak = adpatch._speed_scan(
        ad20, vid20, np.arange(a - 0.002, a + 0.00201, 0.0002))
    return float(peak)


def pair_files(videos, ads, envs, durations, progress=None):
    """Score every duration-compatible (video, AD) pair and assign them
    one-to-one. Returns (rows, unused_ads): one row per video with keys
    video, ad (or None), score, status ("matched" / "check" / "no
    match"), alternatives [(ad, score), ...] best first."""
    cand = [(v, d) for v in videos for d in ads
            if durations.get(v) and durations.get(d) and
            abs(durations[v] / durations[d] - 1) <= DUR_TOLERANCE]
    scores = {}
    for i, (v, d) in enumerate(cand):
        scores[v, d] = match_score(envs[d], envs[v])
        if progress:
            progress((i + 1) / max(1, len(cand)))

    def competitors(v, d):
        """Best other score in v's row and d's column."""
        row = [s for (vv, dd), s in scores.items() if vv == v and dd != d]
        col = [s for (vv, dd), s in scores.items() if dd == d and vv != v]
        return max(row + col, default=0.0)

    rows, taken_v, taken_d = {}, set(), set()
    for (v, d), s in sorted(scores.items(), key=lambda kv: -kv[1]):
        if v in taken_v or d in taken_d or s < MIN_PAIR_SCORE:
            continue
        rival = competitors(v, d)
        unique = s >= UNIQUENESS * rival
        if not unique:
            # near-tie: let the file names decide, if they clearly do
            others = [dd for (vv, dd) in scores if vv == v and dd != d]
            best_other_name = max((name_similarity(v, o) for o in others),
                                  default=0.0)
            unique = name_similarity(v, d) - best_other_name >= 0.2
        rows[v] = {"video": v, "ad": d, "score": s,
                   "status": "matched" if unique else "check"}
        taken_v.add(v)
        taken_d.add(d)
    out = []
    for v in videos:
        alts = sorted(((d, s) for (vv, d), s in scores.items() if vv == v),
                      key=lambda x: -x[1])
        row = rows.get(v, {"video": v, "ad": None, "score": 0.0,
                           "status": "no match"})
        row["alternatives"] = alts
        out.append(row)
    return out, [d for d in ads if d not in taken_d]


def scan(folders, recursive=True, log=print, progress=None):
    """Discover, analyze and pair. `progress(phase, frac)` with phases
    "probe", "analyze", "pair". Returns (rows, unused_ads)."""
    videos, ads = discover(folders, recursive)
    log(f"found {len(videos)} video and {len(ads)} audio file(s)")
    info = probe_all(videos + ads)
    if progress:
        progress("probe", 1.0)
    durations = {p: d for p, (d, _) in info.items()}
    # audio-only files with a video extension (e.g. .mp4 AD) count as AD
    ads += [v for v in videos if not info[v][1]]
    videos = [v for v in videos if info[v][1]]
    keep = [p for p in videos + ads if durations[p] >= MIN_DURATION]
    videos = [v for v in videos if v in keep]
    ads = [d for d in ads if d in keep]

    # only analyze files that have at least one duration-compatible partner
    def partners(p, others):
        return any(abs(durations[p] / durations[o] - 1) <= DUR_TOLERANCE
                   for o in others)
    need = [v for v in videos if partners(v, ads)] + \
        [d for d in ads if partners(d, videos)]
    log(f"analyzing {len(need)} file(s) (cached ones are instant)")
    envs, done = {}, [0]
    lock = threading.Lock()

    def analyze(p):
        env = adpatch.cached_envelope(p)
        with lock:
            done[0] += 1
            if progress:
                progress("analyze", done[0] / max(1, len(need)))
        return p, env
    with ThreadPoolExecutor(3) as pool:
        envs = dict(pool.map(analyze, need))
    rows, unused = pair_files(
        [v for v in videos if v in envs], [d for d in ads if d in envs],
        envs, durations,
        progress=(lambda f: progress("pair", f)) if progress else None)
    rows += [{"video": v, "ad": None, "score": 0.0, "status": "no match",
              "alternatives": []} for v in videos if v not in envs]
    unused += [d for d in ads if d not in envs]
    return rows, unused


# --------------------------------------------------------------------------
# Resumable queue
# --------------------------------------------------------------------------

class Queue:
    """Jobs persisted to QUEUE_FILE after every change. Item status:
    pending / running / done / error / skipped (output already there)."""

    def __init__(self, items=None, options=None, path=QUEUE_FILE):
        self.items = items or []
        self.options = options or {}
        self.path = path
        self.lock = threading.Lock()
        self.stop = threading.Event()

    @classmethod
    def create(cls, pairs, outdir=None, replace=False, path=QUEUE_FILE):
        items = []
        for i, (video, ad) in enumerate(pairs):
            stem = os.path.splitext(os.path.basename(video))[0]
            out = os.path.join(outdir or os.path.dirname(video),
                               stem + ".AD.mkv")
            items.append({"id": i, "video": video, "ad": ad, "output": out,
                          "status": "skipped" if os.path.exists(out)
                          else "pending",
                          "phase": None, "frac": 0.0})
        q = cls(items, {"outdir": outdir, "replace": replace,
                        "created": time.time()}, path)
        q.save()
        return q

    @classmethod
    def load(cls, path=QUEUE_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        q = cls(data.get("items", []), data.get("options", {}), path)
        for it in q.items:   # interrupted mid-job: run it again
            if it["status"] == "running":
                it["status"], it["phase"], it["frac"] = "pending", None, 0.0
        return q

    def save(self):
        with self.lock:
            data = json.dumps({"items": self.items,
                               "options": self.options}, indent=1)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, self.path)   # atomic: never a half-written queue

    @property
    def pending(self):
        return [it for it in self.items if it["status"] == "pending"]

    def run(self, log=print, on_update=None):
        """Patch every pending item in order (sequential: the mux is
        disk-bound, parallel muxes on one disk just thrash)."""
        for it in self.items:
            if self.stop.is_set():
                break
            if it["status"] != "pending":
                continue
            it.update(status="running", phase=None, frac=0.0,
                      started=time.time())
            self.save()
            log(f"[{it['id'] + 1}/{len(self.items)}] "
                f"{os.path.basename(it['video'])}")

            def progress(phase, frac, it=it):
                it["phase"], it["frac"] = phase, frac
                if on_update:
                    on_update(it)
            try:
                os.makedirs(os.path.dirname(it["output"]), exist_ok=True)
                res = adpatch.patch(it["video"], it["ad"],
                                    output=it["output"],
                                    replace=self.options.get("replace"),
                                    log=log, progress=progress)
                it.update(status="done", confidence=res["confidence"],
                          band=res["band"], warnings=res["warnings"],
                          pieces=len(res["pieces"]) or 1)
            except Exception as e:   # one bad pair must not stop the batch
                it.update(status="error", error=str(e))
                log(f"  error: {e}")
            it["ended"] = time.time()
            self.save()
            if on_update:
                on_update(it)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_rows(rows, unused):
    print(f"\n{'#':>2}  {'status':<9} {'score':>5}  video  <-  AD")
    for i, r in enumerate(rows, 1):
        ad = os.path.basename(r["ad"]) if r["ad"] else "-"
        print(f"{i:>2}  {r['status']:<9} {r['score']:5.2f}  "
              f"{os.path.basename(r['video'])}  <-  {ad}")
    if unused:
        print("unpaired AD files: " +
              ", ".join(os.path.basename(d) for d in unused))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="adpatch.py batch",
        description="Pair movies with AD files by audio content and patch "
                    "them all (resumable).")
    p.add_argument("folders", nargs="*", help="folders (or files) to scan")
    p.add_argument("-o", "--outdir", help="write outputs here (default: "
                   "next to each video)")
    p.add_argument("--replace", action="store_true",
                   help="drop the original audio tracks")
    p.add_argument("--yes", action="store_true",
                   help="don't ask; process pairs with status 'matched' "
                        "(rows marked 'check' need you to confirm)")
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--resume", action="store_true",
                   help="continue the last interrupted batch")
    args = p.parse_args(argv)

    if args.resume:
        q = Queue.load()
        if not q or not q.pending:
            sys.exit("nothing to resume")
    else:
        if not args.folders:
            p.error("give at least one folder (or --resume)")
        rows, unused = scan(args.folders, not args.no_recursive)
        _print_rows(rows, unused)
        chosen = [r for r in rows if r["status"] == "matched"]
        checks = [r for r in rows if r["status"] == "check"]
        if not args.yes and checks:
            ans = input(f"\ninclude the {len(checks)} 'check' row(s) "
                        "too? [y/N] ").strip().lower()
            if ans == "y":
                chosen += checks
        if not chosen:
            sys.exit("no pairs to process")
        if not args.yes:
            if input(f"patch {len(chosen)} pair(s)? [y/N] ").strip() \
                    .lower() != "y":
                sys.exit("cancelled")
        q = Queue.create([(r["video"], r["ad"]) for r in chosen],
                         args.outdir, args.replace)

    bar = adpatch.ConsoleProgress() if sys.stderr.isatty() else None
    try:
        q.run(log=bar.log if bar else print,
              on_update=(lambda it: bar.update(it["phase"], it["frac"])
                         if it["phase"] else None) if bar else None)
    except KeyboardInterrupt:
        q.save()
        sys.exit("\ninterrupted - continue later with: adpatch.py batch "
                 "--resume")
    done = [it for it in q.items if it["status"] == "done"]
    print(f"\n{len(done)}/{len(q.items)} done")
    for it in q.items:
        extra = (f"{it.get('band', '')} {it.get('confidence', 0):.2f}"
                 if it["status"] == "done" else it.get("error", ""))
        print(f"  {it['status']:<8} {os.path.basename(it['output'])}  "
              f"{extra}")


if __name__ == "__main__":
    main()
