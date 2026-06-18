"""
Dynamic Open Graph share images for trips.

Renders a branded 1200×630 PNG per ride (route + date + price) so that a shared
trip link previews as a polished card on iMessage / WhatsApp / Facebook / X
instead of the generic site image. Rendered with Pillow + the bundled Nunito
variable font; pure in-process, no external calls.
"""
import os
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

_FONT_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "fonts", "Nunito.ttf")

# Brand palette (mirrors static/css/style.css)
_BG        = "#FAFAF7"
_CARD      = "#FFFFFF"
_INK       = "#0F172A"
_MUTED     = "#64748B"
_PRIMARY   = "#006C5B"
_PRIMARY_D = "#004D41"

_W, _H = 1200, 630


@lru_cache(maxsize=16)
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


def render_home_og(lang: str = "is") -> bytes:
    """Branded homepage share card (1200×630) — pale sky, sun, mountains, the
    SameFare wordmark and the hero tagline. Used as the site-wide og:image."""
    l1, l2, sub = _HOME.get(lang, _HOME["is"])
    img  = Image.new("RGB", (_W, _H), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    # Sky gradient
    top, bot = (222, 239, 246), (255, 255, 255)
    for y in range(_H):
        t = y / _H
        draw.line([(0, y), (_W, y)],
                  fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))

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

    # Tagline (two lines, shrink to fit the wider of the two)
    tf = _fit_font(draw, max(l1, l2, key=len), _W - 160, 86, 800, min_size=56)
    _center(draw, l1, tf, 196, "#0F2A24")
    _center(draw, l2, tf, 296, "#2E9D77")
    _center(draw, sub, _font(34, 600), 416, "#5A6E67")

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _arrow(draw, x, cy, length, color, thickness=10):
    """Draw a right-pointing arrow (Nunito has no → glyph). Returns its width."""
    head = 22
    x2 = x + length
    draw.line([(x, cy), (x2 - head, cy)], fill=color, width=thickness)
    draw.polygon([(x2 - head, cy - head), (x2, cy), (x2 - head, cy + head)], fill=color)
    return length


def render_trip_og(origin: str, destination: str, date_label: str,
                   price_label: str, seats_label: str = "") -> bytes:
    """Return PNG bytes for a trip's share card."""
    img  = Image.new("RGB", (_W, _H), _BG)
    draw = ImageDraw.Draw(img)

    # Left brand accent bar
    draw.rectangle([0, 0, 16, _H], fill=_PRIMARY)

    pad_x = 96
    content_w = _W - pad_x * 2
    arrow_w = 70

    # Wordmark
    draw.text((pad_x, 60), "SameFare", font=_font(46, 900), fill=_PRIMARY)

    # Route — origin on one line, a drawn arrow + destination on the next
    # (handles long Icelandic names; shrinks to fit the widest of the two).
    rf = _fit_font(draw, destination, content_w - arrow_w - 28, 82, 800)
    draw.text((pad_x, 156), origin, font=rf, fill=_INK)
    dest_y = 268
    dasc = rf.getmetrics()[0]
    _arrow(draw, pad_x + 4, dest_y + dasc // 2, arrow_w, _PRIMARY)
    draw.text((pad_x + arrow_w + 28, dest_y), destination, font=rf, fill=_PRIMARY_D)

    # Date / time
    draw.text((pad_x, 398), date_label, font=_font(38, 600), fill=_MUTED)

    # Price pill (bottom-left)
    pf = _font(44, 800)
    pill_pad_x, pill_pad_y = 32, 16
    tw = draw.textlength(price_label, font=pf)
    asc, desc = pf.getmetrics()
    th = asc + desc
    py1 = _H - 50
    py0 = py1 - (th + pill_pad_y * 2)
    px0 = pad_x
    px1 = px0 + tw + pill_pad_x * 2
    draw.rounded_rectangle([px0, py0, px1, py1], radius=(py1 - py0) // 2, fill=_PRIMARY)
    draw.text((px0 + pill_pad_x, py0 + pill_pad_y), price_label, font=pf, fill="#FFFFFF")

    # Seats note next to the pill
    if seats_label:
        draw.text((px1 + 28, py0 + pill_pad_y + 2), seats_label, font=_font(34, 600), fill=_MUTED)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
