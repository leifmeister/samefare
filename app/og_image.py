"""
Dynamic Open Graph share images for trips.

Renders a branded 1200×630 PNG per ride so that a shared trip link previews as a
polished card on iMessage / WhatsApp / Facebook / X instead of the generic site
image. Rendered with Pillow + the bundled Nunito variable font; pure in-process,
no external calls.
"""
import math
import os
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

_FONT_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "fonts", "Nunito.ttf")

# Brand palette (mirrors static/css/style.css)
_BG        = "#FAFAF7"
_INK       = "#0F2A24"
_MUTED     = "#5A6E67"
_PRIMARY   = "#006C5B"
_PRIMARY_D = "#004D41"
_AMBER     = "#E0A92E"   # destination marker (matches the app's gold dropoff dot)
_GREEN_OK  = "#16A34A"

_W, _H = 1200, 630


@lru_cache(maxsize=24)
def _font(size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(_FONT_PATH, size)
    try:
        f.set_variation_by_axes([weight])   # Nunito is a variable font
    except Exception:
        pass
    return f


def _fit_font(draw, text, max_width, size, weight, min_size=40):
    """Shrink the font until `text` fits within max_width (long city names)."""
    while size > min_size:
        f = _font(size, weight)
        if draw.textlength(text, font=f) <= max_width:
            return f
        size -= 4
    return _font(min_size, weight)


def _center(draw, text, font, y, fill):
    draw.text(((_W - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)


# Homepage share card copy, per language.
_HOME = {
    "is": ("Verum Sameferða", "um Ísland", "Finndu ódýrar ferðir á milli íslenskra bæja."),
    "en": ("Share the journey", "across Iceland", "Find affordable rides between Iceland's towns."),
}

# Trip-card chrome words, per language (the data itself is passed in pre-localised).
_TRIP = {
    "is": {"instant": "Tafarlaus bókun", "id": "Skilríki", "lic": "Ökuskírteini", "phone": "Sími"},
    "en": {"instant": "Instant book",    "id": "ID",        "lic": "Licence",     "phone": "Phone"},
}


def _sky(draw):
    """Pale-sky vertical gradient used by both cards."""
    top, bot = (222, 239, 246), (255, 255, 255)
    for y in range(_H):
        t = y / _H
        draw.line([(0, y), (_W, y)],
                  fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))


def render_home_og(lang: str = "is") -> bytes:
    """Branded homepage share card (1200×630) — pale sky, sun, mountains, the
    SameFare wordmark and the hero tagline. Used as the site-wide og:image."""
    l1, l2, sub = _HOME.get(lang, _HOME["is"])
    img  = Image.new("RGB", (_W, _H), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    _sky(draw)
    draw.ellipse([978, 63, 1122, 207], fill="#FCE096")                       # sun
    draw.polygon([(0, _H), (0, 520), (250, 478), (600, 524), (950, 482),
                  (_W, 520), (_W, _H)], fill="#AFC8BD")                       # back range
    draw.polygon([(0, _H), (0, 556), (300, 528), (600, 560), (900, 530),
                  (_W, 556), (_W, _H)], fill="#8CB0A2")                       # front range

    # Two-tone wordmark, centered
    wf = _font(54, 900)
    ws = draw.textlength("Same", font=wf)
    wx = (_W - (ws + draw.textlength("Fare", font=wf))) / 2
    draw.text((wx, 60), "Same", font=wf, fill="#004D41")
    draw.text((wx + ws, 60), "Fare", font=wf, fill="#006C5B")

    tf = _fit_font(draw, max(l1, l2, key=len), _W - 160, 86, 800, min_size=56)
    _center(draw, l1, tf, 196, "#0F2A24")
    _center(draw, l2, tf, 296, "#2E9D77")
    _center(draw, sub, _font(34, 600), 416, "#5A6E67")

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Trip card helpers ──────────────────────────────────────────────────────────

def _wordmark(draw, x, y, size):
    wf = _font(size, 900)
    sw = draw.textlength("Same", font=wf)
    draw.text((x, y), "Same", font=wf, fill=_PRIMARY_D)
    draw.text((x + sw, y), "Fare", font=wf, fill=_PRIMARY)
    return sw + draw.textlength("Fare", font=wf)


def _check(draw, x, y, s, color):
    """A check mark whose bounding box is roughly s×s, top-left at (x, y)."""
    draw.line([(x + s * 0.16, y + s * 0.55), (x + s * 0.42, y + s * 0.82),
               (x + s * 0.86, y + s * 0.22)],
              fill=color, width=max(3, int(s * 0.16)), joint="curve")


def _bez(p0, p1, p2, p3, n=14):
    """Sample a cubic Bézier into n points (excluding the start)."""
    out = []
    for i in range(1, n + 1):
        t = i / n; mt = 1 - t
        out.append((mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0],
                    mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1]))
    return out


def _arc20(x1, y1, x2, y2, r, n=18):
    """Sample an SVG elliptical arc (rx=ry=r, φ=0, large-arc=0, sweep=0)."""
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    rx = ry = r
    lam = dx*dx/(rx*rx) + dy*dy/(ry*ry)
    if lam > 1:
        sc = math.sqrt(lam); rx *= sc; ry *= sc
    num = rx*rx*ry*ry - rx*rx*dy*dy - ry*ry*dx*dx
    den = rx*rx*dy*dy + ry*ry*dx*dx
    coef = -math.sqrt(max(0.0, num / den))      # large-arc == sweep → negative
    cxp, cyp = coef*rx*dy/ry, -coef*ry*dx/rx
    cx, cy = cxp + (x1 + x2) / 2, cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        d = (ux*vx + uy*vy) / (math.hypot(ux, uy) * math.hypot(vx, vy))
        a = math.acos(max(-1.0, min(1.0, d)))
        return -a if ux*vy - uy*vx < 0 else a

    t1 = ang(1, 0, (dx - cxp) / rx, (dy - cyp) / ry)
    dt = ang((dx - cxp) / rx, (dy - cyp) / ry, (-dx - cxp) / rx, (-dy - cyp) / ry)
    if dt > 0:                                   # sweep == 0 → negative sweep
        dt -= 2 * math.pi
    return [(cx + rx*math.cos(t1 + dt*i/n), cy + ry*math.sin(t1 + dt*i/n))
            for i in range(1, n + 1)]


@lru_cache(maxsize=1)
def _shield_poly():
    """The exact Heroicons solid `shield-check` outline, sampled into a polygon
    and normalised to a (0,0)-anchored box ~16 wide × 16.37 tall."""
    p = [(2.166, 4.999)]
    p += _arc20(2.166, 4.999, 10, 1.944, 11.954)
    p += _arc20(10, 1.944, 17.834, 5, 11.954)
    p += _bez((17.834, 5), (17.944, 5.65), (18.0, 6.32), (18.0, 7.001))
    p += _bez((18.0, 7.001), (18.0, 12.226), (14.66, 16.671), (10.0, 18.318))
    p += _bez((10.0, 18.318), (5.34, 16.67), (2.0, 12.225), (2.0, 7.0))
    p += _bez((2.0, 7.0), (2.0, 6.318), (2.057, 5.65), (2.166, 4.999))
    return tuple((px - 2.0, py - 1.944) for px, py in p)


def _shield(draw, x, y, w, fill, check_color=None):
    """Draw the Heroicons shield-check, width `w`, top-left at (x, y). A check is
    punched in `check_color` (use the background colour for the 'hole' look)."""
    sc = w / 16.0
    draw.polygon([(x + px*sc, y + py*sc) for px, py in _shield_poly()], fill=fill)
    if check_color:
        _check(draw, x + 3.9*sc, y + 5.3*sc, 8.2*sc, check_color)
    return 16.374 * sc


def _site_pill(draw, x, y, label, scale=1.0):
    """Verified chip matching the site's `.trust-badge`: #D1FAE5 pill, #065F46
    text. No icon — the shield by the driver name already signals verified.
    Returns its total width."""
    s = lambda v: v * scale
    f = _font(int(s(27)), 700)
    tw = draw.textlength(label, font=f)
    px = s(24)
    h = s(50)
    w = px + tw + px
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, fill="#D1FAE5")
    asc = f.getmetrics()[0]
    draw.text((x + px, y + (h - asc) / 2 - s(2)), label, font=f, fill="#065F46")
    return w


def _avatar(img, draw, avatar_png, cx, cy, r, initial):
    """Circular driver avatar, or a branded initial disc as a fallback."""
    if avatar_png:
        try:
            av = Image.open(BytesIO(avatar_png)).convert("RGB")
            av = ImageOps.fit(av, (2 * r, 2 * r), centering=(0.5, 0.5))
            mask = Image.new("L", (2 * r, 2 * r), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
            img.paste(av, (cx - r, cy - r), mask)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline="#FFFFFF", width=4)
            return
        except Exception:
            pass
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#CDE5DD")
    f = _font(int(r * 1.0), 800)
    ch = (initial or "?")[:1].upper()
    tw = draw.textlength(ch, font=f)
    asc, desc = f.getmetrics()
    draw.text((cx - tw / 2, cy - (asc + desc) / 2 + 2), ch, font=f, fill=_PRIMARY_D)


def render_trip_og(origin: str, destination: str, date_label: str, time_label: str,
                   price_label: str, per_label: str, seats_label: str = "",
                   driver_name: str = "", id_verified: bool = False,
                   phone_verified: bool = False, instant_book: bool = False,
                   avatar_png: bytes | None = None, lang: str = "is",
                   license_verified: bool = False) -> bytes:
    """Return PNG bytes for a trip's share card (1200×630)."""
    L = _TRIP.get(lang, _TRIP["is"])
    img  = Image.new("RGB", (_W, _H), _BG)
    draw = ImageDraw.Draw(img)

    _sky(draw)
    # Soft sun glow, top-right
    glow = Image.new("RGB", (_W, _H), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([980, 40, 1180, 240], fill="#FCE096")
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    img = Image.composite(Image.new("RGB", (_W, _H), "#FCE7A0"), img,
                          glow.convert("L").point(lambda p: int(p * 0.55)))
    draw = ImageDraw.Draw(img)
    # Mountain silhouettes along the bottom
    draw.polygon([(0, _H), (0, 588), (260, 556), (560, 590), (860, 558),
                  (_W, 586), (_W, _H)], fill="#C4D8CE")
    draw.polygon([(0, _H), (0, 610), (320, 586), (640, 612), (940, 588),
                  (_W, 608), (_W, _H)], fill="#A2C2B4")

    pad = 80
    # ── Header: wordmark + instant-book chip ────────────────────────────────────
    _wordmark(draw, pad, 52, 46)
    if instant_book:
        f = _font(26, 800)
        label = L["instant"]
        tw = draw.textlength(label, font=f)
        h = 50
        # lightning glyph (drawn) + label
        w = 24 + 22 + 10 + tw + 26
        x0 = _W - pad - w
        draw.rounded_rectangle([x0, 52, x0 + w, 52 + h], radius=h // 2, fill=_PRIMARY)
        lx, ly = x0 + 24, 52 + 9
        draw.polygon([(lx + 14, ly), (lx, ly + 18), (lx + 9, ly + 18),
                      (lx + 4, ly + 32), (lx + 20, ly + 12), (lx + 10, ly + 12)],
                     fill="#FCE096")
        draw.text((x0 + 24 + 22 + 10, 52 + (h - 34) / 2), label, font=f, fill="#FFFFFF")

    # ── Route: green→amber timeline + city names ───────────────────────────────
    name_x = pad + 56
    name_w = _W - name_x - pad
    rf = _fit_font(draw, max(origin, destination, key=len), name_w, 92, 800, min_size=54)
    asc = rf.getmetrics()[0]
    o_top, d_top = 150, 286
    o_cy, d_cy = o_top + asc * 0.42, d_top + asc * 0.42
    dot = 17
    draw.line([(pad + 18, o_cy), (pad + 18, d_cy)], fill="#9DBDB2", width=6)
    draw.ellipse([pad + 18 - dot, o_cy - dot, pad + 18 + dot, o_cy + dot], fill=_PRIMARY)
    draw.ellipse([pad + 18 - dot, d_cy - dot, pad + 18 + dot, d_cy + dot], fill=_AMBER)
    draw.text((name_x, o_top), origin, font=rf, fill=_INK)
    draw.text((name_x, d_top), destination, font=rf, fill=_PRIMARY_D)

    # ── Meta row: date · time, seats ───────────────────────────────────────────
    mf = _font(36, 700)
    meta = f"{date_label} · {time_label}"
    my = 408
    draw.text((name_x, my), meta, font=mf, fill=_MUTED)
    if seats_label:
        sx = name_x + draw.textlength(meta, font=mf) + 28
        sf = _font(30, 700)
        sw = draw.textlength(seats_label, font=sf)
        draw.rounded_rectangle([sx, my - 2, sx + sw + 36, my + 46], radius=24,
                               fill="#FFFFFF", outline="#D8E6E0", width=1)
        draw.text((sx + 18, my + 4), seats_label, font=sf, fill=_PRIMARY_D)

    # ── Driver row (bottom-left): avatar + name (+shield) + verified pills ──────
    dy = 520
    if driver_name:
        _avatar(img, draw, avatar_png, pad + 36, dy, 36, driver_name)
        nx = pad + 36 + 36 + 22
        nf = _font(38, 800)
        ny = dy - 52
        draw.text((nx, ny), driver_name, font=nf, fill=_INK)
        asc = nf.getmetrics()[0]
        if id_verified:
            ne = nx + draw.textlength(driver_name, font=nf)
            shw = 33
            _shield(draw, ne + 16, ny + (asc - 16.374 / 16 * shw) / 2, shw, _PRIMARY, "#FFFFFF")
        labels = [lab for ok, lab in ((id_verified, L["id"]),
                                      (license_verified, L["lic"]),
                                      (phone_verified, L["phone"])) if ok]
        px = nx
        for lab in labels[:2]:
            px += _site_pill(draw, px, dy + 6, lab) + 12

    # ── Price pill (bottom-right), the eye-catcher ─────────────────────────────
    pf = _font(52, 900)
    sf = _font(30, 700)
    pw = draw.textlength(price_label, font=pf)
    perw = draw.textlength(per_label, font=sf)
    pad_in = 40
    inner = pw + 12 + perw
    h = 96
    x1 = _W - pad
    x0 = x1 - (inner + pad_in * 2)
    y0 = dy - 48
    draw.rounded_rectangle([x0, y0, x1, y0 + h], radius=h // 2, fill=_PRIMARY)
    asc2, desc2 = pf.getmetrics()
    ty = y0 + (h - (asc2 + desc2)) / 2
    draw.text((x0 + pad_in, ty), price_label, font=pf, fill="#FFFFFF")
    draw.text((x0 + pad_in + pw + 12, ty + (asc2 - sf.getmetrics()[0]) + 6),
              per_label, font=sf, fill="#BFE6DB")

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── 9:16 vertical card for Instagram / Facebook Stories ─────────────────────────

_SW, _SH = 1080, 1920


def render_trip_story(origin: str, destination: str, date_label: str, time_label: str,
                      price_label: str, per_label: str, seats_label: str = "",
                      driver_name: str = "", id_verified: bool = False,
                      phone_verified: bool = False, instant_book: bool = False,
                      avatar_png: bytes | None = None, lang: str = "is",
                      cta: str = "", license_verified: bool = False) -> bytes:
    """Return PNG bytes for a 1080×1920 Stories card (Instagram / Facebook)."""
    L = _TRIP.get(lang, _TRIP["is"])
    img  = Image.new("RGB", (_SW, _SH), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    # Sky gradient
    top, bot = (222, 239, 246), (255, 255, 255)
    for y in range(_SH):
        t = y / _SH
        draw.line([(0, y), (_SW, y)],
                  fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    # Sun glow, upper-right
    glow = Image.new("RGB", (_SW, _SH), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([720, 140, 1120, 540], fill="#FCE096")
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    img = Image.composite(Image.new("RGB", (_SW, _SH), "#FCE7A0"), img,
                          glow.convert("L").point(lambda p: int(p * 0.5)))
    draw = ImageDraw.Draw(img)
    # Mountains along the bottom
    draw.polygon([(0, _SH), (0, 1772), (360, 1716), (720, 1778), (1080, 1724),
                  (1080, _SH)], fill="#C4D8CE")
    draw.polygon([(0, _SH), (0, 1824), (430, 1782), (820, 1826), (1080, 1788),
                  (1080, _SH)], fill="#A2C2B4")

    pad = 96
    # Wordmark, centered near the top
    wf = _font(76, 900)
    ws = draw.textlength("Same", font=wf)
    full = ws + draw.textlength("Fare", font=wf)
    wx = (_SW - full) / 2
    draw.text((wx, 150), "Same", font=wf, fill=_PRIMARY_D)
    draw.text((wx + ws, 150), "Fare", font=wf, fill=_PRIMARY)

    # Instant-book chip, centered under the wordmark
    if instant_book:
        f = _font(36, 800)
        label = L["instant"]
        tw = draw.textlength(label, font=f)
        h = 74
        w = 44 + 28 + 14 + tw + 44
        x0 = (_SW - w) / 2
        y0 = 290
        draw.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=h // 2, fill=_PRIMARY)
        lx, ly = x0 + 44, y0 + 16
        draw.polygon([(lx + 19, ly), (lx, ly + 25), (lx + 12, ly + 25),
                      (lx + 6, ly + 44), (lx + 27, ly + 16), (lx + 13, ly + 16)],
                     fill="#FCE096")
        draw.text((x0 + 44 + 28 + 14, y0 + (h - 48) / 2), label, font=f, fill="#FFFFFF")

    # Route — the hero. Green→amber timeline + big city names.
    name_x = pad + 74
    name_w = _SW - name_x - pad
    rf = _fit_font(draw, max(origin, destination, key=len), name_w, 150, 800, min_size=78)
    asc = rf.getmetrics()[0]
    o_top, d_top = 440, 440 + 240
    o_cy, d_cy = o_top + asc * 0.42, d_top + asc * 0.42
    dot = 26
    draw.line([(pad + 26, o_cy), (pad + 26, d_cy)], fill="#9DBDB2", width=9)
    draw.ellipse([pad + 26 - dot, o_cy - dot, pad + 26 + dot, o_cy + dot], fill=_PRIMARY)
    draw.ellipse([pad + 26 - dot, d_cy - dot, pad + 26 + dot, d_cy + dot], fill=_AMBER)
    draw.text((name_x, o_top), origin, font=rf, fill=_INK)
    draw.text((name_x, d_top), destination, font=rf, fill=_PRIMARY_D)

    # Date · time + seats chip
    my = d_top + 200
    mf = _font(54, 700)
    meta = f"{date_label} · {time_label}"
    draw.text((pad, my), meta, font=mf, fill=_MUTED)
    if seats_label:
        sy = my + 92
        sf = _font(46, 700)
        sw = draw.textlength(seats_label, font=sf)
        draw.rounded_rectangle([pad, sy, pad + sw + 56, sy + 80], radius=40,
                               fill="#FFFFFF", outline="#D8E6E0", width=2)
        draw.text((pad + 28, sy + 14), seats_label, font=sf, fill=_PRIMARY_D)

    # Driver block: avatar + name (+shield) + verified pills
    dy = my + 230
    if driver_name:
        r = 66
        _avatar(img, draw, avatar_png, pad + r, dy + r, r, driver_name)
        nx = pad + 2 * r + 36
        nf = _fit_font(draw, driver_name, _SW - nx - pad - 80, 62, 800, min_size=40)
        ny = dy + 6
        draw.text((nx, ny), driver_name, font=nf, fill=_INK)
        asc = nf.getmetrics()[0]
        if id_verified:
            ne = nx + draw.textlength(driver_name, font=nf)
            shw = 50
            _shield(draw, ne + 20, ny + (asc - 16.374 / 16 * shw) / 2, shw, _PRIMARY, "#FFFFFF")
        labels = [lab for ok, lab in ((id_verified, L["id"]),
                                      (license_verified, L["lic"]),
                                      (phone_verified, L["phone"])) if ok]
        px = nx
        for lab in labels[:2]:
            px += _site_pill(draw, px, dy + 100, lab, scale=1.5) + 16

    # Price pill — big, prominent
    py = dy + 250
    pf = _font(88, 900)
    sf2 = _font(48, 700)
    pw = draw.textlength(price_label, font=pf)
    perw = draw.textlength(per_label, font=sf2)
    inner = pw + 18 + perw
    padin = 70
    h = 158
    x0 = pad
    x1 = x0 + inner + padin * 2
    if x1 > _SW - pad:
        x0 = (_SW - (inner + padin * 2)) / 2
        x1 = x0 + inner + padin * 2
    draw.rounded_rectangle([x0, py, x1, py + h], radius=h // 2, fill=_PRIMARY)
    asc2, desc2 = pf.getmetrics()
    ty = py + (h - (asc2 + desc2)) / 2
    draw.text((x0 + padin, ty), price_label, font=pf, fill="#FFFFFF")
    draw.text((x0 + padin + pw + 18, ty + (asc2 - sf2.getmetrics()[0]) + 10),
              per_label, font=sf2, fill="#BFE6DB")

    # CTA above the mountains
    if cta:
        cf = _font(48, 700)
        cw = draw.textlength(cta, font=cf)
        draw.text(((_SW - cw) / 2, 1580), cta, font=cf, fill=_PRIMARY_D)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
