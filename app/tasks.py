"""
Background tasks — run on a timer inside the FastAPI lifespan.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app.database import SessionLocal
from app import models

log = logging.getLogger(__name__)


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


async def auto_complete_loop() -> None:
    """Runs forever, triggering completion checks every 10 minutes."""
    while True:
        await asyncio.sleep(10 * 60)
        _run_auto_complete()
