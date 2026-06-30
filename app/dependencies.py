from datetime import timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app import models
from app.config import get_settings
from app.database import get_db
from app.utils import _CANONICAL_CITIES

settings = get_settings()


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[models.User]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        user_id: int = int(payload.get("sub"))
        token_version: int = int(payload.get("tv", 0) or 0)
    except (JWTError, TypeError, ValueError):
        return None
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active:
        return None
    # Reject any token issued before the user's latest password reset/change.
    if int(user.token_version or 0) != token_version:
        return None
    return user


def get_current_user(
    user: Optional[models.User] = Depends(get_current_user_optional),
) -> models.User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def _pending_reviews(user: models.User, db: Session) -> list:
    """
    Returns list of booking_ids where user has a completed trip/booking
    but hasn't left a review yet, within the 14-day window.
    Used to show the review nudge banner.
    """
    from datetime import datetime
    cutoff = datetime.utcnow() - timedelta(days=14)
    pending = []
    try:
        # IDs of reviews already written by this user
        reviewed_ids = {
            r.booking_id for r in
            db.query(models.Review.booking_id)
            .filter(models.Review.reviewer_id == user.id)
            .all()
        }

        # Completed bookings as passenger
        for b in (
            db.query(models.Booking)
            .join(models.Trip)
            .filter(
                models.Booking.passenger_id == user.id,
                models.Booking.status       == models.BookingStatus.completed,
                models.Trip.departure_datetime >= cutoff,
            )
            .all()
        ):
            if b.id not in reviewed_ids:
                pending.append(b.id)

        # Completed bookings on driver's trips
        for trip in (
            db.query(models.Trip)
            .filter(
                models.Trip.driver_id          == user.id,
                models.Trip.status             == models.TripStatus.completed,
                models.Trip.departure_datetime >= cutoff,
            )
            .all()
        ):
            for b in trip.bookings:
                if b.status == models.BookingStatus.completed and b.id not in reviewed_ids:
                    pending.append(b.id)
    except Exception:
        pass
    return pending


def get_template_context(request: Request, db: Session = Depends(get_db)):
    from datetime import datetime
    from app.i18n import get_translations, detect_lang
    from app.dates import make_date_formatter
    lang = detect_lang(request)
    user = get_current_user_optional(request, db)
    unread_count = 0
    pending_reviews = []
    if user:
        try:
            booking_unread = (
                db.query(func.count(models.Message.id))
                .join(models.Booking, models.Message.booking_id == models.Booking.id)
                .join(models.Trip,    models.Booking.trip_id    == models.Trip.id)
                .filter(
                    models.Message.sender_id != user.id,
                    models.Message.is_read   == False,  # noqa: E712
                    or_(
                        models.Booking.passenger_id == user.id,
                        models.Trip.driver_id        == user.id,
                    ),
                )
                .scalar() or 0
            )
            # Pre-booking inquiry threads count toward the same nav badge.
            inquiry_unread = (
                db.query(func.count(models.Message.id))
                .join(models.Inquiry, models.Message.inquiry_id == models.Inquiry.id)
                .join(models.Trip,    models.Inquiry.trip_id    == models.Trip.id)
                .filter(
                    models.Message.sender_id != user.id,
                    models.Message.is_read   == False,  # noqa: E712
                    or_(
                        models.Inquiry.passenger_id == user.id,
                        models.Trip.driver_id        == user.id,
                    ),
                )
                .scalar() or 0
            )
            unread_count = booking_unread + inquiry_unread
        except Exception:
            unread_count = 0
        pending_reviews = _pending_reviews(user, db)
    # Incoming request-to-book bookings awaiting THIS user's approval as a driver.
    # Drives the nav badge so a driver sees requests from any page — only counts
    # requests they can still act on (trip active and not yet departed).
    pending_request_count = 0
    if user:
        try:
            pending_request_count = (
                db.query(func.count(models.Booking.id))
                .join(models.Trip, models.Booking.trip_id == models.Trip.id)
                .filter(
                    models.Booking.status          == models.BookingStatus.pending,
                    models.Trip.driver_id          == user.id,
                    models.Trip.status             == models.TripStatus.active,
                    models.Trip.departure_datetime > datetime.utcnow(),
                )
                .scalar() or 0
            )
        except Exception:
            pending_request_count = 0
    # Request-to-book bookings the driver has just accepted that the passenger
    # hasn't acknowledged yet. Drives the passenger's "your ride was accepted"
    # nav badge (My trips) and the home-page banner. Only upcoming, still-active
    # bookings count, so the badge can't get stuck on a departed trip.
    accepted_bookings: list = []
    if user:
        try:
            active_states = (
                models.BookingStatus.awaiting_payment,
                models.BookingStatus.card_saved,
                models.BookingStatus.confirmed,
            )
            candidates = (
                db.query(models.Booking)
                .join(models.Trip, models.Booking.trip_id == models.Trip.id)
                # Eager-load trip: the home route closes its DB session before the
                # template renders, so a lazy load of _ab.trip would detach-error.
                .options(joinedload(models.Booking.trip))
                .filter(
                    models.Booking.passenger_id   == user.id,
                    models.Booking.accepted_at    != None,  # noqa: E711
                    models.Booking.status.in_(active_states),
                    models.Trip.departure_datetime > datetime.utcnow(),
                )
                .order_by(models.Trip.departure_datetime.asc())
                .all()
            )
            accepted_bookings = [b for b in candidates if b.acceptance_unseen]
        except Exception:
            accepted_bookings = []
    accepted_booking_count = len(accepted_bookings)
    email_unverified = user and not user.email_verified
    is_newsletter_subscriber = False
    if user:
        try:
            is_newsletter_subscriber = (
                db.query(models.NewsletterSubscriber)
                .filter(models.NewsletterSubscriber.email == user.email)
                .first()
            ) is not None
        except Exception:
            pass
    return {
        "request":                  request,
        "current_user":             user,
        "unread_message_count":     unread_count,
        "pending_request_count":    pending_request_count,
        "accepted_booking_count":   accepted_booking_count,
        "accepted_bookings":        accepted_bookings,
        "pending_reviews":          pending_reviews,
        "now":                      datetime.utcnow(),
        "beta_mode":                settings.beta_mode,
        "timedelta":                timedelta,
        "email_unverified":         email_unverified,
        "is_newsletter_subscriber": is_newsletter_subscriber,
        "lang":                     lang,
        "meta_pixel_id":            settings.meta_pixel_id,
        "_t":                       get_translations(lang),
        # Locale-aware date formatter: fdate(dt, "%-d %b %Y") → Icelandic month/
        # weekday names when lang is 'is', English otherwise.
        "fdate":                    make_date_formatter(lang),
        # Canonical city list for the shared search-bar city pickers (base.html).
        # Same source the offer-a-ride form uses, so both stay in sync.
        "canonical_cities":         list(_CANONICAL_CITIES),
    }
