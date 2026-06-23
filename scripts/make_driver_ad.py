"""
Driver-recruitment ad creatives in the SameFare hero style.
Renders a square (1080x1080, feed) and a vertical (1080x1350, mobile/Reels).
Cost-sharing framing only — never 'earn money'.
"""
import os
from PIL import Image, ImageDraw, ImageFont

FONT = os.path.join(os.path.dirname(__file__), "..", "static", "fonts", "Nunito.ttf")
DESK = os.path.expanduser("~/Desktop")

HEAD1 = "Deildu akstrinum."
HEAD2 = "Deildu kostnaðinum."
SUB   = "Þú ert hvort sem er að keyra — leyfðu farþegum að deila bensínkostnaðinum með þér."
FOOT  = "Skráðu ferð á samefare.is"


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


def _bg(W, H):
    top, mid, bot = _hex("#BFE3F0"), _hex("#D6ECF4"), _hex("#EAF6FB")
    img = Image.new("RGB", (W, H)); px = img.load()
    for y in range(H):
        t = y / (H - 1)
        col = _lerp(top, mid, t / 0.52) if t <= 0.52 else _lerp(mid, bot, (t - 0.52) / 0.48)
        for x in range(W):
            px[x, y] = col
    return img.convert("RGBA")


def _sun(base, W, H):
    cx, cy, rad = int(W * 0.82), int(H * 0.17), int(W * 0.28)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0)); pl = layer.load()
    core, edge = _hex("#FFF6DE"), _hex("#FFE9B0")
    for y in range(max(0, cy - rad), min(H, cy + rad)):
        for x in range(max(0, cx - rad), min(W, cx + rad)):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / rad
            if d >= 1:
                continue
            if d < 0.42:
                col, a = _lerp(core, edge, d / 0.42), 235
            else:
                col, a = edge, round(235 * (1 - (d - 0.42) / 0.58))
            pl[x, y] = (col[0], col[1], col[2], max(0, a))
    base.alpha_composite(layer)


def _cloud(d, cx, cy, s, alpha=255):
    w = (255, 255, 255, alpha)
    for dx, dy, rx, ry in [(-28,8,32,17),(4,0,30,22),(34,9,26,16)]:
        d.ellipse([cx+(dx-rx)*s, cy+(dy-ry)*s, cx+(dx+rx)*s, cy+(dy+ry)*s], fill=w)
    d.rounded_rectangle([cx-46*s, cy+10*s, cx+46*s, cy+25*s], radius=7.5*s, fill=w)


def _mountains(base, W, H):
    band = int(H * 0.34); top_y = H - band
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    sx = W / 1440.0
    def sp(path): return [(x*sx, top_y + (y/220.0)*band) for x, y in path]
    back = [(0,220),(0,150),(140,90),(300,150),(440,70),(620,150),(780,60),(980,150),(1140,90),(1320,150),(1440,110),(1440,220)]
    front = [(0,220),(0,185),(180,130),(360,185),(520,120),(700,185),(900,115),(1100,185),(1280,135),(1440,185),(1440,220)]
    d.polygon(sp(back), fill=_hex("#AAC9C0"))
    d.polygon(sp(front), fill=_hex("#7BA294"))
    base.alpha_composite(layer)


def _center(draw, text, font, cy, fill, W):
    b = font.getbbox(text); tw = b[2] - b[0]
    draw.text((W/2 - tw/2 - b[0], cy), text, font=font, fill=fill)
    return b[3] - b[1]


def _center_grad(base, text, font, cy, c0, c1, W):
    b = font.getbbox(text); tw, th = b[2]-b[0], b[3]-b[1]
    mask = Image.new("L", (tw+8, th+8), 0)
    ImageDraw.Draw(mask).text((4-b[0], 4-b[1]), text, font=font, fill=255)
    grad = Image.new("RGB", (tw+8, th+8)); gp = grad.load()
    for x in range(tw+8):
        col = _lerp(c0, c1, x/(tw+7))
        for y in range(th+8):
            gp[x, y] = col
    base.paste(grad, (round(W/2 - tw/2) - 4, round(cy) - 4), mask)
    return th


def _wrap(text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if font.getbbox((cur+" "+w).strip())[2] <= max_w:
            cur = (cur+" "+w).strip()
        else:
            lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines


def render(W, H, out):
    base = _bg(W, H)
    _sun(base, W, H)
    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0)); dd = ImageDraw.Draw(deco)
    _cloud(dd, int(W*0.15), int(H*0.22), W/640*1.15, 255)
    _cloud(dd, int(W*0.63), int(H*0.10), W/640*0.9, 205)
    base.alpha_composite(deco)
    _mountains(base, W, H)

    draw = ImageDraw.Draw(base)
    # wordmark
    draw.text((int(W*0.07), int(H*0.06)), "SameFare", font=_font(int(W*0.052), 800), fill=_hex("#0E5C4C"))

    # headline (two lines) — generous gap so the accents/ascenders never collide
    hf = _font(int(W*0.092), 900)
    y = int(H * 0.27)
    h1 = _center(draw, HEAD1, hf, y, _hex("#103A36"), W)
    y2 = y + h1 + int(H*0.050)
    h2 = _center_grad(base, HEAD2, hf, y2, _hex("#006C5B"), _hex("#00A886"), W)

    # subline (wrapped)
    draw = ImageDraw.Draw(base)
    sf = _font(int(W*0.038), 600)
    sy = y2 + h2 + int(H*0.055)
    for line in _wrap(SUB, sf, int(W*0.82)):
        hh = _center(draw, line, sf, sy, _hex("#3D5C5E"), W)
        sy += hh + int(H*0.018)

    # footer pill
    pf = _font(int(W*0.040), 800)
    b = pf.getbbox(FOOT); tw = b[2]-b[0]
    py = int(H * (0.80 if H > W else 0.84))
    pad_x, pad_y = int(W*0.05), int(W*0.028)
    draw.rounded_rectangle(
        [W/2 - tw/2 - pad_x, py - pad_y, W/2 + tw/2 + pad_x, py + (b[3]-b[1]) + pad_y],
        radius=int(W*0.06), fill=_hex("#0E5C4C"))
    _center(draw, FOOT, pf, py - b[1], (255, 255, 255), W)

    base.convert("RGB").save(out, "PNG")
    print("wrote", out)


if __name__ == "__main__":
    render(1080, 1080, os.path.join(DESK, "samefare_driver_ad_square.png"))
    render(1080, 1350, os.path.join(DESK, "samefare_driver_ad_vertical.png"))
