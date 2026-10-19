#!/usr/bin/env python3
"""adpatch - patch a video with its audio-described (AD) soundtrack.

Command line:
    adpatch.py <video> <ad_audio> [-o OUTPUT] [--replace]

Library:
    from adpatch import patch
    result = patch("movie.mp4", "movie_AD.mp3", log=print)

Give it a video file and an audio-description audio file (in either order).
It aligns the AD audio to the video and muxes the corrected AD audio into a
new file next to the video. The video stream is copied untouched; the
original audio is kept as a second track unless `replace` is set.

How alignment works
-------------------
AD files from sources like audiovault.net are the *full movie soundtrack*
with narration mixed into natural pauses - so the AD audio and the video's
own audio share almost all their content. We model the relationship as

    video_time = a * ad_time + b

where `a` captures speed drift (e.g. a 25fps PAL rip of a 23.976fps film
plays 4.2% fast) and `b` captures a start offset (trimmed studio logos,
extra intros). Both are recovered by cross-correlating onset-strength
envelopes of the two audio streams - see fit_alignment().

This linear model intentionally does NOT handle "extended audio
description" (where the video is paused while long descriptions play,
making the AD cut genuinely longer scene-by-scene). That requires editing
the video itself. Such files are detected and reported as weak matches.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import imageio_ffmpeg

# All alignment happens on lightweight "onset envelopes", not raw audio:
# audio is decoded to mono at SR, then reduced to one loudness-change value
# per HOP samples. 100 envelope frames per second gives 10 ms alignment
# resolution - well inside the ~100 ms tolerance for AD narration sync.
SR = 8000              # decode sample rate (Hz)
HOP = 80               # samples per envelope frame
FPS = SR / HOP         # envelope frame rate: 100 Hz

# Thresholds, all empirically chosen (see docs/ARCHITECTURE.md):
MIN_SEGMENT_SCORE = 0.15   # normalized correlation below this = no match
NEGLIGIBLE_DRIFT = 5e-5    # |a-1| below this: skip atempo (~0.4s over 2h)
WEAK_SCORE = 0.12          # per-segment "weak match" flag in the report

AD_RATE = 48000            # sample rate of the encoded AD track

# AD encoders in order of preference, with their priming delay (samples)
# that mkv does NOT trim (mp4's edit list does). Windows' Media Foundation
# AAC encoder is ~4x faster than ffmpeg's and adds no delay; ffmpeg's
# own aac is the fallback (e.g. Windows "N" editions without MF).
AD_ENCODERS = (("aac_mf", 0), ("aac", 1024))
_ad_encoder = None

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".wmv",
              ".flv", ".ts", ".mpg", ".mpeg", ".vob", ".3gp"}

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------

def probe_has_video(path):
    """True if ffmpeg sees a real video stream (ignores mp3 cover art,
    which ffmpeg reports as an 'attached pic' video stream)."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                          capture_output=True, text=True, errors="replace")
    return any("Video:" in line and "attached pic" not in line
               for line in proc.stderr.splitlines())


def probe_duration(path):
    """Container duration in seconds from `ffmpeg -i` (no ffprobe in the
    pip-bundled ffmpeg), or None if unknown."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                          capture_output=True, text=True, errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        return None
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)


def _drain(pipe):
    """Read a pipe to the end on a background thread; returns a function
    that joins it and yields the bytes. Prevents the pipe-buffer deadlock
    (~4 KB on Windows) when ffmpeg writes lots of warnings to stderr
    while we're blocked reading its other output."""
    chunks = []
    t = threading.Thread(target=lambda: chunks.append(pipe.read()),
                         daemon=True)
    t.start()

    def result():
        t.join()
        return b"".join(chunks)
    return result


def run_ffmpeg(cmd, duration=None, progress=None):
    """Run an ffmpeg command, reporting completion 0..1 to `progress` from
    its -progress output. Returns (returncode, stderr_text)."""
    cmd = [cmd[0], "-nostats", "-progress", "pipe:1"] + cmd[1:]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    err = _drain(proc.stderr)
    for line in proc.stdout:
        if progress and duration and line.startswith(b"out_time_us="):
            try:
                us = int(line.split(b"=", 1)[1])
            except ValueError:   # "N/A" before the first frame
                continue
            progress(min(1.0, max(0.0, us / 1e6 / duration)))
    rc = proc.wait()
    return rc, err().decode(errors="replace")


def classify_inputs(a, b):
    """Return (video_path, audio_path) from two paths given in any order.

    Extension check first (cheap), ffmpeg stream probe as tiebreaker."""
    ea, eb = os.path.splitext(a)[1].lower(), os.path.splitext(b)[1].lower()
    if ea in VIDEO_EXTS and eb not in VIDEO_EXTS:
        return a, b
    if eb in VIDEO_EXTS and ea not in VIDEO_EXTS:
        return b, a
    if probe_has_video(a) and not probe_has_video(b):
        return a, b
    if probe_has_video(b) and not probe_has_video(a):
        return b, a
    raise ValueError("could not tell which input is the video and which is "
                     "the AD audio - pass video first, audio second")


# --------------------------------------------------------------------------
# Envelope extraction
# --------------------------------------------------------------------------

def onset_envelope(path, progress=None):
    """Decode the first audio stream and return its onset-strength envelope.

    The envelope is the half-wave-rectified frame-to-frame change in log
    energy, z-normalized, at 100 Hz. Loudness *changes* survive both the
    narration mixed over the AD soundtrack and level/EQ differences between
    releases, which makes this far more robust than correlating waveforms.

    Decoding is streamed through ffmpeg 20 s at a time - a 3-hour movie
    never occupies more than a few MB of memory here. `progress`, if
    given, is called with the fraction decoded (0..1).
    """
    total = None
    if progress:
        dur = probe_duration(path)
        total = dur * SR * 4 if dur else None
    # -vn/-sn/-dn: don't set up decoders for streams we never read
    cmd = [FFMPEG, "-v", "error", "-i", path, "-vn", "-sn", "-dn",
           "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    err = _drain(proc.stderr)
    energies = []
    leftover = b""
    done = 0
    bytes_per_read = 4 * SR * 20
    while True:
        chunk = proc.stdout.read(bytes_per_read)
        if not chunk:
            break
        done += len(chunk)
        if total:
            progress(min(1.0, done / total))
        buf = leftover + chunk
        usable = (len(buf) // 4 // HOP) * HOP * 4
        if usable:
            x = np.frombuffer(buf[:usable], dtype=np.float32)
            energies.append((x.reshape(-1, HOP) ** 2).sum(axis=1))
        leftover = buf[usable:]
    err = err().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed decoding {path}:\n{err}")
    if not energies:
        raise RuntimeError(f"no audio decoded from {path}")

    log_energy = np.log10(np.concatenate(energies) + 1e-9)
    onset = np.maximum(np.diff(log_energy), 0.0)
    onset -= onset.mean()
    std = onset.std()
    if std > 0:
        onset /= std
    return onset.astype(np.float32)


# Bump whenever SR, HOP or the onset formula change: invalidates the cache.
ENVELOPE_VERSION = 1
CACHE_DIR = os.environ.get("ADPATCH_CACHE_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "ADPatcher", "cache", "envelopes")
CACHE_LIMIT = 500e6   # bytes; oldest entries pruned beyond this


def _cache_key(path):
    """Content-based key (size + first/last MB), so re-uploads and renamed
    copies of the same file still hit."""
    size = os.path.getsize(path)
    h = hashlib.sha1(f"v{ENVELOPE_VERSION}:{SR}:{HOP}:{size}".encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read())
    return h.hexdigest()


def cached_envelope(path, progress=None):
    """onset_envelope() with an on-disk cache (~3 MB per 2 h of audio).
    Set ADPATCH_NO_CACHE=1 to bypass (e.g. for timing measurements)."""
    if os.environ.get("ADPATCH_NO_CACHE"):
        return onset_envelope(path, progress)
    try:
        cache = os.path.join(CACHE_DIR, _cache_key(path) + ".npy")
        if os.path.exists(cache):
            env = np.load(cache)
            os.utime(cache)   # mark recently used for pruning
            if progress:
                progress(1.0)
            return env
    except (OSError, ValueError):
        cache = None
    env = onset_envelope(path, progress)
    if cache:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            np.save(cache, env)
            _prune_cache()
        except OSError:
            pass   # cache is best-effort
    return env


def _prune_cache():
    entries = sorted((e.stat().st_mtime, e.stat().st_size, e.path)
                     for e in os.scandir(CACHE_DIR) if e.name.endswith(".npy"))
    total = sum(size for _, size, _ in entries)
    for _, size, path in entries:
        if total <= CACHE_LIMIT:
            break
        os.remove(path)
        total -= size


# --------------------------------------------------------------------------
# Alignment
# --------------------------------------------------------------------------

def _parabolic(y, k):
    """Refine an argmax to sub-frame precision by fitting a parabola
    through the peak and its two neighbours."""
    if 0 < k < len(y) - 1:
        denom = y[k - 1] - 2 * y[k] + y[k + 1]
        if denom != 0:
            return k + 0.5 * (y[k - 1] - y[k + 1]) / denom
    return float(k)


def _pool(env, factor):
    """Downsample an envelope by mean-pooling `factor` frames into one."""
    n = len(env) // factor * factor
    return env[:n].reshape(-1, factor).mean(axis=1)


def _stretch(env, factor):
    """Time-stretch an envelope so content at frame i lands at frame
    factor*i (linear interpolation)."""
    m = int(len(env) * factor)
    return np.interp(np.arange(m) / factor,
                     np.arange(len(env)), env).astype(np.float32)


def _speed_scan(ad_env, vid_env, factors):
    """Try each candidate speed factor: stretch the AD envelope, FFT
    cross-correlate against the video envelope, and score the peak.

    The correct factor lines up onsets across the WHOLE runtime, producing
    a far sharper global peak than any wrong factor. Returns
    (best_factor, best_lag_frames, best_score).
    """
    vid_norm = np.linalg.norm(vid_env)
    best = (-1.0, 1.0, 0)
    for f in factors:
        stretched = _stretch(ad_env, f)
        n = 1 << int(np.ceil(np.log2(len(stretched) + len(vid_env))))
        cc = np.fft.irfft(np.fft.rfft(vid_env, n) *
                          np.conj(np.fft.rfft(stretched, n)), n)
        k = int(np.argmax(cc))
        score = cc[k] / (vid_norm * np.linalg.norm(stretched) + 1e-9)
        lag = k - n if k > n // 2 else k   # wrap negative lags
        if score > best[0]:
            best = (score, f, lag)
    return best[1], best[2], best[0]


def segment_match(ad_env, vid_env, seg_start, seg_len, expected_vpos,
                  search):
    """Locate one AD-envelope segment inside the video envelope.

    Searches a window of +-`search` frames around `expected_vpos` and
    returns (video_pos_frames, score) for the best normalized-correlation
    peak, or None if the window falls outside the video. Score is the
    cosine similarity at the peak: ~0 for unrelated audio, up to ~1 for a
    clean match.
    """
    seg = ad_env[seg_start:seg_start + seg_len]
    w0 = max(0, int(expected_vpos - search))
    w1 = min(len(vid_env), int(expected_vpos + seg_len + search))
    if w1 - w0 < seg_len + 10:
        return None
    vwin = vid_env[w0:w1]

    n = 1 << int(np.ceil(np.log2(len(vwin) + seg_len)))
    cc = np.fft.irfft(np.fft.rfft(vwin, n) * np.conj(np.fft.rfft(seg, n)), n)
    valid = len(vwin) - seg_len
    cc = cc[:valid]
    # sliding L2 norm of the video window, for proper normalization
    csum = np.concatenate(([0.0], np.cumsum(vwin.astype(np.float64) ** 2)))
    vnorm = np.sqrt(csum[seg_len:seg_len + valid] - csum[:valid])
    score = cc / (np.linalg.norm(seg) * vnorm + 1e-9)
    k = int(np.argmax(score))
    return w0 + _parabolic(score, k), float(score[k])


def fit_alignment(ad_env, vid_env, log=print):
    """Recover video_time = a * ad_time + b (seconds).

    Stage 1 - speed scan: grid-search stretch factors 0.95-1.05 (coarse
    0.2% steps, then fine 0.02% steps) on 20 Hz pooled envelopes. This MUST
    come before any segment matching: with uncorrected drift, a segment's
    own onsets smear apart and every local correlation degrades to noise.

    Stage 2 - refinement: stretch the AD envelope by the stage-1 factor
    (so segments are internally aligned), then match 30 s segments spread
    across the runtime and fit a weighted line through (segment position,
    matched position), dropping outliers. This tightens a and b to
    millisecond-level residuals.

    Returns (a, b_seconds, report) where report rows are
    (ad_seconds, video_seconds, score, residual_ms) per checked segment.
    """
    # Stage 1: coarse-to-fine speed scan on pooled (20 Hz) envelopes.
    pool = 5
    ad20, vid20 = _pool(ad_env, pool), _pool(vid_env, pool)
    coarse_grid = np.arange(0.95, 1.0501, 0.002)
    a, _, _ = _speed_scan(ad20, vid20, coarse_grid)
    fine_grid = np.arange(a - 0.002, a + 0.00201, 0.0002)
    a, lag20, peak = _speed_scan(ad20, vid20, fine_grid)

    # Snap to a "special" ratio (exact 1.0, PAL<->film) when one sits
    # within a coarse grid step of the winner and correlates almost as
    # well. On weak-content scans the grid otherwise picks a spuriously
    # drifted factor a few hundred ppm off, which turns into an audible
    # sync slope across the runtime.
    for special in (1.0, 25 / 23.976, 23.976 / 25, 25 / 24, 24 / 25,
                    23.976 / 24, 24 / 23.976):
        if abs(special - a) < 0.003 and abs(special - a) > 1e-9:
            fs, lag_s, peak_s = _speed_scan(ad20, vid20, [special])
            if peak_s >= 0.98 * peak:
                log(f"  snapped speed {a:.6f} -> {fs:.6f} "
                    f"(peak {peak_s:.2f} vs grid {peak:.2f})")
                a, lag20, peak = fs, lag_s, peak_s
            break

    b = float(lag20 * pool)   # convert 20 Hz lag back to 100 Hz frames
    log(f"  coarse: speed {a:.4f}, offset {b / FPS:+.2f}s (peak {peak:.2f})")

    # Stage 2: segment refinement on the stretched full-rate envelope.
    a0 = a
    stretched = _stretch(ad_env, a0)

    seg_len = int(30 * FPS)
    usable = len(stretched) - seg_len
    if usable <= 0:   # very short input: shrink the segment
        seg_len = max(int(len(stretched) * 0.5), int(5 * FPS))
        usable = len(stretched) - seg_len
    n_seg = max(3, min(16, usable // (2 * seg_len) + 1))
    starts = np.linspace(0, usable, n_seg).astype(int)

    matches = []
    for u in starts:
        m = segment_match(stretched, vid_env, u, seg_len,
                          expected_vpos=u + b, search=int(5 * FPS))
        if m is not None:
            matches.append((u, m[0], m[1]))

    a2, b2 = 1.0, b   # mapping correction on top of the stage-1 stretch
    confident = [g for g in matches if g[2] > MIN_SEGMENT_SCORE]
    if len(confident) >= 2:
        for _ in range(2):   # fit, drop outliers, refit once
            x = np.array([g[0] for g in confident], dtype=np.float64)
            y = np.array([g[1] for g in confident], dtype=np.float64)
            w = np.array([g[2] for g in confident])
            a2, b2 = np.polyfit(x, y, 1, w=w)
            resid = np.abs(y - (a2 * x + b2))
            keep = resid < max(3 * np.median(resid) + 1e-9, 0.05 * FPS)
            if keep.all() or keep.sum() < 2:
                break
            confident = [g for g, k in zip(confident, keep) if k]
    elif len(confident) == 1:
        # one trustworthy anchor: refine the offset only
        b2 = confident[0][1] - confident[0][0]
        log("  one confident segment match; refined offset only")
    else:
        log("  warning: too few confident segment matches; keeping the "
            "coarse global alignment")

    a, b = a0 * a2, b2
    report = []
    for u, y, score in matches:
        residual_ms = (y - (a2 * u + b2)) / FPS * 1000
        center_ad = (u + seg_len // 2) / a0
        report.append((center_ad / FPS, (a * center_ad + b) / FPS,
                       score, residual_ms))
    return float(a), float(b / FPS), report


# --------------------------------------------------------------------------
# Muxing
# --------------------------------------------------------------------------

def ad_encoder():
    """(name, priming_samples) of the first AD encoder that works here;
    probed once with a 0.1 s silent encode."""
    global _ad_encoder
    if _ad_encoder is None:
        _ad_encoder = AD_ENCODERS[-1]
        for name, delay in AD_ENCODERS:
            r = subprocess.run(
                [FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                 f"anullsrc=r={AD_RATE}:cl=stereo", "-t", "0.1",
                 "-c:a", name, "-b:a", "192k", "-f", "null", "-"],
                capture_output=True)
            if r.returncode == 0:
                _ad_encoder = (name, delay)
                break
    return _ad_encoder


def build_mux_cmd(video, ad_audio, output, a, b_sec, replace, with_subs,
                  encoder=None):
    """Assemble the ffmpeg command that applies the alignment and muxes.

    The AD audio gets: atempo (speed correction, skipped when negligible),
    then adelay (positive offset) or atrim (negative offset). The video
    stream is copied bit-for-bit. The corrected AD track becomes the
    default audio; the original audio is copied as track 2 unless
    `replace`. `encoder` is an AD_ENCODERS entry (default: ad_encoder()).
    """
    enc_name, priming = encoder or ad_encoder()
    # Fixed output rate: keeps the encoder priming delay known (see below).
    filters = [f"aresample={AD_RATE}"]
    if abs(a - 1.0) > NEGLIGIBLE_DRIFT:
        # content at ad_time t must land at a*t, so play at rate 1/a
        filters.append(f"atempo={1.0 / a:.8f}")
    if os.path.splitext(output)[1].lower() == ".mkv":
        # Encoders may prepend priming samples (ffmpeg aac: 1024); mp4
        # trims them via an edit list, mkv doesn't - so the track would
        # play ~21 ms late. Start the AD that much earlier to cancel it.
        b_sec -= priming / AD_RATE
    if b_sec >= 0:
        delay_ms = int(round(b_sec * 1000))
        if delay_ms:
            filters.append(f"adelay=delays={delay_ms}:all=1")
    else:
        filters.append(f"atrim=start={-b_sec:.4f}")
        filters.append("asetpts=PTS-STARTPTS")
    chain = ",".join(filters) if filters else "anull"

    cmd = [FFMPEG, "-y", "-v", "error",
           "-i", video, "-i", ad_audio,
           "-filter_complex", f"[1:a:0]{chain}[ad]",
           "-map", "0:v", "-c:v", "copy",
           "-map", "[ad]", "-c:a:0", enc_name, "-b:a:0", "192k",
           "-metadata:s:a:0", "title=Audio Description",
           "-metadata:s:a:0", "language=eng",
           "-disposition:a:0", "default"]
    if not replace:
        cmd += ["-map", "0:a:0?", "-c:a:1", "copy",
                "-disposition:a:1", "0",
                "-metadata:s:a:1", "title=Original Audio"]
    if with_subs:
        cmd += ["-map", "0:s?", "-c:s", "copy"]
    cmd += ["-map_chapters", "0", output]
    return cmd


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def patch(input_a, input_b, output=None, replace=False, log=print,
          progress=None):
    """Full pipeline: classify inputs, align, verify, mux.

    Returns a dict:
        output      path of the written file
        a, b        alignment (video_time = a*ad_time + b, b in seconds)
        report      [(ad_sec, video_sec, score, residual_ms), ...]
        weak        number of report rows below the weak-match threshold
        suspect     True if most segments matched poorly (probably a
                    different cut / extended audio description)
        timings     {phase: seconds} for analyze (wall time; video_env
                    and ad_env run in parallel inside it), align, mux,
                    total
    `progress(phase, fraction)`, if given, is called as work advances;
    phases in order: "analyze", "align", "mux".

    Raises ValueError / RuntimeError with a user-readable message on
    failure.
    """
    def report(phase):
        return (lambda f: progress(phase, f)) if progress else None

    for f in (input_a, input_b):
        if not os.path.isfile(f):
            raise ValueError(f"file not found: {f}")
    video, ad_audio = classify_inputs(input_a, input_b)
    output = output or os.path.splitext(video)[0] + ".AD.mkv"
    if os.path.splitext(output)[1].lower() not in (".mkv", ".mp4", ".m4v"):
        output += ".mkv"

    timings = {}
    t_start = clock = time.perf_counter()

    def lap(phase):
        nonlocal clock
        now = time.perf_counter()
        timings[phase] = now - clock
        clock = now

    log(f"video: {os.path.basename(video)}")
    log(f"AD audio: {os.path.basename(ad_audio)}")

    # Both envelopes decode in parallel: the work is in the ffmpeg child
    # processes and numpy, so threads overlap fine despite the GIL. The
    # phase finishes with the slower one, so that one drives progress.
    log("analyzing video and AD audio...")
    fracs = {"video": 0.0, "ad": 0.0}

    def analyze(which, path):
        t0 = time.perf_counter()

        def prog(f):
            fracs[which] = f
            if progress:
                progress("analyze", min(fracs.values()))
        env = cached_envelope(path, prog)
        timings[f"{which}_env"] = time.perf_counter() - t0
        log(f"  {which}: {len(env) / FPS / 60:.1f} min "
            f"({timings[which + '_env']:.1f}s)")
        return env

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_v = pool.submit(analyze, "video", video)
        fut_a = pool.submit(analyze, "ad", ad_audio)
        vid_env, ad_env = fut_v.result(), fut_a.result()
    lap("analyze")

    log("aligning...")
    if progress:
        progress("align", 0.0)
    a, b_sec, rows = fit_alignment(ad_env, vid_env, log=log)
    if progress:
        progress("align", 1.0)
    lap("align")

    drift_ppm = (a - 1.0) * 1e6
    log(f"  offset: {b_sec:+.3f}s   speed factor: {a:.6f} "
        f"({drift_ppm:+.0f} ppm)")
    if abs(a - 25 / 23.976) < 0.002 or abs(a - 23.976 / 25) < 0.002:
        log("  note: drift matches PAL<->film speed difference "
            "(different framerate sources) - correcting")

    log("  sync check (AD time -> video time, match score, residual):")
    weak = 0
    for ad_t, vid_t, score, resid in rows:
        flag = ""
        if score <= WEAK_SCORE:
            weak += 1
            flag = "  <- weak match"
        log(f"    {ad_t / 60:6.1f}m -> {vid_t / 60:6.1f}m   "
            f"score {score:4.2f}   {resid:+6.0f} ms{flag}")
    suspect = weak > len(rows) // 2
    if suspect:
        log("  warning: most segments matched poorly - the AD file may be "
            "for a different cut of this video (or an extended audio "
            "description, which pauses the video and cannot be patched "
            "with a time shift). Output may be out of sync.")

    log("muxing (video stream copied, AD audio encoded)...")
    duration = len(vid_env) / FPS

    def mux(with_subs, encoder):
        return run_ffmpeg(build_mux_cmd(video, ad_audio, output, a, b_sec,
                                        replace, with_subs, encoder),
                          duration, report("mux"))

    encoder = ad_encoder()
    rc, err = mux(True, encoder)
    if rc != 0 and "Subtitle" in err:
        # some subtitle codecs (e.g. mp4 mov_text) can't be copied into
        # mkv - retry without subtitles rather than failing the whole job
        rc, err = mux(False, encoder)
    if rc != 0 and encoder != AD_ENCODERS[-1]:
        # the fast encoder can reject odd inputs (channel layouts etc.)
        log(f"  {encoder[0]} failed - retrying with ffmpeg's aac")
        encoder = AD_ENCODERS[-1]
        rc, err = mux(False, encoder)
    if rc != 0:
        raise RuntimeError(f"muxing failed:\n{err}")
    lap("mux")
    timings["total"] = time.perf_counter() - t_start

    log(f"done: {output} ({os.path.getsize(output) / 1e6:.0f} MB)")
    mb = os.path.getsize(video) / 1e6
    log(f"timings: analyze {timings['analyze']:.1f}s (video "
        f"{timings['video_env']:.1f}s, "
        f"{mb / max(timings['video_env'], 1e-9):.0f} MB/s; AD "
        f"{timings['ad_env']:.1f}s) | align {timings['align']:.1f}s | "
        f"mux {timings['mux']:.1f}s | total {timings['total']:.1f}s")
    log("AD track is the default audio; original audio kept as track 2."
        if not replace else "original audio replaced with AD track.")
    return {"output": output, "a": a, "b": b_sec, "report": rows,
            "weak": weak, "suspect": suspect, "timings": timings}


def main():
    p = argparse.ArgumentParser(
        description="Patch a video with its audio-description track.")
    p.add_argument("inputs", nargs=2, metavar="FILE",
                   help="the video and the AD audio file, in any order")
    p.add_argument("-o", "--output", help="output path (default: "
                   "<video name>.AD.mkv next to the video)")
    p.add_argument("--replace", action="store_true",
                   help="drop the original audio instead of keeping it "
                        "as a second track")
    args = p.parse_args()
    bar = ConsoleProgress() if sys.stderr.isatty() else None
    try:
        patch(args.inputs[0], args.inputs[1],
              output=args.output, replace=args.replace,
              log=bar.log if bar else print,
              progress=bar.update if bar else None)
    except (ValueError, RuntimeError) as e:
        if bar:
            bar.clear()
        sys.exit(f"error: {e}")


PHASE_LABELS = {"analyze": "1/3 analyzing video + AD audio",
                "align": "2/3 aligning",
                "mux": "3/3 writing output"}


class ConsoleProgress:
    """One self-redrawing progress line on stderr, kept below the log:
    log lines clear it, print, and redraw it. ASCII only (cp1252/cp437
    consoles)."""

    WIDTH = 30

    def __init__(self):
        self.line = ""
        self.last = None

    def clear(self):
        if self.line:
            sys.stderr.write("\r" + " " * len(self.line) + "\r")
            sys.stderr.flush()

    def draw(self):
        sys.stderr.write("\r" + self.line)
        sys.stderr.flush()

    def log(self, msg):
        self.clear()
        print(msg, flush=True)
        self.draw()

    def update(self, phase, frac):
        pct = int(frac * 100)
        if (phase, pct) == self.last:
            return
        self.last = (phase, pct)
        filled = int(frac * self.WIDTH)
        self.clear()
        self.line = (f"  [{'#' * filled}{'-' * (self.WIDTH - filled)}] "
                     f"{pct:3d}%  {PHASE_LABELS.get(phase, phase)}")
        self.draw()
        if phase == "mux" and pct >= 100:
            sys.stderr.write("\n")
            self.line = ""


if __name__ == "__main__":
    main()
