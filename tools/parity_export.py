#!/usr/bin/env python3
"""Export harness envelopes + adpatch's alignment results for the Kotlin
parity test (android/app/src/test/.../AlignerParityTest.kt).

    .venv\\Scripts\\python tools\\harness.py          # creates the media
    .venv\\Scripts\\python tools\\parity_export.py    # -> %TEMP%/adpatch-parity
    cd android && gradle testReleaseUnitTest -Dparity.dir=%TEMP%\\adpatch-parity

Envelopes are raw little-endian float32 (*.f32); expected.json (and
expected.tsv for the JVM test) hold a, b, pieces and confidence per case.
Everything stays in %TEMP% (never the repo).
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import adpatch  # noqa: E402
from tools import harness  # noqa: E402,F401  (same CASES / file naming)


def main():
    work = os.path.join(tempfile.gettempdir(), "adpatch-harness")
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        tempfile.gettempdir(), "adpatch-parity")
    os.makedirs(out, exist_ok=True)
    video = os.path.join(work, "video_pcm_10m.mkv")
    vid = adpatch.onset_envelope(video)
    vid.astype("<f4").tofile(os.path.join(out, "video.f32"))
    expected = {}
    for name in harness.CASES:
        ad_path = os.path.join(work, f"ad4_{name}_10m.mp3")
        if not os.path.exists(ad_path):
            sys.exit(f"missing {ad_path} - run tools/harness.py first")
        ad = adpatch.onset_envelope(ad_path)
        ad.astype("<f4").tofile(os.path.join(out, f"ad_{name}.f32"))
        a, b, _, sig, pieces = adpatch.fit_alignment(ad, vid,
                                                     log=lambda *x: None)
        conf, band, _ = adpatch.confidence(sig)
        expected[name] = {
            "a": a, "b": b, "confidence": conf, "band": band,
            "pieces": [[q["ad_start"], q["b"]] for q in pieces or []],
        }
        print(f"{name}: a={a:.6f} b={b:+.3f} {band} "
              f"pieces={len(pieces or [])}")
    with open(os.path.join(out, "expected.json"), "w") as f:
        json.dump(expected, f, indent=1)
    # same data as TSV for the JVM test (android's org.json is a stub there):
    # case  a  b  band  pieces as "ad_start:b,ad_start:b"
    with open(os.path.join(out, "expected.tsv"), "w") as f:
        for name, e in expected.items():
            pcs = ",".join(f"{s0!r}:{bk!r}" for s0, bk in e["pieces"])
            f.write("\t".join([name, repr(e["a"]), repr(e["b"]),
                               e["band"], pcs]) + "\n")
    print(f"exported to {out}")


if __name__ == "__main__":
    main()
