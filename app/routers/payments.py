"""
Payment router — Rapyd-backed authorise-then-capture model.

Case A (ride ≤ 7 days away)
    Passenger completes an embedded Rapyd checkout; card is authorised now
    (capture=false).  Seat is confirmed immediately on authorisation.
    Capture fires at departure_datetime via the background task.

Case B (ride > 7 days away)
    Passenger saves their card via an embedded Rapyd SCA-authenticated checkout
    (amount=0, save_payment_method=true).  A Rapyd customer & PM token are
    stored.  A background task fires a merchant-initiated authorisation
    (MIT) 24 h before departure.  On success the booking is confirmed; on
    failure the passenger gets a 2-hour SMS window to update their card
    (+5 % service fee surcharge).

Rules
-----
- Never confirm a booking from a redirect alone — wait for the webhook.
- Never store raw card details.
- Use idempotency keys for every Rapyd create/capture call.
- Beta mode bypasses Rapyd entirely (one-click confirm, zero charge).
"""

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app import models, email as mailer
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app import rapyd as rapyd_client
from app.rapyd import RapydError, generate_idempotency_key

settings  = get_settings()
templates = Jinja2Templates(directory="templates")
router    = APIRouter(prefix="/payments", tags=["payments"])
log       = logging.getLogger(__name__)

# How many days out before we switch from Case A (auth now) to Case B (save card)
CASE_B_THRESHOLD_DAYS = 7


# ── Helpers ───────────────────────────────────────────────────────────────────

def _expire_booking(db: Session, booking: models.Booking) -> None:
    """
    Cancel an expired awaiting-payment booking and release held seats.
    Idempotent — status check prevents double-cancellation.
    Caller is responsible for db.commit().
    """
    if booking.status != models.BookingStatus.awaiting_payment:
        return
    booking.status = models.BookingStatus.cancelled
    if booking.trip.status == models.TripStatus.active:
        trip = (
            db.query(models.Trip)
            .filter(models.Trip.id == booking.trip_id)
            .with_for_update()
            .first()
        )
        if trip:
            trip.seats_available = min(
                trip.seats_total,
                trip.seats_available + booking.seats_booked,
            )


def service_fee_rate(contribution: int, retry_surcharge: bool = False) -> float:
    """18 % normally; 23 % when a passenger retries after a failed MIT."""
    return 0.23 if retry_surcharge else 0.18


def calc_fees(
    contribution: int,
    retry_surcharge: bool = False,
) -> tuple[int, int, int]:
    """Return (service_fee, passenger_total, driver_payout)."""
    rate    = service_fee_rate(contribution, retry_surcharge)
    fee     = round(contribution * rate)
    return fee, contribution + fee, contribution


def _payment_case(departure_datetime: datetime) -> str:
    """
    Return 'A' if departure is ≤7 days away, 'B' if further.

    Uses timedelta comparison rather than .days so that fractional days are
    counted correctly — e.g. 7 days 23 hours is >7 days and must use Case B,
    because a Case A authorisation would expire before the trip departs.
    """
    return "A" if (departure_datetime - datetime.utcnow()) <= timedelta(days=CASE_B_THRESHOLD_DAYS) else "B"


def _get_or_create_payment(
    db: Session,
    booking: models.Booking,
) -> models.Payment:
    """
    Return the existing Payment record for this booking, or create a minimal
    one (status=pending) if none exists yet.
    """
    if booking.payment:
        return booking.payment

    contribution = booking.subtotal
    fee          = booking.service_fee
    total        = booking.total_price
    payout       = contribution
    case         = _payment_case(booking.trip.departure_datetime)

    payment = models.Payment(
        booking_id      = booking.id,
        passenger_total = total,
        driver_payout   = payout,
        platform_fee    = fee,
        status          = models.PaymentStatus.pending,
        idempotency_key = generate_idempotency_key(),
        payment_case    = case,
        capture_at      = booking.trip.departure_datetime,
        auth_scheduled_for = (
            booking.trip.departure_datetime - timedelta(hours=24)
            if case == "B" else None
        ),
    )
    db.add(payment)
    db.flush()   # gives payment.id without committing
    return payment


def _issue_rapyd_refund(
    db: Session,
    booking: models.Booking,
    amount: int,
    reason: str = "requested_by_customer",
) -> None:
    """
    Record a refund intent on the Payment record.  Does NOT call Rapyd.

    Callers must NOT pre-set payment.refund_amount or payment.status before
    calling this function — those fields are written here and only here.
    Caller must db.commit() afterwards.

    Why no direct Rapyd call here
    ------------------------------
    A direct call inside the request handler creates two failure windows:

      1. Process dies between the API call and the caller's db.commit()
         → Rapyd has the refund; DB has nothing; passenger money returned
           with no local record and PayoutItem not cancelled.

      2. Rapyd succeeds but db.commit() fails for any reason
         → Same unrecoverable state.

    Instead, this function records the intent in refund_requested status and
    returns.  The caller's single db.commit() lands the booking cancellation
    and the refund intent atomically.  _run_retry_refunds() then calls Rapyd
    using the stable idempotency key f"refund-{booking.id}-{amount}", handles
    handle_refund_payout_impact(), and commits the terminal status — all in
    one place, with safe retry on every failure mode.

    State machine
    -------------
    amount == 0 or no payment          → no-op; status unchanged
    no rapyd_payment_id (beta/pre-auth)→ immediately refunded/partial_refund
                                         (no Rapyd call; no real money moved)
    has rapyd_payment_id               → refund_requested
                                         (_run_retry_refunds completes the flow)
    """
    payment = booking.payment
    if not payment or amount <= 0:
        return

    payment.refund_amount = amount

    if not payment.rapyd_payment_id:
        # Beta mode or pre-authorisation cancellation — no Rapyd payment exists.
        # Treat as immediately terminal; no network call or retry needed.
        payment.status = (
            models.PaymentStatus.refunded
            if amount >= payment.passenger_total
            else models.PaymentStatus.partial_refund
        )
        return

    # Durable intent — caller commits this together with the booking state
    # change (cancellation, seat restoration) before any provider call is made.
    payment.status = models.PaymentStatus.refund_requested
    log.info(
        "Refund intent recorded: booking=%s amount=%s ISK — "
        "retry task will submit to Rapyd",
        booking.id, amount,
    )


# ── Checkout page ─────────────────────────────────────────────────────────────

@router.get("/checkout/{booking_id}", response_class=HTMLResponse)
def checkout_page(
    booking_id: int,
    request:    Request,
    ctx:        dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:         Session = Depends(get_db),
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
    if booking.status not in (
        models.BookingStatus.awaiting_payment,
        models.BookingStatus.card_saved,   # allow re-visit for Case B (shows status)
    ):
        return RedirectResponse("/bookings", status_code=303)

    # Block checkout if the trip has already departed — regardless of deadline.
    if booking.trip.departure_datetime <= datetime.utcnow():
        if booking.status == models.BookingStatus.awaiting_payment:
            _expire_booking(db, booking)
            db.commit()
        return RedirectResponse("/bookings?payment_expired=1", status_code=303)

    # Expired payment window (awaiting_payment only — card_saved has no deadline)
    if (
        booking.status == models.BookingStatus.awaiting_payment
        and booking.payment_deadline
        and datetime.utcnow() > booking.payment_deadline
    ):
        _expire_booking(db, booking)
        db.commit()
        return RedirectResponse("/bookings?payment_expired=1", status_code=303)

    # If already past the card-saved stage, just show the status page.
    # Exception: retry_pending means the MIT failed and retry_payment() has
    # already created a fresh checkout page — fall through so the passenger
    # can actually see the card-update form.
    if booking.status == models.BookingStatus.card_saved:
        payment_for_check = booking.payment
        if (not payment_for_check
                or payment_for_check.status != models.PaymentStatus.retry_pending):
            return RedirectResponse(
                f"/payments/card-saved/{booking_id}", status_code=303
            )

    # ── Beta mode: skip Rapyd entirely ────────────────────────────────────────
    if settings.beta_mode:
        contribution = booking.subtotal
        fee          = booking.service_fee
        total        = booking.total_price
        fee_pct      = round(fee / contribution * 100) if contribution and fee else 0
        return templates.TemplateResponse("payments/checkout.html", {
            **ctx,
            "booking":      booking,
            "contribution": contribution,
            "service_fee":  fee,
            "fee_pct":      fee_pct,
            "total":        total,
            "driver_payout": contribution,
            "payment_case": "A",
            "checkout_id":  None,
            "rapyd_js_url": None,
        })

    # ── Real Rapyd flow ───────────────────────────────────────────────────────
    payment = _get_or_create_payment(db, booking)
    case    = payment.payment_case or _payment_case(booking.trip.departure_datetime)

    # Commit the new Payment record (flushed but not yet committed by
    # _get_or_create_payment) so it survives any subsequent API failure.
    # Must happen before any Rapyd call so a rollback on API error does not
    # wipe the record.
    db.commit()

    s            = get_settings()
    complete_url = f"{s.base_url}/payments/complete/{booking_id}"
    cancel_url   = f"{s.base_url}/payments/checkout/{booking_id}"

    def _checkout_error_response(rapyd_error: str = "Payment system temporarily unavailable. Please try again."):
        return templates.TemplateResponse("payments/checkout.html", {
            **ctx,
            "booking":       booking,
            "contribution":  booking.subtotal,
            "service_fee":   booking.service_fee,
            "fee_pct":       round(booking.service_fee / booking.subtotal * 100) if booking.subtotal else 0,
            "total":         booking.total_price,
            "driver_payout": booking.subtotal,
            "payment_case":  case,
            "checkout_id":   None,
            "rapyd_js_url":  rapyd_client.js_url(),
            "rapyd_error":   rapyd_error,
        })

    # Reuse existing checkout page if already created (page refresh / back button)
    if not payment.rapyd_checkout_id:
        if case == "B":
            # ── Phase 1: ensure a durable Rapyd customer exists ───────────────
            # customer creation and its DB commit are separated from checkout
            # creation so that a checkout failure does not orphan the customer.
            # The stable idempotency key means a retry returns the same Rapyd
            # customer rather than opening a duplicate.
            if not payment.rapyd_customer_id:
                try:
                    customer_id = rapyd_client.create_customer(
                        email             = current_user.email,
                        name              = current_user.full_name,
                        idempotency_key   = f"customer-{payment.idempotency_key}",
                    )
                except RapydError as exc:
                    log.error(
                        "Rapyd customer create failed for booking %s: %s",
                        booking_id, exc,
                    )
                    return _checkout_error_response()

                payment.rapyd_customer_id = customer_id
                try:
                    # Commit the customer ID now — before checkout creation.
                    # If checkout subsequently fails the ID is preserved and the
                    # next page load reuses the existing Rapyd customer.
                    db.commit()
                except Exception as exc:
                    log.error(
                        "DB commit for rapyd_customer_id failed (booking %s): %s",
                        booking_id, exc,
                    )
                    db.rollback()
                    return _checkout_error_response()
            else:
                customer_id = payment.rapyd_customer_id

            # ── Phase 2: create checkout (customer already durable) ───────────
            try:
                checkout_data = rapyd_client.create_checkout_page(
                    amount               = 0,
                    capture              = True,
                    complete_url         = f"{s.base_url}/payments/card-saved/{booking_id}",
                    cancel_url           = cancel_url,
                    idempotency_key      = payment.idempotency_key,
                    metadata             = {"booking_id": booking_id, "case": "B"},
                    customer_id          = customer_id,
                    save_payment_method  = True,
                )
                payment.rapyd_checkout_id = checkout_data["id"]
                db.commit()
            except RapydError as exc:
                log.error(
                    "Rapyd checkout create failed for booking %s (Case B): %s",
                    booking_id, exc,
                )
                db.rollback()
                return _checkout_error_response()

        else:
            # ── Case A: authorise full amount with capture=False ──────────────
            try:
                checkout_data = rapyd_client.create_checkout_page(
                    amount          = booking.total_price,
                    capture         = False,
                    complete_url    = complete_url,
                    cancel_url      = cancel_url,
                    idempotency_key = payment.idempotency_key,
                    metadata        = {"booking_id": booking_id, "case": "A"},
                )
                payment.rapyd_checkout_id = checkout_data["id"]
                db.commit()
            except RapydError as exc:
                log.error(
                    "Rapyd checkout create failed for booking %s (Case A): %s",
                    booking_id, exc,
                )
                db.rollback()
                return _checkout_error_response()

    contribution = booking.subtotal
    fee          = booking.service_fee
    total        = booking.total_price
    fee_pct      = round(fee / contribution * 100) if contribution and fee else 0

    return templates.TemplateResponse("payments/checkout.html", {
        **ctx,
        "booking":       booking,
        "contribution":  contribution,
        "service_fee":   fee,
        "fee_pct":       fee_pct,
        "total":         total,
        "driver_payout": contribution,
        "payment_case":  case,
        "checkout_id":   payment.rapyd_checkout_id,
        "rapyd_js_url":  rapyd_client.js_url(),
    })


# ── Beta bypass ───────────────────────────────────────────────────────────────

@router.post("/checkout/{booking_id}/beta", response_class=HTMLResponse)
def beta_confirm(
    booking_id:   int,
    request:      Request,
    current_user: models.User = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """One-click booking confirmation — only when BETA_MODE=true."""
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

    payment = models.Payment(
        booking_id      = booking.id,
        passenger_total = 0,
        driver_payout   = 0,
        platform_fee    = 0,
        status          = models.PaymentStatus.authorised,
        card_brand      = "Beta",
        payment_case    = "A",
        capture_at      = booking.trip.departure_datetime,
    )
    db.add(payment)

    had_discount        = booking.service_fee == 0
    booking.service_fee = 0
    booking.total_price = 0
    booking.status      = models.BookingStatus.confirmed

    if had_discount:
        sub = (
            db.query(models.NewsletterSubscriber)
            .filter(
                models.NewsletterSubscriber.email         == booking.passenger.email,
                models.NewsletterSubscriber.discount_used == False,  # noqa: E712
            )
            .first()
        )
        if sub:
            sub.discount_used = True

    db.commit()
    db.refresh(booking)

    mailer.booking_confirmed_to_passenger(booking)
    mailer.booking_confirmed_to_driver(booking)

    return RedirectResponse(f"/payments/success/{booking_id}", status_code=303)


# ── Post-checkout redirect targets ────────────────────────────────────────────

@router.get("/complete/{booking_id}", response_class=HTMLResponse)
def payment_complete_redirect(
    booking_id:   int,
    request:      Request,
    ctx:          dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Rapyd redirects here after the Case A checkout flow.
    Never confirm from the redirect alone — check actual booking status.
    If webhook has already fired → redirect to success.
    Otherwise → show the processing/polling page.
    """
    booking = (
        db.query(models.Booking)
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)

    if booking.status == models.BookingStatus.confirmed:
        return RedirectResponse(f"/payments/success/{booking_id}", status_code=303)

    if booking.status == models.BookingStatus.cancelled:
        return RedirectResponse("/bookings?payment_expired=1", status_code=303)

    # Webhook not yet received — show polling page
    return templates.TemplateResponse("payments/processing.html", {
        **ctx, "booking": booking,
    })


@router.get("/card-saved/{booking_id}", response_class=HTMLResponse)
def card_saved_page(
    booking_id:   int,
    request:      Request,
    ctx:          dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Rapyd redirects here after the Case B save-card checkout.
    If the webhook has fired already, booking will be card_saved.
    Otherwise show a brief "processing" state.
    """
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)

    if booking.status == models.BookingStatus.confirmed:
        return RedirectResponse(f"/payments/success/{booking_id}", status_code=303)

    if booking.status == models.BookingStatus.cancelled:
        return RedirectResponse("/bookings?payment_expired=1", status_code=303)

    # card_saved or still awaiting_payment (webhook in-flight)
    return templates.TemplateResponse("payments/card_saved.html", {
        **ctx, "booking": booking,
    })


@router.get("/auth-failed/{booking_id}", response_class=HTMLResponse)
def auth_failed_page(
    booking_id:   int,
    request:      Request,
    ctx:          dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Shown when a Case B MIT authorization fails.
    Passenger has 2 h to update their card.  The +5 % surcharge is visible here.
    """
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)

    payment = booking.payment
    if not payment or payment.status != models.PaymentStatus.retry_pending:
        return RedirectResponse(f"/my-trips?tab=bookings", status_code=303)

    return templates.TemplateResponse("payments/auth_failed.html", {
        **ctx,
        "booking":         booking,
        "retry_deadline":  payment.retry_deadline,
        "new_total":       booking.total_price,   # already updated with +5 % surcharge
        "new_fee_pct":     23,
    })


@router.post("/retry/{booking_id}", response_class=HTMLResponse)
def retry_payment(
    booking_id:   int,
    request:      Request,
    ctx:          dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Passenger submits a new card during the 2-hour retry window (Case B failure).
    Creates a fresh Rapyd checkout page for a new save-card flow with the updated
    service fee already baked in.
    """
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)

    payment = booking.payment
    if not payment or payment.status != models.PaymentStatus.retry_pending:
        return RedirectResponse(f"/my-trips?tab=bookings", status_code=303)

    if payment.retry_deadline and datetime.utcnow() > payment.retry_deadline:
        return RedirectResponse("/bookings?retry_expired=1", status_code=303)

    if settings.beta_mode:
        return RedirectResponse(f"/payments/checkout/{booking_id}", status_code=303)

    s = get_settings()
    try:
        # Generate a fresh idempotency key for this new checkout attempt
        new_key = generate_idempotency_key()
        checkout_data = rapyd_client.create_checkout_page(
            amount               = 0,
            capture              = True,
            complete_url         = f"{s.base_url}/payments/card-saved/{booking_id}",
            cancel_url           = f"{s.base_url}/payments/auth-failed/{booking_id}",
            idempotency_key      = new_key,
            metadata             = {"booking_id": booking_id, "case": "B", "retry": True},
            customer_id          = payment.rapyd_customer_id,
            save_payment_method  = True,
        )
        payment.rapyd_checkout_id = checkout_data["id"]
        payment.idempotency_key   = new_key
        db.commit()
    except RapydError as exc:
        log.error("Rapyd retry checkout failed for booking %s: %s", booking_id, exc)
        db.rollback()
        return RedirectResponse(f"/payments/auth-failed/{booking_id}?rapyd_error=1", status_code=303)

    return RedirectResponse(f"/payments/checkout/{booking_id}", status_code=303)


# ── Booking status API (for frontend polling) ─────────────────────────────────

@router.get("/status/{booking_id}")
def booking_status_api(
    booking_id:   int,
    current_user: models.User = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Lightweight JSON endpoint polled by the processing page.
    Returns {"status": "confirmed"|"awaiting_payment"|"card_saved"|"cancelled"}.
    """
    booking = (
        db.query(models.Booking)
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse({"status": str(booking.status)})


# ── Success page ──────────────────────────────────────────────────────────────

@router.get("/success/{booking_id}", response_class=HTMLResponse)
def payment_success(
    booking_id:   int,
    request:      Request,
    ctx:          dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session = Depends(get_db),
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
