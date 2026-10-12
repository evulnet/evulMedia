#!/usr/bin/env python3
"""Generate the evulMedia icon set into static/ (needs Pillow only).

    venv/bin/python scripts/make_icons.py

Writes static/favicon.png (48) and static/apple-touch-icon.png (180).
favicon.svg is hand-authored and not touched here.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static"
BG = (22, 26, 32, 255)          # --bg2 dark
ACC1 = (155, 123, 255, 255)     # --acc2
ACC2 = (77, 163, 255, 255)      # --acc


def play_icon(size: int, transparent: bool, corner: int) -> Image.Image:
    mode = "RGBA" if transparent else "RGB"
    img = Image.new(mode, (size, size))
    d = ImageDraw.Draw(img)
    fill = BG if not transparent else BG
    if corner and transparent:
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=corner, fill=fill)
    else:
        d.rectangle((0, 0, size - 1, size - 1), fill=fill)
    # play triangle, visually centered (shifted slightly right), vertical
    # gradient between the two accent colors
    m = size * 0.30
    x0, x1 = size * 0.34, size - m
    y0, y1 = m, size - m
    ymid = (y0 + y1) / 2
    for y in range(int(y0), int(y1) + 1):
        t = 1 - abs(y - ymid) / (y1 - ymid)          # 0 at tips, 1 mid-height
        rx = x0 + (x1 - x0) * t
        t2 = (y - y0) / max(1.0, y1 - y0)             # vertical gradient pos
        col = tuple(int(a + (b - a) * t2) for a, b in zip(ACC1[:3], ACC2[:3])) + (255,)
        if rx > x0 + 0.5:
            d.line([(x0, y), (rx, y)], fill=col)
    return img


def main():
    OUT.mkdir(exist_ok=True)
    fav = play_icon(48, transparent=True, corner=10)
    fav.save(OUT / "favicon.png")
    apple = play_icon(180, transparent=False, corner=0)   # no alpha, no radius
    apple.save(OUT / "apple-touch-icon.png")
    print("wrote", OUT / "favicon.png", "and", OUT / "apple-touch-icon.png")


if __name__ == "__main__":
    main()
