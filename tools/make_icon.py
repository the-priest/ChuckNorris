"""make_icon.py — the 🥋 mark, drawn to match the README emoji.

A karate gi: crossed white lapels over a dark ground, cinched with the gold
belt that runs through the whole app. Rounded-square badge, flat, legible at
32px. Regenerate with:  python3 tools/make_icon.py
"""
from PIL import Image, ImageDraw
from pathlib import Path

GOLD = (182, 137, 47, 255)
GOLD_HI = (214, 168, 74, 255)
GI = (238, 234, 226, 255)
GI_SH = (206, 200, 189, 255)
BG0 = (18, 18, 21, 255)
BG1 = (11, 11, 13, 255)
BELT_D = (140, 103, 30, 255)

S = 1024                      # draw big, downsample for clean edges
R = int(S * 0.22)             # corner radius


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def build():
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # badge with a subtle vertical lift
    for i in range(S):
        t = i / S
        c = tuple(int(BG0[j] * (1 - t) + BG1[j] * t) for j in range(3)) + (255,)
        d.rectangle((0, i, S, i + 1), fill=c)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=R, fill=255)
    badge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    badge.paste(im, (0, 0), mask)
    im = badge
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((7, 7, S - 8, S - 8), radius=R - 5, outline=GOLD, width=9)

    cx = S // 2
    sh_y = int(S * 0.30)          # shoulder line
    hem_y = int(S * 0.78)         # jacket hem
    body = int(S * 0.165)         # half-width of the torso
    cuff = int(S * 0.395)         # how far the sleeves reach out
    sl_top = sh_y + int(S * 0.005)
    sl_bot = sh_y + int(S * 0.175)

    # sleeves first, so the torso overlaps them at the shoulder
    for sx in (-1, 1):
        x0 = cx + sx * body
        d.polygon([(x0, sl_top),
                   (cx + sx * cuff, sl_top + int(S * 0.045)),
                   (cx + sx * cuff, sl_bot + int(S * 0.035)),
                   (x0, sl_bot)], fill=GI_SH)
        # cuff band
        d.line([(cx + sx * cuff, sl_top + int(S * 0.045)),
                (cx + sx * cuff, sl_bot + int(S * 0.035))], fill=GI, width=14)

    # torso
    d.polygon([(cx - body, sh_y), (cx + body, sh_y),
               (cx + body + int(S * 0.02), hem_y),
               (cx - body - int(S * 0.02), hem_y)], fill=GI)

    # the crossover: two wide lapels meeting in a deep V
    lap = int(S * 0.085)
    # right lapel (drawn under)
    d.polygon([(cx + body, sh_y), (cx + body - lap, sh_y),
               (cx - int(S * 0.015), int(S * 0.60)),
               (cx - int(S * 0.015) + lap, int(S * 0.60))], fill=GI_SH)
    # left lapel (over the top — the classic left-over-right)
    d.polygon([(cx - body, sh_y), (cx - body + lap, sh_y),
               (cx + int(S * 0.015), int(S * 0.60)),
               (cx + int(S * 0.015) - lap, int(S * 0.60))], fill=GI)
    # gold piping down both lapel edges makes the V unmistakable
    d.line([(cx - body, sh_y), (cx + int(S * 0.015), int(S * 0.60))],
           fill=GOLD_HI, width=11)
    d.line([(cx + body, sh_y), (cx - int(S * 0.015), int(S * 0.60))],
           fill=GOLD_HI, width=11)
    d.line([(cx - body, sh_y), (cx + body, sh_y)], fill=GOLD_HI, width=9)

    # belt
    by = int(S * 0.605)
    bh = int(S * 0.075)
    d.rectangle((cx - body - int(S * 0.03), by, cx + body + int(S * 0.03), by + bh), fill=GOLD)
    d.rectangle((cx - body - int(S * 0.03), by + bh - 11,
                 cx + body + int(S * 0.03), by + bh), fill=BELT_D)

    # knot + the two hanging ends
    kw = int(S * 0.062)
    d.rounded_rectangle((cx - kw, by - 9, cx + kw, by + bh + 9), radius=12, fill=GOLD_HI)
    d.line((cx, by - 5, cx, by + bh + 5), fill=BELT_D, width=5)
    for sx in (-1, 1):
        x = cx + sx * int(S * 0.022)
        d.polygon([(x, by + bh + 4), (x + sx * int(S * 0.045), by + bh + 4),
                   (x + sx * int(S * 0.024), int(S * 0.77))], fill=GOLD)

    return im.resize((512, 512), Image.LANCZOS)


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "assets"
    out.mkdir(exist_ok=True)
    icon = build()
    icon.save(out / "chucknorris-icon.png")
    for sz in (256, 128, 64, 48, 32):
        icon.resize((sz, sz), Image.LANCZOS).save(out / f"chucknorris-icon-{sz}.png")
    print("wrote", out / "chucknorris-icon.png", "+ 5 sizes")
