from datetime import timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import get_db

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
    except (JWTError, TypeError, ValueError):
        return None
    user = db.query(models.User).filter(models.User.id == user_id).first()
    return user if (user and user.is_active) else None


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
    user = get_current_user_optional(request, db)
    unread_count = 0
    pending_reviews = []
    if user:
        try:
            unread_count = (
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
        except Exception:
            unread_count = 0
        pending_reviews = _pending_reviews(user, db)
    return {
        "request":              request,
        "current_user":         user,
        "unread_message_count": unread_count,
        "pending_reviews":      pending_reviews,
        "now":                  datetime.utcnow(),
        "beta_mode":            settings.beta_mode,
        "timedelta":            timedelta,
    }
