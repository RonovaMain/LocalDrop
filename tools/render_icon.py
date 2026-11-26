"""Render the LocalDrop icon from its vector geometry (design/icon.svg).

Writes
  design/icon-safe.svg      same artwork scaled into the Android adaptive-icon safe zone
  src/assets/icon.png       1024x1024 safe-zone version  (default + Android launcher, never cropped)
  src/assets/icon_windows.png  1024x1024 original proportions (desktop icons have no mask)
  installer/localdrop.ico, installer/linux/localdrop-512.png  for the Windows / Linux installers

Needs Pillow + numpy (dev only).  Usage:  python tools/render_icon.py
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SS = 4                                  # supersampling
BG = (0x12, 0x14, 0x20)
C0, C1 = (0x5E, 0xEA, 0xF8), (0x92, 0x82, 0xF8)
G = (180, 260, 840, 760)                # gradient x1 y1 x2 y2 (user space)
CLOUD = "M 320 710 H 715 C 805 710 870 645 870 560 C 870 482 814 420 738 412 C 712 310 622 240 512 240 C 392 240 295 325 280 442 C 205 456 154 516 154 585 C 154 656 218 710 320 710 Z"
STROKES = [(CLOUD, 58), ("M 385 450 L 475 535 L 385 620", 56), ("M 515 620 H 645", 56)]
SAFE_RADIUS = 313                       # 66dp of 108dp -> 61% of 1024, radius in px


def flatten(d: str, step: float = 1.0):
    toks = re.findall(r"[MHLCZ]|-?\d+(?:\.\d+)?", d)
    pts, i, cur, start = [], 0, (0.0, 0.0), (0.0, 0.0)
    num = lambda: float(toks.pop(0))
    while toks:
        cmd = toks.pop(0)
        if cmd == "M":
            cur = start = (num(), num()); pts.append(cur)
        elif cmd in "LH":
            nxt = (num(), num()) if cmd == "L" else (num(), cur[1])
            n = max(2, int(np.hypot(nxt[0] - cur[0], nxt[1] - cur[1]) / step))
            pts += [(cur[0] + (nxt[0] - cur[0]) * k / n, cur[1] + (nxt[1] - cur[1]) * k / n) for k in range(1, n + 1)]
            cur = nxt
        elif cmd == "C":
            p1, p2, p3 = (num(), num()), (num(), num()), (num(), num())
            n = 400
            for k in range(1, n + 1):
                t = k / n; u = 1 - t
                pts.append((u**3 * cur[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                            u**3 * cur[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]))
            cur = p3
        elif cmd == "Z":
            pts.append(start); cur = start
    return pts


def geometry():
    return [(flatten(d), w) for d, w in STROKES]


def fit_transform():
    """scale + shift that centres the artwork on the canvas and keeps it inside the safe circle."""
    allpts = [(p, w) for pts, w in geometry() for p in pts]
    xs = [p[0] for p, w in allpts]; ys = [p[1] for p, w in allpts]
    cx = (min(xs) - 29 + max(xs) + 29) / 2          # outer stroke edge ~ half stroke width
    cy = (min(ys) - 29 + max(ys) + 29) / 2
    far = max(np.hypot(p[0] - cx, p[1] - cy) + w / 2 for p, w in allpts)
    return SAFE_RADIUS / far, cx, cy


def render(scale: float, cx: float, cy: float, size: int = 1024) -> Image.Image:
    S = size * SS
    k = scale * SS
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    for pts, w in geometry():
        r = w / 2 * k
        for x, y in pts:
            X, Y = (x - cx) * k + S / 2, (y - cy) * k + S / 2
            d.ellipse([X - r, Y - r, X + r, Y + r], fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    ux, uy = (xx + 0.5 - size / 2) / scale + cx, (yy + 0.5 - size / 2) / scale + cy   # back to user space
    x1, y1, x2, y2 = G
    t = np.clip(((ux - x1) * (x2 - x1) + (uy - y1) * (y2 - y1)) / ((x2 - x1) ** 2 + (y2 - y1) ** 2), 0, 1)
    grad = np.stack([C0[i] + (C1[i] - C0[i]) * t for i in range(3)], axis=-1).astype(np.uint8)
    img = Image.new("RGB", (size, size), BG)
    img.paste(Image.fromarray(grad, "RGB"), mask=mask)
    return img


def safe_svg(scale: float, cx: float, cy: float) -> str:
    src = (ROOT / "design" / "icon.svg").read_text(encoding="utf-8")
    head, rest = src.split("<!-- Контур облака -->")
    body = rest.replace("</svg>", "").rstrip()
    body = "\n".join("  " + ln if ln.strip() else ln for ln in body.splitlines())
    tf = f"translate(512 512) scale({scale:.4f}) translate({-cx:g} {-cy:g})"
    return f'{head}<g transform="{tf}">\n  <!-- Контур облака -->{body}\n  </g>\n</svg>\n'


if __name__ == "__main__":
    s, cx, cy = fit_transform()
    print(f"safe-zone scale={s:.4f}  artwork centre=({cx:g},{cy:g})")
    original = render(1.0, 512, 512)                                    # original proportions, as designed
    original.save(ROOT / "src/assets/icon_windows.png")
    (ROOT / "installer" / "linux").mkdir(parents=True, exist_ok=True)
    original.resize((512, 512), Image.LANCZOS).save(ROOT / "installer/linux/localdrop-512.png")
    original.save(ROOT / "installer/localdrop.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    render(s, cx, cy).save(ROOT / "src/assets/icon.png")
    (ROOT / "design" / "icon-safe.svg").write_text(safe_svg(s, cx, cy), encoding="utf-8")
