from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app import models, email as mailer
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app.routers.payments import calc_fees

settings = get_settings()
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/bookings", tags=["bookings"])


def _newsletter_discount(db: Session, user: models.User):
    """
    Return the NewsletterSubscriber row if this user has an unused first-ride
    discount, otherwise None.
    """
    return (
        db.query(models.NewsletterSubscriber)
        .filter(
            models.NewsletterSubscriber.email         == user.email,
            models.NewsletterSubscriber.discount_used == False,  # noqa: E712
        )
        .first()
    )


@router.get("", response_class=HTMLResponse)
def my_bookings(request: Request):
    # Consolidated into /my-trips
    params = request.query_params
    qs = f"?tab=bookings{'&' + str(params) if params else ''}"
    return RedirectResponse(f"/my-trips{qs}", status_code=301)


@router.get("/trip/{trip_id}", response_class=HTMLResponse)
def book_trip_page(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user.email_verified and not get_settings().beta_mode:
        return RedirectResponse("/check-your-email", status_code=303)
    if current_user.id_verification != models.VerificationStatus.approved:
        return RedirectResponse(f"/verify?next=book&trip={trip_id}", status_code=303)

    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        return templates.TemplateResponse("errors/404.html", {**ctx}, status_code=404)
    if trip.driver_id == current_user.id:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)
    if trip.status != models.TripStatus.active or trip.seats_available < 1:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    has_discount = _newsletter_discount(db, current_user) is not None
    return templates.TemplateResponse("bookings/create.html", {
        **ctx, "trip": trip, "error": None, "has_discount": has_discount,
    })


@router.post("/trip/{trip_id}", response_class=HTMLResponse)
def create_booking(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    seats_booked: int = Form(1),
    message: str = Form(""),
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        return templates.TemplateResponse("errors/404.html", {**ctx}, status_code=404)

    has_discount = _newsletter_discount(db, current_user) is not None
    err_ctx = {**ctx, "trip": trip, "has_discount": has_discount}

    if trip.driver_id == current_user.id:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": "You cannot book your own trip."}, status_code=400)

    if seats_booked > trip.seats_available:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": f"Only {trip.seats_available} seat(s) available."}, status_code=400)

    # Check if passenger already has an active booking on this trip
    existing = db.query(models.Booking).filter(
        models.Booking.trip_id == trip_id,
        models.Booking.passenger_id == current_user.id,
        models.Booking.status.in_([models.BookingStatus.pending, models.BookingStatus.confirmed]),
    ).first()
    if existing:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": "You already have a booking on this trip."}, status_code=400)

    contribution = trip.price_per_seat * seats_booked
    subscriber   = _newsletter_discount(db, current_user)
    if subscriber:
        service_fee = 0
        total       = contribution
    else:
        service_fee, total, _ = calc_fees(contribution)

    if trip.instant_book:
        # Instant: hold seats now, go straight to payment
        initial_status = models.BookingStatus.awaiting_payment
        trip.seats_available = max(0, trip.seats_available - seats_booked)
    else:
        # Requires approval: don't hold seats yet, wait for driver
        initial_status = models.BookingStatus.pending

    booking = models.Booking(
        trip_id=trip_id,
        passenger_id=current_user.id,
        seats_booked=seats_booked,
        total_price=total,
        service_fee=service_fee,
        message=message or None,
        status=initial_status,
    )
    db.add(booking)
    if subscriber:
        subscriber.discount_used = True
    db.commit()
    db.refresh(booking)

    if trip.instant_book:
        return RedirectResponse(f"/payments/checkout/{booking.id}", status_code=303)
    else:
        # Notify driver of the pending request
        mailer.booking_request_to_driver(booking)
        return RedirectResponse("/bookings?requested=1", status_code=303)


def _refund_preview(booking) -> dict:
    """
    Calculate the refund a passenger would receive if they cancelled now.
    Returns a dict with 'amount', 'label', and 'policy'.
    Does NOT modify anything — safe to call from a GET handler.
    """
    now = datetime.utcnow()
    if not booking.payment:
        return {"amount": 0, "label": "No charge yet", "policy": "free"}

    departure    = booking.trip.departure_datetime
    hours_left   = (departure - now).total_seconds() / 3600
    mins_since   = (now - booking.created_at).total_seconds() / 60
    contribution = booking.payment.driver_payout
    total        = booking.payment.passenger_total

    if mins_since <= 30 and hours_left >= 24:
        return {
            "amount": total,
            "label":  f"Full refund — {total:,} ISK",
            "policy": "Within 30-minute grace period",
        }
    elif hours_left >= 24:
        return {
            "amount": contribution,
            "label":  f"Partial refund — {contribution:,} ISK",
            "policy": "Service fee is non-refundable",
        }
    elif hours_left > 0:
        half = round(contribution * 0.5)
        return {
            "amount": half,
            "label":  f"Partial refund — {half:,} ISK",
            "policy": "Less than 24 hours before departure — 50% of contribution",
        }
    else:
        return {
            "amount": 0,
            "label":  "No refund",
            "policy": "Trip has already departed",
        }


@router.get("/{booking_id}/cancel", response_class=HTMLResponse)
def cancel_booking_page(
    booking_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    cancellable = (models.BookingStatus.awaiting_payment,
                   models.BookingStatus.pending,
                   models.BookingStatus.confirmed)
    if not booking or booking.passenger_id != current_user.id or booking.status not in cancellable:
        return RedirectResponse("/bookings", status_code=303)

    return templates.TemplateResponse("bookings/cancel_confirm.html", {
        **ctx,
        "booking": booking,
        "refund":  _refund_preview(booking),
    })


@router.post("/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)

    cancellable = (models.BookingStatus.awaiting_payment,
                   models.BookingStatus.pending,
                   models.BookingStatus.confirmed)
    if booking.status not in cancellable:
        return RedirectResponse("/bookings", status_code=303)

    seats_were_held = booking.status != models.BookingStatus.pending
    if seats_were_held:
        booking.trip.seats_available = min(
            booking.trip.seats_total,
            booking.trip.seats_available + booking.seats_booked,
        )
    booking.status = models.BookingStatus.cancelled

    if booking.payment:
        preview = _refund_preview(booking)
        refund  = preview["amount"]
        booking.payment.refund_amount = refund
        booking.payment.status = (
            models.PaymentStatus.refunded       if refund == booking.payment.passenger_total else
            models.PaymentStatus.partial_refund if refund > 0 else
            models.PaymentStatus.partial_refund
        )

    db.commit()
    db.refresh(booking)
    if seats_were_held:
        mailer.booking_cancelled_to_driver(booking)
    mailer.booking_cancelled_to_passenger(booking)
    return RedirectResponse("/bookings?cancelled=1", status_code=303)


@router.post("/{booking_id}/confirm")
def confirm_booking(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if booking and booking.trip.driver_id == current_user.id:
        if booking.status == models.BookingStatus.pending:
            # Check there are still enough seats (no seats held yet for manual-approval trips)
            if booking.trip.seats_available >= booking.seats_booked:
                booking.trip.seats_available = max(
                    0, booking.trip.seats_available - booking.seats_booked
                )
                booking.status = models.BookingStatus.awaiting_payment
                db.commit()
                db.refresh(booking)
                mailer.booking_approved_to_passenger(booking)
            # If not enough seats, silently do nothing (driver sees it's still pending)
    return RedirectResponse("/my-trips?tab=rides", status_code=303)


@router.post("/{booking_id}/no-show")
def mark_passenger_no_show(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Driver marks a confirmed passenger as a no-show (only after departure)."""
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if (not booking
            or booking.trip.driver_id != current_user.id
            or booking.status != models.BookingStatus.confirmed
            or datetime.utcnow() < booking.trip.departure_datetime + timedelta(minutes=15)):
        return RedirectResponse("/my-trips?tab=rides", status_code=303)

    booking.status = models.BookingStatus.no_show
    # Passenger forfeits their contribution — no refund issued
    db.commit()
    return RedirectResponse(f"/trips/{booking.trip_id}?no_show=1", status_code=303)


@router.post("/{booking_id}/driver-no-show")
def report_driver_no_show(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Passenger reports the driver as a no-show (only after departure). Issues full refund."""
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if (not booking
            or booking.passenger_id != current_user.id
            or booking.status != models.BookingStatus.confirmed
            or datetime.utcnow() < booking.trip.departure_datetime + timedelta(minutes=15)):
        return RedirectResponse("/my-trips?tab=bookings", status_code=303)

    # Flag the trip so the driver can be penalised by auto-ratings
    booking.trip.driver_no_show = True
    # Cancel the booking and issue a full refund
    booking.status = models.BookingStatus.cancelled
    if booking.payment:
        booking.payment.refund_amount = booking.payment.passenger_total
        booking.payment.status = models.PaymentStatus.refunded

    # Issue an immediate 1-star auto-review for the driver (no grace period for no-shows)
    existing_review = (
        db.query(models.Review)
        .filter(
            models.Review.booking_id  == booking.id,
            models.Review.review_type == models.ReviewType.passenger_to_driver,
        )
        .first()
    )
    if not existing_review:
        db.add(models.Review(
            booking_id  = booking.id,
            trip_id     = booking.trip_id,
            reviewer_id = current_user.id,
            reviewee_id = booking.trip.driver_id,
            review_type = models.ReviewType.passenger_to_driver,
            rating      = 1,
            is_auto     = True,
        ))

    db.commit()
    return RedirectResponse("/my-trips?tab=bookings&driver_no_show=1", status_code=303)


@router.post("/{booking_id}/reject")
def reject_booking(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if booking and booking.trip.driver_id == current_user.id:
        if booking.status == models.BookingStatus.pending:
            # Pending on manual-approval trips never held seats — nothing to release
            booking.status = models.BookingStatus.rejected
            db.commit()
    return RedirectResponse("/my-trips?tab=rides", status_code=303)
