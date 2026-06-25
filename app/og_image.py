"""
Dynamic Open Graph share images for trips.

Renders a branded 1200×630 PNG per ride so that a shared trip link previews as a
polished card on iMessage / WhatsApp / Facebook / X instead of the generic site
image. Rendered with Pillow + the bundled Nunito variable font; pure in-process,
no external calls.
"""
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
    "is": {"instant": "Tafarlaus bókun", "id": "Skilríki", "phone": "Sími"},
    "en": {"instant": "Instant book",    "id": "ID",        "phone": "Phone"},
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


def _verified_pill(draw, x, y, label):
    """A soft green '✓ label' chip. Returns its total width."""
    f = _font(26, 700)
    tw = draw.textlength(label, font=f)
    h = 44
    chk = 26
    w = 18 + chk + 8 + tw + 18
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2,
                           fill="#E7F6EE", outline="#BfE6CE", width=1)
    _check(draw, x + 18, y + (h - chk) / 2, chk, _GREEN_OK)
    draw.text((x + 18 + chk + 8, y + (h - 32) / 2), label, font=f, fill="#15803D")
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
                   avatar_png: bytes | None = None, lang: str = "is") -> bytes:
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

    # ── Driver row (bottom-left): avatar + name + verified pills ────────────────
    dy = 524
    if driver_name:
        _avatar(img, draw, avatar_png, pad + 36, dy, 36, driver_name)
        nx = pad + 36 + 36 + 22
        nf = _font(38, 800)
        draw.text((nx, dy - 50), driver_name, font=nf, fill=_INK)
        px = nx
        py = dy + 4
        if id_verified:
            px += _verified_pill(draw, px, py, L["id"]) + 12
        if phone_verified:
            _verified_pill(draw, px, py, L["phone"])

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
