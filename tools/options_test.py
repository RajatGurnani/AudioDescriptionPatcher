#!/usr/bin/env python3
"""Output-options test: tracks, subtitles, AD codecs x containers (with
sync), loudness match, fit-to-size.

    .venv\\Scripts\\python tools\\options_test.py [--no-fit]

Builds a 4-min test movie with two audio tracks (PCM "eng", AC-3 "spa")
and two SRT subtitle tracks (eng, fre), plus an external SRT and a PAL-
drifted AD file, then runs adpatch.patch() with different options and
checks the output's streams (codec, language, title, default/forced) and
that the AD track is in sync (independently measured, <= 20 ms). Fit-to-
size re-encodes the --realistic harness video (run tools/harness.py
--realistic --minutes 20 once). Media in %TEMP%/adpatch-options. Exit 1 on
failure.
"""

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(errors="replace")
import adpatch  # noqa: E402
import harness  # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), "adpatch-options")
fails = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name +
          (f"  ({detail})" if detail else ""))
    if not ok:
        fails.append(name)


def ff(*args):
    subprocess.run([adpatch.FFMPEG, "-y", "-v", "error", *args], check=True)


def srt(path, label):
    with open(path, "w", encoding="utf-8") as f:
        for i in range(1, 6):
            f.write(f"{i}\n00:00:{i * 5:02d},000 --> 00:00:{i * 5 + 3:02d},"
                    f"000\n{label} line {i}\n\n")


def prepare():
    os.makedirs(WORK, exist_ok=True)
    wav = os.path.join(WORK, "src.wav")
    video = os.path.join(WORK, "movie.mkv")
    ad = os.path.join(WORK, "movie_AD.mp3")
    quiet = os.path.join(WORK, "movie_AD_quiet.mp3")
    ext = os.path.join(WORK, "extra.spa.srt")
    if not os.path.exists(video):
        harness.write_wav(wav, harness.soundtrack(31, 4.0))
        srt(os.path.join(WORK, "eng.srt"), "English")
        srt(os.path.join(WORK, "fre.srt"), "French")
        # explicit length: -shortest would stop at the last subtitle
        ff("-f", "lavfi", "-i", "color=c=black:s=160x90:r=24:d=240",
           "-i", wav, "-i", wav, "-i", os.path.join(WORK, "eng.srt"),
           "-i", os.path.join(WORK, "fre.srt"),
           "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
           "-map", "4:s", "-c:v", "libx264", "-preset", "ultrafast",
           "-c:a:0", "pcm_s16le", "-c:a:1", "ac3", "-b:a:1", "192k",
           "-c:s", "srt",
           "-metadata:s:a:0", "language=eng",
           "-metadata:s:a:1", "language=spa",
           "-metadata:s:a:1", "title=Spanish",
           "-metadata:s:s:0", "language=eng",
           "-metadata:s:s:1", "language=fre", video)
        ff("-i", wav, "-af", harness.SPEEDUP, "-c:a", "libmp3lame",
           "-b:a", "128k", ad)
        ff("-i", wav, "-af", harness.SPEEDUP + ",volume=-10dB",
           "-c:a", "libmp3lame", "-b:a", "128k", quiet)
        srt(ext, "Spanish")
    return video, ad, quiet, ext


def run(name, video, ad, out_ext, options):
    out = os.path.join(WORK, f"out_{name}.{out_ext}")
    res = adpatch.patch(video, ad, output=out, log=lambda *a: None,
                        options=options)
    return out, res, adpatch.probe_streams(out)


def kinds(streams, typ):
    return [s for s in streams if s["type"] == typ]


def in_sync(out, ref_env):
    lags = harness.verify_output(out, ref_env)
    worst = max((abs(x) for x in lags), default=float("inf"))
    return len(lags) >= 5 and worst <= harness.MAX_LAG_MS, f"{worst:.1f} ms"


def main():
    os.environ["ADPATCH_NO_CACHE"] = "1"
    video, ad, quiet, ext = prepare()
    ref = adpatch.onset_envelope(video)
    st = adpatch.probe_streams(video)
    check("probe: 1 video, 2 audio, 2 subs",
          [len(kinds(st, t)) for t in ("video", "audio", "subtitle")]
          == [1, 2, 2])
    check("probe: languages/titles",
          [s["language"] for s in st[1:]] == ["eng", "spa", "eng", "fre"]
          and st[2]["title"] == "Spanish", str([s["language"] for s in st]))

    # 1. defaults
    out, res, s = run("default", video, ad, "mkv", None)
    a, sub = kinds(s, "audio"), kinds(s, "subtitle")
    check("default: AD + first original, English subs only",
          len(a) == 2 and [t["language"] for t in sub] == ["eng"],
          f"{len(a)} audio, subs {[t['language'] for t in sub]}")
    # untagged subtitles: can't tell which is English -> keep them all
    untagged = [{"language": "", "type_index": i} for i in range(2)]
    tagged = [{"language": "fre", "type_index": 0},
              {"language": "eng", "type_index": 1}]
    check("english_tracks: untagged kept, tagged filtered",
          len(adpatch.english_tracks(untagged)) == 2 and
          [t["type_index"] for t in adpatch.english_tracks(tagged)] == [1])
    check("default: AD first, default, titled",
          a[0]["default"] and a[0]["title"] == "Audio Description"
          and a[0]["language"] == "eng" and not a[1]["default"])
    check("default: sync", *in_sync(out, ref))
    # mkv track numbers: 1 video, 2 AD, 3 original. Each audio track's
    # first block must come before ~1 s of other content in the file, or
    # players start silent until a seek (owner's report, 2026-10)
    gaps = harness.interleave_gap_ms(out, (2, 3))
    check("default: audio interleaved from the start",
          all(g <= 1000 for g in gaps.values()), str(gaps))

    # 2. all audio, one kept sub + external forced, AC-3 AD, original default
    out, res, s = run("tracks", video, ad, "mkv", {
        "keep_audio": "all", "keep_subs": [1], "ad_codec": "ac3",
        "ad_bitrate": "384k", "default_track": "original",
        "add_subs": [{"path": ext, "language": "spa", "title": "Ext",
                      "forced": True}]})
    a, sub = kinds(s, "audio"), kinds(s, "subtitle")
    check("tracks: 3 audio (AD ac3 + pcm + ac3)",
          [x["codec"] for x in a] == ["ac3", "pcm_s16le", "ac3"],
          str([x["codec"] for x in a]))
    check("tracks: original is default, AD not",
          not a[0]["default"] and a[1]["default"] and not a[2]["default"])
    check("tracks: subs = fre + external spa forced",
          [x["language"] for x in sub] == ["fre", "spa"] and sub[1]["forced"]
          and sub[1]["title"] == "Ext", str([(x["language"], x["forced"])
                                             for x in sub]))
    check("tracks: ac3 in mkv sync (256-sample priming)", *in_sync(out, ref))

    # 3. mp4: pcm original -> aac (note), subs -> mov_text, opus AD
    out, res, s = run("mp4", video, ad, "mp4",
                      {"keep_audio": "all", "keep_subs": "all",
                       "ad_codec": "opus", "ad_bitrate": "128k"})
    a, sub = kinds(s, "audio"), kinds(s, "subtitle")
    check("mp4: audio codecs opus + aac (converted) + ac3",
          [x["codec"] for x in a] == ["opus", "aac", "ac3"],
          str([x["codec"] for x in a]))
    check("mp4: note about conversion",
          any("converted to AAC" in n for n in res["notes"]), res["notes"])
    check("mp4: subs as mov_text", [x["codec"] for x in sub] ==
          ["mov_text", "mov_text"], str([x["codec"] for x in sub]))
    check("mp4: opus sync", *in_sync(out, ref))

    # 4. E-AC-3 mono, no subs, no original audio
    out, res, s = run("eac3", video, ad, "mkv",
                      {"ad_codec": "eac3", "ad_bitrate": "192k",
                       "ad_channels": 1, "keep_audio": "none",
                       "keep_subs": "none"})
    a = kinds(s, "audio")
    check("eac3: only the AD, mono, no subs",
          len(a) == 1 and a[0]["codec"] == "eac3" and
          a[0]["channels"] == "mono" and not kinds(s, "subtitle"),
          a[0]["details"])
    check("eac3: sync", *in_sync(out, ref))

    # 5. loudness match: AD 10 dB quieter -> raised to the original level
    out, res, s = run("loud", video, quiet, "mkv", {"loudness_match": True})
    lv_orig = adpatch.mean_volume_db(video)
    lv_out = adpatch.mean_volume_db(out)
    check("loudness: AD within 1.5 dB of original",
          abs(lv_out - lv_orig) <= 1.5, f"{lv_out:.1f} vs {lv_orig:.1f} dB")
    check("loudness: sync", *in_sync(out, ref))

    # 6. voice boost (PC twin of the Android feature): extra boosted
    # track + boosted AD, in sync, per container / AD encoder priming
    def stream_env(path, idx):
        wav = os.path.join(WORK, f"_a{idx}.wav")
        ff("-i", path, "-map", f"0:a:{idx}", "-ac", "1", "-ar", "8000", wav)
        return adpatch.onset_envelope(wav)

    def lag_ok(env):
        import numpy as np
        L, lags = 500, []
        for s0 in range(1000, min(len(env), len(ref)) - L - 1000, 2000):
            m = adpatch.segment_match(env, ref, s0, L, s0, 100)
            if m and m[1] >= 0.3:
                lags.append((m[0] - s0) / adpatch.FPS * 1000)
        worst = max((abs(x) for x in lags), default=float("inf"))
        return len(lags) >= 5 and worst <= harness.MAX_LAG_MS,             f"{worst:.1f} ms over {len(lags)} windows"

    saved_enc = adpatch._ad_encoder
    for tag, ext, enc in (("vb_mkv", "mkv", None),
                          ("vb_mkv_aac", "mkv", ("aac", 1024)),
                          ("vb_mp4", "mp4", None)):
        adpatch._ad_encoder = enc or saved_enc
        out, res, s = run(tag, video, ad, ext,
                          {"voice_boost": "high", "voice_boost_ad": True})
        a = kinds(s, "audio")
        check(f"{tag}: AD + original + boosted track",
              len(a) == 3 and "voice boost" in a[2]["title"] and
              not a[2]["default"], str([(x["codec"], x["title"]) for x in a]))
        check(f"{tag}: boosted AD in sync", *in_sync(out, ref))
        check(f"{tag}: boosted original in sync", *lag_ok(stream_env(out, 2)))
    adpatch._ad_encoder = saved_enc

    # 7. fit to size (GPU encoder if any) on the realistic harness video
    big = os.path.join(tempfile.gettempdir(), "adpatch-harness",
                       "video_real_20m.mkv")
    big_ad = os.path.join(tempfile.gettempdir(), "adpatch-harness",
                          "ad4_pal_20m.mp3")
    if "--no-fit" in sys.argv:
        print("skip fit-to-size (--no-fit)")
    else:
        if not os.path.exists(big) or not os.path.exists(big_ad):
            print("generating the realistic 20 min harness media ...")
            harness.prepare(os.path.dirname(big), 20, realistic=True)
        target = 0.3
        out, res, s = run("fit", big, big_ad, "mp4",
                          {"fit_gb": target, "max_height": 144})
        size = os.path.getsize(out) / adpatch.GIB
        v = kinds(s, "video")[0]
        check(f"fit: {target} GB target, under it but not far",
              0.85 <= size / target <= 1.0, f"{size:.3f} GB")
        check("fit: hevc, height 144", v["codec"] == "hevc" and
              "x144" in v["details"], v["details"][:60])
        check("fit: encoder note", any("re-encoded" in n
                                       for n in res["notes"]), res["notes"])
        check("fit: sync", *in_sync(out, adpatch.onset_envelope(big)))

        # 8. fit with a lossless track (no bitrate in `ffmpeg -i`) + subs:
        # the old 640 kb/s guess under-counted it and overshot the target
        lossless = os.path.join(WORK, "fit_lossless.mkv")
        srt_path = os.path.join(WORK, "fit.srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            for i in range(200):
                f.write(f"{i + 1}\n00:{i // 10:02d}:{(i % 10) * 6:02d},000 --> "
                        f"00:{i // 10:02d}:{(i % 10) * 6 + 3:02d},000\n"
                        f"line {i}\n\n")
        ff("-i", big, "-i", srt_path, "-map", "0:v", "-map", "0:a", "-map", "0:a",
           "-map", "1", "-c:v", "copy", "-c:a:0", "copy", "-c:a:1", "flac",
           "-ac:a:1", "2", "-ar:a:1", "48000", "-c:s", "srt", lossless)
        flac_kb = [t for t in kinds(adpatch.probe_streams(lossless), "audio")
                   if t["codec"] == "flac"][0]["kbps"]
        target = 0.45
        out, res, s = run("fit_lossless", lossless, big_ad, "mkv",
                          {"fit_gb": target, "max_height": 144,
                           "keep_audio": "all", "keep_subs": "all"})
        size = os.path.getsize(out) / adpatch.GIB
        # simple test footage can't use all its video bits, and here the
        # video is a small part of the file: under is fine, over is the bug
        check(f"fit lossless: {target} GB target, under it "
              f"(flac shows {flac_kb} kb/s in ffmpeg -i)",
              0.7 <= size / target <= 1.0, f"{size:.3f} GB")
        # 8b. lower resolution without a size target: fixed-quality HEVC;
        # asking for more than the video has keeps it as is (no re-encode)
        out8, res8, s8 = run("downscale", big, big_ad, "mkv", {"max_height": 144})
        v = kinds(s8, "video")[0]
        check("downscale only: hevc 144p, fixed-quality note",
              v["codec"] == "hevc" and "x144" in v["details"] and
              any("fixed quality" in n for n in res8["notes"]),
              f"{v['codec']} {v['details'][:40]} {res8['notes']}")
        check("downscale only: sync", *in_sync(out8, adpatch.onset_envelope(big)))
        out8, res8, s8 = run("noscale", big, big_ad, "mkv", {"max_height": 1080})
        check("resolution above the video's: copied, not re-encoded",
              kinds(s8, "video")[0]["codec"] == "rawvideo",
              kinds(s8, "video")[0]["codec"])

        # 9. the second-try path: plan with a margin that's too generous so
        # the first encode overshoots; the retry must land under target.
        # Noise video: it uses every bit it gets (simple test footage
        # can't, so it would never overshoot)
        noisy = os.path.join(WORK, "fit_noise.mkv")
        if not os.path.exists(noisy):
            ff("-f", "lavfi", "-i",
               "nullsrc=s=320x180:r=24:d=120,geq=random(1)*255:128:128",
               "-i", os.path.join(WORK, "src.wav"), "-t", "120",
               "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
               "-c:a", "pcm_s16le", noisy)
        noisy_ad = os.path.join(WORK, "fit_noise_AD.mp3")
        if not os.path.exists(noisy_ad):
            ff("-i", os.path.join(WORK, "src.wav"), "-t", "120",
               "-af", "adelay=1500:all=1", "-c:a", "libmp3lame", noisy_ad)
        target = 0.03
        saved_margin, adpatch.FIT_MARGIN = adpatch.FIT_MARGIN, 1.3
        logs = []
        out9 = os.path.join(WORK, "out_fit_retry.mkv")
        try:
            adpatch.patch(noisy, noisy_ad, output=out9, log=logs.append,
                          options={"fit_gb": target})
        finally:
            adpatch.FIT_MARGIN = saved_margin
        size = os.path.getsize(out9) / adpatch.GIB
        retried = [x for x in logs if "encoding again" in x]
        check("fit retry: first encode was over, second one under",
              bool(retried) and size <= target,
              f"{size:.3f} GB; {retried[0].strip() if retried else 'no retry'}")
        # 10. "best picture for the size": slowest preset (+ whatever the
        # GPU supports) still lands under the target
        out10 = os.path.join(WORK, "out_fit_size.mkv")
        adpatch.patch(noisy, noisy_ad, output=out10, log=lambda *a: None,
                      options={"fit_gb": 0.03, "fit_priority": "size"})
        size = os.path.getsize(out10) / adpatch.GIB
        check("fit priority size: under target",
              size <= 0.03, f"{size:.4f} GB, extras "
              f"{adpatch._nvenc_size_args() if adpatch.video_encoder() == 'hevc_nvenc' else '-'}")
        check("fit lossless: 3 audio + 1 subtitle",
              len(kinds(s, "audio")) == 3 and len(kinds(s, "subtitle")) == 1,
              str([(t["type"], t["codec"]) for t in s]))

    print("\nPASS" if not fails else f"\nFAIL: {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
