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
_run_prepare_driver_payouts  Daily: batch payout_ready items + digest SMS (approve on-demand in admin).
"""
import asyncio
import datetime as _dt_module
import logging
from datetime import datetime, timedelta
from itertools import groupby

from sqlalchemy import Integer, func
from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app import models, sms, email as mailer
from app import rapyd as rapyd_client
from app.fuel import refresh_fuel_price
from app.utils import build_route_graph, recompute_seats_available
from app.rapyd import RapydError

log = logging.getLogger(__name__)


# ── Auto-complete ─────────────────────────────────────────────────────────────

def _run_auto_complete() -> None:
    """
    Mark trips and their confirmed bookings as completed once BOTH the estimated
    arrival time has passed AND the driver-no-show reporting window has closed.

    estimated_arrival = departure + (distance ÷ 80 km/h) + 1 h buffer.
    Set at trip creation; recomputed on edit.

    The no-show window (departure + 4 h, per report_driver_no_show) gates
    completion as well: on short rides estimated_arrival can fall inside that
    window, and completing early would flip confirmed bookings to `completed`
    and lock passengers/drivers out of reporting a legitimate no-show. So a trip
    completes at max(estimated_arrival, departure + 4 h).

    Trips created before estimated_arrival existed (NULL) fall back to
    departure + 6 h — already beyond the 4 h window.
    """
    from sqlalchemy import or_, case as sa_case
    now = datetime.utcnow()
    db  = SessionLocal()
    try:
        stale = (
            db.query(models.Trip)
            .filter(
                models.Trip.status == models.TripStatus.active,
                # Never auto-complete a trip flagged as a driver no-show — its
                # bookings are cancelled+refunded and the driver gets no payout.
                models.Trip.driver_no_show == False,  # noqa: E712
                or_(
                    # New trips: estimated arrival passed AND the 4 h driver-no-show
                    # reporting window has closed.
                    (models.Trip.estimated_arrival != None)  # noqa: E711
                    & (models.Trip.estimated_arrival <= now)
                    & (models.Trip.departure_datetime <= now - timedelta(hours=4)),
                    # Legacy trips without estimated_arrival: 6 h flat fallback
                    # (already beyond the 4 h no-show window).
                    (models.Trip.estimated_arrival == None)  # noqa: E711
                    & (models.Trip.departure_datetime <= now - timedelta(hours=6)),
                ),
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
    Cancel stale bookings whose window has passed:

    1. awaiting_payment bookings past their 24-hour payment deadline — these
       hold seats, which are released back to the trip.
    2. pending (request-to-book) bookings on a trip that has already departed —
       the driver can no longer accept them, so the request would otherwise hang
       as "Pending approval" forever. These hold no seats and have no
       payment_deadline (so the query above never catches them), and no charge
       was ever made (the card was only saved, never authorised), so cancelling
       is clean — no seat release or refund needed.

    Runs every 10 minutes, so maximum overshoot is ~10 minutes.
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

        # Stale pending request-to-book bookings on departed trips.
        stale_pending = (
            db.query(models.Booking)
            .join(models.Trip, models.Booking.trip_id == models.Trip.id)
            .filter(
                models.Booking.status          == models.BookingStatus.pending,
                models.Trip.departure_datetime <= now,
            )
            .all()
        )
        for booking in stale_pending:
            booking.status = models.BookingStatus.cancelled
            booking.cancellation_reason = "request_expired"

        if expired or stale_pending:
            db.commit()
            log.info(
                "Expired %d unpaid booking(s); cancelled %d stale pending request(s) on departed trips",
                len(expired), len(stale_pending),
            )
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
    the next calendar day. A flag on the Trip row ensures each trip is reminded
    only once, no matter how often the job fires.

    Quiet hours: the job only sends during the configured evening window
    (REMINDER_HOUR_START–REMINDER_HOUR_END, local == UTC for Iceland; default
    18:00–22:00) so a reminder never lands in the middle of the night. Because it
    targets trips departing *tomorrow* rather than a rolling "N hours from now"
    window, the send time is decoupled from the departure time — a 09:00 ride is
    reminded the evening before, not at 05:00.
    """
    from app.config import get_settings

    now      = datetime.utcnow()   # Iceland has no DST/offset, so UTC == local.
    settings = get_settings()

    # Outside the evening send window → do nothing this tick.
    if not (settings.reminder_hour_start <= now.hour < settings.reminder_hour_end):
        return

    tomorrow = (now + timedelta(days=1)).date()

    db = SessionLocal()
    try:
        upcoming = (
            db.query(models.Trip)
            .filter(
                models.Trip.status             == models.TripStatus.active,
                func.date(models.Trip.departure_datetime) == tomorrow,
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
            mailer.trip_reminder_to_driver(trip, len(confirmed))

            # Passenger reminders
            for booking in confirmed:
                sms.trip_reminder_to_passenger(booking)
                mailer.trip_reminder_to_passenger(booking)

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
      booking status:
        card_saved  → unchanged  (standard Case B: not yet confirmed, seat held on card token)
        confirmed   → awaiting_payment  (accepted pending booking: was confirmed on driver
                      acceptance but pre-auth failed at T-24h; must not appear as a paid
                      confirmed ride during the retry window)
      retry_deadline  → now + 2 hours
      Notifications   → passenger (SMS + email) + driver (SMS)
    """
    log.warning(
        "MIT did not result in ACT for booking %s — entering retry window. Reason: %s",
        booking.id, reason,
    )

    # No surcharge on retry — fee stays the same as the original booking.
    payment.status         = models.PaymentStatus.retry_pending
    payment.retry_deadline = now + timedelta(hours=2)

    # Confirmed bookings (accepted pending trips whose driver approved before T-24h)
    # must be demoted — a confirmed+retry_pending booking looks fully paid to all UIs.
    # awaiting_payment is the correct state: seat is still held, card is on file,
    # passenger needs to take action. When they add a new card via the Hosted Card
    # page, card_saved_page()/CUSTOMER_PAYMENT_METHOD_CREATED moves awaiting_payment
    # to card_saved and the MIT re-fires.
    if booking.status == models.BookingStatus.confirmed:
        booking.status = models.BookingStatus.awaiting_payment

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
                      → retry window opened (no surcharge; fee unchanged).
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
                # card_saved bookings = regular Case B (standard far-future booking)
                # confirmed bookings  = accepted pending bookings (card saved upfront,
                #                       driver accepted, MIT still fires at T-24h)
                models.Booking.status.in_([
                    models.BookingStatus.card_saved,
                    models.BookingStatus.confirmed,
                ]),
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
                    # Card is genuinely authorised.
                    # Accepted pending bookings are already confirmed (booking.status
                    # was set to confirmed when the driver accepted); only send
                    # confirmation emails for bookings that weren't confirmed yet
                    # (standard Case B: card_saved → confirmed here).
                    was_already_confirmed = booking.status == models.BookingStatus.confirmed

                    payment.status          = models.PaymentStatus.authorised
                    payment.auth_expires_at = now + timedelta(days=7)
                    booking.status          = models.BookingStatus.confirmed

                    db.commit()
                    db.refresh(booking)
                    if not was_already_confirmed:
                        mailer.booking_confirmed_to_passenger(booking)
                        mailer.booking_confirmed_to_driver(booking)
                    log.info(
                        "MIT authorised (ACT) for booking %s — Rapyd payment %s "
                        "(was_already_confirmed=%s)",
                        booking.id, payment.rapyd_payment_id, was_already_confirmed,
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
    # A passenger who cancels after pre-auth is charged in full (no refund policy).
    # cancel_booking() sets capture_at = now so the next cron tick picks this up.
    models.BookingStatus.cancelled,
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
                    active = [b for b in locked_trip.bookings
                              if b.id != booking.id and b.status in {
                                  models.BookingStatus.awaiting_payment,
                                  models.BookingStatus.confirmed,
                                  models.BookingStatus.card_saved,
                              }]
                    graph = build_route_graph(db)
                    locked_trip.seats_available = recompute_seats_available(
                        graph, locked_trip.seats_total, active,
                        locked_trip.origin, locked_trip.destination,
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
                    # Passengers who cancel after pre-auth forfeit the full amount.
                    # The booking is cancelled but the payment is still captured
                    # and the driver is owed their contribution.
                    models.BookingStatus.cancelled,
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
    since configured their bank details (Blikk Icelandic IBAN).

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


# Date (UTC) of the last daily payout send, so the once-a-day window fires only
# once. Module-level: a process restart may re-enter the window, but the send is
# idempotent (already-sent items aren't re-selected) so at worst a duplicate
# digest SMS — never a duplicate transfer.
_last_payout_send_date = None

# Bounded retry for the daily prep: if some driver's batch keeps failing to
# build, retry on the next tick a few times (covers transient DB hiccups) then
# give up for the day so we don't loop/log every 10 min. Systemic failures (the
# whole query) are NOT bounded here — those should keep retrying until the DB
# recovers (see the outer except in _run_prepare_driver_payouts).
PAYOUT_PREP_MAX_ATTEMPTS   = 3
_payout_prep_attempts      = 0
_payout_prep_attempts_date = None




def _run_prepare_driver_payouts() -> None:
    """
    Once a day, batch the day's `payout_ready` PayoutItems per driver into
    `pending` DriverPayout records and send the approver one digest SMS.

    Payouts are deliberately NOT submitted to Blikk here. A Blikk SCA approval
    link is valid only briefly, so a link pre-created now would expire before the
    approver acts. Instead the admin /admin/payouts page submits each batch
    ON DEMAND when the approver clicks Approve, generating a fresh link at that
    moment. This task only prepares the batches and notifies.

    No-op when PAYOUT_ENABLED=False (the default).

    Daily window: building once a day (at/after PAYOUT_RUN_HOUR UTC) bundles all
    of a driver's rides that settled that day into a single batch/approval and
    means the approver gets one digest rather than an all-day trickle.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.payout_enabled:
        return

    global _last_payout_send_date, _payout_prep_attempts, _payout_prep_attempts_date
    now = datetime.utcnow()
    if now.hour < settings.payout_run_hour or _last_payout_send_date == now.date():
        return
    # NB: don't mark today as done until the work actually succeeds — otherwise a
    # failed query or batch build would be skipped until tomorrow with no retry.
    # Building each batch is idempotent (deterministic idempotency key), so a
    # retry on the next tick is safe and re-batches nothing already done.
    # Reset the per-day attempt counter when a new day's run begins.
    if _payout_prep_attempts_date != now.date():
        _payout_prep_attempts      = 0
        _payout_prep_attempts_date = now.date()

    from app.payout import build_driver_payout_batch

    db = SessionLocal()
    try:
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

        built = 0
        failures = 0
        for (driver_id, _method), group in groupby(ready_items, key=_batch_key):
            items = list(group)
            driver = items[0].driver
            try:
                batch = build_driver_payout_batch(db, driver, items)
                if batch:
                    db.commit()
                    built += 1
            except Exception as exc:
                failures += 1
                log.error("Failed to build DriverPayout for driver %s: %s", driver_id, exc)
                db.rollback()

        if built:
            log.info("Prepared %d driver payout batch(es) for approval", built)
        # Mark today as done when every batch prepared cleanly. If any driver
        # failed, retry on the next tick (already-built batches are now
        # `batched`/`pending` and won't be re-selected) — but only up to
        # PAYOUT_PREP_MAX_ATTEMPTS, then give up for the day so we don't loop and
        # log every 10 min on a persistently-failing record.
        if failures == 0:
            _last_payout_send_date = now.date()
        else:
            _payout_prep_attempts += 1
            if _payout_prep_attempts >= PAYOUT_PREP_MAX_ATTEMPTS:
                _last_payout_send_date = now.date()   # give up for today
                log.error(
                    "Prepare driver payouts: %d driver(s) still failing after %d attempts — "
                    "giving up for today; their payout items remain `payout_ready` and need "
                    "manual attention", failures, _payout_prep_attempts,
                )
            else:
                log.warning(
                    "Prepare driver payouts: %d driver(s) failed (attempt %d/%d) — will retry next tick",
                    failures, _payout_prep_attempts, PAYOUT_PREP_MAX_ATTEMPTS,
                )
        # No SMS digest — the operator reviews and approves on the /admin/payouts
        # board, which they check daily.

    except Exception as exc:
        # Whole-task failure (e.g. the query) — date stays unset so we retry.
        log.warning("Prepare driver payouts task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


def _run_reconcile_driver_payouts() -> None:
    """
    Reconcile `sent` Blikk DriverPayout batches against their final bank status.

    Blikk Payment Channel transfers settle asynchronously, so a batch Blikk
    accepted can still be REJECTED at the bank (e.g. insufficient funds in
    SameFare's account). This polls each sent Blikk batch and either confirms it
    (SUCCESS) or fails it (REJECTED/ERROR/CANCELLED → items back to payout_failed
    for operator-driven recovery). Not gated on PAYOUT_ENABLED: if any batch was
    ever sent it must be reconciled regardless of the flag's current value.
    """
    from app.payout import reconcile_blikk_payout

    db = SessionLocal()
    try:
        sent_batches = (
            db.query(models.DriverPayout)
            .filter(
                models.DriverPayout.status == models.DriverPayoutStatus.sent,
                models.DriverPayout.payout_method == models.PayoutMethod.blikk,
            )
            .options(joinedload(models.DriverPayout.items))
            .all()
        )
        for batch in sent_batches:
            try:
                if reconcile_blikk_payout(db, batch):
                    db.commit()
            except Exception as exc:
                log.error("Failed to reconcile DriverPayout %s: %s", batch.id, exc)
                db.rollback()
    except Exception as exc:
        log.warning("Reconcile driver payouts task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── Fuel price refresh ────────────────────────────────────────────────────────

_fuel_price_last_refreshed: datetime | None = None
_FUEL_REFRESH_INTERVAL_H   = 23   # refresh once per day; capped at 7-day cache

# ── Licence expiry monitoring ─────────────────────────────────────────────────
_licence_expiry_last_run_date: "_dt_module.date | None" = None


def _run_licence_expiry_check() -> None:
    """
    Daily licence expiry sweep.  Runs once per UTC calendar day.

    Actions
    -------
    1. 30-day warning  — driver's licence expires in ≤30 days and no warning
                         has been sent in the last 25 days.  Sends a warning
                         email and records licence_expiry_warned_at.
    2. Expired         — licence_expiry is today or in the past.  Resets
                         license_verification → unverified, sets posting_suspended,
                         and emails the driver asking them to re-verify.
    """
    global _licence_expiry_last_run_date
    today = _dt_module.datetime.utcnow().date()
    if _licence_expiry_last_run_date == today:
        return
    _licence_expiry_last_run_date = today

    db = SessionLocal()
    try:
        warn_threshold = today + timedelta(days=30)

        # ── 1. Send 30-day warnings ────────────────────────────────────────────
        # Drivers whose licence expires within 30 days, who haven't been warned
        # recently (> 25 days since last warning or never warned).
        warn_cutoff = datetime.utcnow() - timedelta(days=25)
        drivers_to_warn = (
            db.query(models.User)
            .filter(
                models.User.licence_expiry != None,
                models.User.licence_expiry >  today,          # not yet expired
                models.User.licence_expiry <= warn_threshold,
                models.User.license_verification == models.VerificationStatus.approved,
                models.User.is_active == True,
                models.User.deleted_at == None,
                (
                    (models.User.licence_expiry_warned_at == None) |
                    (models.User.licence_expiry_warned_at < warn_cutoff)
                ),
            )
            .all()
        )
        for driver in drivers_to_warn:
            days_left = (driver.licence_expiry - today).days
            try:
                mailer.licence_expiry_warning(driver, days_left)
                driver.licence_expiry_warned_at = datetime.utcnow()
                log.info(
                    "Licence expiry: warned user %s — %d day(s) remaining.",
                    driver.id, days_left,
                )
            except Exception as exc:
                log.warning("Licence expiry warning email failed for user %s: %s", driver.id, exc)

        # ── 2. Suspend expired licences ────────────────────────────────────────
        expired_drivers = (
            db.query(models.User)
            .filter(
                models.User.licence_expiry != None,
                models.User.licence_expiry <= today,
                models.User.license_verification == models.VerificationStatus.approved,
                models.User.is_active == True,
                models.User.deleted_at == None,
            )
            .all()
        )
        for driver in expired_drivers:
            driver.license_verification = models.VerificationStatus.unverified
            if not driver.posting_suspended:
                driver.posting_suspended  = True
                driver.suspension_reason  = (
                    f"Driver's licence expired on {driver.licence_expiry}. "
                    f"Re-verification required before posting new trips."
                )
            try:
                mailer.licence_expired_suspension(driver)
                log.info(
                    "Licence expiry: suspended user %s — licence expired %s.",
                    driver.id, driver.licence_expiry,
                )
            except Exception as exc:
                log.warning("Licence expired email failed for user %s: %s", driver.id, exc)

        db.commit()

    except Exception as exc:
        log.exception("Licence expiry check failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── AML monitoring ─────────────────────────────────────────────────────────────
# Runs once per calendar day (UTC).  Checks for red-flag patterns across
# bookings and trips created in the previous 24 hours and emails the admin
# inbox if anything hits.  No alert = clean day; silent otherwise.
_aml_last_run_date: "_dt_module.date | None" = None


def _run_aml_monitoring() -> None:
    """
    Nightly AML transaction-monitoring sweep.

    Red flags checked
    -----------------
    1. High booking volume     — any passenger with >5 bookings created in 24 h.
    2. Cancellation cycling    — any passenger with ≥3 cancellations in 24 h and 0
                                 completions (book-and-cancel fraud signal).
    3. Abnormal trip pricing   — any trip posted in 24 h with a price-per-seat
                                 more than 3× the 30-day platform average.
    4. Driver no-show repeat   — any driver with no_shows_confirmed > 1 who is
                                 still active (suspension logic covers 1; this
                                 catches any edge-case that slipped through).

    Fires once per UTC calendar day regardless of how many 10-minute ticks
    have elapsed since midnight.  Only sends an email when ≥1 flag is raised.
    """
    global _aml_last_run_date
    today = _dt_module.datetime.utcnow().date()
    if _aml_last_run_date == today:
        return
    _aml_last_run_date = today

    db = SessionLocal()
    try:
        now   = datetime.utcnow()
        since = now - timedelta(hours=24)
        flags: list[dict] = []

        # ── 1. High booking volume — any passenger with >5 bookings in 24 h ──
        high_volume = (
            db.query(models.User, func.count(models.Booking.id).label("n"))
            .join(models.Booking, models.Booking.passenger_id == models.User.id)
            .filter(
                models.Booking.created_at >= since,
                models.Booking.status.notin_([models.BookingStatus.cancelled]),
            )
            .group_by(models.User.id)
            .having(func.count(models.Booking.id) > 5)
            .all()
        )
        for user, n in high_volume:
            flags.append({
                "flag":    "High booking volume",
                "user_id": user.id,
                "name":    user.full_name,
                "detail":  f"{n} active bookings created in the last 24 h",
            })

        # ── 2. Cancellation cycling — ≥3 cancels, 0 completions in 24 h ─────
        is_cancelled = func.cast(
            models.Booking.status == models.BookingStatus.cancelled, Integer
        )
        is_completed = func.cast(
            models.Booking.status == models.BookingStatus.completed, Integer
        )
        cycling_rows = (
            db.query(
                models.User,
                func.sum(is_cancelled).label("n_cancel"),
                func.sum(is_completed).label("n_complete"),
            )
            .join(models.Booking, models.Booking.passenger_id == models.User.id)
            .filter(models.Booking.created_at >= since)
            .group_by(models.User.id)
            .having(func.sum(is_cancelled) >= 3)
            .all()
        )
        for user, n_cancel, n_complete in cycling_rows:
            if (n_complete or 0) == 0:
                flags.append({
                    "flag":    "Cancellation cycling",
                    "user_id": user.id,
                    "name":    user.full_name,
                    "detail":  f"{n_cancel} cancellations, 0 completions in 24 h",
                })

        # ── 3. Abnormal trip pricing — >3× 30-day platform average ───────────
        avg_price = db.query(func.avg(models.Trip.price_per_seat)).filter(
            models.Trip.created_at >= now - timedelta(days=30),
            models.Trip.status != models.TripStatus.cancelled,
        ).scalar()
        platform_avg = float(avg_price or 0)

        if platform_avg > 0:
            odd_trips = (
                db.query(models.Trip)
                .filter(
                    models.Trip.created_at >= since,
                    models.Trip.price_per_seat > platform_avg * 3,
                    models.Trip.status != models.TripStatus.cancelled,
                )
                .all()
            )
            for trip in odd_trips:
                flags.append({
                    "flag":    "Abnormal trip price",
                    "user_id": trip.driver_id,
                    "name":    trip.driver.full_name if trip.driver else "—",
                    "detail":  (
                        f"Trip #{trip.id}: {trip.price_per_seat:,} ISK/seat "
                        f"({trip.price_per_seat / platform_avg:.1f}× avg {platform_avg:,.0f} ISK) "
                        f"— {trip.origin} → {trip.destination}"
                    ),
                })

        # ── 4. Repeat no-show driver still active ─────────────────────────────
        for user in (
            db.query(models.User)
            .filter(
                models.User.no_shows_confirmed > 1,
                models.User.is_active == True,
                models.User.deleted_at == None,
            )
            .all()
        ):
            flags.append({
                "flag":    "Repeat no-show driver — account still active",
                "user_id": user.id,
                "name":    user.full_name,
                "detail":  (
                    f"{user.no_shows_confirmed} confirmed no-shows; "
                    f"posting_suspended={user.posting_suspended}"
                ),
            })

        if flags:
            mailer.aml_monitoring_alert(flags, today)
            log.info("AML monitoring: %d flag(s) for %s — alert sent.", len(flags), today)
        else:
            log.info("AML monitoring: clean sweep for %s.", today)

    except Exception as exc:
        log.exception("AML monitoring task failed: %s", exc)
    finally:
        db.close()


# ── 90-day counter reset ───────────────────────────────────────────────────────
# Runs once per calendar day (UTC).  Resets cancellations_90d and
# late_cancellations_90d to 0 for any driver whose most recent cancelled trip
# is more than 90 days old, so a bad patch doesn't haunt them forever.
_counter_reset_last_run_date: "_dt_module.date | None" = None


def _run_counter_reset() -> None:
    """
    Daily counter reset.  Runs once per UTC calendar day.

    For every driver with a non-zero 90-day cancellation counter, check
    whether they have cancelled any trip within the last 90 days.  If not,
    zero both counters.  posting_suspended is intentionally left unchanged —
    reinstatement is a manual admin decision.
    """
    global _counter_reset_last_run_date
    today = _dt_module.datetime.utcnow().date()
    if _counter_reset_last_run_date == today:
        return
    _counter_reset_last_run_date = today

    cutoff = _dt_module.datetime.utcnow() - timedelta(days=90)
    db = SessionLocal()
    try:
        # Drivers with at least one non-zero 90-day counter
        drivers = (
            db.query(models.User)
            .filter(
                (models.User.cancellations_90d > 0) |
                (models.User.late_cancellations_90d > 0),
            )
            .all()
        )

        reset_count = 0
        for driver in drivers:
            recent_cancel = (
                db.query(models.Trip)
                .filter(
                    models.Trip.driver_id == driver.id,
                    models.Trip.status    == models.TripStatus.cancelled,
                    models.Trip.updated_at >= cutoff,
                )
                .first()
            )
            if recent_cancel is None:
                # No cancellations in the past 90 days — clear the counters
                driver.cancellations_90d      = 0
                driver.late_cancellations_90d = 0
                reset_count += 1

        if reset_count:
            db.commit()
            log.info("Counter reset: cleared 90-day counters for %d driver(s)", reset_count)

    except Exception as exc:
        log.exception("Counter reset task failed: %s", exc)
        db.rollback()
    finally:
        db.close()


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
        _run_prepare_driver_payouts()   # daily: build batches + digest (approve on-demand in admin)
        _run_reconcile_driver_payouts() # confirm/fail sent Blikk payouts by bank settlement status
        # ── Pricing data ────────────────────────────────────────────────────────
        _run_refresh_fuel_price()       # cache apis.is p80 petrol price (once/day)
        # ── Daily once-per-day checks ────────────────────────────────────────────
        _run_licence_expiry_check()     # 30-day warning + suspend expired licences
        _run_aml_monitoring()           # red-flag sweep; emails admin if anything hits
        _run_counter_reset()            # zero 90-day cancel counters when window has cleared
