"""Reference + evaluation for shot-cut detection (docs/plan/10, A3).

Dev tool, not part of the app.

  # reference cuts: PySceneDetect AdaptiveDetector (BSD-3; needs
  # `pip install scenedetect opencv-python-headless`)
  python tools/shots_ref.py ref MEDIA -o ref.json [--engine ffmpeg]
  # our detector (same features + rules as describe/ShotDetector.kt) on
  # ffmpeg-decoded tiny YUV frames
  python tools/shots_ref.py own MEDIA -o own.json [--dump-scores X.f32]
  # score cuts against the reference (+-TOL s)
  python tools/shots_ref.py eval ref.json own.json

JSON: {"cuts": [t, ...], "duration": s, ...}; ref adds "cut_frames",
own adds per-frame "pts" (eval maps reference frames onto real pts).
"""
import argparse
import json
import subprocess
import sys

import numpy as np

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = "ffmpeg"

# feature grid + rules - keep identical to describe/ShotDetector.kt
GW, GH = 64, 36          # luma grid (block means)
ADAPT_WIN = 2            # neighbours on each side for the adaptive ratio
ADAPT_RATIO = 3.0        # score / mean(neighbour scores) to call a cut
MIN_SCORE = 12.0         # absolute floor (mean |dY|+|dU|+|dV| on 0..255)
MIN_SHOT_S = 0.6         # no two cuts closer than this
TOL_S = 0.25             # eval tolerance


def tiny_frames(path):
    """(times, Y[n,GH,GW], U[n,GH/2,GW/2], V) via ffmpeg area downscale."""
    cmd = [FFMPEG, "-v", "info", "-i", path, "-an", "-vf",
           f"scale={GW}:{GH}:flags=area,format=yuv420p,showinfo",
           "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    p = subprocess.run(cmd, capture_output=True, check=True)
    fs = GW * GH * 3 // 2
    raw = np.frombuffer(p.stdout, np.uint8)
    n = len(raw) // fs
    raw = raw[:n * fs].reshape(n, fs)
    y = raw[:, :GW * GH].reshape(n, GH, GW).astype(np.float32)
    u = raw[:, GW * GH:GW * GH * 5 // 4].reshape(n, GH // 2, GW // 2).astype(np.float32)
    v = raw[:, GW * GH * 5 // 4:].reshape(n, GH // 2, GW // 2).astype(np.float32)
    ts = [float(t) for t in
          __import__("re").findall(rb"pts_time:([0-9.]+)", p.stderr)][:n]
    return np.array(ts), y, u, v


def scores(y, u, v):
    """Content score per frame i (vs frame i-1); score[0] = 0."""
    s = np.zeros(len(y), np.float32)
    s[1:] = (np.abs(np.diff(y, axis=0)).mean((1, 2))
             + np.abs(np.diff(u, axis=0)).mean((1, 2))
             + np.abs(np.diff(v, axis=0)).mean((1, 2)))
    return s


def cuts_from_scores(ts, s):
    cuts, last = [], -1e9
    n = len(s)
    for i in range(1, n):
        nb = [s[j] for j in range(i - ADAPT_WIN, i + ADAPT_WIN + 1)
              if j != i and 1 <= j < n]
        ref = max(sum(nb) / max(len(nb), 1), 1e-3)
        if s[i] >= MIN_SCORE and s[i] / ref >= ADAPT_RATIO \
                and ts[i] - last >= MIN_SHOT_S:
            cuts.append(round(float(ts[i]), 3))
            last = ts[i]
    return cuts


def cmd_own(a):
    ts, y, u, v = tiny_frames(a.media)
    s = scores(y, u, v)
    if a.dump_scores:
        s.astype("<f4").tofile(a.dump_scores)
        ts.astype("<f8").tofile(a.dump_scores + ".t")
    cuts = cuts_from_scores(ts, s)
    dur = float(ts[-1]) if len(ts) else 0.0
    json.dump({"cuts": cuts, "duration": dur,
               "fps": round(len(ts) / max(dur, 1e-9), 3),
               "pts": [round(float(t), 4) for t in ts]}, open(a.o, "w"))
    print(f"{len(cuts)} cuts in {dur:.0f} s ({len(ts)} frames)")


def cmd_ref(a):
    if a.engine == "ffmpeg":
        # ffmpeg's scene score (MAFD-based, independent of ours) on the
        # real pts timeline - for files whose frame-count timing is off
        cmd = [FFMPEG, "-v", "info", "-i", a.media, "-an", "-vf",
               "scale=320:-2,select='gt(scene,0.3)',showinfo", "-f", "null", "-"]
        err = subprocess.run(cmd, capture_output=True).stderr
        cuts = [round(float(t), 3) for t in
                __import__("re").findall(rb"pts_time:([0-9.]+)", err)]
        cuts = [c for i, c in enumerate(cuts) if i == 0 or c - cuts[i - 1] >= MIN_SHOT_S]
        json.dump({"cuts": cuts, "engine": "ffmpeg"}, open(a.o, "w"))
        print(f"{len(cuts)} ffmpeg scene cuts")
        return
    from scenedetect import AdaptiveDetector, detect
    scenes = detect(a.media, AdaptiveDetector())
    cuts = [round(s[0].get_seconds(), 3) for s in scenes[1:]]
    frames = [s[0].get_frames() for s in scenes[1:]]
    dur = scenes[-1][1].get_seconds() if scenes else 0.0
    json.dump({"cuts": cuts, "cut_frames": frames, "duration": dur}, open(a.o, "w"))
    print(f"{len(cuts)} reference cuts in {dur:.0f} s")


def cmd_eval(a):
    ref = json.load(open(a.ref))
    own = json.load(open(a.own))
    r, o = ref["cuts"], own["cuts"]
    # PySceneDetect times = frame index / nominal fps, wrong for VFR or
    # offset starts: map its frame numbers onto our decoded frames' pts
    if "cut_frames" in ref and own.get("pts"):
        pts = own["pts"]
        r = [round(pts[f], 3) for f in ref["cut_frames"] if f < len(pts)]
    hit_r = [t for t in r if any(abs(t - x) <= TOL_S for x in o)]
    hit_o = [x for x in o if any(abs(t - x) <= TOL_S for t in r)]
    rec = len(hit_r) / max(len(r), 1)
    prec = len(hit_o) / max(len(o), 1)
    print(f"ref {len(r)}, ours {len(o)}: recall {rec:.1%}, precision {prec:.1%}")
    if a.show:
        print("  missed:", [t for t in r if t not in hit_r][:a.show])
        print("  extra: ", [x for x in o if x not in hit_o][:a.show])
    return 0 if rec >= 0.85 else 1


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ref"); r.add_argument("media"); r.add_argument("-o", required=True)
    r.add_argument("--engine", choices=["pyscenedetect", "ffmpeg"], default="pyscenedetect")
    o = sub.add_parser("own"); o.add_argument("media"); o.add_argument("-o", required=True)
    o.add_argument("--dump-scores")
    e = sub.add_parser("eval"); e.add_argument("ref"); e.add_argument("own")
    e.add_argument("--show", type=int, default=0)
    a = p.parse_args()
    return {"ref": cmd_ref, "own": cmd_own, "eval": cmd_eval}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main() or 0)
