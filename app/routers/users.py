import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app import models
from app.database import get_db
from app.dependencies import get_current_user, get_template_context

templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["users"])


@router.get("/users/{user_id}", response_class=HTMLResponse)
def public_profile(
    user_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    db: Session = Depends(get_db),
):
    user = (
        db.query(models.User)
        .options(
            joinedload(models.User.reviews_received).joinedload(models.Review.reviewer),
            joinedload(models.User.reviews_received).joinedload(models.Review.trip),
        )
        .filter(models.User.id == user_id, models.User.is_active == True)
        .first()
    )
    if not user:
        return templates.TemplateResponse("errors/404.html", {**ctx}, status_code=404)

    # Upcoming active trips as driver
    upcoming_trips = (
        db.query(models.Trip)
        .filter(
            models.Trip.driver_id == user_id,
            models.Trip.status == models.TripStatus.active,
            models.Trip.departure_datetime >= datetime.utcnow(),
            models.Trip.seats_available > 0,
        )
        .order_by(models.Trip.departure_datetime)
        .limit(5)
        .all()
    )

    # Reviews received as a driver, newest first
    driver_reviews = sorted(
        [r for r in user.reviews_received
         if r.review_type == models.ReviewType.passenger_to_driver],
        key=lambda r: r.created_at,
        reverse=True,
    )

    return templates.TemplateResponse("users/public_profile.html", {
        **ctx,
        "profile_user":   user,
        "upcoming_trips": upcoming_trips,
        "driver_reviews": driver_reviews,
    })


@router.get("/my-trips", response_class=HTMLResponse)
def my_trips_page(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # ── Passenger: all bookings ───────────────────────────────────────────────
    my_bookings = (
        db.query(models.Booking)
        .filter(models.Booking.passenger_id == current_user.id)
        .order_by(models.Booking.created_at.desc())
        .all()
    )

    # ── Driver: all trips + pending requests ──────────────────────────────────
    my_rides = (
        db.query(models.Trip)
        .filter(models.Trip.driver_id == current_user.id)
        .order_by(models.Trip.departure_datetime.desc())
        .all()
    )
    pending_bookings = [
        b for trip in my_rides
        for b in trip.bookings
        if b.status == models.BookingStatus.pending
    ]

    # Which tab to open: default to rides if driver has trips, else bookings
    tab = request.query_params.get("tab", "bookings" if my_bookings else "rides")

    return templates.TemplateResponse("my_trips.html", {
        **ctx,
        "my_bookings":     my_bookings,
        "my_rides":        my_rides,
        "pending_bookings": pending_bookings,
        "tab":             tab,
    })


@router.get("/profile", response_class=HTMLResponse)
def profile(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("profile.html", {**ctx})


@router.post("/profile/edit", response_class=HTMLResponse)
def edit_profile(
    request:          Request,
    ctx:              dict         = Depends(get_template_context),
    current_user:     models.User  = Depends(get_current_user),
    db:               Session      = Depends(get_db),
    full_name:        str          = Form(...),
    phone:            str          = Form(""),
    bio:              str          = Form(""),
    default_car_make:  str         = Form(""),
    default_car_model: str         = Form(""),
    default_car_year:  str         = Form(""),
    default_car_type:  str         = Form("sedan"),
):
    current_user.full_name = full_name
    current_user.phone     = phone or None
    current_user.bio       = bio or None
    current_user.default_car_make  = default_car_make  or None
    current_user.default_car_model = default_car_model or None
    current_user.default_car_year  = int(default_car_year) if default_car_year.strip() else None
    try:
        current_user.default_car_type = models.CarType(default_car_type)
    except ValueError:
        current_user.default_car_type = models.CarType.sedan
    db.commit()
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/avatar", response_class=HTMLResponse)
def upload_avatar(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    photo: UploadFile = File(...),
):
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
    ext = os.path.splitext(photo.filename or "")[-1].lower()
    if ext not in allowed:
        return RedirectResponse("/profile", status_code=303)

    content = photo.file.read()
    if len(content) > 5 * 1024 * 1024:  # 5 MB limit
        return RedirectResponse("/profile", status_code=303)

    os.makedirs("static/avatars", exist_ok=True)

    # Delete old avatar file if it exists
    if current_user.avatar_url:
        old_path = current_user.avatar_url.lstrip("/")
        if os.path.exists(old_path):
            os.remove(old_path)

    filename = f"{uuid.uuid4().hex}{ext}"
    with open(f"static/avatars/{filename}", "wb") as f:
        f.write(content)

    current_user.avatar_url = f"/static/avatars/{filename}"
    db.commit()
    return RedirectResponse("/profile", status_code=303)
