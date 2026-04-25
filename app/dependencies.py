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


def get_template_context(request: Request, db: Session = Depends(get_db)):
    from datetime import datetime
    user = get_current_user_optional(request, db)
    unread_count = 0
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
            unread_count = 0   # table may not exist yet on first run
    return {
        "request":              request,
        "current_user":         user,
        "unread_message_count": unread_count,
        "now":                  datetime.utcnow(),
        "beta_mode":            settings.beta_mode,
    }
