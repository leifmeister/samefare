"""
Shared utility helpers.
"""
import unicodedata
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# ── City name normalisation ───────────────────────────────────────────────────

# Canonical list of Icelandic city names used throughout the platform.
# Must stay in sync with ICELANDIC_CITIES in app/routers/trips.py.
_CANONICAL_CITIES: tuple[str, ...] = (
    "Akureyri", "Blönduós", "Borgarnes", "Egilsstaðir", "Hella",
    "Höfn", "Húsavík", "Hveragerði", "Ísafjörður", "Keflavík",
    "Kirkjubæjarklaustur", "Mývatn", "Ólafsvík", "Reykjavík",
    "Sauðárkrókur", "Selfoss", "Siglufjörður", "Stykkishólmur", "Vík",
)


def _strip_diacritics(s: str) -> str:
    """
    Return ASCII-folded lowercase string.

    Handles both composed characters (e.g. 'á' → 'a', 'ö' → 'o') via NFD
    decomposition AND Icelandic-specific characters that have no ASCII
    decomposition and would otherwise be silently dropped:
      ð / Ð → d
      þ / Þ → th
      æ / Æ → ae
    """
    # Pre-substitute Icelandic letters that don't decompose under NFD
    s = s.replace("ð", "d").replace("Ð", "d")
    s = s.replace("þ", "th").replace("Þ", "th")
    # æ is romanised as bare 'a' in most Icelandic place names
    # (e.g. Kirkjubæjarklaustur → Kirkjubajarklaustur), not 'ae'
    s = s.replace("æ", "a").replace("Æ", "a")
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii").lower()


_CITY_LOOKUP: dict[str, str] = {_strip_diacritics(c): c for c in _CANONICAL_CITIES}


def canonical_city(name: str) -> str:
    """
    Return the canonical Icelandic spelling of a city name.

    Handles ASCII input from users who skip diacritics
    (e.g. 'Reykjavik' → 'Reykjavík', 'akureyri' → 'Akureyri').
    Unknown names are returned stripped but otherwise unchanged so that
    ILIKE substring matching on freeform input still works.
    """
    return _CITY_LOOKUP.get(_strip_diacritics(name.strip()), name.strip())


# ── Route graph & intermediate-stop detection ────────────────────────────────

# A city B is "on the way" from A to C if the triangular detour is within this
# fraction of the direct A→C distance.  10 % is tight enough for Iceland's
# mostly-linear road network; it filters out cities that are adjacent but off-
# route (e.g. Selfoss is NOT on the direct Reykjavík→Akureyri Ring Road).
_ROUTE_TOLERANCE = 0.10


def build_route_graph(db: "Session") -> dict[str, dict[str, float]]:
    """
    Return adjacency dict ``{city: {neighbour: distance_km}}`` built from all
    active rows in the ``routes`` table.

    Both directions (A→B and B→A) are stored as separate rows so the graph is
    directional — important for the ordering check in segment matching.
    """
    from app import models as _m
    graph: dict[str, dict[str, float]] = {}
    for r in db.query(_m.Route).filter(_m.Route.is_active == True).all():  # noqa: E712
        graph.setdefault(r.origin, {})[r.destination] = float(r.distance_km)
    return graph


def route_km(
    graph: dict[str, dict[str, float]],
    origin: str,
    destination: str,
) -> Optional[float]:
    """Return the direct road distance (km) between two cities, or None."""
    return graph.get(origin, {}).get(destination)


def is_on_route(
    graph: dict[str, dict[str, float]],
    trip_origin: str,
    trip_destination: str,
    city: str,
) -> bool:
    """
    Return True if *city* lies on the direct route from *trip_origin* to
    *trip_destination*.

    Endpoints are always considered "on the route".  An intermediate city is
    on the route when the triangular inequality holds within _ROUTE_TOLERANCE:

        dist(A→B) + dist(B→C)  ≤  dist(A→C) × (1 + tolerance)
    """
    if city in (trip_origin, trip_destination):
        return True
    d_total = graph.get(trip_origin, {}).get(trip_destination)
    if d_total is None:
        return False
    d1 = graph.get(trip_origin, {}).get(city)
    d2 = graph.get(city, {}).get(trip_destination)
    if d1 is None or d2 is None:
        return False
    return (d1 + d2) <= d_total * (1 + _ROUTE_TOLERANCE)


def prorate_segment_price(
    full_price_per_seat: int,
    seg_km: float,
    total_km: float,
) -> int:
    """
    Return a prorated price for a partial segment.

    Rounds to the nearest 100 ISK and enforces a 200 ISK floor so the driver
    always earns something meaningful even on very short hops.
    """
    if total_km <= 0:
        return full_price_per_seat
    ratio    = seg_km / total_km
    prorated = round(full_price_per_seat * ratio / 100) * 100
    return max(200, prorated)


def safe_redirect(target: str, fallback: str = "/") -> str:
    """
    Return a safe same-origin redirect path from *target*.

    * If *target* is already a relative path (``/trips``, ``/trips?d=1``)
      it is returned as-is — provided it doesn't start with ``//``
      (protocol-relative, treated as external by browsers).
    * If *target* is an absolute URL (e.g. the full ``Referer`` header
      ``https://www.samefare.com/trips``), only the path+query+fragment
      is returned so the host is stripped.
    * Anything else (external host, ``javascript:``, empty) returns
      *fallback* (default ``"/"``).
    """
    t = (target or "").strip()
    if not t:
        return fallback

    # Absolute URL — extract path only (strips host, prevents open redirect)
    parsed = urlparse(t)
    if parsed.scheme or parsed.netloc:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        if parsed.fragment:
            path += "#" + parsed.fragment
        # path is now guaranteed to start with "/" and have no host
        return path if path.startswith("/") else fallback

    # Relative path — reject protocol-relative (//evil.com)
    if t.startswith("/") and not t.startswith("//"):
        return t

    return fallback
