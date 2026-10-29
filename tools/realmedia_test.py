#!/usr/bin/env python3
"""Real films with real AudioVault AD tracks (not synthetic): pairing,
alignment and the batch pipeline on actual media.

    .venv\\Scripts\\python tools\\realmedia_test.py [MEDIA_DIR]

MEDIA_DIR (default .local\\testmedia) holds video\\*.mp4 and ad\\*.mp3 -
public-domain films from the Internet Archive and their AD files from
AudioVault, which are NOT in the repo (different cuts and speeds from
the AD's source: the real-world case). Steps:
  1. scan by content (real names): every film pairs with its AD;
  2. the same files as a fake TV show (Test Show S01E01.. / AD named by
     episode): episode-code pairing, one comparison per episode;
  3. patch every pair through the batch pipeline (3 in flight, one
     writer): per film confidence, pieces, speed, offset, timings.
Prints a table; exit 1 if a film pairs wrongly or a patch fails.
"""

import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import adpatch  # noqa: E402
import batch  # noqa: E402


def key(name):
    """Comparable film title from either side's file name."""
    s = os.path.splitext(os.path.basename(name))[0].lower()
    s = re.sub(r"\[.*?\]|\(.*?\)|\b(cn|us|uk)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\bthe\b|\ba\b", " ", s)
    return " ".join(s.split())[:8]


def main():
    media = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, ".local", "testmedia")
    vdir, adir = os.path.join(media, "video"), os.path.join(media, "ad")
    out = os.path.join(media, "out")
    fails = []
    videos = sorted(os.listdir(vdir))
    ads = sorted(os.listdir(adir))
    truth = {}
    for v in videos:
        hit = [a for a in ads if key(a) == key(v)]
        truth[v] = hit[0] if hit else None
    print(f"{len(videos)} videos, {len(ads)} AD files, "
          f"{sum(1 for x in truth.values() if x)} expected pairs\n")

    # 1. real names, content pairing
    t = time.time()
    logs = []
    rows, unused = batch.scan([vdir, adir], log=logs.append)
    print(f"1. scan by content: {time.time() - t:.0f}s, "
          + next(x for x in logs if x.startswith("compared ")))
    for r in rows:
        v = os.path.basename(r["video"])
        got = os.path.basename(r["ad"]) if r["ad"] else None
        ok = got == truth.get(v) and (r["status"] == "matched") == bool(got)
        print(f"   {'ok ' if ok else 'BAD'} {r['status']:<9} "
              f"{r['score']:.2f}  {v}  <-  {got}")
        if not ok:
            fails.append(f"pairing {v}: got {got} ({r['status']}), want "
                         f"{truth.get(v)}")

    # 2. the same files as a TV show
    show = os.path.join(media, "show")
    shutil.rmtree(show, ignore_errors=True)
    os.makedirs(os.path.join(show, "Test Show", "Season 1"))
    os.makedirs(os.path.join(show, "AD"))
    pairs = [(v, a) for v, a in truth.items() if a]
    for i, (v, a) in enumerate(pairs, 1):
        os.link(os.path.join(vdir, v), os.path.join(
            show, "Test Show", "Season 1", f"Test Show S01E{i:02d}.mp4"))
        os.link(os.path.join(adir, a), os.path.join(
            show, "AD", f"Test Show - S01E{i:02d} (AD).mp3"))
    t = time.time()
    logs = []
    rows2, _ = batch.scan([os.path.join(show, "Test Show"),
                           os.path.join(show, "AD")], log=logs.append)
    n = int(next(x for x in logs if x.startswith("compared ")).split()[1])
    bad = [r for r in rows2 if r["status"] != "matched" or
           os.path.basename(r["ad"] or "")[12:18] !=
           os.path.basename(r["video"])[10:16]]
    print(f"\n2. as a TV show: {time.time() - t:.0f}s, {n} comparisons for "
          f"{len(rows2)} episodes, {len(bad)} wrong")
    if bad or n > 2 * len(rows2):
        fails.append(f"show pairing: {len(bad)} wrong, {n} comparisons")

    # 3. patch everything through the batch pipeline
    shutil.rmtree(out, ignore_errors=True)
    q = batch.Queue.create(
        [(os.path.join(vdir, v), os.path.join(adir, a)) for v, a in pairs],
        out, path=os.path.join(media, "queue.json"))
    t = time.time()
    q.run(log=lambda *a: None)
    print(f"\n3. patched {len(pairs)} films in {time.time() - t:.0f}s "
          "(3 in flight, one writer)")
    print(f"   {'film':<34}{'conf':>6} {'band':<13}{'pieces':>6} "
          f"{'speed':>9} {'time':>6}")
    for it in q.items:
        name = os.path.basename(it["video"])[:33]
        if it["status"] != "done":
            print(f"   {name:<34} ERROR {it.get('error', '')[:60]}")
            fails.append(f"patch {name}: {it.get('error', '')[:80]}")
            continue
        speed = next((x for x in it.get("log", []) if "speed factor" in x),
                     "")
        m = re.search(r"speed factor: ([\d.]+)", speed)
        print(f"   {name:<34}{it['confidence']:6.2f} {it['band']:<13}"
              f"{it['pieces']:>6} {m.group(1) if m else '?':>9} "
              f"{it['ended'] - it['started']:5.0f}s")
    print("\n" + ("PASS" if not fails else "FAIL:\n  " + "\n  ".join(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
