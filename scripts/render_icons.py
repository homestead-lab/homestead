"""Renders the app icons an installed Homestead needs, from the mark's geometry.

Phones want PNGs at fixed sizes - and a "maskable" one with room to be cropped
to a circle or squircle - and cannot use the SVG. The mark is a rounded square,
a house outline and two bars, so this draws exactly that with signed distance
fields: no image library, and the output is the same on every run.

    python scripts/render_icons.py
"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "web" / "icons"

TILE = (0x10, 0x14, 0x16)
BARS = (0xF7, 0xF4, 0xEA)
SKY = ((0xFF, 0xD1, 0x66), (0xF5, 0x9E, 0x0B))
STROKE = 5.5
HOUSE = [(12, 29), (32, 13), (52, 29), (52, 52), (12, 52), (12, 29)]
LINES = [((20, 34), (44, 34)), ((20, 43), (44, 43))]


def _segment(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _rounded_rect(px, py, x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    qx, qy = abs(px - cx) - hx, abs(py - cy) - hy
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def _cover(distance, pixel):
    """Coverage of a pixel by a shape, from its signed distance, antialiased."""
    return max(0.0, min(1.0, 0.5 - distance / pixel))


def _over(dst, color, alpha):
    r, g, b, a = dst
    out = alpha + a * (1 - alpha)
    if out <= 0:
        return (0, 0, 0, 0)
    mix = [(c * alpha + d * a * (1 - alpha)) / out for c, d in zip(color, (r, g, b))]
    return (*mix, out)


def render(size, bleed=False, scale=1.0, mono=False):
    """bleed: fill the whole square (maskable, Apple). scale: the mark's share of it."""
    unit = 64 / size / scale            # mark units per pixel
    offset = 32 - 32 / scale            # centres the scaled mark
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            px, py = offset + (x + 0.5) * unit, offset + (y + 0.5) * unit
            pixel = (0, 0, 0, 0)
            if bleed:
                pixel = (*TILE, 1.0)
            elif not mono:
                pixel = _over(pixel, TILE, _cover(_rounded_rect(px, py, 2, 2, 62, 62, 16), unit))
            house = min(_segment(px, py, a, b) for a, b in zip(HOUSE, HOUSE[1:])) - STROKE / 2
            bars = min(_segment(px, py, a, b) for a, b in LINES) - STROKE / 2
            if mono:
                pixel = _over(pixel, (255, 255, 255), _cover(min(house, bars), unit))
            else:
                t = max(0.0, min(1.0, ((px - 12) * 40 + (py - 8) * 48) / (40 * 40 + 48 * 48)))
                sky = tuple(a + (b - a) * t for a, b in zip(*SKY))
                pixel = _over(pixel, sky, _cover(house, unit))
                pixel = _over(pixel, BARS, _cover(bars, unit))
            row += bytes(max(0, min(255, round(v))) for v in pixel[:3])
            row.append(max(0, min(255, round(pixel[3] * 255))))
        rows.append(bytes(row))
    return _png(size, b"".join(rows))


def _png(size, raw):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


ICONS = {
    "icon-192.png": dict(size=192),
    "icon-512.png": dict(size=512),
    # Maskable: the platform crops to its own shape, keeping the middle 80%.
    "maskable-512.png": dict(size=512, bleed=True, scale=0.78),
    "apple-touch-icon.png": dict(size=180, bleed=True, scale=0.86),
    # The small monochrome badge Android shows in the status bar.
    "badge-96.png": dict(size=96, mono=True, scale=1.1),
}

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, options in ICONS.items():
        (OUT / name).write_bytes(render(**options))
        print(name)
