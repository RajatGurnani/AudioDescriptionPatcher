#!/usr/bin/env python3
"""Render the app icon (same artwork as the Android launcher icon:
android/app/src/main/res/drawable/ic_launcher_foreground.xml on
#0B7A75) to static/icon.ico - numpy only, PNG-in-ICO (Vista+).

    .venv\\Scripts\\python tools\\make_icon.py
"""

import os
import struct
import zlib

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BG = (0x0B, 0x7A, 0x75)


def render(size, ss=4):
    """RGBA uint8 image; geometry in the 108-unit Android viewport."""
    n = size * ss
    c = (np.arange(n) + 0.5) / n * 108
    x, y = np.meshgrid(c, c)
    img = np.zeros((n, n, 4))

    # rounded-square background (radius 22 units)
    r = 22
    dx = np.maximum(np.maximum(r - x, x - (108 - r)), 0)
    dy = np.maximum(np.maximum(r - y, y - (108 - r)), 0)
    img[dx * dx + dy * dy <= r * r] = (*BG, 255)

    def paint(mask, rgb):
        img[mask] = (*rgb, 255)

    def rounded_rect(x0, y0, x1, y1, rad):
        dx = np.maximum(np.maximum(x0 + rad - x, x - (x1 - rad)), 0)
        dy = np.maximum(np.maximum(y0 + rad - y, y - (y1 - rad)), 0)
        return (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1) & \
            (dx * dx + dy * dy <= rad * rad)

    def triangle(ax, ay, bx, by, cx, cy):
        def side(px, py, qx, qy):
            return (qx - px) * (y - py) - (qy - py) * (x - px)
        s1, s2, s3 = side(ax, ay, bx, by), side(bx, by, cx, cy), \
            side(cx, cy, ax, ay)
        return ((s1 >= 0) & (s2 >= 0) & (s3 >= 0)) | \
            ((s1 <= 0) & (s2 <= 0) & (s3 <= 0))

    # speech bubble (body + tail) with the play triangle cut out - same
    # geometry as the Android vector
    bubble = rounded_rect(28, 32, 80, 68, 8) | triangle(39, 67, 50, 67,
                                                         37, 79)
    paint(bubble & ~triangle(48, 41, 48, 59, 63, 50), (255, 255, 255))

    # downsample (box filter) with premultiplied alpha
    a = img[..., 3:] / 255
    pre = np.concatenate([img[..., :3] * a, a * 255], axis=2)
    pre = pre.reshape(size, ss, size, ss, 4).mean(axis=(1, 3))
    alpha = pre[..., 3:] / 255
    rgb = np.where(alpha > 0, pre[..., :3] / np.maximum(alpha, 1e-9), 0)
    return np.concatenate([rgb, pre[..., 3:]], axis=2).round() \
        .clip(0, 255).astype(np.uint8)


def png_bytes(rgba):
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[i].tobytes() for i in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + \
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    sizes = (16, 24, 32, 48, 64, 128, 256)
    pngs = [png_bytes(render(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, data in zip(sizes, pngs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                               len(data), offset)
        offset += len(data)
    out = os.path.join(ROOT, "static", "icon.ico")
    with open(out, "wb") as f:
        f.write(header + entries + b"".join(pngs))
    with open(os.path.join(ROOT, "static", "icon.png"), "wb") as f:
        f.write(pngs[-1])   # for the web page / previews
    print(f"wrote {out} ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    main()
