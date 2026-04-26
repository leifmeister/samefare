"""
Background tasks — run on a timer inside the FastAPI lifespan.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app.database import SessionLocal
from app import models

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

                # ── Case 2: late cancellation by a paying passenger ───────
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


# ── Main loop ─────────────────────────────────────────────────────────────────

async def auto_complete_loop() -> None:
    """Runs forever, triggering completion and rating checks every 10 minutes."""
    while True:
        await asyncio.sleep(10 * 60)
        _run_auto_complete()
        _run_auto_ratings()
