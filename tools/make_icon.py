"""make_icon.py — build the 🥋 app icon from the source gi artwork.

The source is the karate-gi emoji with a baked-in checkerboard "transparency"
pattern. That pattern is perfectly neutral (R==G==B) while the gi itself is
subtly warm, so neutrality — not colour distance — is what separates them.
Only neutral regions CONNECTED TO THE BORDER are removed, so highlights inside
the gi survive.

    python3 tools/make_icon.py [source.png]
"""
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SOURCE = ASSETS / "gi-source.png"

BG_TOP = (26, 26, 30, 255)
BG_BOT = (13, 13, 16, 255)
GOLD = (182, 137, 47, 255)
S = 1024
RADIUS = int(S * 0.22)
PAD = 0.13          # breathing room inside the badge


def cut_out(img):
    """Return the gi on real transparency, cropped tight."""
    rgb = img.convert("RGB")
    a = np.array(rgb).astype(np.int16)
    h, w, _ = a.shape
    spread = a.max(axis=2) - a.min(axis=2)
    cand = (a.min(axis=2) >= 232) & (spread <= 2)

    bg = np.zeros((h, w), bool)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if cand[y, x] and not bg[y, x]:
                bg[y, x] = True; dq.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if cand[y, x] and not bg[y, x]:
                bg[y, x] = True; dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and cand[ny, nx] and not bg[ny, nx]:
                bg[ny, nx] = True; dq.append((ny, nx))

    alpha = Image.fromarray(np.where(bg, 0, 255).astype(np.uint8), "L")
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.8))
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    box = out.getbbox()
    return out.crop(box) if box else out


def badge():
    """Dark rounded square with a thin gold rim — matches the app chrome."""
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    d = ImageDraw.Draw(grad)
    for i in range(S):
        t = i / S
        d.rectangle((0, i, S, i + 1),
                    fill=tuple(int(BG_TOP[j] * (1 - t) + BG_BOT[j] * t) for j in range(3)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=RADIUS, fill=255)
    im.paste(grad, (0, 0), mask)
    ImageDraw.Draw(im).rounded_rectangle((8, 8, S - 9, S - 9), radius=RADIUS - 6,
                                         outline=GOLD, width=9)
    return im


def build(source):
    gi = cut_out(Image.open(source))
    im = badge()
    room = int(S * (1 - 2 * PAD))
    scale = min(room / gi.width, room / gi.height)
    gi = gi.resize((max(1, int(gi.width * scale)), max(1, int(gi.height * scale))),
                   Image.LANCZOS)

    # soft drop shadow so white cloth separates from the dark badge
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sh.paste((0, 0, 0, 130), ((S - gi.width) // 2, (S - gi.height) // 2 + 10), gi)
    im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)))
    im.alpha_composite(gi, ((S - gi.width) // 2, (S - gi.height) // 2))
    return im


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCE
    if not src.exists():
        sys.exit(f"no source artwork at {src}")
    icon = build(src).resize((512, 512), Image.LANCZOS)
    ASSETS.mkdir(exist_ok=True)
    icon.save(ASSETS / "chucknorris-icon.png")
    for sz in (256, 128, 64, 48, 32):
        icon.resize((sz, sz), Image.LANCZOS).save(ASSETS / f"chucknorris-icon-{sz}.png")
    # a transparent, badge-free cut-out for the README / docs
    cut_out(Image.open(src)).resize((512, 512), Image.LANCZOS).save(ASSETS / "gi-mark.png")
    print(f"wrote {ASSETS/'chucknorris-icon.png'} + 5 sizes + gi-mark.png")
