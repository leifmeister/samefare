from datetime import datetime, date
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app import models, email as mailer
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_template_context

settings = get_settings()

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/trips", tags=["trips"])

ALL_CAR_TYPES = [e.value for e in models.CarType]

ICELANDIC_CITIES = [
    "Reykjavík", "Akureyri", "Keflavík", "Selfoss", "Vík", "Höfn",
    "Egilsstaðir", "Ísafjörður", "Borgarnes", "Hveragerði", "Hella",
    "Kirkjubæjarklaustur", "Stykkishólmur", "Húsavík", "Sauðárkrókur",
    "Siglufjörður", "Dalvík", "Blönduós", "Varmahlíð", "Ólafsvík",
]


def _sort_trips(trips: list, sort: Optional[str]) -> list:
    if sort == "price_asc":
        return sorted(trips, key=lambda t: t.price_per_seat)
    if sort == "price_desc":
        return sorted(trips, key=lambda t: t.price_per_seat, reverse=True)
    # default: soonest departure first
    return sorted(trips, key=lambda t: t.departure_datetime)


# ── List / Search ─────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def trips_list(
    request: Request,
    ctx: dict = Depends(get_template_context),
    db: Session = Depends(get_db),
    origin:      Optional[str] = None,
    destination: Optional[str] = None,
    travel_date: Optional[str] = None,
    seats:       Optional[int] = None,
    sort:        Optional[str] = None,
):
    # Parse travel_date safely — browser sends "" when the field is left blank
    parsed_date: Optional[date] = None
    if travel_date:
        try:
            parsed_date = date.fromisoformat(travel_date)
        except ValueError:
            travel_date = ""

    query = (
        db.query(models.Trip)
        .options(joinedload(models.Trip.driver).joinedload(models.User.reviews_received))
        .filter(
            models.Trip.status == models.TripStatus.active,
            models.Trip.departure_datetime >= datetime.utcnow(),
            models.Trip.seats_available > 0,
        )
    )

    if origin:
        query = query.filter(models.Trip.origin.ilike(f"%{origin}%"))
    if destination:
        query = query.filter(models.Trip.destination.ilike(f"%{destination}%"))
    if parsed_date:
        day_start = datetime(parsed_date.year, parsed_date.month, parsed_date.day, 0, 0)
        day_end   = datetime(parsed_date.year, parsed_date.month, parsed_date.day, 23, 59)
        query = query.filter(
            models.Trip.departure_datetime >= day_start,
            models.Trip.departure_datetime <= day_end,
        )
    if seats:
        query = query.filter(models.Trip.seats_available >= seats)

    trips = _sort_trips(query.all(), sort)
    active_filters = sum([bool(origin), bool(destination), bool(travel_date), bool(seats)])

    ctx_extra = {
        "trips": trips,
        "origin": origin or "",
        "destination": destination or "",
        "travel_date": parsed_date.isoformat() if parsed_date else "",
        "seats": seats or 1,
        "sort": sort or "soonest",
        "active_filters": active_filters,
        "cities": ICELANDIC_CITIES,
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
        "allows_luggage": True, "allows_pets": False,
        "smoking": False, "instant_book": True,
        "description": "",
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
            vals["allows_luggage"] = source.allows_luggage
            vals["allows_pets"]    = source.allows_pets
            vals["smoking"]        = source.smoking
            vals["instant_book"]   = source.instant_book
            defaults = {
                "car_make":  source.car_make  or "",
                "car_model": source.car_model or "",
                "car_year":  source.car_year  or "",
                "car_type":  str(source.car_type) if source.car_type else "sedan",
            }
            return_banner = f"{source.origin} → {source.destination}"

    return templates.TemplateResponse("trips/create.html", {
        **ctx,
        "car_types":     ALL_CAR_TYPES,
        "cities":        ICELANDIC_CITIES,
        "error":         None,
        "defaults":      defaults,
        "vals":          vals,
        "return_banner": return_banner,
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
    description:        str   = Form(""),
    pickup_address:     str   = Form(""),
    dropoff_address:    str   = Form(""),
    # Checkboxes: unchecked sends nothing — use Optional[str] and convert
    allows_luggage_raw: Optional[str] = Form(None),
    allows_pets_raw:    Optional[str] = Form(None),
    smoking_raw:        Optional[str] = Form(None),
    instant_book_raw:   Optional[str] = Form(None),
):
    if current_user.license_verification != models.VerificationStatus.approved:
        return RedirectResponse("/verify?next=driver", status_code=303)

    allows_luggage = allows_luggage_raw is not None
    allows_pets    = allows_pets_raw    is not None
    smoking        = smoking_raw        is not None
    instant_book   = instant_book_raw   is not None

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
            "allows_pets":     allows_pets,
            "smoking":         smoking,
            "instant_book":    instant_book,
            "description":     description,
        },
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
        description=description or None,
        pickup_address=pickup_address or None,
        dropoff_address=dropoff_address or None,
        allows_luggage=allows_luggage,
        allows_pets=allows_pets,
        smoking=smoking,
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
            "allows_pets":     trip.allows_pets,
            "smoking":         trip.smoking,
            "instant_book":    trip.instant_book,
            "description":     trip.description or "",
        },
        "defaults": {
            "car_make":  trip.car_make  or "",
            "car_model": trip.car_model or "",
            "car_year":  trip.car_year  or "",
            "car_type":  str(trip.car_type) if trip.car_type else "sedan",
        },
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
    description:     str  = Form(""),
    pickup_address:  str  = Form(""),
    dropoff_address: str  = Form(""),
    allows_luggage_raw: Optional[str] = Form(None),
    allows_pets_raw:    Optional[str] = Form(None),
    smoking_raw:        Optional[str] = Form(None),
    instant_book_raw:   Optional[str] = Form(None),
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip or trip.driver_id != current_user.id:
        return RedirectResponse("/profile", status_code=303)
    if trip.status != models.TripStatus.active:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    allows_luggage = allows_luggage_raw is not None
    allows_pets    = allows_pets_raw    is not None
    smoking        = smoking_raw        is not None
    instant_book   = instant_book_raw   is not None

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
            "allows_luggage": allows_luggage, "allows_pets": allows_pets,
            "smoking": smoking, "instant_book": instant_book,
            "description": description,
        },
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

    # seats_total can't drop below already-confirmed seats
    confirmed_seats = sum(
        b.seats_booked for b in trip.bookings
        if b.status in (models.BookingStatus.confirmed, models.BookingStatus.pending)
    )
    if seats_total < confirmed_seats:
        return templates.TemplateResponse("trips/edit.html",
            {**err_ctx, "error": f"Can't reduce seats below {confirmed_seats} — already booked."}, status_code=400)

    trip.origin             = origin.strip()
    trip.destination        = destination.strip()
    trip.departure_datetime = departure_dt
    trip.seats_available    = seats_total - confirmed_seats
    trip.seats_total        = seats_total
    trip.price_per_seat     = price_per_seat
    trip.car_make           = car_make  or None
    trip.car_model          = car_model or None
    trip.car_year           = int(car_year) if car_year.isdigit() else None
    trip.car_type           = car_type
    trip.description        = description     or None
    trip.pickup_address     = pickup_address  or None
    trip.dropoff_address    = dropoff_address or None
    trip.allows_luggage     = allows_luggage
    trip.allows_pets        = allows_pets
    trip.smoking            = smoking
    trip.instant_book       = instant_book

    db.commit()
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


@router.get("/{trip_id}", response_class=HTMLResponse)
def trip_detail(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    db: Session = Depends(get_db),
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        return templates.TemplateResponse("errors/404.html", {**ctx}, status_code=404)

    confirmed_bookings = [b for b in trip.bookings
                          if b.status in (models.BookingStatus.confirmed,
                                          models.BookingStatus.completed)]
    return templates.TemplateResponse("trips/detail.html", {
        **ctx,
        "trip": trip,
        "confirmed_bookings": confirmed_bookings,
    })


@router.post("/{trip_id}/cancel")
def cancel_trip(
    trip_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if trip and trip.driver_id == current_user.id and trip.status == models.TripStatus.active:
        trip.status = models.TripStatus.cancelled
        affected = []
        for b in trip.bookings:
            if b.status in (models.BookingStatus.pending, models.BookingStatus.confirmed,
                            models.BookingStatus.awaiting_payment):
                b.status = models.BookingStatus.cancelled
                affected.append(b)
        db.commit()
        # Email all affected passengers
        for b in affected:
            mailer.trip_cancelled_to_passenger(b)
    return RedirectResponse("/profile", status_code=303)


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
