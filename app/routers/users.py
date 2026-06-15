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

from app import models, sms
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app.fuel import active_policy
from app.routers.auth import verify_password

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["users"])


def profile_completion(user: models.User) -> dict:
    """
    Return a completion summary, grouped by goal so the checklist mirrors the
    /verify page (Ride / Drive / ID-badge) instead of one flat list — passengers,
    drivers, and badge-seekers each see only what's relevant to them.

    Shape: {groups: [{key, title_key, optional, steps:[{key,label_key,done,url}]}],
            completed, total, percent, is_complete, show_driver}. Counts cover the
    REQUIRED groups only; the optional ID badge never blocks reaching 100 %.
    """
    # `role` defaults to 'both' for everyone, so it can't distinguish a rider from
    # a driver. Only surface the driver-only group once the user shows real driver
    # intent — they've posted a trip, started licence verification, or added payout
    # details. A pure rider never sees the "Drive — offer rides" group.
    is_driver = (
        bool(user.trips)
        or user.license_verification != models.VerificationStatus.unverified
        or bool(user.kennitala or user.blikk_account_iban)
    )

    photo = {"key": "photo", "label_key": "profile_step_photo",
             "done": bool(user.avatar_url), "url": "/profile#photo"}
    phone = {"key": "phone", "label_key": "profile_step_phone",
             "done": bool(user.phone and user.phone_verified), "url": "/profile#phone"}
    bio   = {"key": "bio", "label_key": "profile_step_bio",
             "done": bool(user.bio), "url": "/profile#bio"}
    identity = {"key": "identity", "label_key": "profile_step_identity",
                "done": user.id_verification == models.VerificationStatus.approved,
                "url": "/verify"}
    licence = {"key": "licence", "label_key": "profile_step_licence",
               "done": user.license_verification == models.VerificationStatus.approved,
               "url": "/verify"}
    payout  = {"key": "payout", "label_key": "profile_step_payout",
               "done": bool(user.kennitala and user.blikk_account_iban),
               "url": "/profile#payout"}

    # Group titles reuse the /verify overview keys for consistency across pages.
    groups = [
        {"key": "ride", "title_key": "verify_ov_ride", "optional": False,
         "steps": [photo, phone, bio]},
    ]
    if is_driver:
        groups.append({"key": "drive", "title_key": "verify_ov_drive", "optional": False,
                       "steps": [licence, payout]})
    groups.append({"key": "badge", "title_key": "verify_ov_badge", "optional": True,
                   "steps": [identity]})

    required = [s for g in groups if not g["optional"] for s in g["steps"]]
    completed = sum(1 for s in required if s["done"])
    total     = len(required)
    return {
        "groups":      groups,
        "completed":   completed,
        "total":       total,
        "percent":     round(completed / total * 100) if total else 100,
        "is_complete": completed == total,
        "show_driver": is_driver,
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

    # Upcoming active trips as driver — but not while the driver is under posting
    # suspension (no-show / cancellation thresholds). is_active is already enforced
    # on the profile lookup above; this hides the bookable trips of a still-active
    # but suspended driver, matching the search/booking guards.
    upcoming_trips = (
        []
        if user.posting_suspended
        else db.query(models.Trip)
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

    # Acknowledge any "your ride was accepted" notifications now that the
    # passenger is looking at their bookings — clears the nav badge and the
    # home-page banner on the next request.
    _seen_any = False
    for b in all_bookings:
        if b.acceptance_unseen:
            b.acceptance_seen_at = now
            _seen_any = True
    if _seen_any:
        db.commit()

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
    current_user.full_name = (full_name or "").strip()[:100]

    new_phone = sms.normalize_phone(phone or None)
    if new_phone != current_user.phone:
        # Number changed — verification is no longer valid
        current_user.phone            = new_phone
        current_user.phone_verified   = False
        current_user.phone_otp        = None
        current_user.phone_otp_expires = None
    # If unchanged, leave phone_verified (and any pending OTP) untouched
    current_user.bio       = (bio or "").strip()[:2000] or None
    current_user.default_car_make  = default_car_make  or None
    current_user.default_car_model = default_car_model or None
    current_user.default_car_year  = int(default_car_year) if default_car_year.strip() else None
    try:
        current_user.default_car_type = models.CarType(default_car_type)
    except ValueError:
        current_user.default_car_type = models.CarType.sedan
    db.commit()
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/payout", response_class=HTMLResponse)
def save_payout_details(
    request:      Request,
    ctx:          dict        = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
    kennitala:    str         = Form(""),
    blikk_iban:   str         = Form(""),
):
    """Save driver payout details — kennitala and Icelandic IBAN."""
    from app.routers.users import profile_completion

    def _err(msg: str):
        completion = profile_completion(current_user)
        return templates.TemplateResponse("profile.html", {
            **ctx, "completion": completion, "payout_error": msg,
        }, status_code=400)

    # Validate kennitala — 10 digits, no spaces/dashes
    kt = kennitala.strip().replace("-", "").replace(" ", "")
    if kt and (not kt.isdigit() or len(kt) != 10):
        return _err("Kennitala must be exactly 10 digits (e.g. 1234567890).")

    # Validate IBAN — must start with IS, 26 chars, alphanumeric
    iban = blikk_iban.strip().upper().replace(" ", "")
    if iban and (not iban.startswith("IS") or len(iban) != 26 or not iban.isalnum()):
        return _err("IBAN must be a valid Icelandic IBAN (IS + 24 digits, e.g. IS140159260076545510730390).")

    current_user.kennitala          = kt   or None
    current_user.blikk_account_iban = iban or None

    # Blikk needs BOTH kennitala and an Icelandic IBAN. Set the method only when
    # both are present; clear it if either is missing (e.g. kennitala cleared),
    # so a driver can't stay flagged payout-ready with incomplete details.
    current_user.payout_method = (
        models.PayoutMethod.blikk if (kt and iban) else None
    )

    db.commit()
    return RedirectResponse("/profile?payout_saved=1", status_code=303)


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
    # Invalidate any previously-issued tokens (revokes other sessions)...
    current_user.token_version = (current_user.token_version or 0) + 1
    db.commit()

    resp = templates.TemplateResponse(
        "profile.html",
        {**ctx, "completion": completion, "password_ok": True},
    )
    # ...but keep THIS session signed in by re-issuing its cookie with the new
    # token_version (otherwise the user who just changed their password is
    # immediately logged out).
    from app.routers.auth import create_access_token
    from app.config import get_settings as _get_settings
    _s = _get_settings()
    resp.set_cookie(
        "access_token",
        create_access_token(current_user.id, current_user.token_version),
        httponly=True, max_age=_s.access_token_expire_minutes * 60,
        samesite="lax", secure=_s.secure_cookies,
    )
    return resp


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

    now = datetime.utcnow()

    # Deleting an account must NOT silently flip live trips/bookings to cancelled:
    # that skips refunds, payout reversal, seat recomputation, and passenger
    # notifications. Instead, block deletion while the user still has obligations
    # that have to go through the real cancellation/refund flows, and tell them to
    # cancel those first (driver: POST /trips/{id}/cancel; passenger: cancel from
    # "My trips"). Past / in-flight bookings are left untouched so their existing
    # state machines (capture → complete → payout) finish normally.
    active_booking_states = {
        models.BookingStatus.pending,
        models.BookingStatus.awaiting_payment,
        models.BookingStatus.card_saved,
        models.BookingStatus.confirmed,
    }
    driving_with_passengers = [
        t for t in current_user.trips
        if t.status == models.TripStatus.active
        and t.departure_datetime > now
        and any(b.status in active_booking_states for b in t.bookings)
    ]
    # Only count bookings the passenger can actually self-cancel. A money-locked
    # booking (captured / refund-in-flight) can't be cancelled from the UI and its
    # money flow is already committed — leave it to capture → complete → payout
    # rather than deadlocking deletion on something the user can't resolve.
    money_locked_states = {
        models.PaymentStatus.captured,
        models.PaymentStatus.capture_requested,
        models.PaymentStatus.refund_requested,
        models.PaymentStatus.refund_failed,
        models.PaymentStatus.refunded,
        models.PaymentStatus.partial_refund,
    }
    live_bookings = [
        b for b in current_user.bookings
        if b.status in active_booking_states
        and b.trip.departure_datetime > now
        and not (b.payment and b.payment.status in money_locked_states)
    ]
    if driving_with_passengers or live_bookings:
        parts = []
        if driving_with_passengers:
            parts.append(
                f"{len(driving_with_passengers)} upcoming ride(s) you're driving "
                f"that have booked passengers"
            )
        if live_bookings:
            parts.append(f"{len(live_bookings)} active booking(s) you hold")
        msg = (
            "Please cancel your live rides and bookings before deleting your account, "
            "so passengers are refunded and notified correctly. You still have "
            + " and ".join(parts)
            + ". Cancel them from “My trips”, then delete your account."
        )
        return templates.TemplateResponse(
            "profile.html",
            {**ctx, "completion": profile_completion(current_user), "delete_error": msg},
            status_code=400,
        )

    # No passenger-affecting obligations remain — any leftover upcoming active
    # trips are empty (no live bookings), so cancelling them directly is safe:
    # nothing to refund, no seats to recompute, no one to notify.
    for trip in current_user.trips:
        if trip.status == models.TripStatus.active and trip.departure_datetime > now:
            trip.status = models.TripStatus.cancelled

    # Anonymise PII — trip/payment rows are retained for legal/tax compliance.
    # ⚠️  AML RETENTION: didit_identity_session_id, didit_licence_session_id,
    # id_verification, and license_verification must NOT be cleared here — they
    # are required by Icelandic Act no. 140/2018 Art. 24 for 5 years from account
    # closure.  aml_retain_until records the expiry date of this obligation.
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
    current_user.deleted_at       = now
    # Lock the AML retention clock: KYC session references may not be purged
    # before this date even if the user requests erasure.
    from datetime import timedelta
    current_user.aml_retain_until = now + timedelta(days=5 * 365)

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


def _looks_like_image(b: bytes) -> bool:
    """True if the bytes start with a known image signature (JPEG/PNG/WEBP/GIF/HEIF)."""
    return (
        b[:3] == b"\xff\xd8\xff"                       # JPEG
        or b[:8] == b"\x89PNG\r\n\x1a\n"               # PNG
        or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")   # WEBP
        or b[:6] in (b"GIF87a", b"GIF89a")             # GIF
        or b[4:8] == b"ftyp"                           # HEIC/HEIF (ISO-BMFF)
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
    # Validate by magic bytes, not just the extension — stops an HTML/script
    # polyglot uploaded as ".png" being stored and served from our origin.
    if not _looks_like_image(content):
        return RedirectResponse("/profile?avatar_error=1", status_code=303)

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
