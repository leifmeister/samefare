from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app import models
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app.routers.payments import calc_fees

settings = get_settings()
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.get("", response_class=HTMLResponse)
def my_bookings(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bookings = (
        db.query(models.Booking)
        .filter(models.Booking.passenger_id == current_user.id)
        .order_by(models.Booking.created_at.desc())
        .all()
    )
    return templates.TemplateResponse("bookings/list.html", {**ctx, "bookings": bookings})


@router.get("/trip/{trip_id}", response_class=HTMLResponse)
def book_trip_page(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        return templates.TemplateResponse("errors/404.html", {**ctx}, status_code=404)
    if trip.driver_id == current_user.id:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)
    if trip.status != models.TripStatus.active or trip.seats_available < 1:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    return templates.TemplateResponse("bookings/create.html", {
        **ctx, "trip": trip, "error": None,
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

    err_ctx = {**ctx, "trip": trip}

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

    contribution        = trip.price_per_seat * seats_booked
    service_fee, total, _ = calc_fees(contribution)

    booking = models.Booking(
        trip_id=trip_id,
        passenger_id=current_user.id,
        seats_booked=seats_booked,
        total_price=total,
        service_fee=service_fee,
        message=message or None,
        status=models.BookingStatus.awaiting_payment,
    )
    db.add(booking)
    # Hold seats while passenger completes payment
    trip.seats_available = max(0, trip.seats_available - seats_booked)
    db.commit()
    db.refresh(booking)
    return RedirectResponse(f"/payments/checkout/{booking.id}", status_code=303)


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

    # Release held seats
    booking.trip.seats_available = min(
        booking.trip.seats_total,
        booking.trip.seats_available + booking.seats_booked,
    )
    booking.status = models.BookingStatus.cancelled

    # Apply refund policy if payment exists
    if booking.payment:
        now       = datetime.utcnow()
        departure = booking.trip.departure_datetime
        hours_left = (departure - now).total_seconds() / 3600
        contribution = booking.payment.driver_payout

        if hours_left >= 24:
            refund = contribution          # full contribution back
            booking.payment.status = models.PaymentStatus.refunded
        elif hours_left > 0:
            refund = round(contribution * 0.5)   # 50 % back
            booking.payment.status = models.PaymentStatus.partial_refund
        else:
            refund = 0                     # no refund after departure
            booking.payment.status = models.PaymentStatus.partial_refund

        booking.payment.refund_amount = refund

    db.commit()
    return RedirectResponse("/bookings", status_code=303)


@router.post("/{booking_id}/confirm")
def confirm_booking(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if booking and booking.trip.driver_id == current_user.id:
        if booking.status == models.BookingStatus.pending:
            booking.status = models.BookingStatus.confirmed
            db.commit()
    return RedirectResponse("/profile", status_code=303)


@router.post("/{booking_id}/reject")
def reject_booking(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if booking and booking.trip.driver_id == current_user.id:
        if booking.status == models.BookingStatus.pending:
            # Release seats back
            booking.trip.seats_available = min(
                booking.trip.seats_total,
                booking.trip.seats_available + booking.seats_booked,
            )
            booking.status = models.BookingStatus.rejected
            db.commit()
    return RedirectResponse("/profile", status_code=303)
