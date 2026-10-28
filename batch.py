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
3. episodes: SxxEyy / 1x02 / "Episode 2" (+ season folder) are read from
   the names; a video is first scored only against AD files with the same
   episode code, then its season, then everything - a cleanly named show
   costs ~one comparison per episode instead of episodes x AD files;
4. score = coverage: the share of 20 s AD segments that line up with the
   video at offsets shared by 2+ segments. A shared theme, recap or
   credits explains only a few segments, so a wrong episode stays low;
5. assign one-to-one, best first; a pair is "matched" only if it clearly
   beats every competitor in its row and column, else "check" (the user
   confirms). Equal episode codes (or, without codes, file-name
   similarity) only break near-ties.

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
MIN_COVERAGE = 0.3       # below: these files don't share a soundtrack
MATCH_COVERAGE = 0.5     # "matched" needs at least this much coverage
UNIQUENESS = 2.0         # ... and must beat any competitor by this factor
SEG_SECONDS = 20         # coverage segment length
MAX_SEGMENTS = 24
FAMILY_TOL = 0.3         # seconds: segment offsets this close agree
SEG_PEAK_RATIO = 1.3     # segment peak vs its next-best peak (noise ~1.1)
QUEUE_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "ADPatcher", "queue.json")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _is_output(name):
    # *.partial.* = an unfinished mux (adpatch renames it when done)
    return re.search(r"(\.AD|\.partial)\.(mkv|mp4|m4v)$", name,
                     re.I) is not None


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


_SXE = re.compile(
    r"(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})(?![0-9p])"
    r"((?:-e?\d{1,3}(?![0-9p])|[ ._]*-?[ ._]*e\d{1,3}(?![0-9p]))*)")
_NXE = re.compile(r"(?<![0-9a-z])(\d{1,2})x(\d{2,3})(?![0-9a-z])")
_SEASON = re.compile(r"(?<![a-z0-9])(?:season|series|staffel|s)[ ._-]*"
                     r"(\d{1,2})(?![0-9])")
_EPISODE = re.compile(r"(?<![a-z0-9])(?:episode|ep|e)[ ._-]*(\d{1,3})"
                      r"(?![0-9p])")


def parse_episode(path):
    """(season, (episode, ...)) from a file name, or None. Season comes
    from the parent folder ("Season 2", "S02") when the name only has an
    episode number; it is None when nothing gives it. Double episodes
    ("S01E01-E02", "S01E01E02") list every episode."""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    m = _SXE.search(stem)
    if m:
        first = int(m.group(2))
        eps = [first]
        for e, num in re.findall(r"(e?)(\d+)", m.group(3)):
            # "-12" without an E is only a range when it's close
            # ("S01E01-03"); "S01E05-12 Title" is episode 5
            if e or 0 < int(num) - first <= 3:
                eps.append(int(num))
        if len(eps) == 2 and 0 < eps[1] - eps[0] <= 3:   # range E01-E03
            eps = list(range(eps[0], eps[1] + 1))
        return int(m.group(1)), tuple(eps)
    m = _NXE.search(stem)
    if m:
        return int(m.group(1)), (int(m.group(2)),)
    m = _EPISODE.search(stem)
    if not m:
        return None
    season = _SEASON.search(stem[:m.start()])
    if not season:
        season = _SEASON.search(
            os.path.basename(os.path.dirname(path)).lower())
    if not season and not m.group(0).startswith("ep"):
        return None   # a bare "E 5" without any season is too ambiguous
    return (int(season.group(1)) if season else None), (int(m.group(1)),)


def same_episode(a, b):
    return bool(a and b and a[1] == b[1] and
                (a[0] == b[0] or a[0] is None or b[0] is None))


def same_season(a, b):
    return bool(a and b and a[0] is not None and a[0] == b[0])


def episode_label(code):
    if not code:
        return ""
    eps = "-".join(f"E{e:02d}" for e in code[1])
    return eps if code[0] is None else f"S{code[0]:02d}{eps}"


def content_score(ad_env, vid_env):
    """Same-soundtrack score in 0..1: coverage. Speed from the stage-1
    scan; then up to MAX_SEGMENTS 20 s AD segments are each searched over
    the whole video, and a segment counts if it matches (score >
    MIN_SEGMENT_SCORE, clear of its next-best peak) at an offset that at
    least one other segment shares. Cut edits and ad breaks just form several offset families;
    a shared intro/recap/credits forms families of one or two segments
    only, and noise agrees with nothing."""
    pool, fps = 5, adpatch.FPS
    ad20, vid20 = adpatch._pool(ad_env, pool), adpatch._pool(vid_env, pool)
    a, _, _ = adpatch._speed_scan(ad20, vid20,
                                  np.arange(0.95, 1.0501, 0.002))
    a, _, _ = adpatch._speed_scan(
        ad20, vid20, np.arange(a - 0.002, a + 0.00201, 0.0002))
    st = adpatch._stretch(ad_env, a)
    seg = int(SEG_SECONDS * fps)
    if len(st) < 3 * seg:      # short file: shorter segments
        seg = max(int(5 * fps), len(st) // 3)
    if len(st) < seg or len(vid_env) < seg + 10:
        return 0.0
    n_seg = max(3, min(MAX_SEGMENTS, len(st) // seg))
    starts = np.linspace(0, len(st) - seg, n_seg).astype(int)

    vid = vid_env.astype(np.float64)
    n = 1 << int(np.ceil(np.log2(len(vid) + seg)))
    V = np.fft.rfft(vid, n)
    valid = len(vid) - seg
    csum = np.concatenate(([0.0], np.cumsum(vid ** 2)))
    vnorm = np.sqrt(np.maximum(csum[seg:seg + valid] - csum[:valid], 0))
    offsets = []
    for u in starts:
        s_ = st[u:u + seg].astype(np.float64)
        cc = np.fft.irfft(V * np.conj(np.fft.rfft(s_, n)), n)[:valid]
        sc = cc / (np.linalg.norm(s_) * vnorm + 1e-9)
        k = int(np.argmax(sc))
        # over a whole video, noise peaks reach the segment threshold too:
        # a real match must also stand clear of the segment's next-best
        # peak (more than 2 s away)
        away = np.abs(np.arange(valid) - k) > 2 * fps
        second = sc[away].max() if away.any() else 0.0
        if sc[k] > adpatch.MIN_SEGMENT_SCORE and \
                sc[k] >= SEG_PEAK_RATIO * second:
            offsets.append((k - u) / fps)
    offsets.sort()
    # few segments (short file): two that agree could be a shared intro
    # alone, so a family needs three
    min_family = 2 if n_seg >= 6 else 3
    agreed, i = 0, 0
    while i < len(offsets):       # families of offsets within FAMILY_TOL
        j = i
        while j + 1 < len(offsets) and offsets[j + 1] - offsets[i] <= \
                FAMILY_TOL:
            j += 1
        if j - i + 1 >= min_family:
            agreed += j - i + 1
        i = j + 1
    return agreed / n_seg


def pair_files(videos, ads, envs, durations, progress=None, stats=None):
    """Score (video, AD) pairs and assign them one-to-one. Returns
    (rows, unused_ads): one row per video with keys video, ad (or None),
    score (coverage), status ("matched" / "check" / "no match"),
    episode, note, alternatives [(ad, score), ...] best first.

    Pass 1 scores each video against the AD files with its episode code
    only. Pass 2 takes the videos that didn't settle and tries the AD
    files pass 1 didn't settle - same season first, then all (duration
    gate always). So an episode without AD costs a few comparisons, not
    one per AD in the show. `stats["scored"]` counts comparisons."""
    codes = {p: parse_episode(p) for p in list(videos) + list(ads)}
    scores = {}

    def gate(v, d):
        return durations.get(v) and durations.get(d) and \
            abs(durations[v] / durations[d] - 1) <= DUR_TOLERANCE

    def best_if_settled(v, tried):
        got = sorted(((scores[v, d], d) for d in tried), reverse=True)
        if got and got[0][0] >= MATCH_COVERAGE and (
                len(got) == 1 or got[0][0] >= UNIQUENESS * got[1][0]):
            return got[0][1]
        return None

    def try_tiers(v, tiers):
        tried = []
        for tier in tiers:
            tier = [d for d in tier if d not in tried and gate(v, d)]
            for d in tier:
                if (v, d) not in scores:
                    scores[v, d] = content_score(envs[d], envs[v])
                tried.append(d)
            best = best_if_settled(v, tried)
            if best:
                return best
        return None

    done_v, done_d = set(), set()
    for i, v in enumerate(videos):           # pass 1: same episode code
        best = try_tiers(v, [[d for d in ads
                              if same_episode(codes[v], codes[d])]])
        if best:
            done_v.add(v)
            done_d.add(best)
        if progress:
            progress(0.5 * (i + 1) / max(1, len(videos)))
    rest = [d for d in ads if d not in done_d]
    for i, v in enumerate(videos):           # pass 2: everything else
        if v not in done_v:
            try_tiers(v, [[d for d in rest
                           if same_season(codes[v], codes[d])], rest])
        if progress:
            progress(0.5 + 0.5 * (i + 1) / max(1, len(videos)))
    if stats is not None:
        stats["scored"] = len(scores)

    def competitors(v, d):
        """Best other score in v's row and d's column."""
        row = [s for (vv, dd), s in scores.items() if vv == v and dd != d]
        col = [s for (vv, dd), s in scores.items() if dd == d and vv != v]
        return max(row + col, default=0.0)

    rows, taken_v, taken_d = {}, set(), set()
    for (v, d), s in sorted(scores.items(), key=lambda kv: -kv[1]):
        if v in taken_v or d in taken_d or s < MIN_COVERAGE:
            continue
        rival = competitors(v, d)
        unique = s >= UNIQUENESS * rival
        others = [dd for (vv, dd) in scores if vv == v and dd != d]
        if not unique and codes[v]:
            # near-tie: equal episode codes decide, if no rival has them
            unique = same_episode(codes[v], codes[d]) and not any(
                same_episode(codes[v], codes[o]) for o in others)
        elif not unique:
            # near-tie, no episode codes: names decide if they clearly do
            best_other_name = max((name_similarity(v, o) for o in others),
                                  default=0.0)
            unique = name_similarity(v, d) - best_other_name >= 0.2
        unique = unique and s >= MATCH_COVERAGE
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
        row["episode"] = episode_label(codes[v])
        row["note"] = ""
        out.append(row)
    return out, [d for d in ads if d not in taken_d]


def note_double_episodes(rows, unused):
    """An unpaired episode whose number sits inside an unused multi-episode
    AD (S01E01-E02; twice as long, so never a pairing candidate) gets a
    note saying so."""
    multi = [(d, parse_episode(d)) for d in unused]
    multi = [(d, c) for d, c in multi if c and len(c[1]) > 1]
    for r in rows:
        c = parse_episode(r["video"])
        if r["ad"] or not c:
            continue
        for d, dc in multi:
            if c[1][0] in dc[1] and dc[0] in (c[0], None):
                r["note"] = (f"{os.path.basename(d)} covers "
                             f"{episode_label(dc)} (double episode) - "
                             "not supported yet")
                break


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
    stats = {}
    rows, unused = pair_files(
        [v for v in videos if v in envs], [d for d in ads if d in envs],
        envs, durations,
        progress=(lambda f: progress("pair", f)) if progress else None,
        stats=stats)
    log(f"compared {stats.get('scored', 0)} video/AD pair(s)")
    rows += [{"video": v, "ad": None, "score": 0.0, "status": "no match",
              "alternatives": [], "episode": episode_label(parse_episode(v)),
              "note": ""} for v in videos if v not in envs]
    unused += [d for d in ads if d not in envs]
    note_double_episodes(rows, unused)
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
    def create(cls, pairs, outdir=None, replace=False, path=QUEUE_FILE,
               options=None):
        opts = adpatch.resolve_options(options, replace)
        ext = "mp4" if opts["container"] == "mp4" else "mkv"
        # with an output folder, mirror the folders below the videos'
        # common root (Show/Season 1/...), so "Season 1/Episode 4" and
        # "Season 2/Episode 4" don't collide
        dirs = [os.path.dirname(os.path.abspath(v)) for v, _ in pairs]
        try:
            common = os.path.commonpath(dirs) if dirs else ""
        except ValueError:            # different drives
            common = ""
        items = []
        for i, (video, ad) in enumerate(pairs):
            stem = os.path.splitext(os.path.basename(video))[0]
            folder = os.path.dirname(video)
            if outdir:
                rel = os.path.relpath(dirs[i], common) if common else \
                    os.path.basename(dirs[i])
                folder = os.path.normpath(os.path.join(outdir, rel))
            out = os.path.join(folder, f"{stem}.AD.{ext}")
            if opts["replace_original"]:
                # same name as the video; already done = it has our track
                out = adpatch.replace_target(video, opts)
                done = adpatch.has_ad_track(video, opts["ad_title"])
            else:
                done = os.path.exists(out)
            items.append({"id": i, "video": video, "ad": ad, "output": out,
                          "status": "skipped" if done else "pending",
                          "phase": None, "frac": 0.0,
                          "episode": episode_label(parse_episode(video))})
        q = cls(items, {"outdir": outdir, "replace": replace,
                        "patch_options": options or {},
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

    def retry_failed(self):
        """Put errored items back in the queue (resume retries them)."""
        for it in self.items:
            if it["status"] == "error":
                it.update(status="pending", phase=None, frac=0.0)
                it.pop("error", None)

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
                                    options=self.options.get(
                                        "patch_options"),
                                    log=log, progress=progress)
                it.update(status="done", confidence=res["confidence"],
                          band=res["band"], warnings=res["warnings"],
                          pieces=len(res["pieces"]) or 1,
                          size=os.path.getsize(it["output"]))
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
        note = f"  ({r['note']})" if r.get("note") else ""
        print(f"{i:>2}  {r['status']:<9} {r['score']:5.2f}  "
              f"{os.path.basename(r['video'])}  <-  {ad}{note}")
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
                   help="continue the last interrupted batch (failed "
                        "items are tried again)")
    adpatch.add_option_args(p)
    args = p.parse_args(argv)
    options = adpatch.options_from_args(args)

    if args.resume:
        q = Queue.load()
        if q:
            q.retry_failed()
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
                         args.outdir, args.replace, options=options)

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
