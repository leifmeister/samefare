"""
Blikk-specific endpoints.

GET  /bookings/{id}/blikk-return          — Blikk redirects the passenger here
                                            after they complete (or abandon) payment.
GET  /bookings/{id}/blikk-pay             — "Pay via Blikk" landing page for
                                            non-instant trips that have been approved.
POST /bookings/{id}/blikk-request-fare    — Driver triggers the fare payment at ride time.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app import models, email as mailer
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app import blikk as blikk_client
from app.blikk import BlikkError
from app.utils import build_route_graph, recompute_seats_available

log       = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
router    = APIRouter(tags=["blikk"])

_SEAT_HOLDING_STATUSES = frozenset({
    models.BookingStatus.awaiting_payment,
    models.BookingStatus.confirmed,
    models.BookingStatus.card_saved,
})


def _refresh_seats(trip: models.Trip, db: Session) -> None:
    graph  = build_route_graph(db)
    active = [b for b in trip.bookings if b.status in _SEAT_HOLDING_STATUSES]
    trip.seats_available = recompute_seats_available(
        graph, trip.seats_total, active, trip.origin, trip.destination,
    )


# ── Return URL — Blikk redirects here after the passenger pays ─────────────────

@router.get("/bookings/{booking_id}/blikk-return", response_class=HTMLResponse)
def blikk_return(
    booking_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    flow: str = "fee",   # "fee" (service fee) or "fare" (driver fare at ride time)
):
    """
    Handle the Blikk redirect-back after payment.

    We verify the payment status via GET /payment/{id} before confirming anything.
    Blikk may append query params (status, paymentId, etc.) but we ignore them in
    favour of the authoritative server-side check.
    """
    booking = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.trip),
            joinedload(models.Booking.payment),
            joinedload(models.Booking.passenger),
        )
        .filter(models.Booking.id == booking_id)
        .first()
    )

    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/my-trips?tab=bookings", status_code=303)

    payment = booking.payment

    # ── Fee return ─────────────────────────────────────────────────────────────
    if flow == "fee":
        if not payment or not payment.blikk_fee_payment_id:
            return templates.TemplateResponse("payments/blikk_return.html", {
                **ctx,
                "booking":  booking,
                "success":  False,
                "flow":     flow,
                "message":  "Payment record not found.",
            }, status_code=400)

        try:
            blikk_payment = blikk_client.get_payment(payment.blikk_fee_payment_id)
        except BlikkError as exc:
            log.error("Blikk get_payment failed for booking %d: %s", booking_id, exc)
            return templates.TemplateResponse("payments/blikk_return.html", {
                **ctx,
                "booking":  booking,
                "success":  False,
                "flow":     flow,
                "message":  "Could not verify payment status. Please contact support.",
            })

        if blikk_client.is_completed(blikk_payment):
            # Confirm the booking
            payment.status = models.PaymentStatus.blikk_fee_paid
            if booking.status == models.BookingStatus.awaiting_payment:
                booking.status = models.BookingStatus.confirmed
                # Recompute seats now that booking is confirmed
                trip = (
                    db.query(models.Trip)
                    .filter(models.Trip.id == booking.trip_id)
                    .with_for_update()
                    .first()
                )
                if trip:
                    _refresh_seats(trip, db)
            db.commit()
            try:
                mailer.booking_confirmed_to_passenger(booking)
                mailer.booking_confirmed_to_driver(booking)
            except Exception:
                pass
            return templates.TemplateResponse("payments/blikk_return.html", {
                **ctx,
                "booking": booking,
                "success": True,
                "flow":    flow,
                "message": "Payment received — your seat is confirmed!",
            })
        elif blikk_client.is_failed(blikk_payment):
            payment.status = models.PaymentStatus.failed
            # Leave booking as awaiting_payment so passenger can retry
            db.commit()
            return templates.TemplateResponse("payments/blikk_return.html", {
                **ctx,
                "booking":  booking,
                "success":  False,
                "flow":     flow,
                "message":  "Payment failed or was cancelled. You can try again below.",
            })
        else:
            # Still pending — Blikk may send an in-app notification
            return templates.TemplateResponse("payments/blikk_return.html", {
                **ctx,
                "booking":  booking,
                "success":  False,
                "flow":     flow,
                "message":  "Payment is being processed. We will confirm your booking shortly.",
                "pending":  True,
            })

    # ── Fare return (Flow 2) ───────────────────────────────────────────────────
    if flow == "fare":
        if not payment or not payment.blikk_fare_payment_id:
            return RedirectResponse(f"/trips/{booking.trip_id}", status_code=303)

        try:
            blikk_payment = blikk_client.get_payment(payment.blikk_fare_payment_id)
        except BlikkError as exc:
            log.error("Blikk get_payment (fare) failed for booking %d: %s", booking_id, exc)
            return RedirectResponse(f"/trips/{booking.trip_id}?blikk_fare_error=1", status_code=303)

        if blikk_client.is_completed(blikk_payment):
            payment.status = models.PaymentStatus.blikk_fare_paid
            db.commit()
            return RedirectResponse(f"/trips/{booking.trip_id}?blikk_fare_done=1", status_code=303)
        elif blikk_client.is_failed(blikk_payment):
            payment.status = models.PaymentStatus.failed
            db.commit()
            return RedirectResponse(f"/trips/{booking.trip_id}?blikk_fare_error=1", status_code=303)
        else:
            # Still pending — passenger may not have approved yet
            return RedirectResponse(f"/trips/{booking.trip_id}?blikk_fare_pending=1", status_code=303)

    return RedirectResponse("/my-trips?tab=bookings", status_code=303)


# ── Pay-via-Blikk page (non-instant trips, driver has approved) ────────────────

@router.get("/bookings/{booking_id}/blikk-pay", response_class=HTMLResponse)
def blikk_pay_page(
    booking_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Landing page shown when a driver approves a Blikk booking and the passenger
    needs to pay the service fee.  Creates the Blikk payment and redirects
    the passenger into the Blikk payment flow immediately.
    """
    booking = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.trip),
            joinedload(models.Booking.payment),
            joinedload(models.Booking.passenger),
        )
        .filter(models.Booking.id == booking_id)
        .first()
    )

    if (
        not booking
        or booking.passenger_id != current_user.id
        or booking.status != models.BookingStatus.awaiting_payment
    ):
        return RedirectResponse("/my-trips?tab=bookings", status_code=303)

    trip = booking.trip
    if not trip or booking.payment_method != models.TripPaymentMethod.blikk:
        return RedirectResponse("/my-trips?tab=bookings", status_code=303)

    settings = get_settings()

    # Idempotent: if a Blikk fee payment was already created for this booking,
    # re-initialise and redirect again (handles browser back + retry).
    payment = booking.payment
    if payment and payment.blikk_fee_payment_id:
        try:
            blikk_payment = blikk_client.get_payment(payment.blikk_fee_payment_id)
            if blikk_client.is_completed(blikk_payment):
                # Already paid — confirm and redirect
                payment.status = models.PaymentStatus.blikk_fee_paid
                booking.status = models.BookingStatus.confirmed
                db.commit()
                return RedirectResponse("/my-trips?tab=bookings&blikk_paid=1", status_code=303)
        except BlikkError:
            pass  # fall through and create a new payment

    try:
        payment_id, redirect_url = blikk_client.create_fee_payment(booking, settings.base_url)
    except BlikkError as exc:
        log.error("Blikk create_fee_payment failed for booking %d: %s", booking_id, exc)
        return templates.TemplateResponse("payments/blikk_return.html", {
            **ctx,
            "booking":  booking,
            "success":  False,
            "flow":     "fee",
            "message":  f"Could not initiate Blikk payment: {exc}. Please contact support.",
        }, status_code=502)

    # Persist the payment record
    if not payment:
        payment = models.Payment(
            booking_id      = booking.id,
            passenger_total = booking.total_price,
            driver_payout   = booking.subtotal,
            platform_fee    = booking.service_fee,
            status          = models.PaymentStatus.blikk_fee_pending,
            blikk_fee_payment_id = payment_id,
        )
        db.add(payment)
    else:
        payment.blikk_fee_payment_id = payment_id
        payment.status = models.PaymentStatus.blikk_fee_pending
    db.commit()

    if redirect_url:
        return RedirectResponse(redirect_url, status_code=303)

    # Blikk didn't return a URL — show a pending page
    return templates.TemplateResponse("payments/blikk_return.html", {
        **ctx,
        "booking":  booking,
        "success":  False,
        "flow":     "fee",
        "pending":  True,
        "message":  "Payment request sent. Please complete it in your Blikk app.",
    })


# ── Driver triggers fare payment at ride time ──────────────────────────────────

@router.post("/bookings/{booking_id}/blikk-request-fare")
def blikk_request_fare(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Driver clicks "Request payment" when the passenger gets in the car.

    Creates a Blikk P2P: passenger → driver, amount = driver payout.
    The passenger receives a push notification in their Blikk app.
    """
    booking = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.trip),
            joinedload(models.Booking.payment),
            joinedload(models.Booking.passenger),
        )
        .filter(models.Booking.id == booking_id)
        .first()
    )

    if (
        not booking
        or not booking.trip
        or booking.trip.driver_id != current_user.id
        or booking.status != models.BookingStatus.confirmed
    ):
        return RedirectResponse(f"/trips/{booking.trip_id if booking else ''}", status_code=303)

    settings = get_settings()
    driver_phone = current_user.phone
    if not driver_phone:
        return RedirectResponse(
            f"/trips/{booking.trip_id}?blikk_fare_error=no_phone",
            status_code=303,
        )

    payment = booking.payment
    if payment and payment.blikk_fare_payment_id:
        # Already requested — redirect back to trip
        return RedirectResponse(
            f"/trips/{booking.trip_id}?blikk_fare_done=1",
            status_code=303,
        )

    try:
        fare_id, _ = blikk_client.create_fare_payment(booking, driver_phone, settings.base_url)
    except BlikkError as exc:
        log.error("Blikk create_fare_payment failed for booking %d: %s", booking_id, exc)
        return RedirectResponse(
            f"/trips/{booking.trip_id}?blikk_fare_error=1",
            status_code=303,
        )

    if not payment:
        payment = models.Payment(
            booking_id      = booking.id,
            passenger_total = booking.total_price,
            driver_payout   = booking.subtotal,
            platform_fee    = booking.service_fee,
            status          = models.PaymentStatus.blikk_fare_pending,
            blikk_fare_payment_id = fare_id,
        )
        db.add(payment)
    else:
        payment.blikk_fare_payment_id = fare_id
        payment.status = models.PaymentStatus.blikk_fare_pending
    db.commit()

    return RedirectResponse(
        f"/trips/{booking.trip_id}?blikk_fare_requested=1",
        status_code=303,
    )
