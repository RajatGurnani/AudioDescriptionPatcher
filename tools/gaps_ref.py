"""Reference + evaluation for narration-gap detection (docs/plan/10, A2).

Dev tool, not part of the app. Needs `pip install onnxruntime` on top of
requirements.txt, and a speech model:
- pyannote segmentation-3.0 (MIT, CNRS), ONNX export by sherpa-onnx:
  github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-segmentation-models
  (sherpa-onnx-pyannote-segmentation-3-0, model.int8.onnx) - the default;
- Silero VAD (MIT) silero_vad.onnx - kept for comparison: it misses
  ~40-60 % of film dialogue under music (docs/plan/10, A2).

  # ground-truth dialogue: full mix vs its music+effects stem, optionally
  # limited to subtitle lines (drops breaths/grunts the stem also holds)
  python tools/gaps_ref.py truth FULL ME [--subs X.srt] -o truth.json
  # speech + gaps on any media file (same rules as GapFinder.kt)
  python tools/gaps_ref.py vad MEDIA --model model.int8.onnx -o vad.json
  # score detected gaps (vad.json from this tool or the Android probe)
  python tools/gaps_ref.py eval truth.json vad.json

JSON: {"speech": [[s, e], ...], "gaps": [[s, e], ...], "duration": s}.
"""
import argparse
import json
import re
import subprocess
import sys

import numpy as np

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = "ffmpeg"

# gap rules - keep identical to describe/GapFinder.kt
MIN_GAP_S = 1.5      # shortest gap worth narrating
GAP_GUARD_S = 0.2    # keep this far from speech on both sides
THRESHOLD = 0.5      # Silero speech probability (enter speech)
NEG_THRESHOLD = 0.35  # leave speech below this
MIN_SPEECH_S = 0.25
MIN_SILENCE_S = 0.10


def decode(path, sr, channels=1, layout_downmix=True):
    cmd = [FFMPEG, "-v", "error", "-i", path, "-vn", "-ac", str(channels),
           "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, channels)


def gaps_from_speech(speech, duration):
    gaps, t = [], 0.0
    for s, e in speech + [[duration + GAP_GUARD_S, duration]]:
        g0 = t + GAP_GUARD_S if t > 0 else 0.0
        g1 = min(s - GAP_GUARD_S, duration)
        if g1 - g0 >= MIN_GAP_S:
            gaps.append([round(g0, 3), round(g1, 3)])
        t = max(t, e)
    return gaps


def probs_to_speech(probs, hop_s):
    """Hysteresis on per-window probabilities -> [start, end] seconds."""
    speech, start, below = [], None, None
    for i, p in enumerate(probs):
        t = i * hop_s
        if start is None:
            if p >= THRESHOLD:
                start, below = t, None
        elif p < NEG_THRESHOLD:
            below = t if below is None else below
            if t + hop_s - below >= MIN_SILENCE_S:
                if below - start >= MIN_SPEECH_S:
                    speech.append([start, below])
                start, below = None, None
        else:
            below = None
    if start is not None and len(probs) * hop_s - start >= MIN_SPEECH_S:
        speech.append([start, len(probs) * hop_s])
    return [[round(s, 3), round(e, 3)] for s, e in speech]


SEG_SR = 16000
SEG_WIN = 160000   # pyannote: 10 s windows
SEG_STEP = 80000   # 50 % overlap, probabilities averaged
SEG_HOP = 270      # samples per output frame (receptive_field_shift)


def pyannote_probs(sess, x):
    """P(any speaker active) per 270-sample frame, overlapped windows."""
    nfr = len(x) // SEG_HOP + 1
    acc, cnt = np.zeros(nfr), np.zeros(nfr)
    for s in range(0, max(len(x) - SEG_WIN, 0) + SEG_STEP, SEG_STEP):
        c = x[s:s + SEG_WIN]
        if len(c) < SEG_WIN:
            c = np.pad(c, (0, SEG_WIN - len(c)))
        y = sess.run(None, {"x": c[None, None, :].astype(np.float32)})[0][0]
        p = 1.0 - np.exp(y[:, 0])  # powerset class 0 = nobody speaks
        f0 = s // SEG_HOP
        k = min(len(p), nfr - f0)
        acc[f0:f0 + k] += p[:k]
        cnt[f0:f0 + k] += 1
    return acc / np.maximum(cnt, 1)


def cmd_vad(a):
    import onnxruntime as ort
    sess = ort.InferenceSession(a.model, providers=["CPUExecutionProvider"])
    if "x" in [i.name for i in sess.get_inputs()]:
        x = decode(a.media, SEG_SR)[:, 0]
        probs = pyannote_probs(sess, x)
        dur = len(x) / SEG_SR
        speech = probs_to_speech(probs, SEG_HOP / SEG_SR)
        res = {"speech": speech, "gaps": gaps_from_speech(speech, dur),
               "duration": round(dur, 3), "engine": "pyannote"}
        json.dump(res, open(a.o, "w"))
        print(f"{len(speech)} speech segs, {len(res['gaps'])} gaps "
              f"({sum(e - s for s, e in res['gaps']):.0f} s) in {dur:.0f} s")
        return
    x = decode(a.media, a.sr)[:, 0]
    win = 512 if a.sr == 16000 else 256
    ctx_n = 64 if a.sr == 16000 else 32
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros(ctx_n, np.float32)
    sr = np.array(a.sr, np.int64)
    probs = []
    for i in range(0, len(x) - win + 1, win):
        chunk = x[i:i + win]
        inp = np.concatenate([ctx, chunk])[None, :]
        out, state = sess.run(None, {"input": inp, "state": state, "sr": sr})
        probs.append(float(out[0, 0]))
        ctx = chunk[-ctx_n:]
    dur = len(x) / a.sr
    speech = probs_to_speech(probs, win / a.sr)
    res = {"speech": speech, "gaps": gaps_from_speech(speech, dur),
           "duration": round(dur, 3), "engine": "silero", "sr": a.sr}
    json.dump(res, open(a.o, "w"))
    print(f"{len(speech)} speech segs, {len(res['gaps'])} gaps "
          f"({sum(e - s for s, e in res['gaps']):.0f} s) in {dur:.0f} s")


def cmd_truth(a):
    """Dialogue frames = speech-band power of the full mix clearly above
    the (level-matched) M&E stem. Spectral, so the few-ms offsets and
    mastering differences between stems don't matter."""
    sr, hop, nfft = 16000, 160, 512
    full = decode(a.full, sr)[:, 0]
    me = decode(a.me, sr)[:, 0]
    n = min(len(full), len(me))

    def band_power(x):
        m = (n - nfft) // hop
        idx = np.arange(nfft)[None, :] + hop * np.arange(m)[:, None]
        spec = np.abs(np.fft.rfft(x[:n][idx] * np.hanning(nfft), axis=1)) ** 2
        f = np.fft.rfftfreq(nfft, 1 / sr)
        return spec[:, (f >= 300) & (f <= 3400)].sum(1) / nfft

    pf, pm = band_power(full), band_power(me)
    g = float(np.median(pf / (pm + 1e-12)))   # most frames have no dialogue
    excess_db = 10 * np.log10((pf + 1e-12) / (g * pm + 1e-12))
    level_db = 10 * np.log10(pf + 1e-12)
    on = (excess_db > a.excess_db) & (level_db > a.gate_db)
    on = _close(on, 30)          # close <300 ms holes inside speech
    on = ~_close(~on, 10)        # drop <100 ms blips
    m = len(on)
    speech, i = [], 0
    while i < m:
        if on[i]:
            j = i
            while j < m and on[j]:
                j += 1
            speech.append([round(i / 100, 2), round(j / 100, 2)])
            i = j
        else:
            i += 1
    if a.subs:
        lines = read_srt(a.subs)
        speech = [x for x in speech
                  if any(overlap(x, [s - 0.5, e + 0.5]) > 0 for s, e in lines)]
    dur = n / sr
    res = {"speech": speech, "gaps": gaps_from_speech(speech, dur),
           "duration": round(dur, 3), "gain": round(g, 4)}
    json.dump(res, open(a.o, "w"))
    print(f"gain {g:.3f}, {len(speech)} speech segs "
          f"({sum(e - s for s, e in speech):.0f} s), {len(res['gaps'])} gaps "
          f"({sum(e - s for s, e in res['gaps']):.0f} s) in {dur:.0f} s")


def _close(on, k):
    """Fill False runs shorter than k frames that sit between True runs."""
    on = on.copy()
    i, m = 0, len(on)
    while i < m:
        if not on[i]:
            j = i
            while j < m and not on[j]:
                j += 1
            if 0 < i and j < m and j - i < k:
                on[i:j] = True
            i = j
        else:
            i += 1
    return on


def read_srt(path):
    pat = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+) --> (\d+):(\d+):(\d+)[,.](\d+)")
    out = []
    for m in pat.finditer(open(path, encoding="utf-8", errors="ignore").read()):
        h, mi, se, ms, h2, mi2, se2, ms2 = map(int, m.groups())
        out.append([h * 3600 + mi * 60 + se + ms / 1000,
                    h2 * 3600 + mi2 * 60 + se2 + ms2 / 1000])
    return out


def overlap(a, b):
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def cmd_eval(a):
    t = json.load(open(a.truth))
    v = json.load(open(a.vad))
    shift = a.shift
    gaps = [[s + shift, e + shift] for s, e in v["gaps"]]
    # recall: truth gaps (>= MIN_GAP_S) with >= 50 % covered by found gaps
    tg = t["gaps"]
    found = [g for g in tg
             if sum(overlap(g, x) for x in gaps) >= 0.5 * (g[1] - g[0])]
    # precision: found gaps overlapping truth speech by > 0.3 s
    bad = [x for x in gaps
           if sum(overlap(x, s) for s in t["speech"]) > 0.3]
    tsec = sum(e - s for s, e in tg)
    fsec = sum(overlap(g, x) for g in tg for x in gaps)
    print(f"truth gaps {len(tg)} ({tsec:.0f} s), found {len(gaps)} "
          f"({sum(e - s for s, e in gaps):.0f} s)")
    print(f"recall {len(found) / max(len(tg), 1):.1%} of gaps, "
          f"{fsec / max(tsec, 1e-9):.1%} of gap time")
    print(f"gaps overlapping speech {len(bad) / max(len(gaps), 1):.1%} "
          f"({len(bad)})")
    for x in bad[:a.show]:
        sp = [s for s in t["speech"] if overlap(x, s) > 0]
        print(f"  gap {x[0]:.2f}-{x[1]:.2f} hits speech {sp[:3]}")
    ok = len(found) >= 0.9 * len(tg) and len(bad) <= 0.05 * len(gaps)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("truth")
    t.add_argument("full"); t.add_argument("me")
    t.add_argument("-o", required=True)
    t.add_argument("--subs", help="keep only speech near these subtitle lines")
    t.add_argument("--excess-db", type=float, default=6.0)
    t.add_argument("--gate-db", type=float, default=-60.0)
    v = sub.add_parser("vad")
    v.add_argument("media"); v.add_argument("--model", required=True)
    v.add_argument("--sr", type=int, default=8000, choices=[8000, 16000])
    v.add_argument("-o", required=True)
    e = sub.add_parser("eval")
    e.add_argument("truth"); e.add_argument("vad")
    e.add_argument("--shift", type=float, default=0.0,
                   help="add to vad times to land on the truth timeline")
    e.add_argument("--show", type=int, default=5)
    a = p.parse_args()
    return {"truth": cmd_truth, "vad": cmd_vad, "eval": cmd_eval}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main() or 0)
