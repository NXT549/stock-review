#!/usr/bin/env python3
"""make_icon.py — generate packaging/stock_review.ico with zero dependencies.

Draws a retro **Windows 98-style stock chart**: four chunky black-outlined bars
climbing left to right in the classic Windows flag colours, with a bold trend
arrow riding over them — all on the iconic teal desktop. Win98 on the outside,
stocks on the inside. Rendered entirely with the standard library (no Pillow):
a master scene is rasterised at high resolution and area-averaged down to each
icon size for clean anti-aliasing, then packed into a multi-size Windows
``.ico``:

    * 16 / 32 / 48 px  -> classic 32-bit BGRA DIB frames (universally supported,
      incl. Tk's ``iconbitmap`` on older Windows)
    * 256 px           -> PNG-compressed frame (the modern large-icon slot)

Run it from anywhere::

    py packaging/make_icon.py            # writes packaging/stock_review.ico
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

# --------------------------------------------------------------------------- #
# Palette (RGBA). The Win98 desktop teal, the four flag brights, dark outline.
# --------------------------------------------------------------------------- #
BG = (0, 128, 128, 255)       # #008080 — THE Windows 98 desktop teal
OUTLINE = (12, 12, 12, 255)   # near-black bar borders / ground / arrow
RED = (222, 56, 43, 255)
BLUE = (30, 90, 200, 255)
YELLOW = (250, 190, 20, 255)
GREEN = (60, 160, 40, 255)    # the tallest bar — profit, obviously

MASTER = 1024                 # supersample resolution before downscaling


# --------------------------------------------------------------------------- #
# Tiny software rasteriser over a flat RGBA bytearray
# --------------------------------------------------------------------------- #
def _blank(n: int) -> bytearray:
    return bytearray(n * n * 4)  # all zero => fully transparent


def _fill_rect(buf, n, x0, y0, x1, y1, rgba) -> None:
    x0, x1 = sorted((int(x0), int(x1)))
    y0, y1 = sorted((int(y0), int(y1)))
    for y in range(max(0, y0), min(n, y1)):
        base = (y * n) * 4
        for x in range(max(0, x0), min(n, x1)):
            i = base + x * 4
            buf[i:i + 4] = bytes(rgba)


def _rounded_bg(buf, n, radius, rgba) -> None:
    """Fill the whole canvas with a rounded-corner square."""
    r = radius
    for y in range(n):
        for x in range(n):
            # distance into a corner, else inside
            cx = r - x if x < r else (x - (n - 1 - r) if x > n - 1 - r else 0)
            cy = r - y if y < r else (y - (n - 1 - r) if y > n - 1 - r else 0)
            if cx and cy and (cx * cx + cy * cy) > r * r:
                continue  # outside the rounded corner -> stay transparent
            i = (y * n + x) * 4
            buf[i:i + 4] = bytes(rgba)


def _thick_line(buf, n, x0, y0, x1, y1, w, rgba) -> None:
    """A chunky line: stamp a filled square along the segment."""
    steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for k in range(steps + 1):
        t = k / steps
        cx = x0 + (x1 - x0) * t
        cy = y0 + (y1 - y0) * t
        _fill_rect(buf, n, cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2, rgba)


def _triangle(buf, n, p0, p1, p2, rgba) -> None:
    """A filled triangle (sign-consistent edge test over the bounding box)."""
    def edge(a, b, px, py):
        return (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])

    xs = (p0[0], p1[0], p2[0])
    ys = (p0[1], p1[1], p2[1])
    for y in range(max(0, int(min(ys))), min(n, int(max(ys)) + 1)):
        for x in range(max(0, int(min(xs))), min(n, int(max(xs)) + 1)):
            d0 = edge(p0, p1, x + 0.5, y + 0.5)
            d1 = edge(p1, p2, x + 0.5, y + 0.5)
            d2 = edge(p2, p0, x + 0.5, y + 0.5)
            if (d0 >= 0 and d1 >= 0 and d2 >= 0) or (d0 <= 0 and d1 <= 0 and d2 <= 0):
                i = (y * n + x) * 4
                buf[i:i + 4] = bytes(rgba)


def render_master() -> bytearray:
    """The scene: four black-outlined bars stepping up in the Windows flag
    colours, a ground line, and a bold trend arrow — on the teal desktop."""
    n = MASTER
    buf = _blank(n)
    _rounded_bg(buf, n, radius=int(n * 0.195), rgba=BG)

    ground_y = int(n * 0.855)                     # top of the ground line
    border = max(2, int(n * 0.018))               # bar outline thickness

    # Four bars, ascending: red, blue, yellow, then the tall green one.
    bars = ((0.135, 0.24, RED), (0.315, 0.36, BLUE),
            (0.495, 0.48, YELLOW), (0.675, 0.62, GREEN))
    bar_w = int(n * 0.165)
    for x_frac, h_frac, colour in bars:
        x0 = int(n * x_frac)
        y0 = ground_y - int(n * h_frac)
        _fill_rect(buf, n, x0, y0, x0 + bar_w, ground_y, OUTLINE)
        _fill_rect(buf, n, x0 + border, y0 + border,
                   x0 + bar_w - border, ground_y, colour)

    # The ground the bars stand on.
    _fill_rect(buf, n, int(n * 0.10), ground_y, int(n * 0.90),
               ground_y + int(n * 0.022), OUTLINE)

    # Trend arrow riding the bar tops: a chunky two-segment climb with a dip,
    # ending in a fat arrowhead in the top-right corner.
    w = int(n * 0.052)
    pts = ((int(n * 0.115), int(n * 0.52)),
           (int(n * 0.42), int(n * 0.335)),
           (int(n * 0.545), int(n * 0.415)),
           (int(n * 0.795), int(n * 0.185)))
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        _thick_line(buf, n, x0, y0, x1, y1, w, OUTLINE)

    # Arrowhead: extend past the last point along its segment direction.
    (ax, ay), (bx, by) = pts[-2], pts[-1]
    dx, dy = bx - ax, by - ay
    length = (dx * dx + dy * dy) ** 0.5
    ux, uy = dx / length, dy / length             # unit along the segment
    px_, py_ = -uy, ux                            # unit perpendicular
    head_len, head_w = n * 0.135, n * 0.135
    tip = (bx + ux * head_len, by + uy * head_len)
    base1 = (bx + px_ * head_w / 2, by + py_ * head_w / 2)
    base2 = (bx - px_ * head_w / 2, by - py_ * head_w / 2)
    _triangle(buf, n, tip, base1, base2, OUTLINE)
    return buf


# --------------------------------------------------------------------------- #
# Downscale (premultiplied-alpha area average) MASTER -> target size
# --------------------------------------------------------------------------- #
def downscale(src: bytearray, s: int, t: int) -> bytearray:
    dst = bytearray(t * t * 4)
    for ty in range(t):
        sy0, sy1 = ty * s // t, (ty + 1) * s // t
        for tx in range(t):
            sx0, sx1 = tx * s // t, (tx + 1) * s // t
            ar = ag = ab = aa = 0
            count = 0
            for sy in range(sy0, max(sy0 + 1, sy1)):
                row = (sy * s) * 4
                for sx in range(sx0, max(sx0 + 1, sx1)):
                    i = row + sx * 4
                    r, g, b, a = src[i], src[i + 1], src[i + 2], src[i + 3]
                    ar += r * a
                    ag += g * a
                    ab += b * a
                    aa += a
                    count += 1
            di = (ty * t + tx) * 4
            if aa:
                dst[di] = min(255, ar // aa)
                dst[di + 1] = min(255, ag // aa)
                dst[di + 2] = min(255, ab // aa)
                dst[di + 3] = min(255, aa // count)
            # else leave transparent
    return dst


# --------------------------------------------------------------------------- #
# Encoders
# --------------------------------------------------------------------------- #
def _png(rgba: bytearray, w: int, h: int) -> bytes:
    """Minimal RGBA PNG (filter 0 on every scanline)."""
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgba[y * w * 4:(y + 1) * w * 4]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _dib(rgba: bytearray, w: int, h: int) -> bytes:
    """32-bit BGRA bottom-up DIB + empty AND mask, for an ICO frame."""
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(h - 1, -1, -1):          # bottom-up
        for x in range(w):
            i = (y * w + x) * 4
            r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
            xor += bytes((b, g, r, a))       # BGRA
    and_row = ((w + 31) // 32) * 4           # 1bpp, padded to 4 bytes
    mask = bytes(and_row * h)                # all zero (use alpha instead)
    return header + bytes(xor) + mask


def build_ico(frames: list[tuple[int, bytes, bool]]) -> bytes:
    """frames: list of (size, image_bytes, is_png). Returns .ico bytes."""
    out = bytearray(struct.pack("<HHH", 0, 1, len(frames)))  # ICONDIR
    offset = 6 + 16 * len(frames)
    entries, blobs = bytearray(), bytearray()
    for size, data, _is_png in frames:
        b = 0 if size >= 256 else size       # 0 means 256 in the ICO spec
        entries += struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32,
                               len(data), offset)
        blobs += data
        offset += len(data)
    return bytes(out + entries + blobs)


def main() -> int:
    print("Rendering master scene...")
    master = render_master()
    frames: list[tuple[int, bytes, bool]] = []
    for size in (256, 48, 32, 16):
        img = downscale(master, MASTER, size)
        if size >= 256:
            frames.append((size, _png(img, size, size), True))
        else:
            frames.append((size, _dib(img, size, size), False))
        print(f"  rendered {size}x{size}")
    ico = build_ico(frames)
    out = Path(__file__).resolve().parent / "stock_review.ico"
    out.write_bytes(ico)
    print(f"Wrote {out}  ({len(ico):,} bytes, {len(frames)} sizes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
