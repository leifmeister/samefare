"""
Render a Facebook-group cover (1640x856) that matches the homepage hero:
sky gradient, soft sun, blobby clouds, a small flock, the two mountain ranges,
and the two-tone "Verum Sameferða / um Ísland" headline + subtitle.

Colours/shapes are lifted verbatim from templates/index.html + style.css .hero.
"""
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 1640, 856
FONT = os.path.join(os.path.dirname(__file__), "..", "static", "fonts", "Nunito.ttf")

STRINGS = {
    "is": ("Verum Sameferða", "um Ísland",
           "Finndu ódýrar ferðir á milli íslenskra bæja. Bílstjórar fá "
           "bensínkostnað að hluta endurgreiddan, farþegar ferðast á lægra verði."),
    "en": ("Share the journey", "across Iceland",
           "Find affordable rideshares between Icelandic towns. Drivers recover "
           "their fuel costs, passengers travel for less."),
}


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i+2], 16) for i in (0, 2, 4))


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _font(px, weight=400):
    f = ImageFont.truetype(FONT, px)
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


def _gradient_bg():
    top, mid, bot = _hex("#BFE3F0"), _hex("#D6ECF4"), _hex("#EAF6FB")
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        if t <= 0.52:
            col = _lerp(top, mid, t / 0.52)
        else:
            col = _lerp(mid, bot, (t - 0.52) / 0.48)
        for x in range(W):
            px[x, y] = col
    return img


def _radial_glow(center, radius, stops):
    """stops: list of (pos0-1, (r,g,b), alpha0-255)."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pl = layer.load()
    cx, cy = center
    for y in range(max(0, cy - radius), min(H, cy + radius)):
        for x in range(max(0, cx - radius), min(W, cx + radius)):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / radius
            if d >= 1:
                continue
            for i in range(len(stops) - 1):
                p0, c0, a0 = stops[i]
                p1, c1, a1 = stops[i + 1]
                if p0 <= d <= p1:
                    tt = (d - p0) / (p1 - p0) if p1 > p0 else 0
                    col = _lerp(c0, c1, tt)
                    a = round(a0 + (a1 - a0) * tt)
                    pl[x, y] = (col[0], col[1], col[2], a)
                    break
    return layer


def _cloud(draw, cx, cy, scale, alpha=255):
    w = _hex("#ffffff")
    def E(dx, dy, rx, ry):
        draw.ellipse([cx + (dx - rx) * scale, cy + (dy - ry) * scale,
                      cx + (dx + rx) * scale, cy + (dy + ry) * scale],
                     fill=(w[0], w[1], w[2], alpha))
    # ellipses + base bar, from index.html cloud svg (viewBox 140x56), recentred
    E(42 - 70, 36 - 28, 32, 17)
    E(74 - 70, 28 - 28, 30, 22)
    E(104 - 70, 37 - 28, 26, 16)
    draw.rounded_rectangle([cx + (24 - 70) * scale, cy + (38 - 28) * scale,
                            cx + (116 - 70) * scale, cy + (53 - 28) * scale],
                           radius=7.5 * scale, fill=(w[0], w[1], w[2], alpha))


def _bird(draw, cx, cy, scale):
    col = _hex("#46685F")
    pts_a = [(2, 13), (8, 3), (15, 3), (22, 11)]
    pts_b = [(22, 11), (29, 3), (36, 3), (42, 13)]
    def bez(p):
        out = []
        for t in [i / 24 for i in range(25)]:
            mt = 1 - t
            x = mt**3*p[0][0] + 3*mt**2*t*p[1][0] + 3*mt*t**2*p[2][0] + t**3*p[3][0]
            y = mt**3*p[0][1] + 3*mt**2*t*p[1][1] + 3*mt*t**2*p[2][1] + t**3*p[3][1]
            out.append((cx + (x - 22) * scale, cy + (y - 8) * scale))
        return out
    draw.line(bez(pts_a), fill=col, width=max(2, round(2.6 * scale)), joint="curve")
    draw.line(bez(pts_b), fill=col, width=max(2, round(2.6 * scale)), joint="curve")


def _mountains(base):
    band_h = 320
    top_y = H - band_h
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    sx = W / 1440.0
    def scale_path(path):
        return [(x * sx, top_y + (y / 220.0) * band_h) for x, y in path]
    back = [(0,220),(0,150),(140,90),(300,150),(440,70),(620,150),(780,60),
            (980,150),(1140,90),(1320,150),(1440,110),(1440,220)]
    front = [(0,220),(0,185),(180,130),(360,185),(520,120),(700,185),(900,115),
             (1100,185),(1280,135),(1440,185),(1440,220)]
    d.polygon(scale_path(back), fill=_hex("#AAC9C0"))
    d.polygon(scale_path(front), fill=_hex("#7BA294"))
    base.alpha_composite(layer)


def _gradient_text(base, text, font, cx, cy, c0, c1):
    """Draw centred text filled with a left->right gradient."""
    bbox = font.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    mask = Image.new("L", (tw + 8, th + 8), 0)
    ImageDraw.Draw(mask).text((4 - bbox[0], 4 - bbox[1]), text, font=font, fill=255)
    grad = Image.new("RGB", (tw + 8, th + 8))
    gp = grad.load()
    for x in range(tw + 8):
        col = _lerp(c0, c1, x / (tw + 7))
        for y in range(th + 8):
            gp[x, y] = col
    base.paste(grad, (round(cx - tw / 2) - 4, round(cy) - 4), mask)
    return th


def _center_text(draw, text, font, cy, fill):
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    draw.text((W / 2 - tw / 2 - bbox[0], cy), text, font=font, fill=fill)
    return bbox[3] - bbox[1]


def _wrap(text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.getbbox(trial)[2] <= max_w:
            cur = trial
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines


def render(lang="is", out=None):
    t1, t2, sub = STRINGS[lang]
    base = _gradient_bg().convert("RGBA")

    # sun glow (top-right) — radial #FFF6DE -> #FFE9B0 -> transparent
    base.alpha_composite(_radial_glow(
        (int(W * 0.83), int(H * 0.20)), 360,
        [(0.0, _hex("#FFF6DE"), 235), (0.42, _hex("#FFE9B0"), 170),
         (0.72, _hex("#FFE9B0"), 0)]))

    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(deco)
    # clouds (positions/opacity from .hero__cloud--1/2/3)
    _cloud(dd, int(W * 0.10), int(H * 0.24), 2.4, 255)
    _cloud(dd, int(W * 0.46), int(H * 0.15), 1.9, 200)
    _cloud(dd, int(W * 0.72), int(H * 0.33), 2.2, 230)
    # birds
    _bird(dd, int(W * 0.15), int(H * 0.27), 2.2)
    _bird(dd, int(W * 0.23), int(H * 0.20), 1.7)
    _bird(dd, int(W * 0.80), int(H * 0.30), 2.0)
    base.alpha_composite(deco)

    _mountains(base)

    draw = ImageDraw.Draw(base)
    # wordmark top-left (as on the homepage navbar above the hero)
    wm = _font(46, 800)
    draw.text((64, 54), "SameFare", font=wm, fill=_hex("#0E5C4C"))

    # headline (two lines) — line1 dark, line2 green gradient
    title_f = _font(118, 900)
    y = int(H * 0.30)
    h1 = _center_text(draw, t1, title_f, y, _hex("#103A36"))
    # Generous gap: accented caps (Í) on line 2 rise above normal cap height and
    # would otherwise crowd line 1.
    line_gap = 54
    y2 = y + h1 + line_gap
    bbox2 = title_f.getbbox(t2)
    _gradient_text(base, t2, title_f, W / 2, y2, _hex("#006C5B"), _hex("#00A886"))

    # subtitle
    draw = ImageDraw.Draw(base)
    sub_f = _font(38, 500)
    sy = y2 + (bbox2[3] - bbox2[1]) + 46
    for line in _wrap(sub, sub_f, int(W * 0.62)):
        h = _center_text(draw, line, sub_f, sy, _hex("#3D5C5E"))
        sy += h + 16

    out = out or os.path.join(os.path.expanduser("~/Desktop"),
                              f"samefare_fb_cover_{lang}.png")
    base.convert("RGB").save(out, "PNG")
    print("wrote", out)
    return out


if __name__ == "__main__":
    render("is")
    render("en")
