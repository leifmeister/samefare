import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload, selectinload

from app import models
from app.database import get_db
from app.dependencies import get_current_user, get_template_context

templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["users"])


def profile_completion(user: models.User) -> dict:
    """
    Return a completion summary for the given user.
    Each step is a dict: {key, label, done, url}.
    """
    is_driver = user.role in (models.UserRole.driver, models.UserRole.both)
    steps = [
        {
            "key":   "photo",
            "label": "Add a profile photo",
            "done":  bool(user.avatar_url),
            "url":   "/profile",
        },
        {
            "key":   "phone",
            "label": "Verify your phone number",
            "done":  bool(user.phone and user.phone_verified),
            "url":   "/profile",
        },
        {
            "key":   "bio",
            "label": "Write a short bio",
            "done":  bool(user.bio),
            "url":   "/profile",
        },
        {
            "key":   "identity",
            "label": "Verify your identity",
            "done":  user.id_verification == models.VerificationStatus.approved,
            "url":   "/verify",
        },
    ]
    if is_driver:
        steps.append({
            "key":   "licence",
            "label": "Verify your driver's licence",
            "done":  user.license_verification == models.VerificationStatus.approved,
            "url":   "/verify",
        })

    completed = sum(1 for s in steps if s["done"])
    total     = len(steps)
    return {
        "steps":     steps,
        "completed": completed,
        "total":     total,
        "percent":   round(completed / total * 100),
        "is_complete": completed == total,
    }


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
    now = datetime.utcnow()

    # ── Passenger: all bookings split into upcoming / past ────────────────────
    all_bookings = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.trip).joinedload(models.Trip.driver),
            joinedload(models.Booking.payment),
            selectinload(models.Booking.reviews),
        )
        .filter(models.Booking.passenger_id == current_user.id)
        .order_by(models.Booking.created_at.desc())
        .all()
    )
    # card_saved: seat held, card tokenised for MIT — counts as an upcoming active booking.
    active_statuses = {models.BookingStatus.pending, models.BookingStatus.awaiting_payment,
                       models.BookingStatus.confirmed, models.BookingStatus.card_saved}
    upcoming_bookings = [b for b in all_bookings
                         if b.status in active_statuses
                         and b.trip.departure_datetime >= now]
    past_bookings     = [b for b in all_bookings
                         if b not in upcoming_bookings]

    # ── Driver: all trips split into upcoming / past + pending requests ───────
    all_rides = (
        db.query(models.Trip)
        .options(
            selectinload(models.Trip.bookings).joinedload(models.Booking.payment),
            selectinload(models.Trip.bookings).joinedload(models.Booking.passenger),
        )
        .filter(models.Trip.driver_id == current_user.id)
        .order_by(models.Trip.departure_datetime.desc())
        .all()
    )
    upcoming_rides = [t for t in all_rides
                      if t.status == models.TripStatus.active
                      and t.departure_datetime >= now]
    past_rides     = [t for t in all_rides if t not in upcoming_rides]

    pending_bookings = [
        b for trip in all_rides
        for b in trip.bookings
        if b.status == models.BookingStatus.pending
    ]

    # Which tab to open: default to bookings if passenger has any, else rides
    tab = request.query_params.get("tab", "bookings" if all_bookings else "rides")

    return templates.TemplateResponse("my_trips.html", {
        **ctx,
        "upcoming_bookings": upcoming_bookings,
        "past_bookings":     past_bookings,
        "upcoming_rides":    upcoming_rides,
        "past_rides":        past_rides,
        "pending_bookings":  pending_bookings,
        "tab":               tab,
        "completion":        profile_completion(current_user),
    })


@router.get("/profile", response_class=HTMLResponse)
def profile(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("profile.html", {
        **ctx,
        "completion": profile_completion(current_user),
    })


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

    new_phone = phone or None
    if new_phone != current_user.phone:
        # Number changed — verification is no longer valid
        current_user.phone            = new_phone
        current_user.phone_verified   = False
        current_user.phone_otp        = None
        current_user.phone_otp_expires = None
    # If unchanged, leave phone_verified (and any pending OTP) untouched
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
