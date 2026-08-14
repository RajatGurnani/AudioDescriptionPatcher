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
import os
import subprocess
import sys

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

def onset_envelope(path):
    """Decode the first audio stream and return its onset-strength envelope.

    The envelope is the half-wave-rectified frame-to-frame change in log
    energy, z-normalized, at 100 Hz. Loudness *changes* survive both the
    narration mixed over the AD soundtrack and level/EQ differences between
    releases, which makes this far more robust than correlating waveforms.

    Decoding is streamed through ffmpeg a minute at a time - a 3-hour movie
    never occupies more than a few MB of memory here.
    """
    cmd = [FFMPEG, "-v", "error", "-i", path, "-map", "0:a:0",
           "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    energies = []
    leftover = b""
    bytes_per_read = 4 * SR * 60
    while True:
        chunk = proc.stdout.read(bytes_per_read)
        if not chunk:
            break
        buf = leftover + chunk
        usable = (len(buf) // 4 // HOP) * HOP * 4
        if usable:
            x = np.frombuffer(buf[:usable], dtype=np.float32)
            energies.append((x.reshape(-1, HOP) ** 2).sum(axis=1))
        leftover = buf[usable:]
    err = proc.stderr.read().decode(errors="replace")
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

def build_mux_cmd(video, ad_audio, output, a, b_sec, replace, with_subs):
    """Assemble the ffmpeg command that applies the alignment and muxes.

    The AD audio gets: atempo (speed correction, skipped when negligible),
    then adelay (positive offset) or atrim (negative offset). The video
    stream is copied bit-for-bit. The corrected AD track becomes the
    default audio; the original audio is copied as track 2 unless
    `replace`.
    """
    filters = []
    if abs(a - 1.0) > NEGLIGIBLE_DRIFT:
        # content at ad_time t must land at a*t, so play at rate 1/a
        filters.append(f"atempo={1.0 / a:.8f}")
    if b_sec >= 0:
        delay_ms = int(round(b_sec * 1000))
        if delay_ms:
            filters.append(f"adelay=delays={delay_ms}:all=1")
    else:
        filters.append(f"atrim=start={-b_sec:.4f}")
        filters.append("asetpts=PTS-STARTPTS")
    chain = ",".join(filters) if filters else "anull"

    cmd = [FFMPEG, "-y", "-v", "error", "-stats",
           "-i", video, "-i", ad_audio,
           "-filter_complex", f"[1:a:0]{chain}[ad]",
           "-map", "0:v", "-c:v", "copy",
           "-map", "[ad]", "-c:a:0", "aac", "-b:a:0", "192k",
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

def patch(input_a, input_b, output=None, replace=False, log=print):
    """Full pipeline: classify inputs, align, verify, mux.

    Returns a dict:
        output      path of the written file
        a, b        alignment (video_time = a*ad_time + b, b in seconds)
        report      [(ad_sec, video_sec, score, residual_ms), ...]
        weak        number of report rows below the weak-match threshold
        suspect     True if most segments matched poorly (probably a
                    different cut / extended audio description)
    Raises ValueError / RuntimeError with a user-readable message on
    failure.
    """
    for f in (input_a, input_b):
        if not os.path.isfile(f):
            raise ValueError(f"file not found: {f}")
    video, ad_audio = classify_inputs(input_a, input_b)
    output = output or os.path.splitext(video)[0] + ".AD.mkv"
    if os.path.splitext(output)[1].lower() not in (".mkv", ".mp4", ".m4v"):
        output += ".mkv"

    log(f"video: {os.path.basename(video)}")
    log(f"AD audio: {os.path.basename(ad_audio)}")

    log("analyzing video audio...")
    vid_env = onset_envelope(video)
    log(f"  {len(vid_env) / FPS / 60:.1f} min")
    log("analyzing AD audio...")
    ad_env = onset_envelope(ad_audio)
    log(f"  {len(ad_env) / FPS / 60:.1f} min")

    log("aligning...")
    a, b_sec, report = fit_alignment(ad_env, vid_env, log=log)

    drift_ppm = (a - 1.0) * 1e6
    log(f"  offset: {b_sec:+.3f}s   speed factor: {a:.6f} "
        f"({drift_ppm:+.0f} ppm)")
    if abs(a - 25 / 23.976) < 0.002 or abs(a - 23.976 / 25) < 0.002:
        log("  note: drift matches PAL<->film speed difference "
            "(different framerate sources) - correcting")

    log("  sync check (AD time -> video time, match score, residual):")
    weak = 0
    for ad_t, vid_t, score, resid in report:
        flag = ""
        if score <= WEAK_SCORE:
            weak += 1
            flag = "  <- weak match"
        log(f"    {ad_t / 60:6.1f}m -> {vid_t / 60:6.1f}m   "
            f"score {score:4.2f}   {resid:+6.0f} ms{flag}")
    suspect = weak > len(report) // 2
    if suspect:
        log("  warning: most segments matched poorly - the AD file may be "
            "for a different cut of this video (or an extended audio "
            "description, which pauses the video and cannot be patched "
            "with a time shift). Output may be out of sync.")

    log("muxing (video stream copied, AD audio encoded)...")
    r = subprocess.run(build_mux_cmd(video, ad_audio, output, a, b_sec,
                                     replace, with_subs=True),
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0 and "Subtitle" in r.stderr:
        # some subtitle codecs (e.g. mp4 mov_text) can't be copied into
        # mkv - retry without subtitles rather than failing the whole job
        r = subprocess.run(build_mux_cmd(video, ad_audio, output, a, b_sec,
                                         replace, with_subs=False),
                           capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"muxing failed:\n{r.stderr}")

    log(f"done: {output} ({os.path.getsize(output) / 1e6:.0f} MB)")
    log("AD track is the default audio; original audio kept as track 2."
        if not replace else "original audio replaced with AD track.")
    return {"output": output, "a": a, "b": b_sec, "report": report,
            "weak": weak, "suspect": suspect}


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
    try:
        patch(args.inputs[0], args.inputs[1],
              output=args.output, replace=args.replace)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
