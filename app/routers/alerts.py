"""
Ride alerts — let passengers save a route and get notified when a matching
ride appears.  Works for both logged-in users and guests (email-only).
"""
import logging
import secrets
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models, email as mailer
from app.database import get_db
from app.dependencies import get_current_user_optional, get_template_context
from app.limiter import rate_limit
from app.utils import canonical_city, _strip_diacritics

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["alerts"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def notify_matching_alerts(db: Session, trip: models.Trip) -> None:
    """
    Called right after a new trip is created.
    Finds every active alert that matches the trip and sends one email per alert.

    Matching rules (mirrors the search query in trips.py):
    • alert.origin      is a case-insensitive substring of trip.origin
    • alert.destination is a case-insensitive substring of trip.destination
    • trip.seats_available >= alert.seats
    • if alert.travel_date is set, trip must depart on that date
    • throttle: skip if notified within the last hour (prevents bursts on multi-post)
    """
    try:
        alerts = (
            db.query(models.RideAlert)
            .filter(models.RideAlert.is_active == True)   # noqa: E712
            .all()
        )
        now = datetime.utcnow()
        notified = 0
        for alert in alerts:
            # Route match — compare ASCII-folded names so 'Reykjavik' == 'Reykjavík'
            if _strip_diacritics(alert.origin) not in _strip_diacritics(trip.origin):
                continue
            if _strip_diacritics(alert.destination) not in _strip_diacritics(trip.destination):
                continue
            # Seat availability
            if trip.seats_available < alert.seats:
                continue
            # Date match (if alert has one)
            if alert.travel_date:
                if trip.departure_datetime.date() != alert.travel_date:
                    continue
            # Don't notify the driver about their own trip
            if alert.user_id and alert.user_id == trip.driver_id:
                continue
            # Throttle: at most one notification per alert per hour
            if alert.last_notified_at:
                if (now - alert.last_notified_at).total_seconds() < 3600:
                    continue
            # Expire alerts whose travel_date is in the past
            if alert.travel_date and alert.travel_date < now.date():
                alert.is_active = False
                continue

            mailer.ride_alert_notification(alert, [trip])
            alert.last_notified_at = now
            notified += 1

        if notified or any(not a.is_active for a in alerts):
            db.commit()
        if notified:
            log.info("Sent ride alert notifications for trip %d (%s): %d alert(s)",
                     trip.id, f"{trip.origin}→{trip.destination}", notified)
    except Exception as exc:
        log.warning("notify_matching_alerts failed for trip %d: %s", trip.id, exc)
        db.rollback()


def _parse_travel_date(value: str) -> date | None:
    """Parse an ISO date string from a form field; return None on empty/invalid."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# ── Public: create alert ──────────────────────────────────────────────────────

@router.post("/alerts")
def create_alert(
    request:     Request,
    origin:      str = Form(...),
    destination: str = Form(...),
    travel_date: str = Form(""),
    seats:       int = Form(1),
    email:       str = Form(""),       # required only for guests
    db:          Session = Depends(get_db),
    current_user = Depends(get_current_user_optional),
    _rl=rate_limit(10, 3600),
):
    # Normalise to canonical Icelandic spelling so saved alerts match trip records
    origin      = canonical_city(origin.strip())
    destination = canonical_city(destination.strip())

    # We need at least origin + destination to create a useful alert
    if not origin or not destination:
        # Redirect back to search without saving
        return RedirectResponse(
            "/trips?" + urlencode({"origin": origin, "destination": destination}),
            status_code=303,
        )

    # Reject alerts for impossible same-city routes
    if origin.lower() == destination.lower():
        return RedirectResponse(
            "/trips?" + urlencode({"origin": origin, "destination": destination}),
            status_code=303,
        )

    # Resolve email: prefer logged-in user's email
    if current_user:
        alert_email = current_user.email
        user_id     = current_user.id
    else:
        alert_email = email.strip().lower()
        user_id     = None
        if not alert_email:
            return RedirectResponse(
                "/trips?" + urlencode({"origin": origin, "destination": destination,
                                       "alert_error": "1"}),
                status_code=303,
            )

    parsed_date = _parse_travel_date(travel_date)

    # Upsert: always update criteria on an existing alert (active or not) so that
    # re-saving with a new date/seat count is never silently discarded.
    existing = (
        db.query(models.RideAlert)
        .filter(
            models.RideAlert.email       == alert_email,
            models.RideAlert.origin      == origin,
            models.RideAlert.destination == destination,
        )
        .first()
    )
    was_update = bool(existing)

    if existing:
        existing.is_active   = True
        existing.travel_date = parsed_date
        existing.seats       = max(1, seats)
        db.commit()
    else:
        alert = models.RideAlert(
            user_id     = user_id,
            email       = alert_email,
            origin      = origin,
            destination = destination,
            travel_date = parsed_date,
            seats       = max(1, seats),
            token       = secrets.token_urlsafe(32),
        )
        db.add(alert)
        db.commit()

    # Redirect back to the same search with a success flag.
    # Logged-in users who updated existing criteria also get a link to /my-alerts
    # where a separate ?updated=1 banner explains what changed.
    if was_update and current_user:
        return RedirectResponse("/my-alerts?updated=1", status_code=303)
    params: dict = {"origin": origin, "destination": destination, "alert_saved": "1"}
    if travel_date:
        params["travel_date"] = travel_date
    if seats and seats > 1:
        params["seats"] = str(seats)
    return RedirectResponse("/trips?" + urlencode(params), status_code=303)


# ── Public: unsubscribe via token ─────────────────────────────────────────────

@router.get("/alerts/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_alert(
    token:   str,
    request: Request,
    ctx:     dict    = Depends(get_template_context),
    db:      Session = Depends(get_db),
):
    alert = (
        db.query(models.RideAlert)
        .filter(models.RideAlert.token == token)
        .first()
    )
    if alert and alert.is_active:
        alert.is_active = False
        db.commit()
        success = True
    else:
        success = bool(alert)   # already inactive counts as success

    return templates.TemplateResponse("alerts/unsubscribe.html", {
        **ctx,
        "success": success,
    })


# ── Logged-in: manage alerts ──────────────────────────────────────────────────

@router.get("/my-alerts", response_class=HTMLResponse)
def my_alerts(
    request:      Request,
    ctx:          dict        = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user_optional),
    db:           Session     = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login?next=/my-alerts", status_code=303)

    active_alerts = (
        db.query(models.RideAlert)
        .filter(
            models.RideAlert.user_id   == current_user.id,
            models.RideAlert.is_active == True,   # noqa: E712
        )
        .order_by(models.RideAlert.created_at.desc())
        .all()
    )
    return templates.TemplateResponse("alerts/my_alerts.html", {
        **ctx,
        "active_alerts": active_alerts,
    })


@router.post("/alerts/{alert_id}/delete")
def delete_alert(
    alert_id:     int,
    current_user: models.User = Depends(get_current_user_optional),
    db:           Session     = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=303)
    alert = (
        db.query(models.RideAlert)
        .filter(
            models.RideAlert.id      == alert_id,
            models.RideAlert.user_id == current_user.id,
        )
        .first()
    )
    if alert:
        alert.is_active = False
        db.commit()
    return RedirectResponse("/my-alerts?deleted=1", status_code=303)
