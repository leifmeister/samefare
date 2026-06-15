"""
Payment router — Rapyd-backed authorise-then-capture model.

Case A (instant-book ride ≤ 24 hours away)
    Passenger completes an embedded Rapyd checkout; card is authorised now
    (capture=false).  Seat is confirmed immediately on authorisation.
    Capture fires at departure_datetime via the background task.

Case B (ride > 24 hours away, and ALL request-to-book bookings)
    Passenger saves their card via Rapyd's Hosted Card page
    (/v1/hosted/collect/card — a card-token page, not the checkout page). A
    Rapyd customer & payment-method token are stored. A background task fires a
    merchant-initiated authorisation (MIT) 24 h before departure. On success
    the booking is confirmed; on failure the passenger gets a 2-hour SMS window
    to update their card.

The Case A/B boundary is CASE_B_THRESHOLD_HOURS (24h), matching the
cancellation cut-off — once the pre-auth fires the seat is reserved and the
full amount is forfeited on cancellation.

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

# Threshold for Case A vs Case B:
# Case A — departure is ≤24 hours away → pre-authorise the card immediately at booking.
# Case B — departure is >24 hours away → save the card token at booking, fire a
#           merchant-initiated pre-auth exactly 24 hours before departure.
# The 24-hour boundary matches the cancellation cut-off: once the pre-auth fires, the
# seat is reserved and the passenger forfeits the full amount on cancellation.
CASE_B_THRESHOLD_HOURS = 24


# ── Helpers ───────────────────────────────────────────────────────────────────

def _expire_booking(db: Session, booking: models.Booking) -> None:
    """
    Cancel an expired awaiting-payment booking and release held seats.
    Idempotent — status check prevents double-cancellation.
    Caller is responsible for db.commit().

    Seats are recomputed from remaining active bookings (excluding this one)
    rather than by simple addition.  Simple addition is wrong for segment
    bookings: seats_available is a peak/minimum across all legs, so adding
    back booking.seats_booked can overstate capacity when another leg is
    still fully occupied.
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
            from app.utils import build_route_graph, recompute_seats_available
            active = [b for b in trip.bookings
                      if b.id != booking.id and b.status in {
                          models.BookingStatus.awaiting_payment,
                          models.BookingStatus.confirmed,
                          models.BookingStatus.card_saved,
                      }]
            graph = build_route_graph(db)
            trip.seats_available = recompute_seats_available(
                graph, trip.seats_total, active, trip.origin, trip.destination,
            )


_SERVICE_FEE_RATE  = 0.18
_SERVICE_FEE_FLOOR = 200   # ISK — minimum fee regardless of fare
_ROUNDING_UNIT     = 50    # ISK — passenger total rounded up to nearest 50


def calc_fees(contribution: int) -> tuple[int, int, int]:
    """Return (service_fee, passenger_total, driver_payout).

    Logic:
      1. raw_fee  = round(contribution × 0.18)
      2. fee      = max(raw_fee, SERVICE_FEE_FLOOR)
      3. total    = ceil((contribution + fee) / ROUNDING_UNIT) × ROUNDING_UNIT
      4. service_fee absorbs the rounding delta so driver_payout stays clean.
    """
    raw_fee  = round(contribution * _SERVICE_FEE_RATE)
    fee      = max(raw_fee, _SERVICE_FEE_FLOOR)
    raw_total = contribution + fee
    total    = -(-raw_total // _ROUNDING_UNIT) * _ROUNDING_UNIT  # ceiling div
    service_fee = total - contribution
    return service_fee, total, contribution


def _payment_case(departure_datetime: datetime) -> str:
    """
    Return 'A' if departure is ≤ CASE_B_THRESHOLD_HOURS (24h) away, 'B' if further.

    Case A pre-authorises the card now; Case B saves the card and fires the MIT
    24h before departure. The threshold matches the cancellation cut-off.
    """
    return "A" if (departure_datetime - datetime.utcnow()) <= timedelta(hours=CASE_B_THRESHOLD_HOURS) else "B"


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
        # MIT authorisation time: T-24h for far-future bookings, or right now when
        # the trip is already inside 24h (there is no separate "authorise now"
        # path — everything goes card→MIT). A ≤24h booking is then authorised
        # immediately when the passenger returns from the Hosted Card page.
        auth_scheduled_for = max(
            booking.trip.departure_datetime - timedelta(hours=24),
            datetime.utcnow(),
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
    # Cancel any scheduled capture so the capture task can't race this refund
    # intent and charge (then drop) a booking we're cancelling. Harmless if the
    # payment was already captured (capture task ignores captured payments).
    payment.capture_at = None

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


def _finalize_card_saved(db: Session, booking: models.Booking) -> bool:
    """
    After the Hosted Card page redirects back, read the tokenised card off the
    Rapyd customer and record it on the booking's payment.

    Authoritative — we query Rapyd (retrieve_customer) rather than trusting the
    redirect. Idempotent and best-effort: returns True if the card is now (or
    already) saved, False if not yet available. Never raises.

    For a pending (request-to-book) booking the booking stays pending — the MIT
    fires when the driver accepts. For awaiting_payment it advances to card_saved.
    """
    payment = booking.payment
    if not payment or not payment.rapyd_customer_id:
        return False
    if payment.status == models.PaymentStatus.card_saved:
        return True
    if booking.status not in (
        models.BookingStatus.pending,
        models.BookingStatus.awaiting_payment,
    ):
        return False

    try:
        customer = rapyd_client.retrieve_customer(payment.rapyd_customer_id)
    except RapydError as exc:
        log.warning("retrieve_customer failed finalising card for booking %s: %s", booking.id, exc)
        return False

    pms = ((customer.get("payment_methods") or {}).get("data")) or []
    # Only consider FULLY saved cards: a real token id and no pending 3DS step.
    # A failed/abandoned save (e.g. a corporate card stuck on 3d_verification)
    # leaves a token-less entry on the customer — if that happens to be last in
    # the list (and there's no default), the old `or pms[-1]` fallback picked it
    # and bailed, ignoring a perfectly valid card the passenger also added.
    usable = [
        p for p in pms
        if p.get("id") and p.get("next_action") in (None, "", "not_applicable")
    ]
    if not usable:
        return False
    default_id = customer.get("default_payment_method")
    pm = next((p for p in usable if p.get("id") == default_id), None) or usable[-1]

    payment.rapyd_payment_method_id = pm["id"]
    payment.card_last4 = pm.get("last4")
    payment.card_brand = (pm.get("bin_details") or {}).get("brand") or pm.get("brand")
    payment.status     = models.PaymentStatus.card_saved

    was_pending = booking.status == models.BookingStatus.pending
    if booking.status == models.BookingStatus.awaiting_payment:
        booking.status = models.BookingStatus.card_saved
    db.commit()
    db.refresh(booking)

    try:
        if booking.status == models.BookingStatus.card_saved:
            mailer.card_saved_to_passenger(booking)
        elif was_pending:
            mailer.card_saved_pending_to_passenger(booking)
    except Exception as exc:
        log.warning("card-saved email failed for booking %s: %s", booking.id, exc)

    log.info("Card saved for booking %s via redirect finalisation (pm=%s)", booking.id, pm["id"])
    return True


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
        models.BookingStatus.pending,      # pending bookings: save card upfront before driver accepts
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
    # Pending bookings that already have a card saved show a "waiting for driver" message.
    if booking.status in (models.BookingStatus.card_saved, models.BookingStatus.pending):
        payment_for_check = booking.payment
        if (payment_for_check
                and payment_for_check.status == models.PaymentStatus.card_saved
                and payment_for_check.status != models.PaymentStatus.retry_pending):
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
    # Pending bookings always save the card now and MIT on driver acceptance —
    # regardless of departure date, this is always a Case B (save-card) flow.
    case    = "B" if booking.status == models.BookingStatus.pending else (
        payment.payment_case or _payment_case(booking.trip.departure_datetime)
    )

    # Commit the new Payment record (flushed but not yet committed by
    # _get_or_create_payment) so it survives any subsequent API failure.
    # Must happen before any Rapyd call so a rollback on API error does not
    # wipe the record.
    db.commit()

    s            = get_settings()
    complete_url = f"{s.base_url}/payments/complete/{booking_id}"
    cancel_url   = f"{s.base_url}/payments/checkout/{booking_id}"

    def _checkout_error_response(rapyd_error: str = None):
        # Localised so the banner matches the page language (the reassurance and
        # retry strings below are translated too — avoid a mixed-language error).
        if rapyd_error is None:
            rapyd_error = ctx["_t"]("checkout_unavailable")
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

    # If Rapyd's Hosted Card page bounced back with an error (declined card,
    # failed/abandoned 3DS, or a card the issuer won't store for reuse — common
    # with corporate cards), don't silently relaunch it. Show a clear message so
    # the passenger can try a different card. Retrying (the "Try again" button →
    # /checkout/{id} without card_error) re-creates a fresh card page.
    if request.query_params.get("card_error"):
        return _checkout_error_response(ctx["_t"]("checkout_card_error"))

    # ── Tokenise the card via Rapyd's Hosted Card page (every booking) ────────
    # We ALWAYS save the card via /v1/hosted/collect/card and charge later via a
    # merchant-initiated payment (MIT) — never the Hosted Checkout page, which
    # this Rapyd account rejects with UNAUTHORIZED_API_CALL. The charge timing
    # is driven by payment.auth_scheduled_for: a >24h ("Case B") booking is
    # authorised at T-24h by the background loop; a ≤24h ("Case A") booking is
    # authorised immediately when the passenger returns (see card_saved_page).
    # Phase 1: ensure a durable Rapyd customer exists (separate commit so a later
    # failure does not orphan it; stable idempotency key dedupes).
    if not payment.rapyd_customer_id:
        try:
            customer_id = rapyd_client.create_customer(
                email             = current_user.email,
                name              = current_user.full_name,
                idempotency_key   = f"customer-{payment.idempotency_key}",
            )
        except RapydError as exc:
            log.error("Rapyd customer create failed for booking %s: %s", booking_id, exc)
            return _checkout_error_response()
        payment.rapyd_customer_id = customer_id
        try:
            db.commit()
        except Exception as exc:
            log.error("DB commit for rapyd_customer_id failed (booking %s): %s", booking_id, exc)
            db.rollback()
            return _checkout_error_response()
    else:
        customer_id = payment.rapyd_customer_id

    # Phase 2: create the hosted card-token page and redirect to it. We re-create
    # on each visit (the idempotency key returns the same page within Rapyd's
    # window) so a refresh/back always has a live redirect_url.
    try:
        card_page = rapyd_client.create_card_token_page(
            customer_id          = customer_id,
            complete_url         = f"{s.base_url}/payments/card-saved/{booking_id}",
            cancel_url           = cancel_url,
            complete_payment_url = f"{s.base_url}/payments/card-saved/{booking_id}",
            error_payment_url    = f"{s.base_url}/payments/checkout/{booking_id}?card_error=1",
            idempotency_key      = payment.idempotency_key,
        )
    except RapydError as exc:
        log.error("Rapyd card-token page create failed for booking %s: %s", booking_id, exc)
        db.rollback()
        return _checkout_error_response()

    payment.rapyd_checkout_id = card_page.get("id")
    db.commit()
    redirect_url = card_page.get("redirect_url")
    if not redirect_url:
        log.error("Rapyd card-token page for booking %s returned no redirect_url", booking_id)
        return _checkout_error_response()
    return RedirectResponse(redirect_url, status_code=303)


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
    if booking.status not in (
        models.BookingStatus.awaiting_payment,
        models.BookingStatus.pending,
    ):
        return RedirectResponse("/bookings", status_code=303)

    is_pending = booking.status == models.BookingStatus.pending

    if is_pending:
        # Pending booking: simulate card-save only — booking stays pending.
        # Stub IDs tell confirm_booking a card is on file; beta_mode check
        # there bypasses Rapyd and marks authorised directly at acceptance.
        payment = models.Payment(
            booking_id      = booking.id,
            passenger_total = booking.total_price,
            driver_payout   = booking.subtotal,
            platform_fee    = booking.service_fee,
            status          = models.PaymentStatus.card_saved,
            card_brand      = "Beta",
            payment_case    = "B",
            capture_at      = booking.trip.departure_datetime,
            rapyd_customer_id        = "beta-customer",
            rapyd_payment_method_id  = "beta-pm",
        )
        db.add(payment)
        db.commit()
        db.refresh(booking)
        mailer.card_saved_pending_to_passenger(booking)
        return RedirectResponse(f"/payments/card-saved/{booking_id}", status_code=303)

    # Instant booking: authorise immediately and confirm
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

    # Pull the tokenised card off the Rapyd customer (authoritative). Idempotent
    # with the CUSTOMER_PAYMENT_METHOD_CREATED webhook — whichever lands first.
    if not settings.beta_mode:
        _finalize_card_saved(db, booking)
        db.refresh(booking)

        # ≤24h bookings: authorise immediately instead of waiting for the T-24h
        # loop, so the passenger is confirmed before they leave the page. Pending
        # (request-to-book) bookings are skipped — they authorise when the driver
        # accepts. The card_saved guard + mit-{id} idempotency key in the helper
        # prevent any double charge if the loop or a webhook also runs.
        payment = booking.payment
        if (booking.status == models.BookingStatus.card_saved
                and payment and payment.status == models.PaymentStatus.card_saved
                and payment.auth_scheduled_for
                and payment.auth_scheduled_for <= datetime.utcnow()):
            from app.tasks import authorise_booking_mit
            try:
                authorise_booking_mit(db, booking)
                db.refresh(booking)
            except Exception as exc:
                log.warning("Immediate MIT after card-save failed for booking %s: %s", booking_id, exc)

        # Route on the immediate-authorisation outcome.
        if booking.status == models.BookingStatus.confirmed:
            return RedirectResponse(f"/payments/success/{booking_id}", status_code=303)
        if booking.payment and booking.payment.status == models.PaymentStatus.retry_pending:
            return RedirectResponse(f"/payments/auth-failed/{booking_id}", status_code=303)

    # card_saved or still pending/awaiting_payment (finalisation in-flight)
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
    Passenger has 2 h to update their card. No surcharge — the fee is unchanged
    from the original booking.
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
        "new_total":       booking.total_price,
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
    Creates a fresh Rapyd Hosted Card page (card-token) and redirects the
    passenger to it; on return, card_saved_page() records the new card and the
    MIT re-fires. No surcharge — the fee is unchanged from the original booking.
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

    # Never send the passenger to Rapyd once the trip has left — mirror the
    # checkout_page departure guard. (checkout_page already blocks this; the
    # retry flow had no equivalent, so a departed trip could still open a card
    # page here.)
    if booking.trip.departure_datetime <= datetime.utcnow():
        return RedirectResponse("/bookings?payment_expired=1", status_code=303)

    if payment.retry_deadline and datetime.utcnow() > payment.retry_deadline:
        return RedirectResponse("/bookings?retry_expired=1", status_code=303)

    if settings.beta_mode:
        return RedirectResponse(f"/payments/checkout/{booking_id}", status_code=303)

    s = get_settings()
    try:
        # Fresh card-token page (NOT an amount=0 checkout — that endpoint 401s for
        # saving a card). Same flow as the initial Case B save-card.
        new_key = generate_idempotency_key()
        card_page = rapyd_client.create_card_token_page(
            customer_id          = payment.rapyd_customer_id,
            complete_url         = f"{s.base_url}/payments/card-saved/{booking_id}",
            cancel_url           = f"{s.base_url}/payments/auth-failed/{booking_id}",
            complete_payment_url = f"{s.base_url}/payments/card-saved/{booking_id}",
            error_payment_url    = f"{s.base_url}/payments/auth-failed/{booking_id}?rapyd_error=1",
            idempotency_key      = new_key,
        )
        payment.rapyd_checkout_id = card_page.get("id")
        payment.idempotency_key   = new_key
        db.commit()
        redirect_url = card_page.get("redirect_url")
    except RapydError as exc:
        log.error("Rapyd retry card-token page failed for booking %s: %s", booking_id, exc)
        db.rollback()
        return RedirectResponse(f"/payments/auth-failed/{booking_id}?rapyd_error=1", status_code=303)

    if not redirect_url:
        return RedirectResponse(f"/payments/auth-failed/{booking_id}?rapyd_error=1", status_code=303)
    return RedirectResponse(redirect_url, status_code=303)


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

    # State gate: the "You're booked!" page (incl. driver contact details) must
    # only render for a booking that is genuinely confirmed/completed. A direct
    # URL must not surface a false confirmation for a pending / awaiting-payment
    # / card-saved / cancelled / rejected / failed booking — send each to the
    # page that reflects its real state instead.
    if booking.status not in (
        models.BookingStatus.confirmed,
        models.BookingStatus.completed,
    ):
        if booking.status in (models.BookingStatus.card_saved,
                              models.BookingStatus.pending):
            return RedirectResponse(f"/payments/card-saved/{booking_id}", status_code=303)
        if booking.status == models.BookingStatus.awaiting_payment:
            return RedirectResponse(f"/payments/checkout/{booking_id}", status_code=303)
        return RedirectResponse("/my-trips?tab=bookings", status_code=303)

    return templates.TemplateResponse("payments/success.html", {
        **ctx, "booking": booking,
    })
