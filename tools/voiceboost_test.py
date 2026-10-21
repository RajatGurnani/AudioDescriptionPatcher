#!/usr/bin/env python3
"""Measure the PC voice boost (adpatch.voice_boost_graph) the way
android's VoiceBoostTest measures the Kotlin one.

    .venv\\Scripts\\python tools\\voiceboost_test.py

Synthetic 30 s clip: a formant "voice" in the centre, 10 dB under a
background of music (bass, chords, kick, hats) + pink noise, wide stereo
or mono. ffmpeg's filters can't run on "shadow" stems like the Kotlin
code, so stems are estimated by polarity: process mix = v + b and
F(b + v) and F(b - v); with gains driven by nearly the same signal,
voice ~= (y1 - y2)/2 and background ~= (y1 + y2)/2.

Reports voice-to-background ratio (VBR) gain in the speech band
(300-3400 Hz) and full band, voice level change, peak, and the delay the
chain adds (must be ~0 so the boosted track stays in sync). Exit 1 if
low/high don't improve VBR, peaks exceed -0.9 dBFS or delay > 2 ms.
"""

import os
import subprocess
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import adpatch  # noqa: E402

SR = 48000
DUR = 30


def voice(rng):
    """Formant-ish speech: glottal pulse train (pitch with intonation)
    through 3 formant resonators, in phrases with pauses + fricatives."""
    n = SR * DUR
    t = np.arange(n) / SR
    f0 = 120 + 25 * np.sin(2 * np.pi * 0.4 * t) + 10 * np.sin(2 * np.pi * 3 * t)
    phase = np.cumsum(f0 / SR)
    pulses = (np.diff(np.floor(phase), prepend=0) > 0).astype(float)
    out = np.zeros(n)
    m = int(0.05 * SR)                        # 50 ms resonator response
    k = np.arange(m) / SR
    for fc, bw, g in ((700, 110, 1.0), (1200, 120, 0.6), (2600, 160, 0.35)):
        ir = np.exp(-np.pi * bw * k) * np.sin(2 * np.pi * fc * k)
        size = 1 << int(np.ceil(np.log2(n + m)))
        y = np.fft.irfft(np.fft.rfft(pulses, size) * np.fft.rfft(ir, size),
                         size)[:n]
        out += g * y
    # syllables (4/s) inside phrases (2.5 s on, 1 s off)
    syl = np.clip(np.sin(2 * np.pi * 4 * t), 0, None) ** 0.5
    phrase = ((t % 3.5) < 2.5).astype(float)
    fric = rng.normal(0, 1, n) * ((np.sin(2 * np.pi * 0.9 * t) > 0.97))
    fric = np.diff(fric, prepend=0) * 0.3
    v = (out * syl + fric) * phrase
    return v / (np.sqrt(np.mean(v ** 2)) + 1e-12)


def background(rng, wide):
    n = SR * DUR
    t = np.arange(n) / SR
    bass = 0.6 * np.sin(2 * np.pi * 55 * t) * (1 + 0.3 * np.sin(2 * np.pi * 0.5 * t))
    chord = sum(np.sin(2 * np.pi * f * t) for f in (220, 277, 330, 440, 554))
    kick = np.sin(2 * np.pi * 60 * t) * np.exp(-((t % 0.5) * 30))
    hats = rng.normal(0, 1, n) * np.exp(-((t % 0.25) * 80)) * 0.3
    white = rng.normal(0, 1, (2, n))
    pink = np.cumsum(white, axis=1)
    pink -= np.convolve(pink[0], np.ones(64) / 64, "same")[None, :]
    pink /= np.std(pink) + 1e-12
    left = bass + 0.3 * chord + kick + hats + 0.5 * pink[0]
    if wide:
        chord_r = sum(np.sin(2 * np.pi * f * t + 1.3) for f in (247, 311, 370, 494))
        right = bass + 0.3 * chord_r + kick - hats + 0.5 * pink[1]
    else:
        right = left
    b = np.stack([left, right])
    return b / (np.sqrt(np.mean(b ** 2)) + 1e-12)


def write_wav(path, x):
    pcm = (np.clip(x.T, -1, 1) * 32767).astype("<i2")
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def run_chain(src, level, dst):
    graph = adpatch.voice_boost_graph("[0:a]", "[o]", level)
    r = subprocess.run([adpatch.FFMPEG, "-y", "-v", "error", "-i", src,
                        "-filter_complex", graph, "-map", "[o]",
                        "-c:a", "pcm_f32le", dst], capture_output=True,
                       text=True)
    if r.returncode:
        raise RuntimeError(r.stderr)
    raw = subprocess.run([adpatch.FFMPEG, "-v", "error", "-i", dst, "-f",
                          "f32le", "-ac", "2", "-ar", str(SR), "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, "<f4").reshape(-1, 2).T.astype(float)


def band(x):
    """300-3400 Hz via FFT mask (measurement only)."""
    X = np.fft.rfft(x, axis=-1)
    f = np.fft.rfftfreq(x.shape[-1], 1 / SR)
    X[..., (f < 300) | (f > 3400)] = 0
    return np.fft.irfft(X, x.shape[-1], axis=-1)


def db(p):
    return 10 * np.log10(p + 1e-20)


def delay_ms(a, b):
    """Lag of b vs a (mono sums), ms."""
    a, b = a.sum(0), b.sum(0)
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    cc = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    k = int(np.argmax(cc))
    k = k - n if k > n // 2 else k
    return k / SR * 1000


def main():
    work = os.path.join(tempfile.gettempdir(), "adpatch-voiceboost")
    os.makedirs(work, exist_ok=True)
    rng = np.random.default_rng(3)
    v1 = voice(rng)
    fails = []
    for wide in (True, False):
        b = background(rng, wide)
        v = np.stack([v1, v1]) * 10 ** (-10 / 20)    # centre, -10 dB
        mix_p, mix_m = (b + v) * 0.3, (b - v) * 0.3
        name = "wide" if wide else "mono"
        p_in = os.path.join(work, f"{name}_p.wav")
        m_in = os.path.join(work, f"{name}_m.wav")
        write_wav(p_in, mix_p)
        write_wav(m_in, mix_m)
        vb0, bb0 = band(v * 0.3), band(b * 0.3)
        base_band = db(np.mean(vb0 ** 2)) - db(np.mean(bb0 ** 2))
        base_full = db(np.mean((v * 0.3) ** 2)) - db(np.mean((b * 0.3) ** 2))
        print(f"{name} background: before band VBR {base_band:+.1f} dB, "
              f"full {base_full:+.1f} dB")
        for level in ("low", "high"):
            y1 = run_chain(p_in, level, os.path.join(work, f"{name}_{level}_p.wav"))
            y2 = run_chain(m_in, level, os.path.join(work, f"{name}_{level}_m.wav"))
            n = min(y1.shape[1], y2.shape[1], mix_p.shape[1])
            y1, y2 = y1[:, :n], y2[:, :n]
            vo, bo = (y1 - y2) / 2, (y1 + y2) / 2
            g_band = (db(np.mean(band(vo) ** 2)) - db(np.mean(band(bo) ** 2))
                      - base_band)
            g_full = (db(np.mean(vo ** 2)) - db(np.mean(bo ** 2)) - base_full)
            vlev = db(np.mean(vo ** 2)) - db(np.mean((v[:, :n] * 0.3) ** 2))
            peak = 20 * np.log10(np.max(np.abs(y1)) + 1e-12)
            lag = delay_ms(mix_p[:, :n], y1)
            print(f"  {level:<5} band VBR {g_band:+.1f} dB, full {g_full:+.1f} dB, "
                  f"voice level {vlev:+.1f} dB, peak {peak:+.1f} dBFS, "
                  f"delay {lag:+.2f} ms")
            if g_band <= 0.5 or g_full <= 1.0:
                fails.append(f"{name}/{level}: no VBR gain")
            if peak > -0.9:
                fails.append(f"{name}/{level}: peak {peak:.1f} dBFS")
            if abs(lag) > 2:
                fails.append(f"{name}/{level}: delay {lag:.1f} ms")
    print("PASS" if not fails else f"FAIL: {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
