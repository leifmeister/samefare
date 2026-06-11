"""
Icelandic road-network geometry helpers.

fetch_osrm_polyline(origin, destination)
    Calls the public OSRM demo server and returns real road geometry as a
    [[lat, lon], …] list.  Returns None on any error or unknown city.

ring_road_polyline(origin, destination)
    Fallback approximation using hand-coded Ring Road waypoints.  Used only
    when OSRM is unavailable.  Mirrors the JS ringRoute() in detail.html —
    keep both in sync when adding new cities.
"""

import json
import logging
import urllib.request

log = logging.getLogger(__name__)

# ── WGS-84 city coordinates for OSRM requests ─────────────────────────────────
CITY_COORDS: dict[str, tuple[float, float]] = {
    "Akureyri":              (65.6885, -18.1059),
    "Blönduós":              (65.6617, -20.2886),
    "Borgarnes":             (64.5390, -21.9224),
    "Egilsstaðir":           (65.2675, -14.3947),
    "Hella":                 (63.8333, -20.4000),
    "Höfn":                  (64.2539, -15.2082),
    "Húsavík":               (66.0442, -17.3390),
    "Hveragerði":            (63.9915, -21.1844),
    "Ísafjörður":            (66.0750, -23.1351),
    "Keflavík":              (63.9850, -22.5607),
    "Kirkjubæjarklaustur":   (63.7850, -18.0597),
    "Landmannalaugar":       (63.9930, -19.0670),
    "Landeyjahöfn":          (63.6150, -20.2900),
    "Mývatn":                (65.5955, -17.0093),
    "Ólafsvík":              (64.8955, -23.7149),
    "Reykjavík":             (64.1355, -21.8954),
    "Sauðárkrókur":          (65.7453, -19.6389),
    "Selfoss":               (63.9330, -20.9978),
    "Seyðisfjörður":         (65.2580, -13.9990),
    "Siglufjörður":          (66.1520, -18.9063),
    "Skógarfoss":            (63.5320, -19.5120),
    "Stykkishólmur":         (65.0720, -22.7287),
    "Varmahlíð":             (65.5430, -19.4630),
    "Vík":                   (63.4187, -19.0054),
    "Vopnafjörður":          (65.7553, -14.8410),
    "Þingvellir":            (64.2559, -21.1295),
}

_OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"


def fetch_osrm_polyline(origin: str, destination: str) -> list[list[float]] | None:
    """
    Fetch real road geometry from the public OSRM demo server.
    Returns [[lat, lon], …] suitable for Leaflet, or None on any error.
    """
    o = CITY_COORDS.get(origin)
    d = CITY_COORDS.get(destination)
    if not o or not d:
        return None
    url = (
        f"{_OSRM_BASE}/{o[1]},{o[0]};{d[1]},{d[0]}"
        "?overview=full&geometries=geojson"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SameFare/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        coords = data["routes"][0]["geometry"]["coordinates"]
        return [[lat, lon] for lon, lat in coords]
    except Exception as exc:
        log.warning("OSRM fetch failed for %s→%s: %s", origin, destination, exc)
        return None

# ── Dense Ring Road waypoints ─────────────────────────────────────────────────
# Clockwise from Reykjavík, including intermediate waypoints between cities
# so the rendered path follows the road rather than cutting straight across.

_RING_SEQ: list[list[float]] = [
    [64.135, -21.895],   # 0  Reykjavík
    [64.215, -21.878],   # 1  (Hvalfjörður / Akranes approach)
    [64.537, -21.914],   # 2  Borgarnes
    [64.780, -21.750],   # 3  (Hvammstangi direction)
    [65.397, -20.948],   # 4  (Brú / Hrútafjörður)
    [65.662, -20.291],   # 5  Blönduós
    [65.573, -19.469],   # 6  (Varmahlíð)
    [65.820, -18.450],   # 7  (Öxnadalsheiðar pass — road goes north here)
    [65.683, -18.088],   # 8  Akureyri
    [65.600, -16.970],   # 9  Mývatn
    [65.370, -15.900],   # 10 (Möðrudalur — road dips south-east)
    [65.267, -14.395],   # 11 Egilsstaðir
    [64.790, -14.020],   # 12 (Djúpivogur / Breiðdalsvík)
    [64.657, -14.285],   # 13 (Berufjörður)
    [64.253, -15.207],   # 14 Höfn
    [64.048, -16.180],   # 15 (Skaftafell / Núpsstaður)
    [63.783, -18.055],   # 16 Kirkjubæjarklaustur
    [63.419, -18.998],   # 17 Vík
    [63.530, -19.500],   # 18 (Þórsmörk / Mýrdalssandur)
    [63.834, -20.387],   # 19 Hella
    [63.933, -20.998],   # 20 Selfoss
    [63.991, -21.184],   # 21 Hveragerði
    [64.023, -21.544],   # 22 (Hafnarfjörður / Mosfellsbær)
]
_RING_N = len(_RING_SEQ)

# Ring-road index for each main-ring city.
_RING_IDX: dict[str, int] = {
    "Reykjavík":           0,
    "Borgarnes":           2,
    "Blönduós":            5,
    "Varmahlíð":           6,
    "Akureyri":            8,
    "Mývatn":              9,
    "Egilsstaðir":        11,
    "Höfn":               14,
    "Kirkjubæjarklaustur": 16,
    "Vík":                17,
    "Hella":              19,
    "Selfoss":            20,
    "Hveragerði":         21,
}

# Branch cities: connect at a ring node index with road spur waypoints.
# 'ring_idx' — which ring node they connect to.
# 'exit'     — waypoints from the ring node TO the branch city (ring node excluded).
#              Reverse this list to get city → ring.
_BRANCH: dict[str, dict] = {
    "Keflavík": {
        "ring_idx": 0,
        "exit": [[64.000, -22.150], [63.985, -22.556]],
    },
    "Sauðárkrókur": {
        "ring_idx": 6,
        "exit": [[65.746, -19.639]],
    },
    "Húsavík": {
        "ring_idx": 7,
        "exit": [[65.870, -17.600], [66.042, -17.339]],
    },
    "Siglufjörður": {
        "ring_idx": 7,
        "exit": [[65.900, -18.500], [66.152, -18.910]],
    },
    "Stykkishólmur": {
        "ring_idx": 2,
        "exit": [[64.780, -21.750], [65.000, -22.350], [65.073, -22.726]],
    },
    "Ólafsvík": {
        "ring_idx": 2,
        "exit": [[64.780, -21.750], [65.000, -22.350], [65.073, -22.726], [64.894, -23.714]],
    },
    "Ísafjörður": {
        "ring_idx": 2,
        "exit": [[65.063, -21.784], [65.705, -21.690], [65.900, -22.500], [66.075, -23.137]],
    },
    "Seyðisfjörður": {
        "ring_idx": 9,
        "exit": [[65.258, -13.999]],
    },
    "Vopnafjörður": {
        "ring_idx": 9,
        "exit": [[65.747, -14.841]],
    },
    "Landeyjahöfn": {
        "ring_idx": 17,
        "exit": [[63.615, -20.290]],
    },
    "Skógarfoss": {
        "ring_idx": 15,
        "exit": [[63.532, -19.512]],
    },
    "Landmannalaugar": {
        "ring_idx": 17,
        "exit": [[63.993, -19.067]],
    },
    # Route 36 (Þingvallavegur) NE from the Reykjavík/Mosfellsbær area, over
    # Mosfellsheiði, down to Þingvellir (Golden Circle).
    "Þingvellir": {
        "ring_idx": 0,
        "exit": [[64.167, -21.690], [64.205, -21.380], [64.2559, -21.1295]],
    },
}


def ring_road_polyline(origin: str, destination: str) -> list[list[float]] | None:
    """
    Compute approximate ring-road waypoints between two Icelandic cities.

    Picks the shorter direction (CW vs CCW) around the Ring Road, then
    prepends / appends branch spur waypoints for off-ring cities.

    Returns a [[lat, lon], …] list suitable for Leaflet, or None if either
    city is not in the known network.
    """
    o_branch = _BRANCH.get(origin)
    d_branch = _BRANCH.get(destination)
    oi = _RING_IDX.get(origin,      o_branch["ring_idx"] if o_branch else None)
    di = _RING_IDX.get(destination, d_branch["ring_idx"] if d_branch else None)
    if oi is None or di is None:
        return None

    n = _RING_N

    # Clockwise path (includes both endpoints)
    cw: list[list[float]] = []
    i = oi
    while True:
        cw.append(_RING_SEQ[i])
        if i == di:
            break
        i = (i + 1) % n

    # Counter-clockwise path (includes both endpoints)
    ccw: list[list[float]] = []
    j = oi
    while True:
        ccw.append(_RING_SEQ[j])
        if j == di:
            break
        j = (j - 1 + n) % n

    path = cw if len(cw) <= len(ccw) else ccw

    # Prepend origin branch spur (reversed: city → ring node)
    if o_branch:
        path = list(reversed(o_branch["exit"])) + path

    # Append destination branch spur (ring node → city)
    if d_branch:
        path = path + d_branch["exit"]

    return path
