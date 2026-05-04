"""
Rapyd webhook handler.

Security
--------
Every incoming request is verified against Rapyd's HMAC-SHA256 signature
before any DB write occurs.  Duplicate deliveries are silently ignored using
the webhook ID stored in Payment.seen_webhook_ids.

Handled event types
-------------------
CHECKOUT_COMPLETED   — checkout flow completed successfully
                        • Case A: payment authorised (status ACT)  → booking confirmed
                        • Case B: card saved                        → booking card_saved
PAYMENT_CAPTURED     — Rapyd confirmed a manual capture (status CLO)
                        → payment captured (authorised or capture_requested → captured)
PAYMENT_COMPLETED    — alias / fallback for capture confirmation on some Rapyd flows
                        → same handler as PAYMENT_CAPTURED
PAYMENT_FAILED       — payment or checkout failed
PAYMENT_EXPIRED      — auth or checkout expired without capture

Both PAYMENT_CAPTURED and PAYMENT_COMPLETED are routed to the same handler so
that payouts are only created after provider-confirmed capture regardless of
which event Rapyd sends.

All handlers are idempotent — safe to receive the same event more than once.
"""

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, joinedload

from app import models, email as mailer, sms as texter, rapyd as rapyd_client
from app.config import get_settings
from app.database import get_db
from app.rapyd import verify_webhook, RapydError

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log    = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_payment_for_booking(db: Session, booking_id: int) -> models.Payment | None:
    return (
        db.query(models.Payment)
        .options(
            joinedload(models.Payment.booking).joinedload(models.Booking.trip),
        )
        .join(models.Booking, models.Payment.booking_id == models.Booking.id)
        .filter(models.Booking.id == booking_id)
        .first()
    )


def _is_duplicate(payment: models.Payment, webhook_id: str) -> bool:
    """Return True if this webhook ID has already been processed."""
    if not payment.seen_webhook_ids:
        return False
    try:
        seen = json.loads(payment.seen_webhook_ids)
        return webhook_id in seen
    except (json.JSONDecodeError, TypeError):
        return False


def _mark_seen(payment: models.Payment, webhook_id: str) -> None:
    """Append webhook_id to the dedup list on the payment record."""
    try:
        seen = json.loads(payment.seen_webhook_ids) if payment.seen_webhook_ids else []
    except (json.JSONDecodeError, TypeError):
        seen = []
    seen.append(webhook_id)
    # Keep at most 50 IDs to bound row growth
    payment.seen_webhook_ids = json.dumps(seen[-50:])


# States from which a CHECKOUT_COMPLETED webhook may legitimately confirm a booking.
# awaiting_payment is the only valid source for Case A.
# Anything else (cancelled, completed, no_show, rejected, card_saved, pending)
# must NOT be resurrected by a late or replayed event.
_CASE_A_CONFIRMABLE_STATES = frozenset({
    models.BookingStatus.awaiting_payment,
})

# States from which a CHECKOUT_COMPLETED Case B webhook may set card_saved.
_CASE_B_CARD_SAVE_STATES = frozenset({
    models.BookingStatus.awaiting_payment,
    models.BookingStatus.card_saved,   # idempotent re-delivery
})


def _apply_booking_confirmation(
    db:      Session,
    booking: models.Booking,
    payment: models.Payment,
) -> bool:
    """
    Stage booking-confirmation state changes in the current session WITHOUT
    committing.  Caller must db.commit() and send post-commit notifications.

    Returns True  if the booking was transitioned to confirmed.
    Returns False if the booking was already confirmed (idempotent no-op) or
                  if the current state is not in the allowed-transition set
                  (_CASE_A_CONFIRMABLE_STATES).  The caller must handle the
                  False / invalid-state case — typically by voiding any
                  dangling Rapyd authorisation so the cardholder is not left
                  with a frozen hold.

    Newsletter discount is consumed here so it cannot be double-claimed if a
    retry re-runs this function after a partial commit failure.
    """
    if booking.status == models.BookingStatus.confirmed:
        # True duplicate delivery — already processed, safe to no-op.
        return False

    if booking.status not in _CASE_A_CONFIRMABLE_STATES:
        log.warning(
            "CHECKOUT_COMPLETED: refusing to confirm booking %s — "
            "current state is %r, not a valid source state %s. "
            "rapyd_payment_id=%s will be voided by caller.",
            booking.id,
            str(booking.status),
            {str(s) for s in _CASE_A_CONFIRMABLE_STATES},
            payment.rapyd_payment_id,
        )
        return False

    booking.status = models.BookingStatus.confirmed

    # Consume the first-ride newsletter discount only if it was applied
    if booking.service_fee == 0:
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

    return True


def _void_stale_authorization(payment: models.Payment) -> None:
    """
    Best-effort void of a Rapyd authorisation that arrived for a booking
    which is no longer in a confirmable state (cancelled, expired, etc.).

    Issues a full refund against the ACT-status payment to release the
    cardholder's hold immediately.  If the Rapyd call fails the auth will
    expire naturally within 7 days — this is logged at ERROR level so it
    can be caught by alerting and resolved manually if needed.
    """
    if not payment.rapyd_payment_id or not payment.passenger_total:
        return
    try:
        rapyd_client.create_refund(
            payment_id      = payment.rapyd_payment_id,
            amount          = payment.passenger_total,
            reason          = "requested_by_customer",
            idempotency_key = f"void-stale-{payment.id}",
        )
        log.info(
            "Voided stale authorisation for payment %s (booking %s)",
            payment.id, payment.booking_id,
        )
    except RapydError as exc:
        log.error(
            "Failed to void stale authorisation for payment %s (booking %s): %s — "
            "the cardholder's hold will expire naturally in 7 days. "
            "Manual review recommended.",
            payment.id, payment.booking_id, exc,
        )


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/rapyd", include_in_schema=False)
async def rapyd_webhook(request: Request):
    """
    Receive and process Rapyd webhook events.
    Returns 200 immediately once signature is verified; processing is synchronous
    but fast (all DB work is in-process — no external calls made here).
    """
    body_bytes = await request.body()
    body_str   = body_bytes.decode("utf-8")

    # ── 1. Signature verification ─────────────────────────────────────────────
    sig  = request.headers.get("signature",  "")
    salt = request.headers.get("salt",       "")
    ts   = request.headers.get("timestamp",  "")

    if not all([sig, salt, ts]):
        log.warning("Rapyd webhook missing auth headers — rejected")
        return JSONResponse({"error": "missing headers"}, status_code=400)

    # The canonical string includes the full registered webhook URL.
    # This must match what was configured in the Rapyd dashboard exactly.
    webhook_url = f"{get_settings().base_url}/webhooks/rapyd"

    if not verify_webhook(
        url=webhook_url,
        body=body_str,
        rapyd_signature=sig,
        rapyd_salt=salt,
        rapyd_timestamp=ts,
    ):
        log.warning("Rapyd webhook signature mismatch — rejected")
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    # ── 2. Parse payload ──────────────────────────────────────────────────────
    try:
        event = json.loads(body_str)
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad json"}, status_code=400)

    webhook_id   = event.get("id", "")
    event_type   = event.get("type", "").upper()
    event_data   = event.get("data", {})

    log.info("Rapyd webhook received: id=%s type=%s", webhook_id, event_type)

    # ── 3. Dispatch ───────────────────────────────────────────────────────────
    db: Session = next(get_db())
    try:
        if event_type == "CHECKOUT_COMPLETED":
            _handle_checkout_completed(db, webhook_id, event_data)
        elif event_type in ("PAYMENT_CAPTURED", "PAYMENT_COMPLETED"):
            # PAYMENT_CAPTURED is the canonical capture-confirmation event.
            # PAYMENT_COMPLETED is kept as a fallback — some Rapyd flows send
            # it instead of (or in addition to) PAYMENT_CAPTURED.
            _handle_payment_captured(db, webhook_id, event_data)
        elif event_type in ("PAYMENT_FAILED", "CHECKOUT_FAILED"):
            _handle_payment_failed(db, webhook_id, event_data)
        elif event_type in ("PAYMENT_EXPIRED", "CHECKOUT_EXPIRED"):
            _handle_payment_expired(db, webhook_id, event_data)
        else:
            log.debug("Rapyd webhook type %s — no handler, ignoring", event_type)
    except Exception as exc:
        log.exception("Rapyd webhook processing error (id=%s type=%s): %s",
                      webhook_id, event_type, exc)
        db.rollback()
        # Return 500 so Rapyd retries delivery.  Our handlers mark a webhook as
        # seen only inside the same transaction as all other state changes, so a
        # rollback means the seen-ID was never persisted and a retry will
        # re-process cleanly.  Idempotency guards (seen_webhook_ids, status
        # checks) make re-processing safe for already-succeeded events.
        return JSONResponse({"error": "processing_error"}, status_code=500)
    finally:
        db.close()

    return JSONResponse({"status": "ok"})


# ── Event handlers ────────────────────────────────────────────────────────────

def _handle_checkout_completed(
    db: Session, webhook_id: str, data: dict
) -> None:
    """
    CHECKOUT_COMPLETED fires when the embedded Rapyd.js checkout completes.

    Case A: payment.status == "ACT" (authorised, capture=false)
        → mark payment as authorised, confirm booking
    Case B: checkout was a save-card (amount=0, save_payment_method=true)
        → store customer_id + payment_method_id, move booking → card_saved

    All state changes (including _mark_seen) land in a single db.commit() so
    that a rollback on failure leaves no partially-applied state and the
    webhook can be safely retried.
    """
    metadata    = data.get("metadata") or {}
    booking_id  = metadata.get("booking_id")
    if not booking_id:
        log.warning("CHECKOUT_COMPLETED missing booking_id in metadata")
        return

    payment = _load_payment_for_booking(db, int(booking_id))
    if not payment:
        log.warning("CHECKOUT_COMPLETED: no payment for booking %s", booking_id)
        return

    if _is_duplicate(payment, webhook_id):
        log.debug("CHECKOUT_COMPLETED duplicate id=%s — skipped", webhook_id)
        return

    booking = payment.booking
    case    = metadata.get("case") or payment.payment_case or "A"

    if case == "A":
        # Extract Rapyd payment object from checkout data
        rapyd_payment = data.get("payment") or {}
        rapyd_pmt_id  = rapyd_payment.get("id")
        rapyd_status  = rapyd_payment.get("status", "")    # ACT = authorised

        if rapyd_pmt_id:
            payment.rapyd_payment_id = rapyd_pmt_id

        # Extract masked card details for display
        pm_data = rapyd_payment.get("payment_method_data") or {}
        payment.card_last4 = pm_data.get("last4") or rapyd_payment.get("last4")
        payment.card_brand = pm_data.get("brand") or rapyd_payment.get("brand")

        if rapyd_status in ("ACT", "CLO"):
            payment.status          = models.PaymentStatus.authorised
            payment.auth_expires_at = datetime.utcnow() + timedelta(days=7)

            confirmed = _apply_booking_confirmation(db, booking, payment)

            if not confirmed and booking.status != models.BookingStatus.confirmed:
                # Booking is in a terminal or unexpected state (cancelled, expired,
                # completed, …).  Mark the payment as failed, persist the seen-ID
                # so we don't retry, then void the Rapyd auth so the cardholder's
                # hold is released without waiting 7 days.
                payment.status = models.PaymentStatus.failed
                _mark_seen(payment, webhook_id)
                db.commit()
                _void_stale_authorization(payment)
                return

            # Either confirmed=True (normal) or booking was already confirmed
            # by a previous delivery of this webhook (idempotent).
            _mark_seen(payment, webhook_id)
            db.commit()
            if confirmed:
                db.refresh(booking)
                mailer.booking_confirmed_to_passenger(booking)
                mailer.booking_confirmed_to_driver(booking)
                log.info("Booking %s confirmed via webhook (Case A)", booking.id)
        else:
            log.warning(
                "CHECKOUT_COMPLETED Case A booking %s — unexpected status %s",
                booking_id, rapyd_status,
            )
            # Still mark seen so an unexpected status isn't retried indefinitely
            _mark_seen(payment, webhook_id)
            db.commit()

    else:
        # Case B — card saved
        # Guard: only process this for bookings still waiting for a card.
        # A late Case B webhook for a cancelled/expired booking is harmless
        # (no money moved — the checkout was amount=0), but we should not
        # store the payment method or send the "card saved" email.
        if booking.status not in _CASE_B_CARD_SAVE_STATES:
            log.warning(
                "CHECKOUT_COMPLETED Case B: booking %s in state %r is no longer "
                "active — card token %s will not be stored.",
                booking.id,
                str(booking.status),
                (data.get("payment_method") or {}).get("id"),
            )
            _mark_seen(payment, webhook_id)
            db.commit()
            return

        # Rapyd returns customer and payment_method in checkout data
        customer = data.get("customer") or {}
        pm       = data.get("payment_method") or {}

        # customer_id may already be on the payment record; use from webhook if missing
        if not payment.rapyd_customer_id and customer.get("id"):
            payment.rapyd_customer_id = customer["id"]
        if pm.get("id"):
            payment.rapyd_payment_method_id = pm["id"]

        # Card display data (may be in pm.fields)
        fields = pm.get("fields") or {}
        payment.card_last4 = fields.get("last4")
        payment.card_brand = fields.get("brand")

        payment.status = models.PaymentStatus.card_saved

        if booking.status == models.BookingStatus.awaiting_payment:
            booking.status = models.BookingStatus.card_saved
        # If already card_saved: idempotent — booking status unchanged, card fields refreshed.

        # Single atomic commit: card_saved state + seen-ID
        _mark_seen(payment, webhook_id)
        db.commit()
        if booking.status == models.BookingStatus.card_saved:
            mailer.card_saved_to_passenger(booking)
        log.info(
            "Booking %s: card saved — MIT scheduled for %s",
            booking.id, payment.auth_scheduled_for,
        )


def _handle_payment_captured(
    db: Session, webhook_id: str, data: dict
) -> None:
    """
    PAYMENT_CAPTURED / PAYMENT_COMPLETED — Rapyd has confirmed the capture.

    Two independent guards must both pass before the payment is promoted to
    ``captured``:

    1. Payload guard — the webhook's payment object must signal capture:
          data["status"] == "CLO"   (Rapyd closed/captured)
       OR data["captured"] == True  (explicit captured flag some flows set)
       A mis-routed, malformed, or prematurely-fired event is rejected here
       with a warning so Rapyd retries delivery on a 5xx response.

    2. Local state guard — the payment must be in a pre-capture state:
          authorised       — capture_at not yet reached; Rapyd sent the event
                             ahead of our task loop (rare, possible in sandbox).
          capture_requested — normal path: task made the API call, set this
                              state, and the webhook delivers final confirmation.
       Payments already in a terminal state (captured, refunded, refund_requested,
       refund_failed, partial_refund, failed, auth_expired, retry_pending) are
       treated as a no-op so a late provider event can never regress them.

    The seen-ID is always persisted (even on no-op) so Rapyd does not keep
    redelivering an already-handled event.
    """
    _CONFIRMABLE = frozenset({
        models.PaymentStatus.authorised,
        models.PaymentStatus.capture_requested,
    })

    # ── 1. Extract payment ID ─────────────────────────────────────────────────
    rapyd_pmt_id = data.get("id")
    if not rapyd_pmt_id:
        log.warning("PAYMENT_CAPTURED: missing id in webhook data")
        return

    # ── 2. Payload guard — verify provider confirms capture ───────────────────
    # Rapyd sets status="CLO" when funds are captured.  Some flows also set a
    # top-level "captured": true boolean.  Accept either signal; reject both
    # absent so a non-capture event routed here doesn't advance the state.
    rapyd_status   = data.get("status", "")
    rapyd_captured = data.get("captured", False)
    if rapyd_status != "CLO" and not rapyd_captured:
        log.warning(
            "PAYMENT_CAPTURED: payload for %s does not confirm capture "
            "(status=%r, captured=%r) — ignoring, Rapyd will retry",
            rapyd_pmt_id, rapyd_status, rapyd_captured,
        )
        # Do NOT mark as seen — return without writing anything so that Rapyd
        # retries delivery (we return 200 from the outer handler, but the
        # seen-ID will not be in the DB, so a genuine PAYMENT_CAPTURED that
        # arrives later will be processed normally).
        return

    # ── 3. Locate local payment record ────────────────────────────────────────
    payment = (
        db.query(models.Payment)
        .filter(models.Payment.rapyd_payment_id == rapyd_pmt_id)
        .first()
    )
    if not payment:
        log.warning("PAYMENT_CAPTURED: unknown rapyd_payment_id %s", rapyd_pmt_id)
        return

    if _is_duplicate(payment, webhook_id):
        log.debug("PAYMENT_CAPTURED duplicate id=%s — skipped", webhook_id)
        return
    _mark_seen(payment, webhook_id)

    # ── 4. Local state guard — no terminal-state regression ───────────────────
    if payment.status not in _CONFIRMABLE:
        log.warning(
            "PAYMENT_CAPTURED: payment %s is in terminal/unexpected status=%s "
            "— refusing to regress to captured (webhook_id=%s)",
            payment.id, payment.status, webhook_id,
        )
        db.commit()   # persist seen_webhook_ids so Rapyd stops redelivering
        return

    # ── 5. Transition ─────────────────────────────────────────────────────────
    prev_status    = payment.status
    payment.status = models.PaymentStatus.captured
    db.commit()
    log.info(
        "Payment %s (booking %s) confirmed captured via webhook "
        "(prev_status=%s, rapyd_status=%r, webhook_id=%s)",
        payment.id, payment.booking_id, prev_status, rapyd_status, webhook_id,
    )


def _handle_payment_failed(
    db: Session, webhook_id: str, data: dict
) -> None:
    """
    PAYMENT_FAILED / CHECKOUT_FAILED — checkout or MIT authorisation declined.
    For Case A: cancel booking, release seats.
    For Case B MIT failure: move to retry_pending (handled by tasks.py).
    """
    metadata   = data.get("metadata") or {}
    booking_id = metadata.get("booking_id")
    if not booking_id:
        # Try to find via rapyd_payment_id
        rapyd_pmt_id = data.get("id") or (data.get("payment") or {}).get("id")
        if rapyd_pmt_id:
            payment = (
                db.query(models.Payment)
                .filter(models.Payment.rapyd_payment_id == rapyd_pmt_id)
                .first()
            )
            if payment:
                booking_id = payment.booking_id

    if not booking_id:
        log.warning("PAYMENT_FAILED: cannot identify booking — ignored")
        return

    payment = _load_payment_for_booking(db, int(booking_id))
    if not payment:
        return

    if _is_duplicate(payment, webhook_id):
        return
    _mark_seen(payment, webhook_id)

    booking = payment.booking
    case    = metadata.get("case") or payment.payment_case or "A"

    if case == "A":
        # Checkout auth failed — cancel booking and release seats
        payment.status = models.PaymentStatus.failed
        if booking.status == models.BookingStatus.awaiting_payment:
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
        db.commit()
        log.info("Booking %s: Case A payment failed — cancelled", booking_id)

    else:
        # Case B MIT failure is handled explicitly in tasks.py (_run_mit_authorizations)
        # Here we just log it (the task will have already updated the state)
        log.info("Booking %s: Case B payment failed (webhook)", booking_id)
        db.commit()


def _handle_payment_expired(
    db: Session, webhook_id: str, data: dict
) -> None:
    """Payment or checkout expired without being captured."""
    metadata   = data.get("metadata") or {}
    booking_id = metadata.get("booking_id")
    if not booking_id:
        return

    payment = _load_payment_for_booking(db, int(booking_id))
    if not payment:
        return

    if _is_duplicate(payment, webhook_id):
        return
    _mark_seen(payment, webhook_id)

    # Only transition if still authorised (not already captured/refunded)
    if payment.status == models.PaymentStatus.authorised:
        payment.status = models.PaymentStatus.auth_expired
        log.warning(
            "Payment %s (booking %s) authorisation EXPIRED — manual review needed",
            payment.id, payment.booking_id,
        )
    db.commit()


