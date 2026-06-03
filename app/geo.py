"""
Icelandic road-network geometry helpers.

ring_road_polyline(origin, destination)
    Returns a [[lat, lon], …] waypoint list tracing the Ring Road (Route 1)
    between any two known Icelandic cities.  Used as a server-side fallback
    when no OSRM polyline is stored and as a seeding source for new routes.

    Mirrors the JS ringRoute() function in templates/trips/detail.html —
    keep both in sync when adding new cities.
"""

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
    [65.573, -19.469],   # 6  (Varmahlíð / Víðidalur)
    [65.683, -18.088],   # 7  Akureyri
    [65.600, -16.970],   # 8  Mývatn
    [65.267, -14.395],   # 9  Egilsstaðir
    [64.790, -14.020],   # 10 (Djúpivogur / Breiðdalsvík)
    [64.657, -14.285],   # 11 (Berufjörður)
    [64.253, -15.207],   # 12 Höfn
    [64.048, -16.180],   # 13 (Skaftafell / Núpsstaður)
    [63.783, -18.055],   # 14 Kirkjubæjarklaustur
    [63.419, -18.998],   # 15 Vík
    [63.530, -19.500],   # 16 (Þórsmörk / Mýrdalssandur)
    [63.834, -20.387],   # 17 Hella
    [63.933, -20.998],   # 18 Selfoss
    [63.991, -21.184],   # 19 Hveragerði
    [64.023, -21.544],   # 20 (Hafnarfjörður / Mosfellsbær)
]
_RING_N = len(_RING_SEQ)

# Ring-road index for each main-ring city.
_RING_IDX: dict[str, int] = {
    "Reykjavík":           0,
    "Borgarnes":           2,
    "Blönduós":            5,
    "Varmahlíð":           6,
    "Akureyri":            7,
    "Mývatn":              8,
    "Egilsstaðir":         9,
    "Höfn":               12,
    "Kirkjubæjarklaustur": 14,
    "Vík":                15,
    "Hella":              17,
    "Selfoss":            18,
    "Hveragerði":         19,
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
