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

from app import models, email as mailer, sms as texter, rapyd as rapyd_client, didit as didit_client
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
    models.BookingStatus.pending,      # upfront card-save before driver acceptance
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
        elif event_type == "CUSTOMER_PAYMENT_METHOD_CREATED":
            _handle_payment_method_created(db, webhook_id, event_data)
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
    Case B (LEGACY): an old amount=0 save-card checkout.
        → store customer_id + payment_method_id, move booking → card_saved
        Save-card now uses the Hosted Card page, which fires
        CUSTOMER_PAYMENT_METHOD_CREATED (see _handle_payment_method_created), so
        this branch is retained only for defensive/back-compat handling.

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
            # ACT = pre-authorised (capture=False): schedule capture at departure.
            # CLO = already captured by Rapyd (e.g. capture=True flow or instant
            #       settlement): mark captured immediately; no further capture needed.
            if rapyd_status == "CLO":
                payment.status     = models.PaymentStatus.captured
                payment.capture_at = None          # nothing left to capture
            else:
                payment.status          = models.PaymentStatus.authorised
                payment.auth_expires_at = datetime.utcnow() + timedelta(days=7)

            confirmed = _apply_booking_confirmation(db, booking, payment)

            if not confirmed and booking.status != models.BookingStatus.confirmed:
                # Booking is in a terminal/unexpected state (cancelled, expired,
                # completed, …) so it can't be honoured. The two cases diverge on
                # whether money actually moved:
                #   ACT — only a pre-auth hold exists; nothing was captured. Mark
                #         the payment failed and void the hold to release the card.
                #   CLO — Rapyd ALREADY captured the funds; there is no hold to
                #         void. Marking it failed would silently keep the charge,
                #         so keep it captured and queue a full refund instead.
                _mark_seen(payment, webhook_id)
                if rapyd_status == "CLO":
                    # payment.status is already `captured`; _issue_rapyd_refund
                    # moves it to refund_requested and _run_retry_refunds submits
                    # the refund to Rapyd (idempotent) and reconciles payout impact.
                    from app.routers.payments import _issue_rapyd_refund
                    _issue_rapyd_refund(
                        db, booking, payment.passenger_total or 0,
                        reason="requested_by_customer",
                    )
                    db.commit()
                    log.warning(
                        "CHECKOUT_COMPLETED CLO for non-confirmable booking %s "
                        "(state %r): funds were captured — full refund queued.",
                        booking.id, str(booking.status),
                    )
                else:
                    payment.status = models.PaymentStatus.failed
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
                log.info("Booking %s confirmed via webhook (Case A, rapyd_status=%s)",
                         booking.id, rapyd_status)
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

        was_pending = booking.status == models.BookingStatus.pending
        if booking.status == models.BookingStatus.awaiting_payment:
            booking.status = models.BookingStatus.card_saved
        # Pending bookings: keep booking.status = pending (MIT fires on driver acceptance).
        # Already card_saved: idempotent — booking status unchanged, card fields refreshed.

        _mark_seen(payment, webhook_id)
        db.commit()

        if booking.status == models.BookingStatus.card_saved:
            mailer.card_saved_to_passenger(booking)
        elif was_pending:
            mailer.card_saved_pending_to_passenger(booking)

        log.info(
            "Booking %s: card saved (booking status=%s) — MIT fires on driver acceptance",
            booking.id, booking.status.value,
        )


def _handle_payment_method_created(
    db: Session, webhook_id: str, data: dict
) -> None:
    """
    CUSTOMER_PAYMENT_METHOD_CREATED — a card was tokenised via the Hosted Card
    page (Case B save-card). The redirect handler (card_saved_page) is the
    primary finaliser; this webhook is the backup for passengers who close the
    tab before being redirected back.

    The card-token page accepts no booking metadata, so we correlate by the
    customer id (which we created and stored on the payment). Idempotent: the
    `status != card_saved` guard makes re-delivery a no-op.
    """
    pm_id       = data.get("id")
    customer_id = data.get("customer")
    if not pm_id or not customer_id:
        log.info("CUSTOMER_PAYMENT_METHOD_CREATED: no pm/customer id in payload — ignoring")
        return

    payment = (
        db.query(models.Payment)
        .filter(
            models.Payment.rapyd_customer_id == customer_id,
            models.Payment.status != models.PaymentStatus.card_saved,
        )
        .order_by(models.Payment.id.desc())
        .first()
    )
    if not payment:
        log.info(
            "CUSTOMER_PAYMENT_METHOD_CREATED: no pending payment for customer %s — ignoring",
            customer_id,
        )
        return

    booking = payment.booking
    if booking.status not in _CASE_B_CARD_SAVE_STATES:
        log.warning(
            "CUSTOMER_PAYMENT_METHOD_CREATED: booking %s in state %r — card %s not stored",
            booking.id, str(booking.status), pm_id,
        )
        return

    payment.rapyd_payment_method_id = pm_id
    payment.card_last4 = data.get("last4")
    payment.card_brand = (data.get("bin_details") or {}).get("brand") or data.get("brand")
    payment.status     = models.PaymentStatus.card_saved

    was_pending = booking.status == models.BookingStatus.pending
    if booking.status == models.BookingStatus.awaiting_payment:
        booking.status = models.BookingStatus.card_saved
    db.commit()
    db.refresh(booking)

    if booking.status == models.BookingStatus.card_saved:
        mailer.card_saved_to_passenger(booking)
    elif was_pending:
        mailer.card_saved_pending_to_passenger(booking)

    log.info("Booking %s: card saved via CUSTOMER_PAYMENT_METHOD_CREATED webhook (pm=%s)",
             booking.id, pm_id)


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
       A mis-routed, non-capture, or prematurely-fired event is acknowledged
       (HTTP 200) and ignored, with its seen-ID persisted so Rapyd stops
       redelivering. A webhook payload is immutable, so redelivering the same
       event would never confirm capture — there is nothing to retry. The
       genuine PAYMENT_CAPTURED (status=CLO) arrives as a separate event and is
       processed independently. (Transient *processing* errors still surface as
       a 5xx from the outer handler, which Rapyd does retry.)

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

    # ── 2. Locate local payment record ────────────────────────────────────────
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

    # ── 3. Payload guard — verify provider confirms capture ───────────────────
    # Rapyd sets status="CLO" when funds are captured.  Some flows also set a
    # top-level "captured": true boolean.  Accept either signal.
    rapyd_status   = data.get("status", "")
    rapyd_captured = data.get("captured", False)
    if rapyd_status != "CLO" and not rapyd_captured:
        # Not a capture-confirming event (e.g. a PAYMENT_COMPLETED fired at a
        # different lifecycle point). The payload is immutable, so redelivering
        # it would never confirm capture — acknowledge it (200) and persist the
        # seen-ID so Rapyd stops redelivering. The genuine PAYMENT_CAPTURED
        # (status=CLO) arrives as a separate event and is processed on its own.
        log.warning(
            "PAYMENT_CAPTURED: payload for %s does not confirm capture "
            "(status=%r, captured=%r) — acknowledged and ignored (webhook_id=%s)",
            rapyd_pmt_id, rapyd_status, rapyd_captured, webhook_id,
        )
        db.commit()   # persist seen_webhook_ids
        return

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
        db.commit()
        log.info("Booking %s: Case A payment failed — cancelled", booking_id)

    else:
        # Case B MIT failure is handled explicitly in tasks.py (_run_mit_authorizations)
        # Here we just log it (the task will have already updated the state)
        log.info("Booking %s: Case B payment failed (webhook)", booking_id)
        db.commit()


# ── Didit webhook ─────────────────────────────────────────────────────────────

@router.post("/didit", include_in_schema=False)
async def didit_webhook(request: Request):
    """
    Receive Didit KYC/AML status updates.

    Security: every request is validated with X-Signature-V2 (HMAC-SHA256
    over the sorted-key JSON body) before any DB write occurs.

    Handled status values
    ---------------------
    Approved   → set id_verification / license_verification to approved
    Declined   → set to rejected; store rejection reason if available
    In Review  → keep pending (Didit's human review team is looking at it)
    Abandoned  → reset to unverified so the user can resubmit
    Expired    → reset to unverified so the user can resubmit
    """
    body_bytes = await request.body()
    s          = get_settings()

    # ── 1. Signature verification ─────────────────────────────────────────────
    signature = request.headers.get("x-signature-v2", "")
    if not signature:
        log.warning("Didit webhook missing X-Signature-V2 header — rejected")
        return JSONResponse({"error": "missing signature"}, status_code=400)

    if not s.didit_webhook_secret:
        log.error("DIDIT_WEBHOOK_SECRET not configured — cannot verify webhook")
        return JSONResponse({"error": "misconfigured"}, status_code=500)

    try:
        payload = didit_client.verify_webhook_signature(
            payload_bytes=body_bytes,
            signature=signature,
            secret=s.didit_webhook_secret,
        )
    except ValueError as exc:
        log.warning("Didit webhook signature/timestamp invalid: %s", exc)
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    # ── 2. Filter to relevant event type ──────────────────────────────────────
    webhook_type = payload.get("webhook_type", "")
    if webhook_type != "status.updated":
        log.debug("Didit webhook type %r — no handler, ignoring", webhook_type)
        return JSONResponse({"status": "ignored"})

    status      = payload.get("status", "")
    vendor_data = payload.get("vendor_data", "")
    session_id  = payload.get("session_id", "")

    log.info("Didit webhook: session=%s status=%r vendor_data=%r",
             session_id, status, vendor_data)

    # ── 3. Parse vendor_data → user_id + verification_type ───────────────────
    # vendor_data format: "{user_id}:identity" or "{user_id}:licence"
    try:
        user_id_str, vtype = vendor_data.split(":", 1)
        user_id = int(user_id_str)
    except (ValueError, AttributeError):
        log.warning("Didit webhook: unparseable vendor_data %r (session=%s)",
                    vendor_data, session_id)
        return JSONResponse({"error": "bad vendor_data"}, status_code=400)

    # ── 4. Load user ──────────────────────────────────────────────────────────
    db: Session = next(get_db())
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            log.warning("Didit webhook: unknown user_id %s (session=%s)",
                        user_id, session_id)
            return JSONResponse({"error": "user not found"}, status_code=404)

        # ── 5. Map Didit status → VerificationStatus ──────────────────────────
        approved  = models.VerificationStatus.approved
        rejected  = models.VerificationStatus.rejected
        pending   = models.VerificationStatus.pending
        unverified = models.VerificationStatus.unverified

        if status == didit_client.STATUS_APPROVED:
            new_status = approved
        elif status == didit_client.STATUS_DECLINED:
            new_status = rejected
        elif status == didit_client.STATUS_IN_REVIEW:
            new_status = pending   # Didit human review; keep pending, no email yet
        elif status in (didit_client.STATUS_ABANDONED, didit_client.STATUS_EXPIRED):
            new_status = unverified   # allow retry
        else:
            # In Progress, Not Started — no state change needed
            log.debug("Didit webhook: status %r requires no action", status)
            return JSONResponse({"status": "ok"})

        # ── 5a. Respect a manual admin decision ───────────────────────────────
        # If an admin has manually settled this verification, a later (possibly
        # contradictory) Didit webhook must not silently overturn the human call.
        locked = (user.license_verification_locked if vtype == "licence"
                  else user.id_verification_locked)
        if locked:
            log.info("Didit webhook: %s for user %s is admin-locked — webhook ignored",
                     vtype, user_id)
            return JSONResponse({"status": "ignored_admin_locked"})

        # ── 5b. Ignore events from a stale (superseded/abandoned) session ──────
        # The webhook only carries user_id + type, but Didit can fire late events
        # for an OLD session the user abandoned and restarted. Applying those
        # would overturn a newer decision (e.g. a delayed "Abandoned" for session
        # A knocking out the "Approved" the user earned in session B). Only act on
        # the user's CURRENT session for this verification type.
        current_sid = (user.didit_licence_session_id if vtype == "licence"
                       else user.didit_identity_session_id)
        if session_id and current_sid and session_id != current_sid:
            log.info("Didit webhook: ignoring stale session %s (current=%s) for user %s/%s",
                     session_id, current_sid, user_id, vtype)
            return JSONResponse({"status": "ignored_stale_session"})

        # ── 5c. Never let abandoned/expired downgrade a settled status ─────────
        # Abandoned/Expired → unverified only makes sense while still pending.
        # If the field is already approved (or rejected), a stray reset would
        # silently strip a verified user — leave it untouched.
        current_status = (user.license_verification if vtype == "licence"
                          else user.id_verification)
        if new_status == unverified and current_status != pending:
            log.info("Didit webhook: not resetting %s for user %s — status %s is not pending",
                     vtype, user_id, current_status)
            return JSONResponse({"status": "ignored_no_downgrade"})

        # ── 6. Extract rejection reason if declined ────────────────────────────
        rejection_reason: str | None = None
        if new_status == rejected:
            decision = payload.get("decision") or {}
            # Try to surface a human-readable reason from id_verifications
            id_checks = decision.get("id_verifications") or []
            if id_checks:
                rejection_reason = id_checks[0].get("status") or None

        # ── 7. Apply to the correct verification field(s) ─────────────────────
        if vtype == "licence":
            user.license_verification = new_status
            # Only touch the reason on a decision; don't wipe it on a later
            # In Review / reset event.
            if new_status == rejected:
                user.license_rejection_reason = rejection_reason
            elif new_status == approved:
                user.license_rejection_reason = None
            if new_status == unverified:
                user.didit_licence_session_id = None
            # Licence covers identity — propagate approval
            if new_status == approved:
                user.id_verification      = approved
                user.id_rejection_reason  = None
                # ── Extract document expiry date from Didit payload ────────────
                # Didit returns document fields inside payload["decision"]["document"].
                # Field name varies by document type; try all known variants.
                try:
                    decision = payload.get("decision") or {}
                    doc      = decision.get("document") or {}
                    expiry_raw = (
                        doc.get("expiry_date")
                        or doc.get("date_of_expiry")
                        or doc.get("expiration_date")
                        or doc.get("expires")
                    )
                    if expiry_raw:
                        from datetime import date as _date
                        expiry_str = str(expiry_raw).split("T")[0]   # handle ISO datetime
                        user.licence_expiry          = _date.fromisoformat(expiry_str)
                        user.licence_expiry_warned_at = None          # reset warning on re-verify
                        log.info(
                            "Didit: extracted licence expiry %s for user %s",
                            user.licence_expiry, user_id,
                        )
                    else:
                        log.info(
                            "Didit: no expiry date in payload for user %s — "
                            "decision keys: %s", user_id, list(doc.keys()),
                        )
                except Exception as exc:
                    log.warning("Didit: failed to extract expiry date for user %s: %s", user_id, exc)
            # On decline/unverified: only reset licence; identity keeps its state
        else:
            user.id_verification = new_status
            if new_status == rejected:
                user.id_rejection_reason = rejection_reason
            elif new_status == approved:
                user.id_rejection_reason = None
            if new_status == unverified:
                user.didit_identity_session_id = None

        db.commit()
        log.info("Didit: user %s %s → %s", user_id, vtype, new_status)

        # ── 8. Send notification email (non-blocking) ─────────────────────────
        if new_status == approved:
            mailer.verification_approved(user, vtype)
        elif new_status == rejected:
            mailer.verification_rejected(user, vtype, rejection_reason)

    except Exception as exc:
        log.exception("Didit webhook processing error (session=%s): %s", session_id, exc)
        db.rollback()
        return JSONResponse({"error": "processing_error"}, status_code=500)
    finally:
        db.close()

    return JSONResponse({"status": "ok"})


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


