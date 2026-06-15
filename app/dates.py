"""
Locale-aware date formatting (en + is).

strftime always emits English month/weekday names (C locale). For Icelandic we
post-process the output, swapping the English names for their Icelandic forms —
names only; digits and times are locale-neutral. Full names are replaced before
abbreviations, and abbreviation keys stay capitalised so they never match the
already-substituted lowercase Icelandic text.
"""

_IS_REPLACEMENTS = [
    # Full month names
    ("January", "janúar"), ("February", "febrúar"), ("March", "mars"),
    ("April", "apríl"), ("May", "maí"), ("June", "júní"), ("July", "júlí"),
    ("August", "ágúst"), ("September", "september"), ("October", "október"),
    ("November", "nóvember"), ("December", "desember"),
    # Full weekday names
    ("Monday", "mánudagur"), ("Tuesday", "þriðjudagur"), ("Wednesday", "miðvikudagur"),
    ("Thursday", "fimmtudagur"), ("Friday", "föstudagur"), ("Saturday", "laugardagur"),
    ("Sunday", "sunnudagur"),
    # Abbreviated months
    ("Jan", "jan"), ("Feb", "feb"), ("Mar", "mar"), ("Apr", "apr"), ("Jun", "jún"),
    ("Jul", "júl"), ("Aug", "ágú"), ("Sep", "sep"), ("Oct", "okt"), ("Nov", "nóv"),
    ("Dec", "des"),
    # Abbreviated weekdays
    ("Mon", "mán"), ("Tue", "þri"), ("Wed", "mið"), ("Thu", "fim"), ("Fri", "fös"),
    ("Sat", "lau"), ("Sun", "sun"),
]


def localize_strftime(dt, fmt: str, lang: str = "en") -> str:
    if dt is None:
        return ""
    out = dt.strftime(fmt)
    if lang == "is":
        for en, ic in _IS_REPLACEMENTS:
            out = out.replace(en, ic)
    return out


def make_date_formatter(lang: str):
    """Return a template-friendly ``fdate(dt, fmt)`` bound to the request language."""
    def _fmt(dt, fmt: str) -> str:
        return localize_strftime(dt, fmt, lang)
    return _fmt
