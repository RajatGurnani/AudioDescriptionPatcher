#!/usr/bin/env python3
"""Synthetic accuracy + timing harness for adpatch (no real media needed).

    .venv\\Scripts\\python tools\\harness.py [--minutes 10] [--cases pal,ntsc]

Generates an onset-rich synthetic soundtrack, builds a tiny video from it
and several "AD" variants with known speed/offset transforms (what real
AudioVault files look like: PAL speedup, trimmed intros, narration mixed
in), runs adpatch.patch() on each and checks:

  1. recovered alignment vs ground truth: |da| < 20 ppm, |db| < 10 ms
  2. the OUTPUT file itself: its AD track is cross-correlated against the
     source soundtrack in windows across the runtime, independently of
     the aligner's own report; every |lag| must be <= 20 ms
  3. the unrelated-audio case is flagged `suspect`

Media is written to %TEMP%/adpatch-harness (never the repo) and reused
between runs; results go to harness_results.json there. Exit code 1 if
any check fails. See docs/plan/00-baseline-and-harness.md.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import adpatch  # noqa: E402

GEN_SR = 16000
PAL = 25 / 23.976

# name -> (ffmpeg filter on the soundtrack, a_true, b_true, narration)
# Ground truth follows adpatch's model: video_time = a * ad_time + b.
# Speed changes use asetrate+aresample (sample-exact, pitch shifts - like
# a real PAL speedup), not atempo, whose WSOLA latency shifts content by
# 10-20 ms and would corrupt the ground truth.
# asetrate only takes integer rates, so resample to 23976 Hz first and
# relabel as 25000 Hz: an exact 25/23.976 ratio.
SPEEDUP = f"aresample=23976,asetrate=25000,aresample={GEN_SR}"
SLOWDOWN = f"aresample=25000,asetrate=23976,aresample={GEN_SR}"
CASES = {
    "offset_pos": ("adelay=4000:all=1", 1.0, -4.0, False),
    "offset_neg": ("atrim=start=3.5,asetpts=PTS-STARTPTS", 1.0, 3.5, False),
    "pal": (f"{SPEEDUP},atrim=start=2,asetpts=PTS-STARTPTS",
            PAL, 2 * PAL, False),
    "ntsc": (SLOWDOWN, 1 / PAL, 0.0, False),
    "narration": (f"{SPEEDUP},atrim=start=2,asetpts=PTS-STARTPTS",
                  PAL, 2 * PAL, True),
    "wrong": (None, None, None, False),   # unrelated soundtrack
}

MAX_DA_PPM = 20
MAX_DB_MS = 10
MAX_LAG_MS = 20


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{r.stderr}")


def write_wav(path, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(GEN_SR)
        w.writeframes(pcm.tobytes())


def soundtrack(seed, minutes):
    """Gated noise bursts with random spacing, length, level and
    brightness over a low noise floor: dense, unambiguous onsets."""
    rng = np.random.default_rng(seed)
    n = int(minutes * 60 * GEN_SR)
    x = rng.normal(0, 0.005, n)
    t = 0.0
    while True:
        t += rng.exponential(0.6) + 0.05
        dur = rng.uniform(0.05, 0.6)
        i0, i1 = int(t * GEN_SR), int((t + dur) * GEN_SR)
        if i1 >= n:
            break
        burst = rng.normal(0, 1, i1 - i0)
        k = int(rng.integers(1, 12))          # crude low-pass = darker
        if k > 1:
            burst = np.convolve(burst, np.ones(k) / k, mode="same") * np.sqrt(k)
        env = np.minimum(1, np.arange(i1 - i0) / (0.005 * GEN_SR))
        env *= np.exp(-np.arange(i1 - i0) / (rng.uniform(0.05, 0.4) * GEN_SR))
        x[i0:i1] += burst * env * rng.uniform(0.05, 0.4)
    return x


def narration(seed, seconds):
    """Speech-like interjections (syllable-rate modulated band noise),
    a few seconds every 20-60 s - stands in for the describer's voice."""
    rng = np.random.default_rng(seed + 1000)
    n = int(seconds * GEN_SR)
    x = np.zeros(n)
    t = rng.uniform(5, 20)
    while t < seconds - 8:
        dur = rng.uniform(2, 6)
        i0 = int(t * GEN_SR)
        m = int(dur * GEN_SR)
        tt = np.arange(m) / GEN_SR
        band = np.diff(rng.normal(0, 1, m + 1))           # bright-ish noise
        syll = np.abs(np.sin(2 * np.pi * rng.uniform(3, 5) * tt)) ** 2
        x[i0:i0 + m] += band * syll * 0.25
        t += dur + rng.uniform(20, 60)
    return x


def prepare(work, minutes):
    """Generate (or reuse) source wav, video, and every AD variant."""
    os.makedirs(work, exist_ok=True)
    tag = f"{minutes:g}m"
    src = os.path.join(work, f"src_{tag}.wav")
    other = os.path.join(work, f"other_{tag}.wav")
    # PCM audio: an AAC track would carry ~64 ms of encoder priming at
    # 16 kHz that mkv does not trim, shifting the ground truth
    video = os.path.join(work, f"video_pcm_{tag}.mkv")
    if not os.path.exists(src):
        write_wav(src, soundtrack(1, minutes))
    if not os.path.exists(other):
        write_wav(other, soundtrack(2, minutes))
    if not os.path.exists(video):
        run([adpatch.FFMPEG, "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=black:s=160x90:r=24",
             "-i", src, "-shortest", "-c:v", "libx264", "-preset",
             "ultrafast", "-c:a", "pcm_s16le", video])
    ads = {}
    for name, (filt, _, _, narr) in CASES.items():
        ad = os.path.join(work, f"ad3_{name}_{tag}.mp3")
        ads[name] = ad
        if os.path.exists(ad):
            continue
        if filt is None:
            run([adpatch.FFMPEG, "-y", "-v", "error", "-i", other,
                 "-c:a", "libmp3lame", "-b:a", "128k", ad])
            continue
        if narr:
            nar = os.path.join(work, f"narration_{tag}.wav")
            write_wav(nar, narration(1, minutes * 60 * 1.1))
            run([adpatch.FFMPEG, "-y", "-v", "error", "-i", src, "-i", nar,
                 "-filter_complex",
                 f"[0:a]{filt}[s];[s][1:a]amix=inputs=2:duration=first:"
                 "normalize=0",
                 "-c:a", "libmp3lame", "-b:a", "128k", ad])
        else:
            run([adpatch.FFMPEG, "-y", "-v", "error", "-i", src,
                 "-af", filt, "-c:a", "libmp3lame", "-b:a", "128k", ad])
    return src, video, ads


def verify_output(output, src_env):
    """Lag (ms) of the output's AD track vs the source soundtrack in
    windows across the runtime - independent of adpatch's own report."""
    out_env = adpatch.onset_envelope(output)   # 0:a:0 = the AD track
    seg = int(30 * adpatch.FPS)
    usable = min(len(out_env), len(src_env)) - seg
    lags = []
    for start in np.linspace(0.05 * usable, 0.95 * usable, 9).astype(int):
        m = adpatch.segment_match(out_env, src_env, start, seg,
                                  expected_vpos=start,
                                  search=int(1 * adpatch.FPS))
        if m is None or m[1] < 0.3:
            continue
        lags.append((m[0] - start) / adpatch.FPS * 1000)
    return lags


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--minutes", type=float, default=10)
    p.add_argument("--cases", default=",".join(CASES))
    p.add_argument("--workdir",
                   default=os.path.join(tempfile.gettempdir(),
                                        "adpatch-harness"))
    p.add_argument("--container", choices=("mkv", "mp4"), default="mkv",
                   help="output container (mp4 drops the PCM original "
                        "track, which mp4 can't hold)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print adpatch's own log")
    args = p.parse_args()

    print(f"generating media in {args.workdir} ...")
    src, video, ads = prepare(args.workdir, args.minutes)
    src_env = adpatch.onset_envelope(src)

    results, failed = [], False
    print(f"\n{'case':<11} {'da ppm':>8} {'db ms':>8} {'lag max':>8} "
          f"{'conf/susp':>9}  {'env v/a':>11} {'align':>6} {'mux':>6}  ok")
    for name in args.cases.split(","):
        _, a_true, b_true, _ = CASES[name]
        out = os.path.join(args.workdir, f"out_{name}.{args.container}")
        log = []
        res = adpatch.patch(video, ads[name], output=out,
                            replace=args.container == "mp4",
                            log=print if args.verbose else log.append)
        t = res["timings"]
        row = {"case": name, "a": res["a"], "b": res["b"],
               "suspect": res["suspect"], "weak": res["weak"],
               "timings": t}
        if a_true is None:
            ok = res["suspect"]
            da = db = lag_max = None
        else:
            da = (res["a"] - a_true) * 1e6
            db = (res["b"] - b_true) * 1000
            lags = verify_output(out, src_env)
            lag_max = max((abs(x) for x in lags), default=float("inf"))
            row.update(da_ppm=da, db_ms=db, lags_ms=lags)
            ok = (abs(da) < MAX_DA_PPM and abs(db) < MAX_DB_MS
                  and lag_max <= MAX_LAG_MS and len(lags) >= 5)
        row["ok"] = ok = bool(ok)
        row["suspect"] = bool(res["suspect"])
        failed |= not ok
        results.append(row)

        fmt = lambda v, f: "-" if v is None else format(v, f)  # noqa: E731
        print(f"{name:<11} {fmt(da, '+8.1f')} {fmt(db, '+8.1f')} "
              f"{fmt(lag_max, '8.1f')} {str(res['suspect']):>9}  "
              f"{t['video_env']:5.1f}/{t['ad_env']:<5.1f} "
              f"{t['align']:6.2f} {t['mux']:6.2f}  "
              f"{'PASS' if ok else 'FAIL'}")
        os.remove(out)

    path = os.path.join(args.workdir, "harness_results.json")
    with open(path, "w") as f:
        json.dump({"minutes": args.minutes, "results": results}, f, indent=1)
    print(f"\nresults: {path}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
