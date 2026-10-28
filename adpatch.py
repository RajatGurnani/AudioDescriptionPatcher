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
import json
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

# AD codec choices -> (encoder, priming samples mkv doesn't trim), measured
# against the source at sample level (tools/options_test.py re-checks).
# "aac" means ad_encoder() (aac_mf, falling back to ffmpeg's aac).
AD_CODECS = {"opus": ("libopus", 0), "ac3": ("ac3", 256),
             "eac3": ("eac3", 256)}

# Output options (patch(options=...), CLI flags, web/desktop panel,
# presets). None = today's behaviour.
DEFAULT_OPTIONS = {
    "container": "mkv",        # mkv | mp4 (when no output path is given)
    "ad_codec": "aac",         # aac | opus | ac3 | eac3
    "ad_bitrate": "192k",
    "ad_channels": None,       # None = as the AD file, or 1 / 2
    "ad_language": "eng",
    "ad_title": "Audio Description",
    "default_track": "ad",     # ad | original: which plays by default
    "keep_audio": "first",     # all | first | none | [type indexes]
    "keep_subs": "english",    # english | all | none | [type indexes]
    "add_subs": [],            # [{"path", "language", "title",
                               #   "default", "forced"}]
    "loudness_match": False,   # level the AD to the original's loudness
    "fit_gb": None,            # re-encode video to fit N GB (slow)
    "max_height": None,        # with fit_gb: downscale to this height
    "fit_priority": "speed",   # with fit_gb: speed | size (slowest preset,
                               # best picture per GB)
    "voice_boost": "off",      # off | low | high (dialogue clarity)
    "voice_boost_track": True,  # add "original + voice boost" track
    "voice_boost_ad": False,   # also boost the AD track itself
    "replace_original": False,  # result takes the video's name; the
                                # original goes to the Recycle Bin
}
# Voice boost (dialogue clarity) - the PC twin of android VoiceBoost.kt,
# same parameter table: (side gain, low-shelf dB @150 Hz, presence dB
# @2.5 kHz, duck dB for out-of-band sound while speech, compressor ratio,
# threshold dB, makeup dB). See docs/plan/11-voice-boost.md.
VOICE_BOOST = {
    "low": (0.6, -3.0, 4.0, 4.0, 2.0, -28.0, 5.5),
    "high": (0.35, -6.0, 7.0, 8.0, 3.0, -30.0, 8.5),
}
VOICE_BOOST_LIMITER_MS = 5.0   # alimiter look-ahead, trimmed back off
# ffmpeg's acompressor/sidechain ducking take more level than the Kotlin
# chain; +6 dB extra makeup brings the voice back to ~0 dB like Android
# (tools/voiceboost_test.py) - clarity up, overall loudness not.
VOICE_BOOST_PC_MAKEUP_DB = 6.0


def voice_boost_graph(inp, out, level, tag="vb", rate=AD_RATE,
                      extra_trim_s=0.0):
    """filter_complex fragment: [inp] -> voice-boosted stereo [out].

    Chain (as on Android): low-shelf cut -> side level down (dialogue and
    narration sit in the centre) -> presence peak -> split into the
    speech band (300-3400 Hz) and the rest; the rest is ducked by a
    compressor keyed on the speech band (speech present -> duck) -> RMS
    compressor + makeup -> -1 dBFS limiter. The limiter looks ahead
    VOICE_BOOST_LIMITER_MS, which is trimmed off again so the track stays
    sample-aligned with the original; `extra_trim_s` trims more (encoder
    priming in mkv).
    """
    side, shelf, peak, duck, ratio, thresh, makeup = VOICE_BOOST[level]
    t = tag
    band = "highpass=f=300,lowpass=f=3400"
    duck_ratio = 1 + duck / 2              # ~duck dB at ~4-8 dB over key
    return ";".join([
        f"{inp}aresample={rate},aformat=channel_layouts=stereo,"
        f"lowshelf=f=150:g={shelf},stereotools=slev={side},"
        f"equalizer=f=2500:t=q:w=0.8:g={peak},asplit=4"
        f"[{t}x][{t}y][{t}z][{t}k]",
        f"[{t}x]{band}[{t}band]",
        f"[{t}z]{band},volume=-1[{t}nb]",
        f"[{t}y][{t}nb]amix=inputs=2:normalize=0[{t}rest]",
        f"[{t}k]{band}[{t}key]",
        f"[{t}rest][{t}key]sidechaincompress=threshold=0.02:"
        f"ratio={duck_ratio}:attack=20:release=300[{t}ducked]",
        f"[{t}band][{t}ducked]amix=inputs=2:normalize=0,"
        f"acompressor=threshold={10 ** (thresh / 20):.5f}:ratio={ratio}:"
        f"attack=10:release=200:"
        f"makeup={10 ** ((makeup + VOICE_BOOST_PC_MAKEUP_DB) / 20):.4f},"
        f"alimiter=limit=0.891:attack={VOICE_BOOST_LIMITER_MS}:"
        f"release=80:level=0,"
        f"atrim=start={VOICE_BOOST_LIMITER_MS / 1000 + extra_trim_s:.6f},"
        f"asetpts=PTS-STARTPTS{out}",
    ])


MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "opus", "flac", "alac"}
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
VIDEO_ENCODERS = ("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265")
_video_encoder = None


# Named option sets. Built-ins are read-only; user presets live in
# %LOCALAPPDATA%\ADPatcher\presets.json (web/desktop "Save preset").
BUILTIN_PRESETS = {
    "Default (MKV, AAC, AD + original)": {},
    "TV-friendly (MP4, AC-3, all audio)": {
        "container": "mp4", "ad_codec": "ac3", "ad_bitrate": "448k",
        "keep_audio": "all"},
    "Archive (MKV, Opus, every track)": {
        "container": "mkv", "ad_codec": "opus", "ad_bitrate": "160k",
        "keep_audio": "all", "keep_subs": "all"},
    "Phone (MP4, 720p, max 2 GB)": {
        "container": "mp4", "fit_gb": 2.0, "max_height": 720},
    "AD only (replace original audio)": {"keep_audio": "none"},
    "Clear dialogue (voice boost high, extra track)": {
        "voice_boost": "high"},
}
PRESETS_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "ADPatcher", "presets.json")


def load_presets():
    """Built-in presets + the user's saved ones (user wins on a name)."""
    presets = {k: dict(v) for k, v in BUILTIN_PRESETS.items()}
    try:
        with open(PRESETS_FILE, encoding="utf-8") as f:
            presets.update(json.load(f))
    except (OSError, ValueError):
        pass
    return presets


def save_preset(name, options):
    """Store a user preset (only keys that differ from the defaults)."""
    try:
        with open(PRESETS_FILE, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError):
        user = {}
    user[name] = {k: v for k, v in options.items()
                  if k in DEFAULT_OPTIONS and v != DEFAULT_OPTIONS[k]}
    os.makedirs(os.path.dirname(PRESETS_FILE), exist_ok=True)
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(user, f, indent=1)


def add_option_args(p):
    """Output-option flags shared by the single-file and batch CLIs."""
    g = p.add_argument_group("output options")
    g.add_argument("--preset", help="start from a named preset "
                   "(see --list-presets); flags below override it")
    g.add_argument("--list-presets", action="store_true")
    g.add_argument("--container", choices=("mkv", "mp4"))
    g.add_argument("--ad-codec", choices=("aac", "opus", "ac3", "eac3"))
    g.add_argument("--ad-bitrate", help="e.g. 192k")
    g.add_argument("--ad-channels", type=int, choices=(1, 2))
    g.add_argument("--ad-lang", help="AD track language (default eng)")
    g.add_argument("--ad-title", help="AD track title")
    g.add_argument("--replace-original", action="store_true",
                   help="modify the original video instead of writing a "
                        "copy: the result takes its name and the original "
                        "goes to the Recycle Bin")
    g.add_argument("--original-default", action="store_true",
                   help="make the original audio the default track")
    g.add_argument("--keep-audio", help="original audio tracks to keep: "
                   "all | first (default) | none | e.g. 0,2")
    g.add_argument("--keep-subs", help="subtitle tracks to keep: english "
                   "(default; all if none are language-tagged) | all | "
                   "none | e.g. 1")
    g.add_argument("--add-subs", action="append", default=[],
                   metavar="FILE[:LANG[:forced]]",
                   help="add an external subtitle file (repeatable)")
    g.add_argument("--loudness-match", action="store_true",
                   help="level the AD to the original's loudness")
    g.add_argument("--fit-gb", type=float, help="re-encode the video to "
                   "fit this size (GPU if available; slow)")
    g.add_argument("--fit-priority", choices=("speed", "size"),
                   help="with --fit-gb: speed (default) or size = slowest "
                        "encoder preset, best picture for the size")
    g.add_argument("--max-height", type=int, help="with --fit-gb: "
                   "downscale to this height, e.g. 720")
    g.add_argument("--voice-boost", choices=("off", "low", "high"),
                   help="dialogue clarity: adds an 'original + voice "
                        "boost' track (same DSP as the Android app)")
    g.add_argument("--voice-boost-ad", action="store_true",
                   help="with --voice-boost: also boost the AD track")
    g.add_argument("--no-voice-boost-track", action="store_true",
                   help="with --voice-boost: don't add the extra track")


def options_from_args(args):
    """Options dict from add_option_args() flags (preset first)."""
    if args.list_presets:
        for name, o in load_presets().items():
            print(f"{name}: {o or '(defaults)'}")
        sys.exit(0)
    opts = {}
    if args.preset:
        presets = load_presets()
        if args.preset not in presets:
            sys.exit(f"unknown preset {args.preset!r} - see --list-presets")
        opts.update(presets[args.preset])

    def tracks(v):
        return v if v in ("all", "first", "none", "english") else \
            [int(x) for x in v.split(",") if x.strip()]
    for key, val in (("container", args.container),
                     ("ad_codec", args.ad_codec),
                     ("ad_bitrate", args.ad_bitrate),
                     ("ad_channels", args.ad_channels),
                     ("ad_language", args.ad_lang),
                     ("ad_title", args.ad_title),
                     ("fit_gb", args.fit_gb),
                     ("max_height", args.max_height),
                     ("fit_priority", args.fit_priority)):
        if val is not None:
            opts[key] = val
    if args.original_default:
        opts["default_track"] = "original"
    if args.replace_original:
        opts["replace_original"] = True
    if args.keep_audio:
        opts["keep_audio"] = tracks(args.keep_audio)
    if args.keep_subs:
        opts["keep_subs"] = tracks(args.keep_subs)
    if args.loudness_match:
        opts["loudness_match"] = True
    if args.voice_boost:
        opts["voice_boost"] = args.voice_boost
    if args.voice_boost_ad:
        opts["voice_boost_ad"] = True
    if args.no_voice_boost_track:
        opts["voice_boost_track"] = False
    subs = []
    for spec in args.add_subs:
        # FILE[:LANG[:forced]] - careful with drive letters (C:\...)
        parts = spec.rsplit(":", 2)
        while len(parts) > 1 and (len(parts[0]) == 1 or
                                  not os.path.exists(parts[0])):
            parts = [parts[0] + ":" + parts[1]] + parts[2:]
        subs.append({"path": parts[0],
                     "language": parts[1] if len(parts) > 1 else "",
                     "forced": len(parts) > 2 and parts[2] == "forced"})
    if subs:
        opts["add_subs"] = subs
    return opts


def resolve_options(options=None, replace=False):
    """DEFAULT_OPTIONS overlaid with `options`; `replace` (old API) means
    keep no original audio."""
    o = dict(DEFAULT_OPTIONS)
    o.update({k: v for k, v in (options or {}).items() if v is not None
              or k in ("fit_gb", "max_height", "ad_channels")})
    if replace:
        o["keep_audio"] = "none"
    return o


def video_encoder():
    """First working HEVC encoder: NVIDIA, Intel, AMD GPUs, then CPU."""
    global _video_encoder
    if _video_encoder is None:
        _video_encoder = VIDEO_ENCODERS[-1]
        for name in VIDEO_ENCODERS:
            r = subprocess.run(
                [FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=256x256:d=0.2", "-c:v", name, "-f", "null",
                 "-"], capture_output=True)
            if r.returncode == 0:
                _video_encoder = name
                break
    return _video_encoder


def _bitrate_bps(text):
    text = str(text).strip().lower()
    mult = 1000 if text.endswith("k") else 1_000_000 \
        if text.endswith("m") else 1
    return float(text.rstrip("km")) * mult


GIB = 1024 ** 3   # "fit N GB" means N GB as Windows counts them (GiB)
FIT_MARGIN = 0.97  # container overhead + encoder overshoot
_copied_bytes_cache = {}


def _stats_bytes(line):
    """Bytes in an ffmpeg final-stats line ("video:12KiB audio:3KiB ...")."""
    total = 0.0
    for kind, num, unit in re.findall(
            r"\b(video|audio|subtitle|other streams):\s*([\d.]+)"
            r"\s*(Ki|k|Mi|M)?B", line):
        # ffmpeg's "kB"/"MB" (older builds) are binary too
        total += float(num) * {"Ki": 1024, "k": 1024, "Mi": 1024 ** 2,
                               "M": 1024 ** 2}.get(unit, 1)
    return total


def stream_sizes(path):
    """{stream index: payload bytes} for every stream of `path`, measured
    in ONE `-c copy` read (one null output per stream), cached per file.
    Exact, unlike the BPS/NUMBER_OF_BYTES tags, which releases often copy
    over unchanged from their source; and lossless tracks (TrueHD, DTS-HD,
    FLAC) often have no bitrate in `ffmpeg -i` at all."""
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_size, st.st_mtime)
    if key not in _copied_bytes_cache:
        streams = probe_streams(path)
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-i", path]
        for s in streams:
            cmd += ["-map", f"0:{s['index']}", "-c", "copy", "-f", "null",
                    "-"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace")
        measured = {}
        for k, line in re.findall(r"\[out#(\d+)/null[^\]]*\]\s*(video:.*)",
                                  r.stderr):
            if int(k) < len(streams):
                measured[streams[int(k)]["index"]] = _stats_bytes(line)
        # fallback for anything ffmpeg didn't report: bitrate x duration
        dur = probe_duration(path) or 0.0
        sizes = {s["index"]: measured.get(
            s["index"], (s["kbps"] or 0) * 1000 * dur / 8) for s in streams}
        if r.returncode != 0 or len(measured) != len(streams):
            return sizes   # partly estimated: don't cache it
        _copied_bytes_cache[key] = sizes
    return _copied_bytes_cache[key]


def copied_stream_bytes(path, maps):
    """Exact payload bytes of the streams in `maps` ("0:a:1", "0:s:0",
    "0:v:0") when stream-copied (see stream_sizes())."""
    if not maps:
        return 0
    sizes, streams = stream_sizes(path), probe_streams(path)
    kinds = {"v": "video", "a": "audio", "s": "subtitle"}
    total = 0.0
    for m in maps:
        _, t, i = m.split(":")
        total += sum(sizes.get(s["index"], 0.0) for s in streams
                     if s["type"] == kinds[t] and s["type_index"] == int(i))
    return total


_nvenc_size = None


def _nvenc_size_args():
    """Best-compression extras for hevc_nvenc that THIS GPU accepts (HEVC
    B-frames and temporal AQ need Turing TU10x / Ampere+; a GTX 1650's
    NVENC refuses both). Probed once with a tiny encode, most capable set
    first."""
    global _nvenc_size
    if _nvenc_size is None:
        base = ["-tune", "hq", "-spatial-aq", "1", "-rc-lookahead", "32"]
        taq = ["-temporal-aq", "1"]
        bfr = ["-bf", "4", "-b_ref_mode", "middle"]
        for extra in (base + taq + bfr, base + taq, base, []):
            r = subprocess.run(
                [FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=256x144:d=1", "-c:v", "hevc_nvenc", "-preset",
                 "p7", *extra, "-f", "null", "-"], capture_output=True)
            if r.returncode == 0:
                _nvenc_size = extra
                break
        else:
            _nvenc_size = []
    return _nvenc_size


def _video_args(opts, streams, duration, other_bytes):
    """Encoder args for a fit-to-size re-encode of the first video stream
    (`-c:v copy` when not requested). `other_bytes` = everything else in
    the output (audio, subtitles); `opts["_fit_scale"]` < 1 shrinks the
    video bitrate for a second try when the first came out too big."""
    if not opts.get("fit_gb"):
        return ["-map", "0:v", "-c:v", "copy"], []
    vids = [st for st in streams if st["type"] == "video"
            and not st["attached_pic"]]
    if vids and vids[0]["hdr"]:
        raise ValueError("HDR video: fit-to-size would wash out the "
                         "colours - use HandBrake for HDR re-encodes")
    budget = opts["fit_gb"] * GIB * FIT_MARGIN - other_bytes
    v_bps = budget * 8 / duration * opts.get("_fit_scale", 1.0)
    if v_bps < 300_000:
        raise ValueError(
            f"{opts['fit_gb']} GB is too small for this length "
            f"({duration / 60:.0f} min) with {other_bytes / GIB:.2f} GB of "
            "audio/subtitles kept - drop original audio tracks (lossless "
            "ones are big) or pick a bigger size")
    enc = video_encoder()
    b = f"{int(v_bps)}"
    args = ["-map", "0:v:0", "-c:v", enc, "-b:v", b,
            "-maxrate", f"{int(v_bps * 1.25)}", "-bufsize", f"{int(v_bps * 2)}"]
    small = opts.get("fit_priority") == "size"
    if enc == "hevc_nvenc":
        args += ["-preset", "p7" if small else "p5", "-rc", "vbr",
                 "-multipass", "fullres"]
        if small:   # slowest + smartest: more bits where the eye looks
            args += _nvenc_size_args()
    elif enc == "hevc_qsv":
        if small:
            args += ["-preset", "veryslow"]
    elif enc == "hevc_amf":
        args += ["-rc", "vbr_peak"]
        if small:
            args += ["-quality", "quality"]
    elif enc == "libx265":
        args += ["-preset", "slow" if small else "medium"]
    args += ["-tag:v", "hvc1"]   # plays in Apple/TV players from mp4
    if opts.get("max_height"):
        args += ["-vf", f"scale=-2:'min({int(opts['max_height'])},ih)'"]
    notes = [f"video re-encoded with {enc} at {v_bps / 1e6:.2f} Mb/s "
             f"to fit {opts['fit_gb']} GB (as Windows counts)"]
    return args, notes

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


_STREAM_RE = re.compile(
    r"Stream #0:(\d+)(?:\[0x[0-9a-f]+\])?(?:\((\w+)\))?: "
    r"(Video|Audio|Subtitle|Data|Attachment): (\w+)(.*)")


def probe_streams(path):
    """Streams of a media file from `ffmpeg -i` (no ffprobe available):
    [{index, type, type_index, codec, language, title, default, forced,
      channels, kbps, attached_pic, hdr, details}] - type_index counts
    per type, i.e. the N of ffmpeg's 0:a:N / 0:s:N specifiers."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                          capture_output=True, text=True, errors="replace")
    streams, counts, cur = [], {}, None
    for line in proc.stderr.splitlines():
        m = _STREAM_RE.search(line)
        if m:
            idx, lang, typ, codec, rest = m.groups()
            typ = typ.lower()
            n = counts.get(typ, 0)
            counts[typ] = n + 1
            kbps = re.search(r"(\d+) kb/s", rest)
            ch = re.search(r"Hz, ([^,]+)", rest)
            cur = {"index": int(idx), "type": typ, "type_index": n,
                   "codec": codec, "language": lang or "", "title": "",
                   "default": "(default)" in rest,
                   "forced": "(forced)" in rest,
                   "channels": ch.group(1).strip() if ch else "",
                   "kbps": int(kbps.group(1)) if kbps else None,
                   "attached_pic": "attached pic" in rest,
                   "hdr": "smpte2084" in rest or "arib-std-b67" in rest,
                   "details": rest.strip(" ,")}
            streams.append(cur)
        elif cur is not None and re.match(r"\s+title\s*:", line):
            cur["title"] = line.split(":", 1)[1].strip()
        elif cur is not None and not cur["title"] and \
                re.match(r"\s+handler_name\s*:", line):
            # mp4 keeps track names in the handler name
            name = line.split(":", 1)[1].strip()
            if name not in ("SoundHandler", "VideoHandler",
                            "SubtitleHandler"):
                cur["title"] = name
        elif cur is not None and not line.startswith("      "):
            cur = None if "Stream #" not in line else cur
    return streams


def mean_volume_db(path, stream="0:a:0"):
    """Average loudness (dB) of one audio stream via ffmpeg volumedetect -
    a full decode, so only used for the opt-in loudness match."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", path, "-map",
                           stream, "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True, errors="replace")
    m = re.search(r"mean_volume: (-?[\d.]+) dB", proc.stderr)
    return float(m.group(1)) if m else None


def probe_has_audio(path):
    """True if ffmpeg sees an audio stream."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                          capture_output=True, text=True, errors="replace")
    return any("Audio:" in line for line in proc.stderr.splitlines())


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


class Cancelled(Exception):
    """Raised from a `progress` callback to stop a job: the running ffmpeg
    is killed and patch() removes its partial output."""


def replace_target(video, opts):
    """Where "replace the original" writes: the video's own name, with the
    container's extension (Movie.mp4 + mkv output -> Movie.mkv)."""
    return os.path.splitext(video)[0] + (
        ".mp4" if opts["container"] == "mp4" else ".mkv")


def has_ad_track(path, title="Audio Description"):
    """True if `path` already carries our AD track (a replaced original
    keeps its name, so the name can't tell)."""
    return any(st["type"] == "audio" and st["title"] == title
               for st in probe_streams(path))


def recycle(path):
    """Move a file to the Windows Recycle Bin (undoable delete)."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, \
        FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW(
        None, FO_DELETE, os.path.abspath(path) + "\0", None,
        FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
        False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted or os.path.exists(path):
        raise RuntimeError(f"couldn't move {path} to the Recycle Bin "
                           f"(code {rc}) - nothing was changed")


def run_ffmpeg(cmd, duration=None, progress=None):
    """Run an ffmpeg command, reporting completion 0..1 to `progress` from
    its -progress output. Returns (returncode, stderr_text). If `progress`
    raises (Cancelled, Ctrl+C), ffmpeg is killed first."""
    cmd = [cmd[0], "-nostats", "-progress", "pipe:1"] + cmd[1:]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    err = _drain(proc.stderr)
    try:
        for line in proc.stdout:
            if progress and duration and line.startswith(b"out_time_us="):
                try:
                    us = int(line.split(b"=", 1)[1])
                except ValueError:   # "N/A" before the first frame
                    continue
                progress(min(1.0, max(0.0, us / 1e6 / duration)))
    except BaseException:
        proc.kill()
        proc.wait()
        raise
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
    try:
        while True:
            chunk = proc.stdout.read(bytes_per_read)
            if not chunk:
                break
            done += len(chunk)
            if total:
                progress(min(1.0, done / total))   # may raise Cancelled
            buf = leftover + chunk
            usable = (len(buf) // 4 // HOP) * HOP * 4
            if usable:
                x = np.frombuffer(buf[:usable], dtype=np.float32)
                energies.append((x.reshape(-1, HOP) ** 2).sum(axis=1))
            leftover = buf[usable:]
    except BaseException:
        proc.kill()
        proc.wait()
        raise
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


def _speed_scan(ad_env, vid_env, factors, scores=None):
    """Try each candidate speed factor: stretch the AD envelope, FFT
    cross-correlate against the video envelope, and score the peak.

    The correct factor lines up onsets across the WHOLE runtime, producing
    a far sharper global peak than any wrong factor. Returns
    (best_factor, best_lag_frames, best_score); every (factor, score) is
    appended to `scores` if given.
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
        if scores is not None:
            scores.append((f, score))
        if score > best[0]:
            best = (score, f, lag)
    return best[1], best[2], best[0]


def _peak_ratio(ad_env, vid_env, factor, exclude, lags=()):
    """Uniqueness of the global correlation peak at `factor`: best peak /
    best peak more than `exclude` frames away from it (and from every
    lag in `lags` - the offsets of the pieces of a different edit, which
    are all genuine). ~1 means another offset fits about as well
    (repetitive audio, e.g. a looping score)."""
    stretched = _stretch(ad_env, factor)
    n = 1 << int(np.ceil(np.log2(len(stretched) + len(vid_env))))
    cc = np.fft.irfft(np.fft.rfft(vid_env, n) *
                      np.conj(np.fft.rfft(stretched, n)), n)
    k = int(np.argmax(cc))
    idx = np.arange(n)
    keep = np.ones(n, dtype=bool)
    for c in (k, *[int(round(x)) % n for x in lags]):
        dist = np.minimum(np.abs(idx - c), n - np.abs(idx - c))  # circular
        keep &= dist > exclude
    if not keep.any():
        return 10.0
    second = cc[keep].max()
    return float(min(10.0, cc[k] / second)) if second > 0 else 10.0


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

    Returns (a, b_seconds, report, signals): report rows are
    (ad_seconds, video_seconds, score, residual_ms, used) per checked
    segment (`used` = kept in the final fit); signals feed confidence().
    """
    # Stage 1: coarse-to-fine speed scan on pooled (20 Hz) envelopes.
    pool = 5
    ad20, vid20 = _pool(ad_env, pool), _pool(vid_env, pool)
    coarse_grid = np.arange(0.95, 1.0501, 0.002)
    coarse_scores = []
    a, _, _ = _speed_scan(ad20, vid20, coarse_grid, coarse_scores)
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
    used = {g[0] for g in confident}
    report = []
    for u, y, score in matches:
        residual_ms = (y - (a2 * u + b2)) / FPS * 1000
        center_ad = (u + seg_len // 2) / a0
        report.append((center_ad / FPS, (a * center_ad + b) / FPS,
                       score, residual_ms, u in used))

    # Evidence for confidence(): how decisive, unique and consistent the
    # fit is (all from numbers computed above, plus one extra FFT).
    best_coarse = max(sc for _, sc in coarse_scores)
    far = [sc for f, sc in coarse_scores if abs(f - a0) >= 0.004]
    resid_used = [abs(r[3]) for r in report if r[4]]
    signals = {
        "peak": float(peak),
        "peak_ratio": _peak_ratio(ad20, vid20, a0, exclude=2 * FPS / pool),
        "speed_margin": float((best_coarse - max(far)) / best_coarse)
        if far and best_coarse > 0 else 0.0,
        "coverage": len([m for m in matches if m[2] > MIN_SEGMENT_SCORE])
        / max(1, len(matches)),
        "inlier_frac": len(used) / max(1, len(
            [m for m in matches if m[2] > MIN_SEGMENT_SCORE])),
        "resid_p95_ms": float(np.percentile(resid_used, 95))
        if resid_used else 1000.0,
        "dur_ratio": float(len(vid_env) / max(1.0, a * len(ad_env))),
    }

    # Different edit? A linear map that leaves a good part of the runtime
    # unmatched (while the files clearly share a soundtrack) is the
    # signature of scenes added/removed. Try piecewise offsets; it only
    # wins if it explains clearly more of the movie (no phantom cuts).
    pieces = None
    if signals["coverage"] < 0.9 and signals["peak"] >= 0.04:
        pw = _fit_piecewise(stretched, vid_env, ad20, vid20, a0, pool,
                            signals, log)
        if pw is not None:
            a, b, report, signals, pieces = pw
    return float(a), float(b / FPS), report, signals, pieces


# --------------------------------------------------------------------------
# Piecewise alignment (different edits)
# --------------------------------------------------------------------------
# Model: video_time = a * ad_time + b_k for AD time in piece k - one speed
# (it's a property of the source frame rate) and an offset per piece.
# Approach adapted from ADSync (MIT, see docs/plan/THIRD_PARTY.md): a
# lattice of offset candidates per analysis window, decoded with Viterbi
# under a constant jump penalty, then vetted into pieces. Re-implemented
# for onset envelopes with a twist: candidates are first clustered into
# "offset families" seen in >= PW_MIN_RUN windows, and every window is
# then scored at every family offset - so each lattice cell carries real
# evidence instead of synthetic pass-through candidates.

PW_WIN = int(20 * FPS)      # analysis window
PW_HOP = int(10 * FPS)      # window step
PW_MIN_RUN = 3              # windows a piece needs (~30-40 s of AD)
PW_MAX_PIECES = 64          # beyond this the files are probably unrelated
PW_FAMILY_TOL = 0.3 * FPS   # offsets closer than this are one family


def _window_candidates(st, vid_env, starts, pool, k=3):
    """Global search: for each AD window (start frame in the stretched AD
    envelope) the top-k video offsets (frames) with their normalized
    correlation, computed at 20 Hz against the whole video envelope."""
    st20, v20 = _pool(st, pool), _pool(vid_env, pool)
    L = PW_WIN // pool
    valid = len(v20) - L + 1
    if valid <= 0:
        return [[] for _ in starts]
    n = 1 << int(np.ceil(np.log2(len(v20) + L)))
    V = np.fft.rfft(v20, n)
    csum = np.concatenate(([0.0], np.cumsum(v20.astype(np.float64) ** 2)))
    vnorm = np.sqrt(np.maximum(csum[L:L + valid] - csum[:valid], 0.0))
    nms = int(1 * FPS / pool)
    out = []
    for u in starts:
        seg = st20[u // pool:u // pool + L]
        sn = np.linalg.norm(seg)
        if len(seg) < L or sn == 0:
            out.append([])
            continue
        cc = np.fft.irfft(V * np.conj(np.fft.rfft(seg, n)), n)[:valid]
        sc = cc / (sn * vnorm + 1e-9)
        cands = []
        for _ in range(k):
            j = int(np.argmax(sc))
            if sc[j] <= 0:
                break
            cands.append((j * pool - u, float(sc[j])))
            sc[max(0, j - nms):j + nms + 1] = -1.0
        out.append(cands)
    return out


def _offset_families(cands, base_offset):
    """Cluster candidate offsets; keep those seen in >= PW_MIN_RUN windows.
    The linear fit's offset is always a family (so the linear answer is
    one of the paths Viterbi can take)."""
    best = [c[0][1] for c in cands if c]
    if not best:
        return [base_offset]
    thr = max(0.12, 0.5 * float(np.median(best)))
    pts = sorted((off, w) for w, cs in enumerate(cands)
                 for off, sc in cs if sc >= thr)
    groups, cur = [], []
    for off, w in pts:
        if cur and off - cur[-1][0] > PW_FAMILY_TOL:
            groups.append(cur)
            cur = []
        cur.append((off, w))
    if cur:
        groups.append(cur)
    fams = [float(np.median([o for o, _ in g])) for g in groups
            if len({w for _, w in g}) >= PW_MIN_RUN]
    if all(abs(f - base_offset) > PW_FAMILY_TOL for f in fams):
        fams.append(float(base_offset))
    return sorted(fams)


def _viterbi(scores, jump):
    """Max-sum path through windows x families; switching family costs
    `jump`. Returns one family index per window."""
    n_win, n_fam = scores.shape
    dp = scores[0].copy()
    back = np.zeros((n_win, n_fam), dtype=int)
    for i in range(1, n_win):
        g = int(np.argmax(dp))
        switch = dp[g] - jump
        take = switch > dp
        back[i] = np.where(take, g, np.arange(n_fam))
        dp = np.where(take, switch, dp) + scores[i]
    path = [int(np.argmax(dp))]
    for i in range(n_win - 1, 0, -1):
        path.append(int(back[i][path[-1]]))
    return path[::-1]


def _runs(path):
    """[(family, first_window, last_window), ...] of consecutive runs."""
    runs, start = [], 0
    for i in range(1, len(path) + 1):
        if i == len(path) or path[i] != path[start]:
            runs.append((path[start], start, i - 1))
            start = i
    return runs


def _vet_runs(path, scores):
    """Merge runs shorter than PW_MIN_RUN windows into the neighbour whose
    offset explains them better (ADSync's segment vetting idea)."""
    path = list(path)
    while True:
        runs = _runs(path)
        short = [r for r in runs if r[2] - r[1] + 1 < PW_MIN_RUN]
        if len(runs) == 1 or not short:
            return path
        fam, i0, i1 = min(short, key=lambda r: r[2] - r[1])
        idx = runs.index((fam, i0, i1))
        options = [runs[j][0] for j in (idx - 1, idx + 1)
                   if 0 <= j < len(runs)]
        best = max(options, key=lambda f: scores[i0:i1 + 1, f].sum())
        path[i0:i1 + 1] = [best] * (i1 - i0 + 1)


def _sim(st, vid_env, p, length, off):
    """Cosine similarity of AD frames [p, p+length) against the video at a
    fixed offset (0 if out of range)."""
    v0 = p + int(round(off))
    if p < 0 or p + length > len(st) or v0 < 0 or \
            v0 + length > len(vid_env):
        return 0.0
    a, v = st[p:p + length], vid_env[v0:v0 + length]
    return float(a @ v / (np.linalg.norm(a) * np.linalg.norm(v) + 1e-9))


def _split_point(st, vid_env, lo, hi, off_a, off_b, probe, step,
                 thr=None):
    """Where (stretched AD frame) piece A hands over to piece B within
    [lo, hi): maximize A-evidence before + B-evidence after. Ties go to
    the LATEST point: AD content that matches neither offset (an
    insertion) then stays with A and gets dropped later by the overlap
    rule, instead of being placed into the video by B. Returns
    (frame, strength threshold used) - pass the threshold back in for a
    finer pass, whose short probes would otherwise mistake chance
    correlations for evidence."""
    ps = np.arange(lo, max(lo + 1, hi - probe), step)
    sa = np.array([_sim(st, vid_env, p, probe, off_a) for p in ps])
    sb = np.array([_sim(st, vid_env, p, probe, off_b) for p in ps])
    # probes matching neither offset well (inserted material) carry no
    # evidence: zero them so they form an exact tie, resolved latest
    if thr is None:
        thr = 0.4 * float(np.percentile(np.maximum(sa, sb), 90))
    d = np.where(np.maximum(sa, sb) >= thr, sa - sb, 0.0)
    # score(t) = sum(d[:t]) - sum(d[t:])  ->  2*cumsum - total
    c = np.concatenate(([0.0], np.cumsum(d)))
    gain = 2 * c - c[-1]
    t = int(np.flatnonzero(gain >= gain.max() - 1e-9)[-1])
    if t >= len(ps):
        return int(ps[-1] + probe), thr
    return (int(ps[t] + probe // 2) if t > 0 else int(ps[0])), thr


def _fit_piecewise(st, vid_env, ad20, vid20, a0, pool, lin_signals, log):
    """Piecewise fit on the stretched AD envelope `st`. Returns
    (a, b_frames_of_longest_piece, report, signals, pieces) or None when
    one offset explains the movie as well."""
    usable = len(st) - PW_WIN
    if usable < PW_HOP * PW_MIN_RUN:
        return None
    starts = list(range(0, usable + 1, PW_HOP))
    cands = _window_candidates(st, vid_env, starts, pool)
    base = cands and max((c[0] for c in cands if c), key=lambda x: x[1],
                         default=(0, 0))[0]
    fams = _offset_families(cands, base)
    if len(fams) < 2:
        return None

    # score every window at every family offset (small local search
    # absorbs the residual wobble)
    n_w, n_f = len(starts), len(fams)
    scores = np.zeros((n_w, n_f))
    pos = np.full((n_w, n_f), np.nan)
    search = int(0.5 * FPS)
    for i, u in enumerate(starts):
        for f, off in enumerate(fams):
            m = segment_match(st, vid_env, u, PW_WIN, u + off, search)
            if m is not None:
                pos[i, f], scores[i, f] = m[0], max(0.0, m[1])

    best_single = (scores > MIN_SEGMENT_SCORE).sum(axis=0).max() / n_w
    jump = max(0.2, 1.5 * float(np.median(scores.max(axis=1))))
    path = _vet_runs(_viterbi(scores, jump), scores)
    runs = _runs(path)
    covered = sum(scores[i, f] > MIN_SEGMENT_SCORE
                  for i, f in enumerate(path)) / n_w
    if len(runs) < 2 or len(runs) > PW_MAX_PIECES or \
            covered < max(best_single, lin_signals["coverage"]) + 0.1:
        return None

    # hand-over points between consecutive pieces: coarse (4 s probes),
    # then fine (1 s probes, 50 ms steps) around the coarse answer
    bounds = [0]
    for (fa, _, ia1), (fb, ib0, _) in zip(runs, runs[1:]):
        # one extra window either side: Viterbi may hand a window of
        # unmatched (inserted) material to either piece
        lo = max(bounds[-1], starts[ia1] - PW_HOP)
        hi = min(len(st), starts[ib0] + PW_WIN + PW_HOP)
        x, thr = _split_point(st, vid_env, lo, hi, fams[fa], fams[fb],
                              int(4 * FPS), int(0.25 * FPS))
        x, _ = _split_point(st, vid_env, max(0, x - int(2.5 * FPS)),
                            x + int(2.5 * FPS), fams[fa], fams[fb],
                            int(1 * FPS), int(0.05 * FPS), thr)
        bounds.append(max(bounds[-1] + 1, x))
    bounds.append(len(st))

    # joint weighted fit: one slope a2, one intercept per piece, using
    # windows that lie fully inside their piece
    pts = []
    for k, (f, i0, i1) in enumerate(runs):
        for i in range(i0, i1 + 1):
            u = starts[i]
            if scores[i, f] > MIN_SEGMENT_SCORE and \
                    bounds[k] <= u and u + PW_WIN <= bounds[k + 1]:
                pts.append((u, pos[i, f], scores[i, f], k))
    n_p = len(runs)
    a2, c = 1.0, np.array(fams)[[r[0] for r in runs]]
    used = list(pts)
    for _ in range(2):
        if len(used) < 2:
            break
        X = np.zeros((len(used), 1 + n_p))
        X[:, 0] = [q[0] for q in used]
        for j, q in enumerate(used):
            X[j, 1 + q[3]] = 1.0
        y = np.array([q[1] for q in used])
        w = np.sqrt([q[2] for q in used])
        present = X[:, 1:].any(axis=0)
        cols = np.concatenate(([True], present))
        sol, *_ = np.linalg.lstsq(X[:, cols] * w[:, None], y * w,
                                  rcond=None)
        a2 = sol[0]
        c = c.copy()
        c[present] = sol[1:]
        resid = np.abs(y - (X[:, cols] @ sol))
        keep = resid < max(3 * np.median(resid) + 1e-9, 0.05 * FPS)
        if keep.all() or keep.sum() < 2:
            break
        used = [q for q, kk in zip(used, keep) if kk]

    a = a0 * a2
    pieces = []
    for k in range(n_p):
        ad0, ad1 = bounds[k] / a0 / FPS, bounds[k + 1] / a0 / FPS
        b_k = c[k] / FPS
        n_in = sum(1 for q in pts if q[3] == k)
        pieces.append({"ad_start": float(ad0), "ad_end": float(ad1),
                       "b": float(b_k),
                       "video_start": float(a * ad0 + b_k),
                       "video_end": float(a * ad1 + b_k),
                       "windows": n_in})
    longest = max(range(n_p), key=lambda k: bounds[k + 1] - bounds[k])

    used_set = {(q[0], q[3]) for q in used}
    report = []
    for k, (f, i0, i1) in enumerate(runs):
        for i in range(i0, i1 + 1):
            u = starts[i]
            if np.isnan(pos[i, f]):
                continue
            resid_ms = (pos[i, f] - (a2 * u + c[k])) / FPS * 1000
            center_ad = (u + PW_WIN // 2) / a0
            report.append((center_ad / FPS,
                           (a * center_ad + c[k]) / FPS, scores[i, f],
                           resid_ms, (u, k) in used_set))

    resid_used = [abs(r[3]) for r in report if r[4]]
    signals = dict(lin_signals)
    signals.update({
        "peak_ratio": _peak_ratio(ad20, vid20, a0, exclude=2 * FPS / pool,
                                  lags=[f / pool for f in fams]),
        "coverage": float(covered),
        "inlier_frac": len(used) / max(1, len(pts)),
        "resid_p95_ms": float(np.percentile(resid_used, 95))
        if resid_used else 1000.0,
        # the length difference is what the pieces explain
        "dur_ratio": 1.0,
        "pieces": n_p,
    })
    log(f"  different edit: {n_p} pieces " + ", ".join(
        f"{q['ad_start'] / 60:.1f}-{q['ad_end'] / 60:.1f}m "
        f"({q['b']:+.2f}s)" for q in pieces))
    return a, float(c[longest]), report, signals, pieces


def _clamp01(x):
    return float(min(1.0, max(0.0, x)))


def confidence(sig):
    """Overall confidence 0..1 + band + human-readable warnings.

    Weighted product, so one catastrophic factor sinks the score. Factor
    list inspired by ADSync's align/confidence.py (MIT; see
    docs/plan/THIRD_PARTY.md); breakpoints tuned on tools/harness.py -
    see docs/plan/03-confidence-report.md.
    """
    c_peak = _clamp01((sig["peak"] - 0.03) / 0.15)
    c_unique = _clamp01((sig["peak_ratio"] - 1.05) / 0.6)
    c_cover = sig["coverage"]
    c_resid = _clamp01(1 - (sig["resid_p95_ms"] - 40) / 160)
    c_dur = _clamp01(1 - (abs(sig["dur_ratio"] - 1) - 0.03) / 0.12)
    score = (c_peak ** 0.25 * c_unique ** 0.15 * c_cover ** 0.3 *
             c_resid ** 0.2 * c_dur ** 0.1)
    band = ("good" if score >= 0.75 else
            "check" if score >= 0.45 else "likely wrong")

    warnings = []
    if sig["peak"] < 0.04 or sig["coverage"] < 0.15:
        warnings.append("These files don't seem to share a soundtrack - "
                        "is this the AD file for this video?")
    elif sig["coverage"] < 0.3 and sig["dur_ratio"] < 0.95:
        warnings.append("The AD file is much longer than the video and "
                        "matches poorly: probably extended audio "
                        "description (pauses the video), which a time "
                        "shift can't fix.")
    elif abs(sig["dur_ratio"] - 1) > 0.03 and sig["coverage"] < 0.7:
        warnings.append("The AD file looks like a different edit of the "
                        "movie (lengths differ and parts don't match). "
                        "Some scenes may be out of sync.")
    if sig["peak_ratio"] < 1.2 and sig["coverage"] >= 0.3:
        warnings.append("Repetitive audio: the offset could be wrong by a "
                        "whole repeat - spot-check the start.")
    if sig["resid_p95_ms"] > 120 and sig["coverage"] >= 0.3:
        warnings.append(f"Sync wobbles by up to "
                        f"{sig['resid_p95_ms']:.0f} ms in places.")
    return round(score, 3), band, warnings


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


def plan_pieces(a, pieces, priming_s=0.0):
    """Turn fitted pieces into render instructions on the video timeline.

    Each piece's AD range [ad_start, ad_end) lands at video time
    a*t + b. Where a piece runs into the next one's start (the AD has
    material the video lacks - an insertion), it is cut there: the later
    piece wins. Where pieces leave a hole (the video has material the AD
    lacks), a gap is returned. `priming_s` (encoder delay that the
    container won't trim) is subtracted from every placement.

    Returns (segments, gaps): segments = [(ad_from, ad_to, delay_s)],
    gaps = [(video_from, video_to, delay_s)] - delay_s already
    priming-corrected and >= 0.
    """
    placed = []
    for i, q in enumerate(pieces):
        t0 = a * q["ad_start"] + q["b"]
        t1 = a * q["ad_end"] + q["b"]
        if i + 1 < len(pieces):
            nxt = pieces[i + 1]
            t1 = min(t1, a * nxt["ad_start"] + nxt["b"])
        if placed:
            t0 = max(t0, placed[-1][3])
        if t1 - t0 < 0.05:
            continue   # fully covered by its neighbours
        placed.append(((t0 - q["b"]) / a, (t1 - q["b"]) / a, t0, t1))

    segments, gaps = [], []
    for i, (s0, s1, t0, t1) in enumerate(placed):
        d0 = t0 - priming_s
        if d0 < 0:   # starts before the video: drop the overhang
            s0 += -d0 / a
            d0 = 0.0
        if s1 - s0 > 0.01:
            segments.append((s0, s1, d0))
        if i + 1 < len(placed) and placed[i + 1][2] - t1 > 0.05:
            g0, g1 = t1, placed[i + 1][2]
            gaps.append((g0, g1, max(0.0, g0 - priming_s)))
    return segments, gaps


def _piecewise_graph(a, pieces, priming_s, fill_gaps, gain=""):
    """filter_complex for a multi-piece AD: each piece trimmed, tempo-
    corrected, faded (10 ms - no clicks at splices) and delayed to its
    video position, plus optional original-audio fill for gaps; all
    summed (they don't overlap) into [ad]."""
    segments, gaps = plan_pieces(a, pieces, priming_s)
    if not fill_gaps:
        gaps = []
    tempo = (f",atempo={1.0 / a:.8f}"
             if abs(a - 1.0) > NEGLIGIBLE_DRIFT else "")
    # mixing needs one layout; the original may be 5.1 -> stereo for all
    fmt = ",aformat=channel_layouts=stereo" if gaps else ""

    def tail(dur, delay):
        fade = (f",afade=t=in:d=0.01,afade=t=out:st={max(0, dur - 0.01):.4f}"
                ":d=0.01" if dur > 0.05 else "")
        return f"{fade},adelay=delays={round(delay * AD_RATE)}S:all=1"

    parts, labels = [], []
    parts.append(f"[1:a:0]asplit={len(segments)}" +
                 "".join(f"[s{i}]" for i in range(len(segments))))
    for i, (s0, s1, d0) in enumerate(segments):
        parts.append(f"[s{i}]atrim=start={s0:.4f}:end={s1:.4f},"
                     f"asetpts=PTS-STARTPTS,aresample={AD_RATE}{tempo}"
                     f"{gain}{fmt}"
                     f"{tail((s1 - s0) * a, d0)}[p{i}]")
        labels.append(f"[p{i}]")
    if gaps:
        parts.append(f"[0:a:0]asplit={len(gaps)}" +
                     "".join(f"[o{i}]" for i in range(len(gaps))))
        for i, (g0, g1, d0) in enumerate(gaps):
            parts.append(f"[o{i}]atrim=start={g0:.4f}:end={g1:.4f},"
                         f"asetpts=PTS-STARTPTS,aresample={AD_RATE}{fmt}"
                         f"{tail(g1 - g0, d0)}[g{i}]")
            labels.append(f"[g{i}]")
    # fresh sample-count timestamps: amix can emit a timestamp-less frame
    # at the end, which the mp4 muxer rejects with ffmpeg's aac
    parts.append("".join(labels) + f"amix=inputs={len(labels)}:"
                 "normalize=0:duration=longest,asetpts=N/SR/TB[ad]")
    return ";".join(parts)


def english_tracks(tracks):
    """English tracks (eng/en). If no track carries a language tag we
    can't tell, so all are kept rather than dropping untagged English."""
    if not any(t["language"] and t["language"] != "und" for t in tracks):
        return tracks
    return [t for t in tracks if t["language"] in ("eng", "en")]


def build_mux_cmd(video, ad_audio, output, a, b_sec, opts=None,
                  streams=None, encoder=None, pieces=None, fill_gaps=True,
                  with_subs=True, gain_db=0.0, duration=None):
    """Assemble the ffmpeg command that applies the alignment and muxes.

    The AD audio gets: atempo (speed correction, skipped when negligible),
    then adelay (positive offset) or atrim (negative offset); with 2+
    `pieces` (a different edit) it is spliced per piece and, if
    `fill_gaps`, holes get the original audio. It becomes track a:0.
    `opts` (resolve_options()) picks the AD codec, which original audio
    and subtitle tracks to keep, external subtitles, default track and
    an optional fit-to-size video re-encode; otherwise the video is
    copied bit-for-bit. `streams` = probe_streams(video). Returns
    (cmd, notes) - notes say what was converted or dropped and why.
    """
    opts = opts or resolve_options()
    streams = streams if streams is not None else probe_streams(video)
    mp4 = os.path.splitext(output)[1].lower() in (".mp4", ".m4v")
    if opts["ad_codec"] in AD_CODECS:
        encoder = AD_CODECS[opts["ad_codec"]]
    enc_name, priming = encoder or ad_encoder()
    # Encoders may prepend priming samples (ffmpeg aac: 1024, ac3: 256);
    # mp4 trims them via an edit list, mkv doesn't - so the AD would play
    # late. Start it that much earlier to cancel it.
    priming_s = 0.0 if mp4 else priming / AD_RATE
    gain = f",volume={gain_db:.2f}dB" if abs(gain_db) >= 0.1 else ""
    notes = []

    # Fixed output rate: keeps the encoder priming delay known.
    filters = [f"aresample={AD_RATE}"]
    if abs(a - 1.0) > NEGLIGIBLE_DRIFT:
        # content at ad_time t must land at a*t, so play at rate 1/a
        filters.append(f"atempo={1.0 / a:.8f}")
    if gain:
        filters.append(gain[1:])
    b_sec -= priming_s
    if b_sec >= 0:
        delay_ms = int(round(b_sec * 1000))
        if delay_ms:
            filters.append(f"adelay=delays={delay_ms}:all=1")
    else:
        filters.append(f"atrim=start={-b_sec:.4f}")
        filters.append("asetpts=PTS-STARTPTS")
    graph = f"[1:a:0]{','.join(filters)}[ad]"
    if pieces and len(pieces) > 1:
        graph = _piecewise_graph(a, pieces, priming_s, fill_gaps, gain)

    audio = [st for st in streams if st["type"] == "audio"]
    vb = opts.get("voice_boost", "off")
    vb = vb if vb in VOICE_BOOST else None
    if vb and opts.get("voice_boost_ad"):
        # both AD graphs end in [ad]: boost it on the way out (the boost
        # adds no delay - its limiter look-ahead is trimmed)
        graph = graph[:-len("[ad]")] + "[ad0];" + \
            voice_boost_graph("[ad0]", "[ad]", vb, "vba")
        notes.append(f"AD track voice-boosted ({vb})")
    vb_track = bool(vb and opts.get("voice_boost_track") and audio)
    if vb_track:
        # extra track from the first original audio; mkv needs the same
        # encoder-priming trim as the AD (mp4's edit list handles it)
        graph += ";" + voice_boost_graph("[0:a:0]", "[vbt]", vb, "vbt",
                                         extra_trim_s=priming_s)
    subs = [st for st in streams if st["type"] == "subtitle"]

    def pick(choice, tracks):
        if choice == "all":
            return tracks
        if choice == "first":
            return tracks[:1]
        if choice in ("none", None):
            return []
        if choice == "english":
            return english_tracks(tracks)
        return [t for t in tracks if t["type_index"] in choice]
    keep_a, keep_s = pick(opts["keep_audio"], audio), \
        pick(opts["keep_subs"], subs) if with_subs else []

    cmd = [FFMPEG, "-y", "-v", "error", "-i", video, "-i", ad_audio]
    for sub in opts["add_subs"]:
        cmd += ["-i", sub["path"]]
    cmd += ["-filter_complex", graph]

    # video (copy, or fit-to-size re-encode)
    other_bytes = 0
    if opts.get("fit_gb"):
        dur = duration or 1.0
        copied = [t for t in keep_a
                  if not (mp4 and t["codec"] not in MP4_AUDIO_OK)]
        subs_kept = [t for t in keep_s
                     if not (mp4 and t["codec"] not in TEXT_SUBS)]
        encoded_bps = _bitrate_bps(opts["ad_bitrate"]) + \
            384_000 * (len(keep_a) - len(copied)) + \
            (192_000 if vb_track else 0)
        other_bytes = copied_stream_bytes(
            video, [f"0:a:{t['type_index']}" for t in copied] +
            [f"0:s:{t['type_index']}" for t in subs_kept]) + \
            encoded_bps * dur / 8 + \
            sum(os.path.getsize(s["path"]) for s in opts["add_subs"])
    v_args, v_notes = _video_args(opts, streams, duration or 1.0,
                                  other_bytes)
    cmd += v_args
    notes += v_notes

    # a:0 = the AD track
    ad_default = opts["default_track"] != "original" or not keep_a
    cmd += ["-map", "[ad]", "-c:a:0", enc_name,
            "-b:a:0", str(opts["ad_bitrate"]),
            "-metadata:s:a:0", f"title={opts['ad_title']}",
            # mp4 players show the handler name as the track name
            "-metadata:s:a:0", f"handler_name={opts['ad_title']}",
            "-metadata:s:a:0", f"language={opts['ad_language']}",
            "-disposition:a:0", "default" if ad_default else "0"]
    if opts["ad_channels"]:
        cmd += ["-ac:a:0", str(int(opts["ad_channels"]))]

    # kept original audio tracks
    for k, t in enumerate(keep_a, start=1):
        cmd += ["-map", f"0:a:{t['type_index']}"]
        if mp4 and t["codec"] not in MP4_AUDIO_OK:
            cmd += [f"-c:a:{k}", "aac", f"-b:a:{k}", "384k"]
            notes.append(f"original audio track {t['type_index'] + 1} "
                         f"({t['codec']}) converted to AAC - mp4 can't "
                         "hold it")
        else:
            cmd += [f"-c:a:{k}", "copy"]
        cmd += [f"-disposition:a:{k}",
                "default" if (k == 1 and not ad_default) else "0"]
        if not t["title"]:
            cmd += [f"-metadata:s:a:{k}", "title=Original Audio",
                    f"-metadata:s:a:{k}", "handler_name=Original Audio"]

    # extra track: original audio + voice boost (never default)
    if vb_track:
        k = 1 + len(keep_a)
        cmd += ["-map", "[vbt]", f"-c:a:{k}", enc_name, f"-b:a:{k}", "192k",
                f"-disposition:a:{k}", "0",
                f"-metadata:s:a:{k}", f"title=Original + voice boost ({vb})",
                f"-metadata:s:a:{k}",
                f"handler_name=Original + voice boost ({vb})"]
        if audio[0]["language"]:
            cmd += [f"-metadata:s:a:{k}", f"language={audio[0]['language']}"]
        notes.append(f"extra track: original audio + voice boost ({vb})")

    # subtitles: kept + external
    out_s = 0
    for t in keep_s:
        if mp4 and t["codec"] not in TEXT_SUBS:
            notes.append(f"subtitle track {t['type_index'] + 1} "
                         f"({t['codec']}, image-based) dropped - mp4 "
                         "can't hold it")
            continue
        cmd += ["-map", f"0:s:{t['type_index']}",
                f"-c:s:{out_s}", "mov_text" if mp4 else "copy"]
        out_s += 1
    for i, sub in enumerate(opts["add_subs"], start=2):
        ext = os.path.splitext(sub["path"])[1].lower()
        cmd += ["-map", f"{i}:s:0", f"-c:s:{out_s}",
                "mov_text" if mp4 else ("webvtt" if ext == ".vtt"
                                        else "copy")]
        if sub.get("language"):
            cmd += [f"-metadata:s:s:{out_s}", f"language={sub['language']}"]
        if sub.get("title"):
            cmd += [f"-metadata:s:s:{out_s}", f"title={sub['title']}"]
        flags = [f for f in ("default", "forced") if sub.get(f)]
        cmd += [f"-disposition:s:{out_s}", "+".join(flags) or "0"]
        out_s += 1
    # Interleaving: copied tracks are ready instantly, the AD comes through
    # decode + filters + encoder. With ffmpeg's default 10 s leeway the
    # first ~16 s of video were written before any audio, so players
    # starting from 0 had no audio until a seek (owner's report). 60 s
    # waits that out; 0 (wait forever) also works but buffers the whole
    # gap between cues of a sparse subtitle track (446 MB on a 2.2 GB film
    # vs 50 MB with a cap) - too much on big remuxes.
    cmd += ["-max_interleave_delta", "60000000"]   # microseconds
    cmd += ["-map_chapters", "0", output]
    return cmd, notes


# --------------------------------------------------------------------------
# Sync report: data for plots, JSON + standalone SVG
# --------------------------------------------------------------------------

PLOT_HZ = 10   # envelope overview resolution sent to the UI / drawn in SVG


def plot_data(vid_env, ad_env, a, b_sec, hz=PLOT_HZ, pieces=None):
    """Video envelope and the *aligned* AD envelope on the video timeline,
    max-pooled to `hz` and clipped at 0 (onsets only) - for eyeballing
    that peaks line up. Returns two float32 arrays of equal length."""
    t = np.arange(len(vid_env)) / FPS
    if pieces and len(pieces) > 1:
        ad_pos = np.full(len(t), -1.0)   # -1 = no AD here (gap)
        for s0, s1, d0 in plan_pieces(a, pieces)[0]:
            m = (t >= d0) & (t < d0 + (s1 - s0) * a)
            ad_pos[m] = (s0 + (t[m] - d0) / a) * FPS
    else:
        ad_pos = (t - b_sec) / a * FPS      # AD frame for each video frame
    ad_on_video = np.interp(ad_pos, np.arange(len(ad_env)), ad_env,
                            left=0.0, right=0.0)
    f = int(FPS // hz)
    n = len(vid_env) // f * f

    def pool_max(x):
        return np.maximum(x[:n], 0).reshape(-1, f).max(axis=1) \
            .astype(np.float32)
    return pool_max(vid_env), pool_max(ad_on_video)


def report_json(result):
    """The JSON-serialisable part of a patch() result."""
    keys = ("output", "a", "b", "confidence", "band", "warnings",
            "signals", "segments", "pieces", "notes", "weak", "suspect",
            "timings")
    return {k: result[k] for k in keys}


def write_report_svg(path, result, plot):
    """Standalone SVG: drift (residual per segment) + envelope overlay.
    No dependencies; opens in any browser."""
    W, PAD = 900, 50
    vid, ad = plot
    dur_min = len(vid) / PLOT_HZ / 60 or 1
    x = lambda minutes: PAD + (W - 2 * PAD) * minutes / dur_min  # noqa: E731
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" '
           f'height="460" font-family="sans-serif" font-size="12">',
           '<rect width="100%" height="100%" fill="#fff"/>',
           f'<text x="{PAD}" y="22" font-size="15">Sync report - '
           f'confidence {result["confidence"]:.2f} ({result["band"]}), '
           f'speed {result["a"]:.6f}, ' + (
               f'{len(result["pieces"])} pieces (different edit)'
               if len(result["pieces"]) > 1 else
               f'offset {result["b"]:+.3f} s') + '</text>']
    # drift panel: y = residual ms, clamped to +-500
    top, h = 40, 160
    y = lambda ms: top + h / 2 - h / 2 * max(-500, min(500, ms)) / 500  # noqa: E731,E501
    out.append(f'<rect x="{PAD}" y="{y(50)}" width="{W - 2 * PAD}" '
               f'height="{y(-50) - y(50)}" fill="#dff3e4"/>')
    out.append(f'<line x1="{PAD}" x2="{W - PAD}" y1="{y(0)}" y2="{y(0)}" '
               'stroke="#999"/>')
    for ms in (500, 50, -50, -500):
        out.append(f'<text x="{PAD - 6}" y="{y(ms) + 4}" text-anchor="end" '
                   f'fill="#666">{ms:+d}</text>')
    out.append(f'<text x="{PAD}" y="{top - 4}" fill="#333">residual '
               '(ms) per 30 s segment - green band = in sync (&#177;50 ms)'
               '</text>')
    for seg in result["segments"]:
        color = "#2a7d46" if seg["used"] else "#aaa"
        out.append(f'<circle cx="{x(seg["video"] / 60):.1f}" '
                   f'cy="{y(seg["residual_ms"]):.1f}" r="4" '
                   f'fill="{color}"><title>{seg["video"] / 60:.1f} min: '
                   f'{seg["residual_ms"]:+.0f} ms, score '
                   f'{seg["score"]:.2f}</title></circle>')
    for q in result["pieces"][1:]:   # where each new piece starts
        px = x(q["video_start"] / 60)
        out.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{top}" '
                   f'y2="{top + h}" stroke="#c08a1a" stroke-dasharray="4"/>')
    # overlay panel: video up, aligned AD mirrored down
    mid, half = 330, 90
    cols = min(len(vid), W - 2 * PAD)
    step = max(1, len(vid) // cols)
    m = len(vid) // step * step
    v = vid[:m].reshape(-1, step).max(axis=1)
    d = ad[:m].reshape(-1, step).max(axis=1)
    scale = max(np.percentile(v, 99.5), np.percentile(d, 99.5), 1e-6)
    pts_v = " ".join(f"{PAD + i:.0f},{mid - half * min(1, val / scale):.1f}"
                     for i, val in enumerate(v))
    pts_d = " ".join(f"{PAD + i:.0f},{mid + half * min(1, val / scale):.1f}"
                     for i, val in enumerate(d))
    out += [f'<text x="{PAD}" y="{mid - half - 8}" fill="#333">audio '
            'onsets: video (up) vs aligned AD (down) - peaks should line '
            'up</text>',
            f'<polyline fill="none" stroke="#3d6bd6" points="{pts_v}"/>',
            f'<polyline fill="none" stroke="#c0601a" points="{pts_d}"/>',
            f'<line x1="{PAD}" x2="{W - PAD}" y1="{mid}" y2="{mid}" '
            'stroke="#999"/>']
    for i in range(0, int(dur_min) + 1, max(1, int(dur_min) // 10 or 1)):
        out.append(f'<text x="{x(i):.0f}" y="{mid + half + 18}" '
                   f'text-anchor="middle" fill="#666">{i} min</text>')
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def patch(input_a, input_b, output=None, replace=False, log=print,
          progress=None, save_report=False, options=None):
    """Full pipeline: classify inputs, align, verify, mux.

    Returns a dict:
        output      path of the written file
        a, b        alignment (video_time = a*ad_time + b, b in seconds)
        confidence  0..1 overall score; band "good" / "check" /
                    "likely wrong"; warnings: list of sentences
        signals     raw evidence behind the score (see confidence())
        segments    [{ad, video, score, residual_ms, used}, ...]
        report      the same rows as tuples (older callers)
        plot        (video_env, aligned_ad_env) at PLOT_HZ for charts
        weak        number of report rows below the weak-match threshold
        suspect     True if the result is probably wrong (most segments
                    weak or band "likely wrong")
        timings     {phase: seconds} for analyze (wall time; video_env
                    and ad_env run in parallel inside it), align, mux,
                    total
    `progress(phase, fraction)`, if given, is called as work advances;
    phases in order: "analyze", "align", "mux". `save_report` also writes
    <output>.sync.json and <output>.sync.svg. `options`: see
    DEFAULT_OPTIONS (container, AD codec, tracks, subtitles, size...);
    result["notes"] lists conversions/drops they caused.

    Raises ValueError / RuntimeError with a user-readable message on
    failure.
    """
    def report(phase):
        return (lambda f: progress(phase, f)) if progress else None

    for f in (input_a, input_b):
        if not os.path.isfile(f):
            raise ValueError(f"file not found: {f}")
    video, ad_audio = classify_inputs(input_a, input_b)
    opts = resolve_options(options, replace)
    if opts["replace_original"]:
        output = replace_target(video, opts)
        if os.path.normcase(os.path.abspath(output)) != os.path.normcase(
                os.path.abspath(video)) and os.path.exists(output):
            raise ValueError(
                f"{os.path.basename(output)} already exists next to the "
                "video - modifying the original would overwrite it. Move "
                "it away, or turn off 'modify the original'.")
    output = output or (os.path.splitext(video)[0] + ".AD." +
                        ("mp4" if opts["container"] == "mp4" else "mkv"))
    if os.path.splitext(output)[1].lower() not in (".mkv", ".mp4", ".m4v"):
        output += ".mkv"
    for sub in opts["add_subs"]:
        if not os.path.isfile(sub["path"]):
            raise ValueError(f"subtitle file not found: {sub['path']}")
    if opts["fit_gb"]:   # fail fast, before minutes of analysis
        dur = probe_duration(video) or 1.0
        _video_args(opts, probe_streams(video), dur,
                    _bitrate_bps(opts["ad_bitrate"]) * dur / 8)

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
    a, b_sec, rows, signals, pieces = fit_alignment(ad_env, vid_env,
                                                    log=log)
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
    for ad_t, vid_t, score, resid, _ in rows:
        flag = ""
        if score <= WEAK_SCORE:
            weak += 1
            flag = "  <- weak match"
        log(f"    {ad_t / 60:6.1f}m -> {vid_t / 60:6.1f}m   "
            f"score {score:4.2f}   {resid:+6.0f} ms{flag}")
    conf, band, warnings = confidence(signals)
    log(f"  confidence {conf:.2f} ({band})")
    for w in warnings:
        log(f"  warning: {w}")
    suspect = weak > len(rows) // 2 or band == "likely wrong"
    if suspect and not warnings:
        log("  warning: most segments matched poorly - the AD file may be "
            "for a different cut of this video (or an extended audio "
            "description, which pauses the video and cannot be patched "
            "with a time shift). Output may be out of sync.")

    streams = probe_streams(video)
    gain_db = 0.0
    if opts["loudness_match"]:
        log("measuring loudness (original vs AD)...")
        orig = mean_volume_db(video) if any(
            st["type"] == "audio" for st in streams) else None
        adv = mean_volume_db(ad_audio)
        if orig is not None and adv is not None:
            gain_db = max(-12.0, min(12.0, orig - adv))
            log(f"  AD level {gain_db:+.1f} dB to match the original")
    log("muxing (" + ("video re-encoded" if opts["fit_gb"] else
                      "video stream copied") + ", AD audio encoded)...")
    duration = len(vid_env) / FPS

    # gaps between pieces get the original audio - only if there is one
    fill = bool(pieces) and probe_has_audio(video)
    if pieces:
        segs, gaps = plan_pieces(a, pieces)
        gap_s = sum(g1 - g0 for g0, g1, _ in gaps)
        drop_s = len(ad_env) / FPS - sum(s1 - s0 for s0, s1, _ in segs)
        log(f"  {gap_s:.0f}s of video has no AD "
            f"({'original audio used' if fill else 'silent'}); "
            f"{max(0.0, drop_s):.0f}s of AD has no matching video "
            "(dropped)")

    notes = []
    video_mb = os.path.getsize(video) / 1e6
    # mux into a .partial file and rename it when ffmpeg succeeds, so a
    # failed or interrupted run never leaves a half-written output that
    # looks finished (batch skips outputs that already exist)
    root, ext = os.path.splitext(output)
    part = root + ".partial" + ext

    def mux(with_subs, encoder):
        cmd, n = build_mux_cmd(video, ad_audio, part, a, b_sec, opts,
                               streams, encoder, pieces, fill, with_subs,
                               gain_db, duration)
        notes[:] = n
        return run_ffmpeg(cmd, duration, report("mux"))

    encoder = None if opts["ad_codec"] in AD_CODECS else ad_encoder()
    try:
        rc, err = mux(True, encoder)
        if rc != 0 and "Subtitle" in err:
            # some subtitle codecs can't go into the container - retry
            # without subtitles rather than failing the whole job
            log("  subtitles couldn't be muxed - retrying without them")
            rc, err = mux(False, encoder)
            notes.append("subtitles dropped: the container rejected them")
        if rc != 0 and encoder is not None and encoder != AD_ENCODERS[-1]:
            # the fast encoder can reject odd inputs (channel layouts etc.)
            log(f"  {encoder[0]} failed - retrying with ffmpeg's aac")
            encoder = AD_ENCODERS[-1]
            rc, err = mux(True, encoder)
        if rc != 0:
            raise RuntimeError(f"muxing failed:\n{err}")
        target = (opts["fit_gb"] or 0) * GIB
        if target and os.path.getsize(part) > target:
            # VBR overshot: one more encode, bitrate scaled down to fit
            # only the video shrinks: scale it by (room left for video) /
            # (video it actually took); audio/subs stay as they were
            size = os.path.getsize(part)
            video_bytes = copied_stream_bytes(part, ["0:v:0"])
            room = target * 0.98 - (size - video_bytes)
            scale = max(0.1, room / max(video_bytes, 1.0))
            opts = dict(opts, _fit_scale=scale)
            log(f"  {size / GIB:.2f} GB is over {opts['fit_gb']} GB - "
                f"encoding again at {scale:.0%} of the video bitrate")
            subs_ok = not any(n.startswith("subtitles dropped")
                              for n in notes)
            rc, err = mux(subs_ok, encoder)   # (mux resets `notes`)
            if rc != 0:
                raise RuntimeError(f"muxing failed:\n{err}")
            if not subs_ok:
                notes.append("subtitles dropped: the container rejected "
                             "them")
            if os.path.getsize(part) > target:
                notes.append(f"still {os.path.getsize(part) / GIB:.2f} GB "
                             f"after a second encode (target "
                             f"{opts['fit_gb']} GB)")
        if opts["replace_original"]:
            # the original must be safely in the Recycle Bin before the
            # result takes its name; if that fails, nothing has changed
            log(f"  moving the original to the Recycle Bin: {video}")
            recycle(video)
        else:
            os.replace(part, output)
    except BaseException:   # incl. Ctrl+C: no half-written leftovers
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    if opts["replace_original"]:
        try:
            os.replace(part, output)
        except OSError as e:
            raise RuntimeError(
                f"the patched video is at {part} but couldn't be renamed "
                f"to {os.path.basename(output)} ({e}); the original is in "
                "the Recycle Bin") from e
    for n in notes:
        log(f"  note: {n}")
    lap("mux")
    timings["total"] = time.perf_counter() - t_start

    log(f"done: {output} ({os.path.getsize(output) / 1e6:.0f} MB)")
    mb = video_mb   # (the video may be in the Recycle Bin by now)
    log(f"timings: analyze {timings['analyze']:.1f}s (video "
        f"{timings['video_env']:.1f}s, "
        f"{mb / max(timings['video_env'], 1e-9):.0f} MB/s; AD "
        f"{timings['ad_env']:.1f}s) | align {timings['align']:.1f}s | "
        f"mux {timings['mux']:.1f}s | total {timings['total']:.1f}s")
    log("AD track is the default audio." if opts["default_track"] != "original"
        else "original audio is the default; AD is track 1.")
    result = {
        "output": output, "a": a, "b": b_sec,
        "confidence": conf, "band": band, "warnings": warnings,
        "signals": signals,
        "segments": [{"ad": r[0], "video": r[1], "score": r[2],
                      "residual_ms": r[3], "used": r[4]} for r in rows],
        "pieces": pieces or [], "notes": notes,
        "report": rows, "weak": weak, "suspect": suspect,
        "timings": timings,
        "plot": plot_data(vid_env, ad_env, a, b_sec, pieces=pieces),
    }
    if save_report:
        stem = os.path.splitext(output)[0]
        with open(stem + ".sync.json", "w", encoding="utf-8") as f:
            json.dump(report_json(result), f, indent=1)
        write_report_svg(stem + ".sync.svg", result, result["plot"])
        log(f"sync report: {stem}.sync.svg (+ .json)")
    return result


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "batch":
        import batch   # folder mode: content-based pairing + queue
        return batch.main(sys.argv[2:])
    p = argparse.ArgumentParser(
        description="Patch a video with its audio-description track.")
    p.add_argument("inputs", nargs="*", metavar="FILE",
                   help="the video and the AD audio file, in any order")
    p.add_argument("-o", "--output", help="output path (default: "
                   "<video name>.AD.mkv next to the video)")
    p.add_argument("--replace", action="store_true",
                   help="drop the original audio instead of keeping it "
                        "as a second track")
    p.add_argument("--report", action="store_true",
                   help="also write <output>.sync.json and .sync.svg "
                        "(drift + waveform-match plot)")
    p.add_argument("--list-streams", action="store_true",
                   help="show the tracks of the given file(s) and exit "
                        "(indexes for --keep-audio / --keep-subs)")
    add_option_args(p)
    args = p.parse_args()
    options = options_from_args(args)
    if args.list_streams:
        for f in args.inputs:
            print(f)
            for st in probe_streams(f):
                if st["type"] in ("audio", "subtitle", "video"):
                    flags = " ".join(x for x in ("default", "forced")
                                     if st[x])
                    print(f"  {st['type']:<8} {st['type_index']}  "
                          f"{st['codec']:<10} {st['language'] or '-':<4} "
                          f"{st['title'] or ''}  {flags}")
        return
    if len(args.inputs) != 2:
        p.error("give the video and the AD audio file")
    bar = ConsoleProgress() if sys.stderr.isatty() else None
    try:
        patch(args.inputs[0], args.inputs[1],
              output=args.output, replace=args.replace,
              save_report=args.report, options=options,
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
