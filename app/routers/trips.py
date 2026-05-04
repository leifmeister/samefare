import json
from datetime import datetime, date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload, selectinload

from app import models, email as mailer, sms as texter
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app.routers.alerts import notify_matching_alerts
from app.routers.payments import _issue_rapyd_refund
from app.estimator import estimate_trip_cost, route_lookup
from app.fuel import active_policy, get_cached_petrol_price
from app.utils import canonical_city, build_route_graph, is_on_route, shortest_path_km, prorate_segment_price, resolve_segment

settings = get_settings()

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/trips", tags=["trips"])

ALL_CAR_TYPES = [e.value for e in models.CarType]

# Booking statuses that occupy seats on the trip.
# awaiting_payment: instant-book or driver-approved, seat held, checkout pending.
# confirmed:        payment authorised (Case A) or MIT fired (Case B).
# card_saved:       Case B SCA-CIT completed, seat held, MIT fires 24 h before departure.
# All three must be counted when enforcing the seats_total floor on edits,
# computing seats_available, and showing the driver who holds a seat.
_SEAT_HOLDING_STATUSES = frozenset({
    models.BookingStatus.awaiting_payment,
    models.BookingStatus.confirmed,
    models.BookingStatus.card_saved,
})

ICELANDIC_CITIES = [
    # Every city here must have at least one row in the routes table so the
    # price-cap calculator can show a cost breakdown.
    # Excluded: suburban/satellite cities that wouldn't represent a real trip
    # (e.g. Kópavogur, Dalvík, Varmahlíð).
    "Akureyri", "Blönduós", "Borgarnes", "Egilsstaðir", "Hella",
    "Höfn", "Húsavík", "Hveragerði", "Ísafjörður", "Keflavík",
    "Kirkjubæjarklaustur", "Mývatn", "Ólafsvík", "Reykjavík",
    "Sauðárkrókur", "Selfoss", "Siglufjörður", "Stykkishólmur", "Vík",
]


ALL_FUEL_TYPES = [e.value for e in models.FuelType]


def _pricing_ctx(
    db: Session,
    origin: str = "",
    destination: str = "",
    seats_total: int = 3,
    car_type: str = "sedan",
    fuel_type: str | None = None,
) -> dict:
    """
    Build the pricing-related context passed to the create/edit templates.

    Reads only from the DB (cache + policy) — never makes an HTTP request,
    so it is safe to call on every GET.

    Returns a dict with:
      estimate       TripCostEstimate | None  — server-side estimate for known routes
      fuel_price     float                    — cached or fallback ISK/L
      fuel_tier      str                      — 'cached' or 'fallback'
      routes_js      str                      — JSON: {"Origin|Dest": km, ...}
      policy_js      str                      — JSON of policy constants for Alpine
      fuel_types     list[str]
    """
    policy = active_policy(db)
    fuel_price, fuel_tier = get_cached_petrol_price(db)

    # Build a distance map for the Alpine JS calculator.
    # Start from direct DB rows, then fill in any multi-hop city pairs so that
    # every combination of known cities produces a valid pricing estimate.
    all_routes = db.query(models.Route).filter(models.Route.is_active == True).all()  # noqa: E712
    routes_map: dict[str, float] = {f"{r.origin}|{r.destination}": float(r.distance_km) for r in all_routes}
    graph = build_route_graph(db)
    for o in ICELANDIC_CITIES:
        for d in ICELANDIC_CITIES:
            if o != d:
                key = f"{o}|{d}"
                if key not in routes_map:
                    km = shortest_path_km(graph, o, d)
                    if km is not None:
                        routes_map[key] = km
    routes_js = json.dumps(routes_map, ensure_ascii=False)

    # Policy constants for the Alpine cost calculator
    policy_js = "null"
    if policy:
        policy_js = json.dumps({
            "kilometragjald": policy.kilometragjald_standard,
            "c_small":        policy.consumption_small,
            "c_standard":     policy.consumption_standard,
            "c_suv":          policy.consumption_suv,
            "c_van":          policy.consumption_van,
            "ev_standard":    policy.ev_consumption_standard,
            "ev_suv":         policy.ev_consumption_suv,
            "elec_price":     policy.electricity_price_isk_per_kwh,
            "wear_tear":      policy.wear_and_tear_isk_per_km,
            "real_depr":      policy.real_depreciation_isk_per_km,
            "depr_factor":    policy.depreciation_factor,
            "cap":            policy.platform_cost_cap_isk_per_km,
            "rounding":       policy.rounding_unit,
        }, ensure_ascii=False)

    # Server-side estimate for pre-filling price when origin/destination are known
    estimate = None
    if policy and origin and destination:
        route = route_lookup(origin.strip(), destination.strip(), db)
        if route:
            estimate = estimate_trip_cost(
                distance_km     = float(route.distance_km),
                seats_total     = seats_total,
                car_type        = car_type or "sedan",
                fuel_type       = fuel_type,
                fuel_price      = fuel_price,
                fuel_price_tier = fuel_tier,
                policy          = policy,
            )

    return {
        "estimate":   estimate,
        "fuel_price": fuel_price,
        "fuel_tier":  fuel_tier,
        "routes_js":  routes_js,
        "policy_js":  policy_js,
        "fuel_types": ALL_FUEL_TYPES,
    }


def _sort_trips(trips: list, sort: Optional[str]) -> list:
    if sort == "price_asc":
        return sorted(trips, key=lambda t: t.price_per_seat)
    if sort == "price_desc":
        return sorted(trips, key=lambda t: t.price_per_seat, reverse=True)
    # default: soonest departure first
    return sorted(trips, key=lambda t: t.departure_datetime)


_VALID_FLEX = frozenset({"exact", "plus_minus_1", "this_week", "weekend"})

# Popular routes shown on empty-state and homepage — ordered by expected traffic
POPULAR_ROUTES = [
    ("Reykjavík",  "Akureyri"),
    ("Reykjavík",  "Selfoss"),
    ("Keflavík",   "Reykjavík"),
    ("Reykjavík",  "Borgarnes"),
    ("Reykjavík",  "Vík"),
    ("Akureyri",   "Húsavík"),
    ("Selfoss",    "Vík"),
    ("Borgarnes",  "Akureyri"),
]


# ── Segment (intermediate-stop) matching ─────────────────────────────────────

class SegmentedTrip:
    """
    Thin proxy around a Trip ORM object that exposes passenger-segment overrides
    for partial-route search results.

    All attributes not explicitly overridden are transparently delegated to the
    underlying Trip via ``__getattr__``, so Jinja2 templates work unchanged.

    Extra attributes added by this class:
      is_partial   – always True (callers only wrap trips that differ from direct)
      pickup_city  – the city where the passenger boards
      dropoff_city – the city where the passenger exits
      price_per_seat – prorated fare for the segment (overrides trip value)
    """

    def __init__(
        self,
        trip: models.Trip,
        pickup_city: str,
        dropoff_city: str,
        segment_price: int,
    ) -> None:
        object.__setattr__(self, "_trip",          trip)
        object.__setattr__(self, "pickup_city",    pickup_city)
        object.__setattr__(self, "dropoff_city",   dropoff_city)
        object.__setattr__(self, "_segment_price", segment_price)
        object.__setattr__(self, "is_partial",
            pickup_city != trip.origin or dropoff_city != trip.destination)

    # Override price for the prorated segment
    @property
    def price_per_seat(self) -> int:  # type: ignore[override]
        return object.__getattribute__(self, "_segment_price")

    # Delegate everything else to the real Trip
    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_trip"), name)


def _find_segment_trips(
    search_origin: str,
    search_dest: str,
    direct_ids: set[int],
    seats: Optional[int],
    range_start: Optional[datetime],
    range_end: Optional[datetime],
    db: Session,
) -> list[SegmentedTrip]:
    """
    Find trips where (search_origin → search_dest) is a partial segment of
    the driver's full route, using the seeded ``routes`` graph.

    Returns SegmentedTrip wrappers with prorated prices.
    Trips already in *direct_ids* are excluded to avoid duplicates.

    The algorithm:
      1. Build a city-distance graph from the ``routes`` table.
      2. Verify that the passenger's segment (search_origin → search_dest) is
         a known route so we can prorate the price.
      3. For every active upcoming trip not in direct_ids, check that both
         search_origin and search_dest lie on the trip's route AND that the
         passenger's origin appears *before* the destination (correct direction).
    """
    graph = build_route_graph(db)

    # We can only prorate if we know the segment's distance.
    # shortest_path_km() traverses the graph so Hveragerði→Vík works even
    # without a direct route-table row for that pair.
    seg_km = shortest_path_km(graph, search_origin, search_dest)
    if seg_km is None:
        return []

    # Candidate query — all active trips except direct matches
    q = (
        db.query(models.Trip)
        .options(
            joinedload(models.Trip.driver).joinedload(models.User.reviews_received),
            joinedload(models.Trip.driver).selectinload(models.User.trips),
        )
        .filter(
            models.Trip.status == models.TripStatus.active,
            models.Trip.departure_datetime >= datetime.utcnow(),
            models.Trip.seats_available > 0,
        )
    )
    if direct_ids:
        q = q.filter(~models.Trip.id.in_(direct_ids))
    if seats:
        q = q.filter(models.Trip.seats_available >= seats)
    if range_start and range_end:
        q = q.filter(
            models.Trip.departure_datetime >= range_start,
            models.Trip.departure_datetime <= range_end,
        )

    results: list[SegmentedTrip] = []

    for trip in q.all():
        total_km = shortest_path_km(graph, trip.origin, trip.destination)
        if total_km is None:
            continue  # unknown route — can't validate or prorate

        # Both search cities must lie on this trip's route
        if not is_on_route(graph, trip.origin, trip.destination, search_origin):
            continue
        if not is_on_route(graph, trip.origin, trip.destination, search_dest):
            continue

        # Verify ordering: search_origin must appear *before* search_dest along the trip
        d_to_pickup = (
            0.0 if trip.origin == search_origin
            else shortest_path_km(graph, trip.origin, search_origin)
        )
        if d_to_pickup is None:
            continue

        d_to_dropoff = (
            total_km if trip.destination == search_dest
            else shortest_path_km(graph, trip.origin, search_dest)
        )
        if d_to_dropoff is None:
            continue

        if d_to_pickup >= d_to_dropoff:
            continue  # wrong direction — passenger's origin is at or past the exit

        segment_price = prorate_segment_price(trip.price_per_seat, seg_km, total_km)
        if segment_price is None:
            continue
        results.append(SegmentedTrip(trip, search_origin, search_dest, segment_price))

    return results


def _flex_date_range(
    parsed_date: Optional[date],
    date_flex: str,
) -> tuple[Optional[datetime], Optional[datetime], str]:
    """
    Compute (range_start, range_end, display_label) for a flex search.

    - exact:         one calendar day (same as before)
    - plus_minus_1:  selected date minus one day through selected date plus one day
    - this_week:     Mon → Sun of the current ISO week
    - weekend:       upcoming Saturday + Sunday

    Returns (None, None, "") when no date constraint is active.
    The label is shown to users only for multi-day ranges.
    """
    today = date.today()

    def _lbl(d: date) -> str:
        # "Thu May 2"  (no leading zero on day)
        return d.strftime("%a %b %-d")

    if date_flex == "plus_minus_1" and parsed_date:
        d_from = parsed_date - timedelta(days=1)
        d_to   = parsed_date + timedelta(days=1)
        return (
            datetime(d_from.year, d_from.month, d_from.day, 0, 0),
            datetime(d_to.year,   d_to.month,   d_to.day,  23, 59),
            f"Showing rides from {_lbl(d_from)} to {_lbl(d_to)}",
        )

    if date_flex == "this_week":
        # ISO week: Monday → Sunday
        dow        = today.weekday()          # Mon=0 … Sun=6
        week_start = today - timedelta(days=dow)
        week_end   = week_start + timedelta(days=6)
        return (
            datetime(week_start.year, week_start.month, week_start.day, 0, 0),
            datetime(week_end.year,   week_end.month,   week_end.day,   23, 59),
            f"Showing rides this week ({_lbl(week_start)} – {_lbl(week_end)})",
        )

    if date_flex == "weekend":
        # Upcoming Saturday; if today is already Saturday, use today
        dow        = today.weekday()          # Mon=0, Sat=5, Sun=6
        days_to_sat = (5 - dow) % 7
        sat = today + timedelta(days=days_to_sat)
        sun = sat + timedelta(days=1)
        return (
            datetime(sat.year, sat.month, sat.day, 0, 0),
            datetime(sun.year, sun.month, sun.day, 23, 59),
            f"Showing rides this weekend ({_lbl(sat)} – {_lbl(sun)})",
        )

    # exact (or any unknown flex) — single calendar day
    if parsed_date:
        return (
            datetime(parsed_date.year, parsed_date.month, parsed_date.day, 0, 0),
            datetime(parsed_date.year, parsed_date.month, parsed_date.day, 23, 59),
            "",
        )

    return None, None, ""


# ── List / Search ─────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def trips_list(
    request: Request,
    ctx: dict = Depends(get_template_context),
    db: Session = Depends(get_db),
    origin:      Optional[str] = None,
    destination: Optional[str] = None,
    travel_date: Optional[str] = None,
    date_flex:   Optional[str] = None,
    seats:       Optional[int] = None,
    sort:        Optional[str] = None,
    alert_saved: Optional[str] = None,
    alert_error: Optional[str] = None,
):
    # Sanitise date_flex — accept only known values, default to "exact"
    date_flex = date_flex if date_flex in _VALID_FLEX else "exact"

    # Parse travel_date safely — browser sends "" when the field is left blank
    parsed_date: Optional[date] = None
    if travel_date:
        try:
            parsed_date = date.fromisoformat(travel_date)
        except ValueError:
            travel_date = ""

    # Normalise input — map 'Reykjavik' → 'Reykjavík', 'akureyri' → 'Akureyri', etc.
    origin      = canonical_city(origin)      if origin      else origin
    destination = canonical_city(destination) if destination else destination

    # Reject same-city searches immediately — no DB query needed
    if origin and destination and origin.strip().lower() == destination.strip().lower():
        ctx_extra = {
            "trips": [], "origin": origin or "", "destination": destination or "",
            "travel_date": travel_date or "", "date_flex": date_flex,
            "date_range_label": "", "seats": seats or 1,
            "sort": sort or "soonest", "active_filters": 0,
            "cities": ICELANDIC_CITIES,
            "alert_saved": False, "alert_error": False,
            "same_city_error": True,
            "popular_routes": POPULAR_ROUTES,
            "travel_date_display": "",
        }
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse("trips/_list_partial.html", {**ctx, **ctx_extra})
        return templates.TemplateResponse("trips/list.html", {**ctx, **ctx_extra})

    query = (
        db.query(models.Trip)
        .options(
            joinedload(models.Trip.driver).joinedload(models.User.reviews_received),
            joinedload(models.Trip.driver).selectinload(models.User.trips),
        )
        .filter(
            models.Trip.status == models.TripStatus.active,
            models.Trip.departure_datetime >= datetime.utcnow(),
            models.Trip.seats_available > 0,
        )
    )

    if origin:
        query = query.filter(models.Trip.origin.ilike(origin))
    if destination:
        query = query.filter(models.Trip.destination.ilike(destination))

    # Apply date range filter (respects flex mode)
    range_start, range_end, date_range_label = _flex_date_range(parsed_date, date_flex)
    if range_start and range_end:
        query = query.filter(
            models.Trip.departure_datetime >= range_start,
            models.Trip.departure_datetime <= range_end,
        )

    if seats:
        query = query.filter(models.Trip.seats_available >= seats)

    direct_trips = query.all()
    direct_ids   = {t.id for t in direct_trips}

    # Expand results with partial-route (intermediate-stop) matches when both
    # endpoints are supplied — skip for open-ended or same-city searches.
    segment_trips: list[SegmentedTrip] = []
    if origin and destination:
        segment_trips = _find_segment_trips(
            search_origin=origin,
            search_dest=destination,
            direct_ids=direct_ids,
            seats=seats,
            range_start=range_start,
            range_end=range_end,
            db=db,
        )

    trips = _sort_trips(direct_trips + segment_trips, sort)

    # A date is "active" when a specific date is set OR a non-exact flex mode is active
    date_active = bool(travel_date) or date_flex in ("this_week", "weekend")
    # Seats=1 is the default — only count it as an active filter when >1
    seats_active = bool(seats) and seats > 1
    active_filters = sum([bool(origin), bool(destination), date_active, seats_active])

    ctx_extra = {
        "trips": trips,
        "origin": origin or "",
        "destination": destination or "",
        "travel_date": parsed_date.isoformat() if parsed_date else "",
        "date_flex": date_flex,
        "date_range_label": date_range_label,
        "seats": seats or 1,
        "sort": sort or "soonest",
        "active_filters": active_filters,
        "cities": ICELANDIC_CITIES,
        "alert_saved": bool(alert_saved),
        "alert_error": bool(alert_error),
        "same_city_error": False,
        "popular_routes": POPULAR_ROUTES,
        "travel_date_display": parsed_date.strftime("%-d %b") if parsed_date else "",
    }

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("trips/_list_partial.html", {**ctx, **ctx_extra})
    return templates.TemplateResponse("trips/list.html", {**ctx, **ctx_extra})


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
def new_trip_page(
    request:   Request,
    ctx:       dict         = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:        Session      = Depends(get_db),
    return_of: Optional[int] = Query(None),
):
    if current_user.license_verification != models.VerificationStatus.approved:
        return RedirectResponse("/verify?next=driver", status_code=303)

    # Default vals and car details from user profile
    vals = {
        "origin": "", "destination": "",
        "departure_date": "", "departure_time": "09:00",
        "seats_total": 3, "price_per_seat": "",
        "pickup_address": "", "dropoff_address": "",
        "allows_luggage": True, "large_luggage": False,
        "allows_pets": False, "smoking": False,
        "chattiness": None,
        "winter_ready": False, "child_seat": False, "flexible_pickup": False,
        "instant_book": True, "description": "",
    }
    defaults = {
        "car_make":  current_user.default_car_make  or "",
        "car_model": current_user.default_car_model or "",
        "car_year":  current_user.default_car_year  or "",
        "car_type":  str(current_user.default_car_type) if current_user.default_car_type else "sedan",
    }
    return_banner = None

    # Pre-fill for return trip
    if return_of:
        source = db.query(models.Trip).filter(
            models.Trip.id == return_of,
            models.Trip.driver_id == current_user.id,
        ).first()
        if source:
            vals["origin"]        = source.destination
            vals["destination"]   = source.origin
            vals["seats_total"]   = source.seats_total
            vals["price_per_seat"] = source.price_per_seat
            vals["allows_luggage"]  = source.allows_luggage
            vals["large_luggage"]   = source.large_luggage
            vals["allows_pets"]     = source.allows_pets
            vals["smoking"]         = source.smoking
            vals["chattiness"]      = source.chattiness
            vals["winter_ready"]    = source.winter_ready
            vals["child_seat"]      = source.child_seat
            vals["flexible_pickup"] = source.flexible_pickup
            vals["instant_book"]    = source.instant_book
            defaults = {
                "car_make":  source.car_make  or "",
                "car_model": source.car_model or "",
                "car_year":  source.car_year  or "",
                "car_type":  str(source.car_type) if source.car_type else "sedan",
            }
            return_banner = f"{source.origin} → {source.destination}"

    pricing = _pricing_ctx(
        db,
        origin      = vals.get("origin", ""),
        destination = vals.get("destination", ""),
        seats_total = vals.get("seats_total", 3),
        car_type    = defaults.get("car_type", "sedan"),
    )
    # Pre-fill price at the cap when creating a return trip (if origin/dest known)
    if pricing["estimate"] and not vals.get("price_per_seat"):
        vals["price_per_seat"] = pricing["estimate"].price_per_seat_cap

    return templates.TemplateResponse("trips/create.html", {
        **ctx,
        "car_types":     ALL_CAR_TYPES,
        "cities":        ICELANDIC_CITIES,
        "error":         None,
        "defaults":      defaults,
        "vals":          vals,
        "return_banner": return_banner,
        **pricing,
    })


@router.post("/new", response_class=HTMLResponse)
def create_trip(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),

    origin:             str   = Form(...),
    destination:        str   = Form(...),
    departure_date:     date  = Form(...),
    departure_time:     str   = Form(...),
    seats_total:        int   = Form(...),
    price_per_seat:     int   = Form(...),
    car_make:           str   = Form(""),
    car_model:          str   = Form(""),
    car_year:           str   = Form(""),
    car_type:           str   = Form("sedan"),
    fuel_type_raw:      Optional[str] = Form(None),
    description:        str   = Form(""),
    pickup_address:     str   = Form(""),
    dropoff_address:    str   = Form(""),
    # Checkboxes: unchecked sends nothing — use Optional[str] and convert
    allows_luggage_raw:  Optional[str] = Form(None),
    large_luggage_raw:   Optional[str] = Form(None),
    allows_pets_raw:     Optional[str] = Form(None),
    smoking_raw:         Optional[str] = Form(None),
    chattiness:          Optional[str] = Form(None),  # 'quiet', 'chatty', or '' / None
    winter_ready_raw:    Optional[str] = Form(None),
    child_seat_raw:      Optional[str] = Form(None),
    flexible_pickup_raw: Optional[str] = Form(None),
    instant_book_raw:    Optional[str] = Form(None),
):
    if current_user.license_verification != models.VerificationStatus.approved:
        return RedirectResponse("/verify?next=driver", status_code=303)

    allows_luggage  = allows_luggage_raw  is not None
    large_luggage   = large_luggage_raw   is not None
    allows_pets     = allows_pets_raw     is not None
    smoking         = smoking_raw         is not None
    chattiness_val  = chattiness if chattiness in ("quiet", "chatty") else None
    winter_ready    = winter_ready_raw    is not None
    child_seat      = child_seat_raw      is not None
    flexible_pickup = flexible_pickup_raw is not None
    instant_book    = instant_book_raw    is not None

    # Resolve fuel type (None is fine — estimator infers from car_type)
    try:
        fuel_type_val = models.FuelType(fuel_type_raw) if fuel_type_raw else None
    except ValueError:
        fuel_type_val = None

    pricing = _pricing_ctx(db, origin, destination, seats_total, car_type,
                           str(fuel_type_val) if fuel_type_val else None)

    err_ctx = {
        **ctx,
        "car_types": ALL_CAR_TYPES,
        "cities":    ICELANDIC_CITIES,
        "defaults": {
            "car_make":  car_make,
            "car_model": car_model,
            "car_year":  car_year,
            "car_type":  car_type,
        },
        "vals": {
            "origin":         origin,
            "destination":    destination,
            "departure_date": str(departure_date) if departure_date else "",
            "departure_time": departure_time,
            "seats_total":    seats_total,
            "price_per_seat": price_per_seat,
            "pickup_address":  pickup_address,
            "dropoff_address": dropoff_address,
            "allows_luggage":  allows_luggage,
            "large_luggage":   large_luggage,
            "allows_pets":     allows_pets,
            "smoking":         smoking,
            "chattiness":      chattiness_val,
            "winter_ready":    winter_ready,
            "child_seat":      child_seat,
            "flexible_pickup": flexible_pickup,
            "instant_book":    instant_book,
            "description":     description,
            "fuel_type":       str(fuel_type_val) if fuel_type_val else "",
        },
        **pricing,
    }

    # Parse departure datetime
    try:
        hour, minute = map(int, departure_time.split(":"))
        departure_dt = datetime(departure_date.year, departure_date.month,
                                departure_date.day, hour, minute)
    except (ValueError, AttributeError):
        return templates.TemplateResponse("trips/create.html",
            {**err_ctx, "error": "Invalid date or time."}, status_code=400)

    if departure_dt <= datetime.utcnow():
        return templates.TemplateResponse("trips/create.html",
            {**err_ctx, "error": "Departure must be in the future."}, status_code=400)

    if origin.strip().lower() == destination.strip().lower():
        return templates.TemplateResponse("trips/create.html",
            {**err_ctx, "error": "Origin and destination cannot be the same."}, status_code=400)

    if not (1 <= seats_total <= 8):
        return templates.TemplateResponse("trips/create.html",
            {**err_ctx, "error": "Seats must be between 1 and 8."}, status_code=400)

    if price_per_seat < 1:
        return templates.TemplateResponse("trips/create.html",
            {**err_ctx, "error": "Price per seat must be at least 1 ISK."}, status_code=400)

    # Enforce the cost-sharing cap — required for ALL routes.
    # If no route row exists the distance is unknown, which means no cap can be
    # computed and the core rule (drivers cover costs, not profit) cannot be
    # enforced.  Block rather than allow uncapped pricing.
    estimate = pricing.get("estimate")
    if estimate is None:
        return templates.TemplateResponse("trips/create.html",
            {**err_ctx, "error": (
                f"We don't have distance data for {origin} → {destination}. "
                "Please choose both cities from the suggested list. "
                "If you think this route should be supported, let us know."
            )}, status_code=400)

    price_snapshot_json = estimate.to_json()
    if price_per_seat > estimate.price_per_seat_cap:
        return templates.TemplateResponse("trips/create.html",
            {**err_ctx, "error": (
                f"Price per seat cannot exceed {estimate.price_per_seat_cap:,} ISK "
                f"for this route ({estimate.distance_km:.0f} km). "
                "This cap ensures passengers only cover their share of trip costs."
            )}, status_code=400)

    trip = models.Trip(
        driver_id=current_user.id,
        origin=origin.strip(),
        destination=destination.strip(),
        departure_datetime=departure_dt,
        seats_total=seats_total,
        seats_available=seats_total,
        price_per_seat=price_per_seat,
        car_make=car_make or None,
        car_model=car_model or None,
        car_year=int(car_year) if car_year.isdigit() else None,
        car_type=car_type,
        fuel_type=fuel_type_val,
        price_snapshot=price_snapshot_json,
        description=description or None,
        pickup_address=pickup_address or None,
        dropoff_address=dropoff_address or None,
        allows_luggage=allows_luggage,
        large_luggage=large_luggage,
        allows_pets=allows_pets,
        smoking=smoking,
        chattiness=chattiness_val,
        winter_ready=winter_ready,
        child_seat=child_seat,
        flexible_pickup=flexible_pickup,
        instant_book=instant_book,
    )
    db.add(trip)

    # Save car details back to the user's profile so next ride is pre-filled
    if car_make:
        current_user.default_car_make = car_make
    if car_model:
        current_user.default_car_model = car_model
    if car_year and car_year.isdigit():
        current_user.default_car_year = int(car_year)
    try:
        current_user.default_car_type = models.CarType(car_type)
    except ValueError:
        pass

    db.commit()
    db.refresh(trip)

    # Notify any passengers who set up an alert for this route
    notify_matching_alerts(db, trip)

    return RedirectResponse(f"/trips/{trip.id}?posted=1", status_code=303)


@router.get("/{trip_id}/edit", response_class=HTMLResponse)
def edit_trip_page(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip or trip.driver_id != current_user.id:
        return RedirectResponse("/profile", status_code=303)
    if trip.status != models.TripStatus.active:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    pricing = _pricing_ctx(
        db,
        origin      = trip.origin,
        destination = trip.destination,
        seats_total = trip.seats_total,
        car_type    = str(trip.car_type) if trip.car_type else "sedan",
        fuel_type   = str(trip.fuel_type) if trip.fuel_type else None,
    )
    return templates.TemplateResponse("trips/edit.html", {
        **ctx,
        "trip":      trip,
        "car_types": ALL_CAR_TYPES,
        "cities":    ICELANDIC_CITIES,
        "error":     None,
        "vals": {
            "origin":         trip.origin,
            "destination":    trip.destination,
            "departure_date": trip.departure_datetime.date().isoformat(),
            "departure_time": trip.departure_datetime.strftime("%H:%M"),
            "seats_total":    trip.seats_total,
            "price_per_seat": trip.price_per_seat,
            "pickup_address":  trip.pickup_address  or "",
            "dropoff_address": trip.dropoff_address or "",
            "allows_luggage":  trip.allows_luggage,
            "large_luggage":   trip.large_luggage,
            "allows_pets":     trip.allows_pets,
            "smoking":         trip.smoking,
            "chattiness":      trip.chattiness,
            "winter_ready":    trip.winter_ready,
            "child_seat":      trip.child_seat,
            "flexible_pickup": trip.flexible_pickup,
            "instant_book":    trip.instant_book,
            "description":     trip.description or "",
            "fuel_type":       str(trip.fuel_type) if trip.fuel_type else "",
        },
        "defaults": {
            "car_make":  trip.car_make  or "",
            "car_model": trip.car_model or "",
            "car_year":  trip.car_year  or "",
            "car_type":  str(trip.car_type) if trip.car_type else "sedan",
        },
        **pricing,
    })


@router.post("/{trip_id}/edit", response_class=HTMLResponse)
def update_trip(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),

    origin:          str  = Form(...),
    destination:     str  = Form(...),
    departure_date:  date = Form(...),
    departure_time:  str  = Form(...),
    seats_total:     int  = Form(...),
    price_per_seat:  int  = Form(...),
    car_make:        str  = Form(""),
    car_model:       str  = Form(""),
    car_year:        str  = Form(""),
    car_type:        str  = Form("sedan"),
    fuel_type_raw:   Optional[str] = Form(None),
    description:     str  = Form(""),
    pickup_address:  str  = Form(""),
    dropoff_address: str  = Form(""),
    allows_luggage_raw:  Optional[str] = Form(None),
    large_luggage_raw:   Optional[str] = Form(None),
    allows_pets_raw:     Optional[str] = Form(None),
    smoking_raw:         Optional[str] = Form(None),
    chattiness:          Optional[str] = Form(None),  # 'quiet', 'chatty', or '' / None
    winter_ready_raw:    Optional[str] = Form(None),
    child_seat_raw:      Optional[str] = Form(None),
    flexible_pickup_raw: Optional[str] = Form(None),
    instant_book_raw:    Optional[str] = Form(None),
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip or trip.driver_id != current_user.id:
        return RedirectResponse("/profile", status_code=303)
    if trip.status != models.TripStatus.active:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    allows_luggage  = allows_luggage_raw  is not None
    large_luggage   = large_luggage_raw   is not None
    allows_pets     = allows_pets_raw     is not None
    smoking         = smoking_raw         is not None
    chattiness_val  = chattiness if chattiness in ("quiet", "chatty") else None
    winter_ready    = winter_ready_raw    is not None
    child_seat      = child_seat_raw      is not None
    flexible_pickup = flexible_pickup_raw is not None
    instant_book    = instant_book_raw    is not None

    try:
        fuel_type_val = models.FuelType(fuel_type_raw) if fuel_type_raw else None
    except ValueError:
        fuel_type_val = None

    pricing = _pricing_ctx(db, origin, destination, seats_total, car_type,
                           str(fuel_type_val) if fuel_type_val else None)

    err_ctx = {
        **ctx,
        "trip":      trip,
        "car_types": ALL_CAR_TYPES,
        "cities":    ICELANDIC_CITIES,
        "defaults": {
            "car_make": car_make, "car_model": car_model,
            "car_year": car_year, "car_type":  car_type,
        },
        "vals": {
            "origin": origin, "destination": destination,
            "departure_date": str(departure_date) if departure_date else "",
            "departure_time": departure_time,
            "seats_total": seats_total, "price_per_seat": price_per_seat,
            "pickup_address": pickup_address, "dropoff_address": dropoff_address,
            "allows_luggage": allows_luggage, "large_luggage":   large_luggage,
            "allows_pets":    allows_pets,    "smoking":         smoking,
            "chattiness":     chattiness_val,
            "winter_ready":   winter_ready,   "child_seat":      child_seat,
            "flexible_pickup": flexible_pickup, "instant_book":  instant_book,
            "description": description,
            "fuel_type":   str(fuel_type_val) if fuel_type_val else "",
        },
        **pricing,
    }

    try:
        hour, minute = map(int, departure_time.split(":"))
        departure_dt = datetime(departure_date.year, departure_date.month,
                                departure_date.day, hour, minute)
    except (ValueError, AttributeError):
        return templates.TemplateResponse("trips/edit.html",
            {**err_ctx, "error": "Invalid date or time."}, status_code=400)

    if departure_dt <= datetime.utcnow():
        return templates.TemplateResponse("trips/edit.html",
            {**err_ctx, "error": "Departure must be in the future."}, status_code=400)

    if origin.strip().lower() == destination.strip().lower():
        return templates.TemplateResponse("trips/edit.html",
            {**err_ctx, "error": "Origin and destination cannot be the same."}, status_code=400)

    if not (1 <= seats_total <= 8):
        return templates.TemplateResponse("trips/edit.html",
            {**err_ctx, "error": "Seats must be between 1 and 8."}, status_code=400)

    if price_per_seat < 1:
        return templates.TemplateResponse("trips/edit.html",
            {**err_ctx, "error": "Price per seat must be at least 1 ISK."}, status_code=400)

    # Enforce the cost-sharing cap — required for ALL routes.
    # Same rule as create: if no route row exists, block rather than allow
    # uncapped pricing.
    estimate = pricing.get("estimate")
    if estimate is None:
        return templates.TemplateResponse("trips/edit.html",
            {**err_ctx, "error": (
                f"We don't have distance data for {origin} → {destination}. "
                "Please choose both cities from the suggested list. "
                "If you think this route should be supported, let us know."
            )}, status_code=400)

    price_snapshot_json = estimate.to_json()
    if price_per_seat > estimate.price_per_seat_cap:
        return templates.TemplateResponse("trips/edit.html",
            {**err_ctx, "error": (
                f"Price per seat cannot exceed {estimate.price_per_seat_cap:,} ISK "
                f"for this route ({estimate.distance_km:.0f} km). "
                "This cap ensures passengers only cover their share of trip costs."
            )}, status_code=400)

    # seats_total can't drop below seats that are already held.
    # awaiting_payment, confirmed, and card_saved all hold seats; pending does
    # not (no seats are deducted until the driver approves the request).
    held_seats = sum(
        b.seats_booked for b in trip.bookings
        if b.status in _SEAT_HOLDING_STATUSES
    )
    if seats_total < held_seats:
        return templates.TemplateResponse("trips/edit.html",
            {**err_ctx, "error": f"Can't reduce seats below {held_seats} — {held_seats} seat(s) are already reserved."}, status_code=400)

    trip.origin             = origin.strip()
    trip.destination        = destination.strip()
    trip.departure_datetime = departure_dt
    trip.seats_available    = seats_total - held_seats
    trip.seats_total        = seats_total
    trip.price_per_seat     = price_per_seat
    trip.car_make           = car_make  or None
    trip.car_model          = car_model or None
    trip.car_year           = int(car_year) if car_year.isdigit() else None
    trip.car_type           = car_type
    trip.fuel_type          = fuel_type_val
    trip.price_snapshot     = price_snapshot_json
    trip.description        = description     or None
    trip.pickup_address     = pickup_address  or None
    trip.dropoff_address    = dropoff_address or None
    trip.allows_luggage     = allows_luggage
    trip.large_luggage      = large_luggage
    trip.allows_pets        = allows_pets
    trip.smoking            = smoking
    trip.chattiness         = chattiness_val
    trip.winter_ready       = winter_ready
    trip.child_seat         = child_seat
    trip.flexible_pickup    = flexible_pickup
    trip.instant_book       = instant_book

    db.commit()
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


@router.get("/{trip_id}", response_class=HTMLResponse)
def trip_detail(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    db: Session = Depends(get_db),
    pickup:  Optional[str] = None,   # segment: passenger's boarding city
    dropoff: Optional[str] = None,   # segment: passenger's exit city
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        return templates.TemplateResponse("errors/404.html", {**ctx}, status_code=404)

    # All bookings that hold a seat — used for the passenger list on the detail page.
    # awaiting_payment: instant-book / approved, seat deducted, checkout not yet done.
    # card_saved:       card on file, MIT charge fires 24 h before departure.
    # confirmed:        fully paid.
    # completed:        trip done.
    confirmed_bookings = [b for b in trip.bookings
                          if b.status in (models.BookingStatus.awaiting_payment,
                                          models.BookingStatus.card_saved,
                                          models.BookingStatus.confirmed,
                                          models.BookingStatus.completed)]
    pending_bookings = [b for b in trip.bookings
                        if b.status == models.BookingStatus.pending]

    # Does an active reverse-direction trip already exist for this driver?
    # Used to suppress "Post return trip" banner when the return is already posted.
    has_return_trip = db.query(models.Trip).filter(
        models.Trip.driver_id   == trip.driver_id,
        models.Trip.origin      == trip.destination,
        models.Trip.destination == trip.origin,
        models.Trip.status      == models.TripStatus.active,
        models.Trip.id          != trip.id,
    ).first() is not None

    # Driver trip summary — only computed for the driver viewing their own completed trip.
    driver_summary = None
    current_user   = ctx.get("current_user")
    if (current_user
            and current_user.id == trip.driver_id
            and trip.status == models.TripStatus.completed):

        bkgs_done      = [b for b in trip.bookings if b.status == models.BookingStatus.completed]
        bkgs_cancelled = [b for b in trip.bookings if b.status == models.BookingStatus.cancelled]
        bkgs_no_show   = [b for b in trip.bookings if b.status == models.BookingStatus.no_show]

        seats_filled = sum(b.seats_booked for b in bkgs_done)
        gross        = sum(b.subtotal      for b in bkgs_done)
        payout       = sum(b.payment.driver_payout for b in bkgs_done if b.payment)

        reviews_given = db.query(models.Review).filter(
            models.Review.trip_id     == trip.id,
            models.Review.reviewer_id == current_user.id,
            models.Review.review_type == models.ReviewType.driver_to_passenger,
        ).count()

        # First completed booking the driver hasn't reviewed yet (for the CTA link)
        reviewed_ids = {
            r.booking_id for r in db.query(models.Review).filter(
                models.Review.trip_id     == trip.id,
                models.Review.reviewer_id == current_user.id,
                models.Review.review_type == models.ReviewType.driver_to_passenger,
            ).all()
        }
        first_unreviewed = next(
            (b.id for b in bkgs_done if b.id not in reviewed_ids), None
        )

        driver_summary = {
            "completed_count":  len(bkgs_done),
            "cancelled_count":  len(bkgs_cancelled),
            "no_show_count":    len(bkgs_no_show),
            "seats_filled":     seats_filled,
            "seats_total":      trip.seats_total,
            "gross":            gross,
            "payout":           payout,
            "reviews_given":    reviews_given,
            "reviews_total":    len(bkgs_done),
            "first_unreviewed": first_unreviewed,
        }

    # Build JSON-LD structured data server-side so user-supplied strings
    # (origin, destination, driver name) are serialised by json.dumps and
    # cannot break out of the <script> tag.  Replace "</" with "<\/" to
    # neutralise any "</script>" sequence that json.dumps would otherwise
    # leave unescaped (the escaped form is valid JSON and parses identically).
    _ld = {
        "@context": "https://schema.org",
        "@type":    "Event",
        "name":     f"Rideshare: {trip.origin} → {trip.destination}",
        "startDate": trip.departure_datetime.strftime("%Y-%m-%dT%H:%M"),
        "eventStatus":          "https://schema.org/EventScheduled",
        "eventAttendanceMode":  "https://schema.org/OfflineEventAttendanceMode",
        "location": {
            "@type": "Place",
            "name":  f"{trip.origin}, Iceland",
        },
        "organizer": {
            "@type": "Person",
            "name":  trip.driver.full_name,
        },
        "offers": {
            "@type":        "Offer",
            "price":        str(trip.price_per_seat),
            "priceCurrency": "ISK",
            "availability": (
                "https://schema.org/InStock"
                if trip.seats_available > 0
                else "https://schema.org/SoldOut"
            ),
            "url": str(request.url.scheme) + "://" + str(request.url.netloc) + f"/trips/{trip.id}",
        },
        "url": str(request.url.scheme) + "://" + str(request.url.netloc) + f"/trips/{trip.id}",
    }
    structured_data = json.dumps(_ld, ensure_ascii=False).replace("</", "<\\/")

    # Look up an active booking this passenger already holds for the trip so
    # the template can render a status-aware CTA instead of a duplicate Book button.
    # Only relevant for logged-in non-drivers; skip the query otherwise.
    current_user_booking = None
    if current_user and current_user.id != trip.driver_id:
        current_user_booking = (
            db.query(models.Booking)
            .filter(
                models.Booking.trip_id      == trip.id,
                models.Booking.passenger_id == current_user.id,
                models.Booking.status.in_([
                    models.BookingStatus.pending,
                    models.BookingStatus.awaiting_payment,
                    models.BookingStatus.card_saved,
                    models.BookingStatus.confirmed,
                ]),
            )
            .first()
        )

    # Validate and price the segment using the same logic as the booking flow.
    # Any invalid or out-of-route segment is silently dropped so the page
    # falls back to full-trip context rather than showing a contradictory price.
    segment_pickup, segment_dropoff, segment_price, _ = resolve_segment(
        build_route_graph(db), trip, pickup or "", dropoff or ""
    )

    return templates.TemplateResponse("trips/detail.html", {
        **ctx,
        "trip": trip,
        "confirmed_bookings": confirmed_bookings,
        "pending_bookings": pending_bookings,
        "has_return_trip": has_return_trip,
        "driver_summary": driver_summary,
        "current_user_booking": current_user_booking,
        "structured_data": structured_data,
        "segment_pickup":  segment_pickup,
        "segment_dropoff": segment_dropoff,
        "segment_price":   segment_price,
    })


@router.get("/{trip_id}/cancel", response_class=HTMLResponse)
def cancel_trip_page(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip or trip.driver_id != current_user.id or trip.status != models.TripStatus.active:
        return RedirectResponse("/my-trips?tab=rides", status_code=303)

    affected = [
        b for b in trip.bookings
        if b.status in (models.BookingStatus.pending, models.BookingStatus.confirmed,
                        models.BookingStatus.awaiting_payment,
                        models.BookingStatus.card_saved)
    ]
    return templates.TemplateResponse("trips/cancel_confirm.html", {
        **ctx,
        "trip":              trip,
        "affected_bookings": affected,
    })


@router.post("/{trip_id}/cancel")
def cancel_trip(
    trip_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip or trip.driver_id != current_user.id or trip.status != models.TripStatus.active:
        return RedirectResponse("/my-trips?tab=rides", status_code=303)

    trip.status = models.TripStatus.cancelled
    affected = []
    for b in trip.bookings:
        if b.status not in (models.BookingStatus.pending, models.BookingStatus.confirmed,
                            models.BookingStatus.awaiting_payment,
                            models.BookingStatus.card_saved):
            continue
        # Capture before mutating so we can branch on the original state below.
        was_card_saved = b.status == models.BookingStatus.card_saved
        b.status = models.BookingStatus.cancelled
        if b.payment:
            if was_card_saved:
                # Card tokenized for MIT but never charged — nothing to refund.
                b.payment.status = models.PaymentStatus.failed
            else:
                # Full refund for driver-initiated cancellations (including service fee).
                # _issue_rapyd_refund owns refund_amount and status — do not pre-set them.
                _issue_rapyd_refund(
                    db, b, b.payment.passenger_total, reason="driver_cancelled"
                )
        affected.append(b)

    db.commit()
    for b in affected:
        mailer.trip_cancelled_to_passenger(b)
        texter.trip_cancelled_to_passenger(b)

    return RedirectResponse("/my-trips?tab=rides&cancelled=1", status_code=303)


@router.post("/{trip_id}/complete-beta")
def complete_trip_beta(
    trip_id:      int,
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    """Beta-only: instantly mark a trip and its confirmed bookings as completed."""
    if not settings.beta_mode:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if trip and trip.driver_id == current_user.id and trip.status == models.TripStatus.active:
        trip.status = models.TripStatus.completed
        for b in trip.bookings:
            if b.status == models.BookingStatus.confirmed:
                b.status = models.BookingStatus.completed
        db.commit()
    return RedirectResponse("/bookings", status_code=303)
