import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload, selectinload

from app import models
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app.fuel import active_policy
from app.routers.auth import verify_password

log = logging.getLogger(__name__)

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
            "url":   "/profile#photo",
        },
        {
            "key":   "phone",
            "label": "Verify your phone number",
            "done":  bool(user.phone and user.phone_verified),
            "url":   "/profile#phone",
        },
        {
            "key":   "bio",
            "label": "Write a short bio",
            "done":  bool(user.bio),
            "url":   "/profile#bio",
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
    # Reviews received as a passenger, newest first
    passenger_reviews = sorted(
        [r for r in user.reviews_received
         if r.review_type == models.ReviewType.driver_to_passenger],
        key=lambda r: r.created_at,
        reverse=True,
    )

    # Rating distribution for driver reviews (stars 5 → 1)
    driver_rating_dist = {s: sum(1 for r in driver_reviews if r.rating == s) for s in range(5, 0, -1)}

    return templates.TemplateResponse("users/public_profile.html", {
        **ctx,
        "profile_user":       user,
        "upcoming_trips":     upcoming_trips,
        "driver_reviews":     driver_reviews,
        "passenger_reviews":  passenger_reviews,
        "driver_rating_dist": driver_rating_dist,
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
            selectinload(models.Trip.bookings).options(
                joinedload(models.Booking.payment),
                joinedload(models.Booking.passenger),
            ),
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

    # ── Driver earnings aggregation ───────────────────────────────────────────
    _monthly: dict = defaultdict(lambda: {"rides": 0, "passengers": 0, "payout": 0})
    for trip in all_rides:
        if trip.status != models.TripStatus.completed:
            continue
        key = trip.departure_datetime.strftime("%Y-%m")
        _monthly[key]["rides"] += 1
        for b in trip.bookings:
            if b.status == models.BookingStatus.completed and b.payment:
                _monthly[key]["passengers"] += 1
                _monthly[key]["payout"] += b.payment.driver_payout or 0

    earnings_months = [
        {
            "month":      k,
            "label":      datetime.strptime(k, "%Y-%m").strftime("%B %Y"),
            "rides":      v["rides"],
            "passengers": v["passengers"],
            "payout":     v["payout"],
        }
        for k, v in sorted(_monthly.items(), reverse=True)
    ]
    lifetime_payout     = sum(m["payout"]     for m in earnings_months)
    lifetime_passengers = sum(m["passengers"] for m in earnings_months)
    lifetime_rides      = sum(m["rides"]      for m in earnings_months)

    # Which tab to open: default to bookings if passenger has any, else rides
    tab = request.query_params.get("tab", "bookings" if all_bookings else "rides")

    return templates.TemplateResponse("my_trips.html", {
        **ctx,
        "upcoming_bookings":    upcoming_bookings,
        "past_bookings":        past_bookings,
        "upcoming_rides":       upcoming_rides,
        "past_rides":           past_rides,
        "all_rides":            all_rides,
        "pending_bookings":     pending_bookings,
        "tab":                  tab,
        "completion":           profile_completion(current_user),
        "earnings_months":      earnings_months,
        "lifetime_payout":      lifetime_payout,
        "lifetime_passengers":  lifetime_passengers,
        "lifetime_rides":       lifetime_rides,
    })


@router.get("/pricing/how-it-works", response_class=HTMLResponse)
def pricing_methodology(
    request: Request,
    ctx: dict = Depends(get_template_context),
    db: Session = Depends(get_db),
):
    policy = active_policy(db)
    return templates.TemplateResponse("pricing/how_it_works.html", {
        **ctx,
        "policy": policy,
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


@router.post("/profile/change-password", response_class=HTMLResponse)
def change_password(
    request:              Request,
    ctx:                  dict        = Depends(get_template_context),
    current_user:         models.User = Depends(get_current_user),
    db:                   Session     = Depends(get_db),
    current_password:     str         = Form(...),
    new_password:         str         = Form(...),
    confirm_new_password: str         = Form(...),
):
    from app.routers.auth import hash_password
    completion = profile_completion(current_user)

    def _err(msg):
        return templates.TemplateResponse(
            "profile.html",
            {**ctx, "completion": completion, "password_error": msg},
            status_code=400,
        )

    if not verify_password(current_password, current_user.hashed_password):
        return _err("Current password is incorrect.")
    if len(new_password) < 8:
        return _err("New password must be at least 8 characters.")
    if new_password != confirm_new_password:
        return _err("New passwords do not match.")
    if new_password == current_password:
        return _err("New password must be different from your current password.")

    current_user.hashed_password = hash_password(new_password)
    db.commit()

    return templates.TemplateResponse(
        "profile.html",
        {**ctx, "completion": completion, "password_ok": True},
    )


@router.post("/profile/delete", response_class=HTMLResponse)
def delete_account(
    request:      Request,
    ctx:          dict        = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
    password:     str         = Form(...),
):
    if not verify_password(password, current_user.hashed_password):
        return templates.TemplateResponse(
            "profile.html",
            {**ctx, "completion": profile_completion(current_user),
             "delete_error": "Incorrect password. Account not deleted."},
            status_code=400,
        )

    # Cancel future active trips the user is driving
    for trip in current_user.trips:
        if (trip.status == models.TripStatus.active
                and trip.departure_datetime > datetime.utcnow()):
            trip.status = models.TripStatus.cancelled

    # Cancel pending/confirmed bookings the user holds as a passenger
    cancellable = {
        models.BookingStatus.pending,
        models.BookingStatus.awaiting_payment,
        models.BookingStatus.card_saved,
        models.BookingStatus.confirmed,
    }
    for booking in current_user.bookings:
        if booking.status in cancellable:
            booking.status = models.BookingStatus.cancelled

    # Anonymise PII — trip/payment rows are retained for legal/tax compliance
    uid = current_user.id
    current_user.full_name        = "Deleted User"
    current_user.email            = f"deleted_{uid}@deleted.invalid"
    current_user.phone            = None
    current_user.phone_verified   = False
    current_user.bio              = None
    current_user.avatar_url       = None
    current_user.birth_year       = None
    current_user.hashed_password  = ""
    current_user.is_active        = False
    current_user.deleted_at       = datetime.utcnow()

    db.commit()

    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response


@router.get("/profile/export")
def export_data(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _dt(v):
        return v.isoformat() if v else None

    user = current_user
    payload = {
        "exported_at": datetime.utcnow().isoformat(),
        "profile": {
            "id":           user.id,
            "email":        user.email,
            "full_name":    user.full_name,
            "phone":        user.phone,
            "bio":          user.bio,
            "birth_year":   user.birth_year,
            "role":         user.role,
            "created_at":   _dt(user.created_at),
            "phone_verified": user.phone_verified,
            "email_verified": user.email_verified,
            "id_verification":      user.id_verification,
            "license_verification": user.license_verification,
            "default_car_make":  user.default_car_make,
            "default_car_model": user.default_car_model,
            "default_car_year":  user.default_car_year,
            "default_car_type":  user.default_car_type,
            "payout_method":     user.payout_method,
            "blikk_account_iban": user.blikk_account_iban,
        },
        "trips_as_driver": [
            {
                "id":                 t.id,
                "origin":             t.origin,
                "destination":        t.destination,
                "departure_datetime": _dt(t.departure_datetime),
                "price_per_seat":     t.price_per_seat,
                "seats_total":        t.seats_total,
                "status":             t.status,
                "created_at":         _dt(t.created_at),
            }
            for t in user.trips
        ],
        "bookings_as_passenger": [
            {
                "id":           b.id,
                "trip_id":      b.trip_id,
                "origin":       b.trip.origin,
                "destination":  b.trip.destination,
                "departure":    _dt(b.trip.departure_datetime),
                "seats":        b.seats_booked,
                "status":       b.status,
                "pickup_city":  b.pickup_city,
                "dropoff_city": b.dropoff_city,
                "created_at":   _dt(b.created_at),
                "payment": {
                    "passenger_total": b.payment.passenger_total,
                    "driver_payout":   b.payment.driver_payout,
                    "platform_fee":    b.payment.platform_fee,
                    "status":          b.payment.status,
                } if b.payment else None,
            }
            for b in user.bookings
        ],
        "reviews_given": [
            {
                "id":          r.id,
                "trip_id":     r.trip_id,
                "reviewee_id": r.reviewee_id,
                "rating":      r.rating,
                "comment":     r.comment,
                "type":        r.review_type,
                "created_at":  _dt(r.created_at),
            }
            for r in user.reviews_given
        ],
        "reviews_received": [
            {
                "id":          r.id,
                "trip_id":     r.trip_id,
                "reviewer_id": r.reviewer_id,
                "rating":      r.rating,
                "comment":     r.comment,
                "type":        r.review_type,
                "created_at":  _dt(r.created_at),
            }
            for r in user.reviews_received
        ],
        "messages": [
            {
                "id":         m.id,
                "booking_id": m.booking_id,
                "direction":  "sent",
                "body":       m.body,
                "sent_at":    _dt(m.sent_at),
            }
            for m in user.messages_sent
        ],
    }

    filename = f"samefare-data-{user.id}-{datetime.utcnow().strftime('%Y%m%d')}.json"
    return Response(
        content=json.dumps(payload, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
