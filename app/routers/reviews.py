"""
Reviews router.

After a trip auto-completes, both passenger and driver have 14 days
to leave a review. One review per side per booking (enforced by DB
unique constraint).
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app import models
from app.database import get_db
from app.dependencies import get_current_user, get_template_context

templates = Jinja2Templates(directory="templates")
router    = APIRouter(prefix="/reviews", tags=["reviews"])

REVIEW_WINDOW_DAYS = 14


def _can_review(booking: models.Booking, current_user: models.User) -> tuple[bool, str | None]:
    """
    Returns (can_review, review_type_str).
    Passenger reviews driver; driver reviews passenger.
    """
    if booking.status != models.BookingStatus.completed:
        return False, None

    cutoff = booking.trip.departure_datetime + timedelta(days=REVIEW_WINDOW_DAYS)
    if datetime.utcnow() > cutoff:
        return False, None

    if current_user.id == booking.passenger_id:
        return True, models.ReviewType.passenger_to_driver
    if current_user.id == booking.trip.driver_id:
        return True, models.ReviewType.driver_to_passenger
    return False, None


@router.get("/new/{booking_id}", response_class=HTMLResponse)
def review_form(
    booking_id:   int,
    request:      Request,
    ctx:          dict         = Depends(get_template_context),
    current_user: models.User  = Depends(get_current_user),
    db:           Session      = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.trip).joinedload(models.Trip.driver),
            joinedload(models.Booking.passenger),
        )
        .filter(models.Booking.id == booking_id)
        .first()
    )
    can, review_type = _can_review(booking, current_user) if booking else (False, None)
    if not can:
        return RedirectResponse("/bookings", status_code=303)

    # Already reviewed?
    existing = (
        db.query(models.Review)
        .filter(
            models.Review.booking_id  == booking_id,
            models.Review.reviewer_id == current_user.id,
            models.Review.review_type == review_type,
        )
        .first()
    )
    if existing:
        return RedirectResponse("/bookings", status_code=303)

    reviewee = (
        booking.trip.driver
        if review_type == models.ReviewType.passenger_to_driver
        else booking.passenger
    )
    return templates.TemplateResponse("reviews/new.html", {
        **ctx,
        "booking":     booking,
        "reviewee":    reviewee,
        "review_type": review_type,
        "error":       None,
    })


@router.post("/new/{booking_id}", response_class=HTMLResponse)
def submit_review(
    booking_id:   int,
    request:      Request,
    ctx:          dict        = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
    rating:       int         = Form(...),
    comment:      str         = Form(""),
):
    booking = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.trip).joinedload(models.Trip.driver),
            joinedload(models.Booking.passenger),
        )
        .filter(models.Booking.id == booking_id)
        .first()
    )
    can, review_type = _can_review(booking, current_user) if booking else (False, None)
    if not can:
        return RedirectResponse("/bookings", status_code=303)

    if not (1 <= rating <= 5):
        reviewee = (
            booking.trip.driver
            if review_type == models.ReviewType.passenger_to_driver
            else booking.passenger
        )
        return templates.TemplateResponse("reviews/new.html", {
            **ctx,
            "booking":     booking,
            "reviewee":    reviewee,
            "review_type": review_type,
            "error":       "Please select a star rating.",
        }, status_code=400)

    # Upsert — replace if somehow submitted twice
    existing = (
        db.query(models.Review)
        .filter(
            models.Review.booking_id  == booking_id,
            models.Review.reviewer_id == current_user.id,
            models.Review.review_type == review_type,
        )
        .first()
    )
    if existing:
        return RedirectResponse("/bookings", status_code=303)

    reviewee_id = (
        booking.trip.driver_id
        if review_type == models.ReviewType.passenger_to_driver
        else booking.passenger_id
    )

    review = models.Review(
        booking_id  = booking_id,
        trip_id     = booking.trip_id,
        reviewer_id = current_user.id,
        reviewee_id = reviewee_id,
        review_type = review_type,
        rating      = rating,
        comment     = comment.strip() or None,
    )
    db.add(review)
    db.commit()

    return RedirectResponse("/bookings?reviewed=1", status_code=303)
