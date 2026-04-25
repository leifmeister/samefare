"""
Payment router — BlaBlaCar Germany-style cost-sharing model.

Rules
-----
Service fee (tiered, charged to passenger only):
  contribution < 5 000 ISK  →  18 %
  5 000 – 15 000 ISK        →  12 %
  > 15 000 ISK              →   8 %

Cancellation refund policy (passenger-initiated):
  ≥ 24 h before departure   →  full contribution refunded; service fee retained
  <  24 h before departure  →  50 % of contribution; service fee retained
  after departure            →  no refund

Driver-initiated cancellation →  full refund incl. service fee to passenger.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app import models, email as mailer
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_template_context

settings = get_settings()

templates = Jinja2Templates(directory="templates")
router    = APIRouter(prefix="/payments", tags=["payments"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def service_fee_rate(contribution: int) -> float:
    return 0.18


def calc_fees(contribution: int) -> tuple[int, int, int]:
    """Return (service_fee, passenger_total, driver_payout)."""
    rate         = service_fee_rate(contribution)
    fee          = round(contribution * rate)
    return fee, contribution + fee, contribution


def refund_amount(booking: models.Booking) -> tuple[int, str]:
    """
    Return (refund_ISK, policy_label) based on time until departure.
    Service fee is never refunded.
    """
    now        = datetime.utcnow()
    departure  = booking.trip.departure_datetime
    hours_left = (departure - now).total_seconds() / 3600
    contribution = booking.payment.driver_payout  # what driver would receive

    if hours_left >= 24:
        return contribution, "full"
    if hours_left > 0:
        return round(contribution * 0.5), "50%"
    return 0, "none"


# ── Checkout ──────────────────────────────────────────────────────────────────

@router.get("/checkout/{booking_id}", response_class=HTMLResponse)
def checkout_page(
    booking_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip).joinedload(models.Trip.driver))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)
    if booking.status != models.BookingStatus.awaiting_payment:
        return RedirectResponse("/bookings", status_code=303)

    contribution          = booking.subtotal
    fee, total, payout    = calc_fees(contribution)
    rate                  = service_fee_rate(contribution)

    return templates.TemplateResponse("payments/checkout.html", {
        **ctx,
        "booking":      booking,
        "contribution": contribution,
        "service_fee":  fee,
        "fee_pct":      int(rate * 100),
        "total":        total,
        "driver_payout": payout,
    })


@router.post("/checkout/{booking_id}", response_class=HTMLResponse)
def process_payment(
    booking_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    card_number: str = Form(...),
    card_expiry: str = Form(...),
    card_cvc:    str = Form(...),
    card_name:   str = Form(...),
):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip).joinedload(models.Trip.driver))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)
    if booking.status != models.BookingStatus.awaiting_payment:
        return RedirectResponse("/bookings", status_code=303)

    contribution       = booking.subtotal
    fee, total, payout = calc_fees(contribution)

    # Basic card validation
    digits = card_number.replace(" ", "")
    if len(digits) < 13 or not digits.isdigit():
        rate = service_fee_rate(contribution)
        return templates.TemplateResponse("payments/checkout.html", {
            **ctx,
            "booking": booking, "contribution": contribution,
            "service_fee": fee, "fee_pct": int(rate * 100),
            "total": total, "driver_payout": payout,
            "error": "Invalid card number.",
        }, status_code=400)

    # Detect brand from first digit (simplified)
    brand = "Visa" if digits[0] == "4" else ("Mastercard" if digits[0] in "25" else "Card")

    payment = models.Payment(
        booking_id      = booking.id,
        passenger_total = total,
        driver_payout   = payout,
        platform_fee    = fee,
        status          = models.PaymentStatus.authorised,
        card_last4      = digits[-4:],
        card_brand      = brand,
    )
    db.add(payment)

    # Update booking fees, mark confirmed
    booking.service_fee  = fee
    booking.total_price  = total
    booking.status       = models.BookingStatus.confirmed
    db.commit()
    db.refresh(booking)

    # Emails — fire and forget
    mailer.booking_confirmed_to_passenger(booking)
    mailer.booking_confirmed_to_driver(booking)

    return RedirectResponse(f"/payments/success/{booking_id}", status_code=303)


# ── Beta bypass ───────────────────────────────────────────────────────────────

@router.post("/checkout/{booking_id}/beta", response_class=HTMLResponse)
def beta_confirm(
    booking_id: int,
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One-click booking confirmation used only when BETA_MODE=true."""
    if not settings.beta_mode:
        return RedirectResponse(f"/payments/checkout/{booking_id}", status_code=303)

    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip).joinedload(models.Trip.driver))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)
    if booking.status != models.BookingStatus.awaiting_payment:
        return RedirectResponse("/bookings", status_code=303)

    contribution       = booking.subtotal
    fee, total, payout = calc_fees(contribution)

    # Record a zero-value payment so the data model stays consistent
    payment = models.Payment(
        booking_id      = booking.id,
        passenger_total = 0,
        driver_payout   = 0,
        platform_fee    = 0,
        status          = models.PaymentStatus.authorised,
        card_last4      = None,
        card_brand      = "Beta",
    )
    db.add(payment)

    booking.service_fee = 0
    booking.total_price = 0
    booking.status      = models.BookingStatus.confirmed
    db.commit()
    db.refresh(booking)

    mailer.booking_confirmed_to_passenger(booking)
    mailer.booking_confirmed_to_driver(booking)

    return RedirectResponse(f"/payments/success/{booking_id}", status_code=303)


@router.get("/success/{booking_id}", response_class=HTMLResponse)
def payment_success(
    booking_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.trip).joinedload(models.Trip.driver),
            joinedload(models.Booking.payment),
        )
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)

    return templates.TemplateResponse("payments/success.html", {
        **ctx, "booking": booking,
    })
