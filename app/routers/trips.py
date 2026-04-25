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
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
):
    if current_user.license_verification != models.VerificationStatus.approved:
        return RedirectResponse("/verify?next=driver", status_code=303)
    return templates.TemplateResponse("trips/create.html", {
        **ctx,
        "car_types": ALL_CAR_TYPES,
        "cities":    ICELANDIC_CITIES,
        "error":     None,
        "defaults":  {
            "car_make":  current_user.default_car_make  or "",
            "car_model": current_user.default_car_model or "",
            "car_year":  current_user.default_car_year  or "",
            "car_type":  str(current_user.default_car_type) if current_user.default_car_type else "sedan",
        },
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
    allows_luggage:     bool  = Form(True),
    allows_pets:        bool  = Form(False),
    smoking:            bool  = Form(False),
    instant_book:       bool  = Form(True),
):
    if current_user.license_verification != models.VerificationStatus.approved:
        return RedirectResponse("/verify?next=driver", status_code=303)

    err_ctx = {**ctx, "car_types": ALL_CAR_TYPES, "cities": ICELANDIC_CITIES}

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
    db.commit()
    db.refresh(trip)
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
