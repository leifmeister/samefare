"""
Facebook Page profile-picture options for SameFare (1000x1000, full-bleed so
Facebook's circular crop yields a clean disc). Brand green gradient = the hero
accent (#006C5B -> #00A886).
"""
import os
from PIL import Image, ImageDraw, ImageFont

S = 1000
FONT = os.path.join(os.path.dirname(__file__), "..", "static", "fonts", "Nunito.ttf")
DESK = os.path.expanduser("~/Desktop")


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i+2], 16) for i in (0, 2, 4))


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _font(px, weight=900):
    f = ImageFont.truetype(FONT, px)
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


def _grad_square(c0, c1, diagonal=True):
    img = Image.new("RGB", (S, S))
    px = img.load()
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * (S - 1)) if diagonal else x / (S - 1)
            px[x, y] = _lerp(c0, c1, t)
    return img


def _centre(draw, text, font, fill, cy=None, dy=0):
    bbox = font.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = S / 2 - tw / 2 - bbox[0]
    y = (cy if cy is not None else S / 2 - th / 2 - bbox[1]) + dy
    draw.text((x, y), text, font=font, fill=fill)
    return th


# ── Option A: "SF" monogram, white on green gradient ────────────────────────
def opt_a():
    img = _grad_square(_hex("#006C5B"), _hex("#00A886")).convert("RGBA")
    d = ImageDraw.Draw(img)
    _centre(d, "SF", _font(440, 900), (255, 255, 255, 255))
    out = os.path.join(DESK, "samefare_profile_A_monogram.png")
    img.convert("RGB").save(out); print("wrote", out)


# ── Option B: hero emblem (sun + mountains), white on green gradient ─────────
def opt_b():
    img = _grad_square(_hex("#006C5B"), _hex("#00A886")).convert("RGBA")
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    soft = (255, 255, 255, 70)
    # sun
    d.ellipse([S*0.40, S*0.20, S*0.60, S*0.40], fill=white)
    d.ellipse([S*0.35, S*0.15, S*0.65, S*0.45], fill=soft)
    # back mountains (soft) + front mountains (solid white)
    d.polygon([(S*0.05, S*0.78), (S*0.32, S*0.46), (S*0.55, S*0.78)], fill=soft)
    d.polygon([(S*0.45, S*0.78), (S*0.70, S*0.42), (S*0.95, S*0.78)], fill=soft)
    d.polygon([(S*0.12, S*0.80), (S*0.40, S*0.52), (S*0.68, S*0.80)], fill=white)
    d.polygon([(S*0.52, S*0.80), (S*0.78, S*0.55), (S*0.99, S*0.80)], fill=white)
    # ground line
    d.rectangle([0, S*0.78, S, S*0.82], fill=white)
    out = os.path.join(DESK, "samefare_profile_B_emblem.png")
    img.convert("RGB").save(out); print("wrote", out)


# ── Option C: stacked wordmark, two-tone on white ───────────────────────────
def opt_c():
    img = Image.new("RGB", (S, S), _hex("#F4FAF8")).convert("RGBA")
    d = ImageDraw.Draw(img)
    f = _font(220, 900)
    _centre(d, "Same", f, _hex("#103A36"), cy=S*0.26)
    _centre(d, "Fare", f, _hex("#00936F"), cy=S*0.52)
    out = os.path.join(DESK, "samefare_profile_C_wordmark.png")
    img.convert("RGB").save(out); print("wrote", out)


if __name__ == "__main__":
    opt_a(); opt_b(); opt_c()
