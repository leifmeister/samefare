"""
Background tasks — run on a timer inside the FastAPI lifespan.

Payment-related tasks (Rapyd integration)
-----------------------------------------
_run_mit_authorizations  Fire Case B merchant-initiated authorisations 24 h before departure.
_run_capture_payments    Capture all authorised payments at departure_datetime.
_run_retry_expiry        Release seats when a failed-MIT retry window closes with no update.

Payout ledger tasks
-------------------
_run_create_payout_items   Pair newly captured+completed payments to driver PayoutItems.
_run_advance_payout_items  Move pending items to payout_ready when bank details are added.
_run_send_driver_payouts   Batch and submit payout_ready items (no-op until PAYOUT_ENABLED=true).
"""
import asyncio
import logging
from datetime import datetime, timedelta
from itertools import groupby

from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app import models, sms, email as mailer
from app import rapyd as rapyd_client
from app.fuel import refresh_fuel_price
from app.rapyd import RapydError

log = logging.getLogger(__name__)


# ── Auto-complete ─────────────────────────────────────────────────────────────

def _run_auto_complete() -> None:
    """
    Mark trips and their confirmed bookings as completed once
    2 hours have passed since the scheduled departure time.

    No lower-bound date filter — any active trip whose departure is more than
    2 hours in the past is overdue and should be completed regardless of age.
    If the worker was down or trips were imported from old data they will be
    swept on the next run.  The only guard against junk test data is the
    future-date validation enforced at trip creation time.
    """
    now    = datetime.utcnow()
    cutoff = now - timedelta(hours=2)
    db = SessionLocal()
    try:
        stale = (
            db.query(models.Trip)
            .filter(
                models.Trip.status == models.TripStatus.active,
                models.Trip.departure_datetime <= cutoff,
            )
            .all()
        )
        for trip in stale:
            trip.status = models.TripStatus.completed
            for booking in trip.bookings:
                if booking.status == models.BookingStatus.confirmed:
                    booking.status = models.BookingStatus.completed
        if stale:
            db.commit()
            log.info("Auto-completed %d trip(s)", len(stale))
    except Exception as exc:
        log.warning("Auto-complete failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Payment expiry ───────────────────────────────────────────────────────────

def _run_expire_payments() -> None:
    """
    Cancel awaiting_payment bookings whose 24-hour payment window has passed.
    Releases held seats back to the trip so they can be booked by someone else.
    The cleanup task runs every 10 minutes, so maximum overshoot is ~10 minutes.
    """
    now = datetime.utcnow()
    db  = SessionLocal()
    try:
        expired = (
            db.query(models.Booking)
            .filter(
                models.Booking.status           == models.BookingStatus.awaiting_payment,
                models.Booking.payment_deadline != None,   # noqa: E711
                models.Booking.payment_deadline <= now,
            )
            .all()
        )
        for booking in expired:
            booking.status = models.BookingStatus.cancelled
            # Lock the trip row before releasing seats to avoid racing with
            # concurrent booking requests on the same trip.
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
        if expired:
            db.commit()
            log.info("Expired %d unpaid booking(s) and released seats", len(expired))
    except Exception as exc:
        log.warning("Payment expiry task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Auto-ratings ──────────────────────────────────────────────────────────────

def _review_exists(db, booking_id: int, review_type: models.ReviewType) -> bool:
    return (
        db.query(models.Review)
        .filter(
            models.Review.booking_id  == booking_id,
            models.Review.review_type == review_type,
        )
        .first() is not None
    )


def _prior_auto_penalties(db, passenger_id: int) -> int:
    """Count 1-star auto-ratings this passenger has already received."""
    return (
        db.query(models.Review)
        .filter(
            models.Review.reviewee_id == passenger_id,
            models.Review.review_type == models.ReviewType.driver_to_passenger,
            models.Review.rating      == 1,
            models.Review.is_auto     == True,  # noqa: E712
        )
        .count()
    )


def _run_auto_ratings() -> None:
    """
    14 days after a trip departs, fill in any missing reviews automatically.

    Rules
    -----
    Smooth ride (booking.status == completed, no review left):
        • passenger → driver : 5 stars
        • driver   → passenger : 5 stars

    Late cancellation (booking paid, cancelled < 24 h before departure):
        • First offence : grace — no rating issued
        • Subsequent   : driver → passenger : 1 star
    """
    cutoff = datetime.utcnow() - timedelta(days=14)
    db = SessionLocal()
    try:
        trips = (
            db.query(models.Trip)
            .filter(
                models.Trip.status             == models.TripStatus.completed,
                models.Trip.departure_datetime <= cutoff,
            )
            .all()
        )

        created = 0

        for trip in trips:
            for booking in trip.bookings:

                # ── Case 1: smooth ride ───────────────────────────────────
                if booking.status == models.BookingStatus.completed:

                    if not _review_exists(db, booking.id,
                                          models.ReviewType.passenger_to_driver):
                        db.add(models.Review(
                            booking_id  = booking.id,
                            trip_id     = trip.id,
                            reviewer_id = booking.passenger_id,
                            reviewee_id = trip.driver_id,
                            review_type = models.ReviewType.passenger_to_driver,
                            rating      = 5,
                            is_auto     = True,
                        ))
                        created += 1

                    if not _review_exists(db, booking.id,
                                          models.ReviewType.driver_to_passenger):
                        db.add(models.Review(
                            booking_id  = booking.id,
                            trip_id     = trip.id,
                            reviewer_id = trip.driver_id,
                            reviewee_id = booking.passenger_id,
                            review_type = models.ReviewType.driver_to_passenger,
                            rating      = 5,
                            is_auto     = True,
                        ))
                        created += 1

                # ── Case 2: passenger no-show (marked by driver) ─────────
                elif booking.status == models.BookingStatus.no_show:
                    if not _review_exists(db, booking.id,
                                          models.ReviewType.driver_to_passenger):
                        if _prior_auto_penalties(db, booking.passenger_id) > 0:
                            # Repeat offender — issue 1-star penalty
                            db.add(models.Review(
                                booking_id  = booking.id,
                                trip_id     = trip.id,
                                reviewer_id = trip.driver_id,
                                reviewee_id = booking.passenger_id,
                                review_type = models.ReviewType.driver_to_passenger,
                                rating      = 1,
                                is_auto     = True,
                            ))
                            created += 1
                        # else first offence — grace period, do nothing

                # ── Case 3: late cancellation by a paying passenger ───────
                elif (booking.status == models.BookingStatus.cancelled
                      and booking.payment is not None):

                    hours_before = (
                        trip.departure_datetime - booking.updated_at
                    ).total_seconds() / 3600

                    if hours_before < 24:
                        if not _review_exists(db, booking.id,
                                              models.ReviewType.driver_to_passenger):
                            if _prior_auto_penalties(db, booking.passenger_id) > 0:
                                # Not a first offence — issue 1-star penalty
                                db.add(models.Review(
                                    booking_id  = booking.id,
                                    trip_id     = trip.id,
                                    reviewer_id = trip.driver_id,
                                    reviewee_id = booking.passenger_id,
                                    review_type = models.ReviewType.driver_to_passenger,
                                    rating      = 1,
                                    is_auto     = True,
                                ))
                                created += 1
                            # else: first offence — grace period, do nothing

        if created:
            db.commit()
            log.info("Auto-ratings: created %d review(s)", created)

    except Exception as exc:
        log.warning("Auto-rating failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Day-before trip reminders ─────────────────────────────────────────────────

def _run_trip_reminders() -> None:
    """
    Send SMS reminders to drivers and confirmed passengers for trips departing
    tomorrow.  Runs once per hour; uses a flag on the Trip row to ensure each
    trip only gets one reminder regardless of how many times the job fires.

    Timing: fires between 19:00–21:00 local time (we store UTC; Iceland is UTC/UTC+0,
    so the window is the same).  Reminds for any trip departing between
    20 and 28 hours from now — catching the "this time tomorrow" window
    no matter when in the hour the job runs.
    """
    now    = datetime.utcnow()
    lo     = now + timedelta(hours=20)
    hi     = now + timedelta(hours=28)

    db = SessionLocal()
    try:
        upcoming = (
            db.query(models.Trip)
            .filter(
                models.Trip.status             == models.TripStatus.active,
                models.Trip.departure_datetime >= lo,
                models.Trip.departure_datetime <  hi,
                models.Trip.reminder_sent      == False,  # noqa: E712
            )
            .all()
        )

        for trip in upcoming:
            confirmed = [
                b for b in trip.bookings
                if b.status == models.BookingStatus.confirmed
            ]
            if not confirmed:
                # No confirmed passengers yet — still remind the driver
                # only if they posted the trip (they need to know nobody booked)
                # BlaBlaCar doesn't remind in this case; skip.
                trip.reminder_sent = True
                continue

            # Driver reminder
            sms.trip_reminder_to_driver(trip, len(confirmed))

            # Passenger reminders
            for booking in confirmed:
                sms.trip_reminder_to_passenger(booking)

            trip.reminder_sent = True
            log.info("Sent reminders for trip %d (%s → %s)", trip.id, trip.origin, trip.destination)

        if upcoming:
            db.commit()

    except Exception as exc:
        log.warning("Trip reminders failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Case B: MIT authorisation (24 h before departure) ─────────────────────────

# Rapyd payment status values relevant to MIT auth.
# Only ACT means the card is genuinely authorised and safe to capture later.
# PEN (pending) means the bank hasn't settled the decision yet — it can still
# fail, so we treat it identically to an explicit failure: open the retry window.
_RAPYD_MIT_ACT = "ACT"


def _apply_mit_failure(
    db,
    booking: "models.Booking",
    payment: "models.Payment",
    now: "datetime",
    reason: str,
) -> None:
    """
    Shared handler for any MIT outcome that is not a confirmed ACT authorisation.
    Covers both hard API errors (RapydError) and soft non-ACT status values
    (PEN, ERR, CAN, or any other status Rapyd may return).

    Transitions:
      payment.status  → retry_pending
      booking stays   → card_saved  (NOT confirmed — seat is not yet paid for)
      service_fee / total_price bumped +5 % (retry surcharge)
      retry_deadline  → now + 2 hours
      Notifications   → passenger (SMS + email) + driver (SMS)
    """
    from app.routers.payments import calc_fees

    log.warning(
        "MIT did not result in ACT for booking %s — entering retry window. Reason: %s",
        booking.id, reason,
    )

    new_fee, new_total, _ = calc_fees(booking.subtotal, retry_surcharge=True)

    booking.service_fee       = new_fee
    booking.total_price       = new_total
    payment.passenger_total   = new_total
    payment.platform_fee      = new_fee
    payment.status            = models.PaymentStatus.retry_pending
    payment.retry_deadline    = now + timedelta(hours=2)
    payment.retry_fee_applied = True

    db.commit()

    sms.mit_auth_failed_to_passenger(booking, payment.retry_deadline)
    mailer.mit_auth_failed_to_passenger(booking, payment.retry_deadline)
    sms.mit_auth_failed_to_driver(booking)


def _run_mit_authorizations() -> None:
    """
    For every booking in `card_saved` state whose `auth_scheduled_for` has
    arrived, fire a Rapyd merchant-initiated payment (capture=False).

    Outcome routing
    ---------------
    Rapyd status ACT  → payment authorised, booking confirmed, passenger notified.
    Rapyd status other (PEN / ERR / CAN / …)
                      → retry window opened, +5 % surcharge applied.
                        A successful API call is NOT the same as an authorised
                        payment — only ACT guarantees the card is held.
    RapydError        → same retry-window path as non-ACT status.
    """
    now = datetime.utcnow()
    db  = SessionLocal()
    try:
        due_payments = (
            db.query(models.Payment)
            .join(models.Booking, models.Payment.booking_id == models.Booking.id)
            .filter(
                models.Booking.status             == models.BookingStatus.card_saved,
                models.Payment.status             == models.PaymentStatus.card_saved,
                models.Payment.auth_scheduled_for != None,   # noqa: E711
                models.Payment.auth_scheduled_for <= now,
                models.Payment.rapyd_payment_method_id != None,   # noqa: E711
            )
            .all()
        )

        for payment in due_payments:
            booking = payment.booking

            if not payment.rapyd_customer_id or not payment.rapyd_payment_method_id:
                log.warning(
                    "MIT skipped for booking %s — missing Rapyd customer/PM", booking.id
                )
                continue

            try:
                mit_data = rapyd_client.create_mit_payment(
                    amount            = booking.total_price,
                    customer_id       = payment.rapyd_customer_id,
                    payment_method_id = payment.rapyd_payment_method_id,
                    capture           = False,
                    idempotency_key   = f"mit-{payment.id}",
                    metadata          = {"booking_id": booking.id, "case": "B"},
                )

                rapyd_status = mit_data.get("status", "")

                # Always persist the Rapyd payment ID regardless of status — it is
                # needed for investigation and for any subsequent capture attempt.
                if mit_data.get("id"):
                    payment.rapyd_payment_id = mit_data["id"]

                if rapyd_status == _RAPYD_MIT_ACT:
                    # Card is genuinely authorised — safe to confirm the booking.
                    payment.status          = models.PaymentStatus.authorised
                    payment.auth_expires_at = now + timedelta(days=7)
                    booking.status          = models.BookingStatus.confirmed

                    db.commit()
                    db.refresh(booking)
                    mailer.booking_confirmed_to_passenger(booking)
                    mailer.booking_confirmed_to_driver(booking)
                    log.info(
                        "MIT authorised (ACT) for booking %s — Rapyd payment %s",
                        booking.id, payment.rapyd_payment_id,
                    )
                else:
                    # Rapyd responded without error but the payment is not ACT.
                    # PEN means the bank is still deciding; ERR/CAN mean failure.
                    # In all cases we cannot confirm the booking — open retry window.
                    _apply_mit_failure(
                        db, booking, payment, now,
                        reason=f"Rapyd returned status {rapyd_status!r} (expected ACT)",
                    )

            except RapydError as exc:
                _apply_mit_failure(
                    db, booking, payment, now,
                    reason=f"RapydError: {exc}",
                )

    except Exception as exc:
        log.warning("MIT authorisation task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Capture payments at departure ──────────────────────────────────────────────

# Booking states where the ride occurred and the card was not cancelled.
# After app downtime the auto-complete task can advance `confirmed` →
# `completed` before the capture task runs, so we must accept all three
# terminal "ride happened" states rather than requiring `confirmed` only.
# `no_show` is included because the passenger forfeits their contribution
# and the driver is still paid.
_CAPTURABLE_BOOKING_STATUSES = frozenset({
    models.BookingStatus.confirmed,
    models.BookingStatus.completed,
    models.BookingStatus.no_show,
})


def _run_capture_payments() -> None:
    """
    Capture every authorised Rapyd payment whose `capture_at` (= departure_datetime)
    has arrived.  Runs every 10 minutes so maximum capture delay is ~10 minutes,
    and also runs once on startup before auto-complete so overdue captures are
    never skipped after downtime.

    Accepted booking states: confirmed, completed, no_show.
    `completed` handles the downtime scenario where auto-complete advanced the
    booking before capture ran.  `no_show` handles passengers who forfeited their
    contribution — the driver is still owed payment.

    State transitions
    -----------------
    Rapyd capture endpoint returns synchronously but settlement may be async.
    We inspect the returned payment object's status field:

      response.status == "CLO" → provider confirmed capture synchronously;
                                  set payment.status = captured immediately.
      response.status != "CLO" → capture is in-flight; set capture_requested
                                  so the PAYMENT_CAPTURED webhook does the final
                                  transition to captured.

    This prevents payout items being created before the provider has confirmed
    the capture.  The idempotency key guards against double-charge on retry.

    On API failure → log error; payment stays `authorised` so the next run will
    retry.  The idempotency key prevents a duplicate charge even if a previous
    attempt partially succeeded on Rapyd's side.
    """
    now = datetime.utcnow()
    db  = SessionLocal()
    try:
        due = (
            db.query(models.Payment)
            .join(models.Booking, models.Payment.booking_id == models.Booking.id)
            .filter(
                models.Payment.status.in_([
                    models.PaymentStatus.authorised,
                    # Retry capture_requested payments that never received the
                    # PAYMENT_CAPTURED webhook (e.g. delivery failure).
                    # The idempotency key prevents a double charge.
                    models.PaymentStatus.capture_requested,
                ]),
                models.Payment.capture_at != None,   # noqa: E711
                models.Payment.capture_at <= now,
                models.Payment.rapyd_payment_id != None,   # noqa: E711
                models.Booking.status.in_(_CAPTURABLE_BOOKING_STATUSES),
            )
            .all()
        )

        for payment in due:
            try:
                resp = rapyd_client.capture_payment(
                    payment.rapyd_payment_id,
                    idempotency_key=f"capture-{payment.id}",
                )
                # resp is the Rapyd payment data dict (already unwrapped from .data)
                rapyd_status = (resp or {}).get("status", "")
                if rapyd_status == "CLO":
                    # Synchronous confirmation — promote directly to captured.
                    payment.status = models.PaymentStatus.captured
                    log.info(
                        "Payment %s (booking %s) captured synchronously (CLO)",
                        payment.id, payment.booking_id,
                    )
                else:
                    # Async confirmation pending — PAYMENT_CAPTURED webhook will
                    # complete the transition.  Do not mark captured yet.
                    payment.status = models.PaymentStatus.capture_requested
                    log.info(
                        "Payment %s (booking %s) capture requested "
                        "(Rapyd status=%r, awaiting PAYMENT_CAPTURED webhook)",
                        payment.id, payment.booking_id, rapyd_status,
                    )
            except RapydError as exc:
                log.error(
                    "Capture FAILED for payment %s (booking %s): %s",
                    payment.id, payment.booking_id, exc,
                )
                # Don't set failed — let the next run retry (idempotency key guards against double-charge)

        if due:
            db.commit()

    except Exception as exc:
        log.warning("Capture task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Retry expiry (Case B MIT failure window) ───────────────────────────────────

def _run_retry_expiry() -> None:
    """
    If a passenger's 2-hour retry window has closed without them updating their
    card, release their seat and cancel the booking.
    Both driver and passenger are notified.
    """
    now = datetime.utcnow()
    db  = SessionLocal()
    try:
        expired = (
            db.query(models.Payment)
            .join(models.Booking, models.Payment.booking_id == models.Booking.id)
            .filter(
                models.Payment.status         == models.PaymentStatus.retry_pending,
                models.Payment.retry_deadline != None,   # noqa: E711
                models.Payment.retry_deadline <= now,
            )
            .all()
        )

        for payment in expired:
            booking = payment.booking
            trip    = booking.trip

            payment.status = models.PaymentStatus.failed
            booking.status = models.BookingStatus.cancelled

            # Release held seats
            if trip.status == models.TripStatus.active:
                locked_trip = (
                    db.query(models.Trip)
                    .filter(models.Trip.id == trip.id)
                    .with_for_update()
                    .first()
                )
                if locked_trip:
                    locked_trip.seats_available = min(
                        locked_trip.seats_total,
                        locked_trip.seats_available + booking.seats_booked,
                    )

            db.commit()
            db.refresh(booking)

            # Notifications
            mailer.booking_cancelled_to_passenger(booking)
            sms.retry_expired_to_driver(booking)
            log.info(
                "Retry window expired for booking %s — seat released, booking cancelled",
                booking.id,
            )

    except Exception as exc:
        log.warning("Retry expiry task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Check for auth_expired payments ───────────────────────────────────────────

def _run_auth_expiry_check() -> None:
    """
    Mark payments as auth_expired if the 7-day Rapyd authorisation window
    has lapsed without a capture.  These need manual review.
    """
    now = datetime.utcnow()
    db  = SessionLocal()
    try:
        lapsed = (
            db.query(models.Payment)
            .filter(
                models.Payment.status          == models.PaymentStatus.authorised,
                models.Payment.auth_expires_at != None,   # noqa: E711
                models.Payment.auth_expires_at <= now,
            )
            .all()
        )
        for payment in lapsed:
            payment.status = models.PaymentStatus.auth_expired
            log.error(
                "AUTH EXPIRED for payment %s (booking %s) — manual review required",
                payment.id, payment.booking_id,
            )
        if lapsed:
            db.commit()

    except Exception as exc:
        log.warning("Auth expiry check failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Refund retry ──────────────────────────────────────────────────────────────

def _run_retry_refunds() -> None:
    """
    Re-submit refunds that were not confirmed by Rapyd on the first attempt.

    Two statuses are retried:
      refund_requested — intent was recorded but the Rapyd call either never
                         happened (process crashed between DB write and API call)
                         or the response was lost.
      refund_failed    — a previous Rapyd call returned an error (provider
                         outage, rate-limit, etc.).

    The idempotency key f"refund-{booking.id}-{amount}" is stable, so Rapyd
    will de-duplicate any request it already accepted — re-submitting a
    refund_requested that actually succeeded on the first call is safe.

    On success → payment.status = refunded / partial_refund.
    On failure → payment.status = refund_failed (will retry again next cycle).
    """
    from app.rapyd import RapydError
    from app import rapyd as rapyd_client
    from app.payout import handle_refund_payout_impact

    db = SessionLocal()
    try:
        stale = (
            db.query(models.Payment)
            .join(models.Booking, models.Payment.booking_id == models.Booking.id)
            .filter(
                models.Payment.status.in_([
                    models.PaymentStatus.refund_requested,
                    models.PaymentStatus.refund_failed,
                ]),
                models.Payment.rapyd_payment_id != None,   # noqa: E711
                models.Payment.refund_amount > 0,
            )
            .options(
                joinedload(models.Payment.booking)
                    .joinedload(models.Booking.trip),
            )
            .all()
        )

        for payment in stale:
            booking = payment.booking
            try:
                rapyd_client.create_refund(
                    payment_id      = payment.rapyd_payment_id,
                    amount          = payment.refund_amount,
                    reason          = "cancellation",
                    idempotency_key = f"refund-{booking.id}-{payment.refund_amount}",
                )
                # Ledger entry + PayoutItem cancel/reverse land in the same commit
                # as the status transition — no partial-state window.
                handle_refund_payout_impact(
                    db, payment, booking.id, payment.refund_amount
                )
                payment.status = (
                    models.PaymentStatus.refunded
                    if payment.refund_amount >= payment.passenger_total
                    else models.PaymentStatus.partial_refund
                )
                db.commit()
                log.info(
                    "Retry refund succeeded: booking=%s amount=%s ISK",
                    booking.id, payment.refund_amount,
                )
            except RapydError as exc:
                payment.status = models.PaymentStatus.refund_failed
                db.commit()
                log.error(
                    "Retry refund still FAILING for booking %s (amount %s ISK): %s",
                    booking.id, payment.refund_amount, exc,
                )

    except Exception as exc:
        log.warning("Refund retry task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Payout ledger tasks ────────────────────────────────────────────────────────

def _run_create_payout_items() -> None:
    """
    Find captured payments whose booking is in a terminal ride state
    (completed or no_show) and create a PayoutItem for each one that
    doesn't already have one.

    Runs AFTER _run_auto_complete() each cycle so that bookings have had
    a chance to transition from confirmed → completed before we check them.
    """
    from app.payout import create_payout_item_for_payment

    db = SessionLocal()
    try:
        eligible = (
            db.query(models.Payment)
            .join(models.Booking, models.Payment.booking_id == models.Booking.id)
            .outerjoin(models.PayoutItem, models.PayoutItem.payment_id == models.Payment.id)
            .filter(
                models.Payment.status == models.PaymentStatus.captured,
                models.Booking.status.in_([
                    models.BookingStatus.completed,
                    models.BookingStatus.no_show,
                ]),
                models.PayoutItem.id == None,  # noqa: E711 — no payout item yet
            )
            .options(
                joinedload(models.Payment.booking)
                    .joinedload(models.Booking.trip)
                    .joinedload(models.Trip.driver),
            )
            .all()
        )
        created = 0
        for payment in eligible:
            item = create_payout_item_for_payment(db, payment)
            if item and item.id:
                db.commit()
                created += 1
        if created:
            log.info("Created %d PayoutItem(s)", created)
    except Exception as exc:
        log.warning("Payout item creation task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


def _run_advance_payout_items() -> None:
    """
    Move `pending` PayoutItems to `payout_ready` for drivers who have
    since configured their bank details (Blikk IBAN or Stripe Connect).

    Safe to run frequently — advance_payout_item() is a no-op when the
    driver has no payout method or the item is already past `pending`.
    """
    from app.payout import advance_payout_item

    db = SessionLocal()
    try:
        pending = (
            db.query(models.PayoutItem)
            .filter(models.PayoutItem.status == models.PayoutItemStatus.pending)
            .options(joinedload(models.PayoutItem.driver))
            .all()
        )
        advanced = 0
        for item in pending:
            if advance_payout_item(db, item):
                advanced += 1
        if advanced:
            db.commit()
            log.info("Advanced %d PayoutItem(s) to payout_ready", advanced)
    except Exception as exc:
        log.warning("Payout item advance task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


def _run_send_driver_payouts() -> None:
    """
    Batch all `payout_ready` PayoutItems per driver and submit each batch
    through the driver's configured payout rail (Blikk or Stripe Connect).

    This task is a no-op when PAYOUT_ENABLED=False (the default) so the
    ledger can be exercised end-to-end in staging without accidentally moving
    money before the provider integrations are wired up.

    When ready to go live: set PAYOUT_ENABLED=true in the environment and
    implement _send_blikk_payout() / _send_stripe_connect_payout() in payout.py.

    Two-phase execution — each phase has its own db.commit()
    ─────────────────────────────────────────────────────────
    Phase 1 — Persist batches (no provider call):
        Group payout_ready items by driver, call build_driver_payout_batch(),
        and commit.  Items move to payout_sent; each DriverPayout is created
        in `pending` status with a deterministic idempotency key derived from
        the committed PayoutItem IDs.  If the process crashes here, Phase 2
        on the next run will find the pending batch and submit it.

    Phase 2 — Submit pending batches:
        Query every `pending` DriverPayout (including any left over from
        previous crashed runs) and call send_driver_payout() for each.
        Commit after each submission.  If the provider accepted the call but
        the commit fails, the next run re-submits the same stored idempotency
        key and the provider de-duplicates — preventing a second real transfer.
    """
    from app.config import get_settings
    from app.payout import build_driver_payout_batch, send_driver_payout

    if not get_settings().payout_enabled:
        return

    db = SessionLocal()
    try:
        # ── Phase 1: Build and persist batches ────────────────────────────────
        # Commit BEFORE touching any provider.  Items become payout_sent here;
        # the batch sits in `pending` until Phase 2 submits it.
        ready_items = (
            db.query(models.PayoutItem)
            .filter(models.PayoutItem.status == models.PayoutItemStatus.payout_ready)
            .options(
                joinedload(models.PayoutItem.driver),
                joinedload(models.PayoutItem.payment),
            )
            .order_by(models.PayoutItem.driver_id)
            .all()
        )

        def _batch_key(item: models.PayoutItem):
            return (item.driver_id, str(item.payout_method))

        for (driver_id, _method), group in groupby(ready_items, key=_batch_key):
            items = list(group)
            driver = items[0].driver
            try:
                batch = build_driver_payout_batch(db, driver, items)
                if batch:
                    db.commit()   # durable before any provider call
            except Exception as exc:
                log.error(
                    "Failed to build DriverPayout for driver %s: %s", driver_id, exc
                )
                db.rollback()

        # ── Phase 2: Submit every pending batch ───────────────────────────────
        # Picks up batches just created above AND any stuck in `pending` from
        # previous runs (e.g. crashed between provider success and db.commit()).
        # The stored idempotency key guarantees provider-side deduplication.
        pending_batches = (
            db.query(models.DriverPayout)
            .filter(models.DriverPayout.status == models.DriverPayoutStatus.pending)
            .options(
                joinedload(models.DriverPayout.driver),
                joinedload(models.DriverPayout.items),
            )
            .all()
        )

        sent = 0
        for batch in pending_batches:
            try:
                success = send_driver_payout(db, batch)
                db.commit()
                if success:
                    sent += 1
                else:
                    log.error(
                        "DriverPayout %s for driver %s failed — see payout_ledger",
                        batch.id, batch.driver_id,
                    )
            except Exception as exc:
                log.error(
                    "Failed to submit DriverPayout %s for driver %s: %s",
                    batch.id, batch.driver_id, exc,
                )
                db.rollback()

        if sent:
            log.info("Submitted %d DriverPayout batch(es)", sent)

    except Exception as exc:
        log.warning("Send driver payouts task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Fuel price refresh ────────────────────────────────────────────────────────

_fuel_price_last_refreshed: datetime | None = None
_FUEL_REFRESH_INTERVAL_H   = 23   # refresh once per day; capped at 7-day cache


def _run_refresh_fuel_price() -> None:
    """
    Fetch the current p80 petrol price from apis.is and store it in
    fuel_price_cache.  Runs at startup and then at most once every
    _FUEL_REFRESH_INTERVAL_H hours so we do not hammer the external API.

    Three-tier fallback is handled inside refresh_fuel_price(); this wrapper
    just enforces the rate limit and logs the outcome.
    """
    global _fuel_price_last_refreshed
    now = datetime.utcnow()
    if (
        _fuel_price_last_refreshed is not None
        and (now - _fuel_price_last_refreshed).total_seconds() < _FUEL_REFRESH_INTERVAL_H * 3600
    ):
        return
    db = SessionLocal()
    try:
        refresh_fuel_price(db)
        _fuel_price_last_refreshed = now
    except Exception as exc:
        log.warning("Fuel price refresh task failed: %s", exc)
    finally:
        db.close()


# ── Main loop ─────────────────────────────────────────────────────────────────

async def auto_complete_loop() -> None:
    """
    Runs forever, triggering all background checks every 10 minutes.

    Ordering within each tick is intentional:
    1. Capture BEFORE auto-complete: confirmed bookings whose departure has
       passed must be captured before they transition to `completed`.
    2. Payout item creation AFTER auto-complete: bookings need to be in a
       terminal state (completed / no_show) before a PayoutItem is created.
    3. Advance and send AFTER creation: so newly created items are picked up
       in the same tick if bank details are already on file.
    """
    while True:
        await asyncio.sleep(10 * 60)
        # ── Incoming payment lifecycle ──────────────────────────────────────
        _run_capture_payments()         # capture authorised payments at departure
        _run_mit_authorizations()       # Case B: auth 24 h before departure
        _run_retry_expiry()             # expire failed-MIT retry windows
        _run_auth_expiry_check()        # surface lapsed auth windows
        _run_expire_payments()          # expire uncompleted checkout sessions
        _run_retry_refunds()            # re-submit refund_requested / refund_failed
        # ── Trip/booking lifecycle ──────────────────────────────────────────
        _run_auto_complete()            # confirmed → completed (2 h after departure)
        _run_auto_ratings()
        _run_trip_reminders()
        # ── Outgoing payout lifecycle (depends on completed bookings above) ─
        _run_create_payout_items()      # pair captured+completed → PayoutItem
        _run_advance_payout_items()     # pending → payout_ready when bank details added
        _run_send_driver_payouts()      # batch and submit (no-op until PAYOUT_ENABLED=true)
        # ── Pricing data ────────────────────────────────────────────────────────
        _run_refresh_fuel_price()       # cache apis.is p80 petrol price (once/day)
