"""
Background tasks — run on a timer inside the FastAPI lifespan.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app.database import SessionLocal
from app import models, sms

log = logging.getLogger(__name__)


# ── Auto-complete ─────────────────────────────────────────────────────────────

def _run_auto_complete() -> None:
    """
    Mark trips and their confirmed bookings as completed once
    2 hours have passed since the scheduled departure time.
    """
    cutoff = datetime.utcnow() - timedelta(hours=2)
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


# ── Main loop ─────────────────────────────────────────────────────────────────

async def auto_complete_loop() -> None:
    """Runs forever, triggering all background checks every 10 minutes."""
    while True:
        await asyncio.sleep(10 * 60)
        _run_auto_complete()
        _run_auto_ratings()
        _run_trip_reminders()
