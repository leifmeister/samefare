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
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app import models, email as mailer
from app.database import get_db
from app.dependencies import get_current_user_optional, get_template_context
from app.limiter import rate_limit
from app.utils import (
    canonical_city,
    build_route_graph, shortest_path_km, is_on_route, seats_for_segment,
)

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["alerts"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _alert_matches_trip(graph, alert: models.RideAlert, trip: models.Trip) -> bool:
    """
    True if `trip` serves the alert's route with enough seats, using the same
    segment logic as search:
      • both alert cities lie on the trip's route, in the correct direction
      • non-direct (mid-route) matches require the trip to allow segments
      • availability is the per-segment count on the alert's leg, not the
        whole-trip minimum
    So an alert for Selfoss→Vík matches a Reykjavík→Vík segment-enabled trip
    even when the whole-trip seat count is 0 on an earlier leg.
    """
    a_o = canonical_city(alert.origin)
    a_d = canonical_city(alert.destination)
    t_o, t_d = trip.origin, trip.destination
    is_direct = (a_o == t_o and a_d == t_d)

    total_km = shortest_path_km(graph, t_o, t_d)
    if total_km is None:
        # Route unknown to the graph — fall back to an exact match + whole-trip seats.
        return is_direct and trip.seats_available >= alert.seats
    if not is_direct and not trip.allow_segments:
        return False
    if not is_on_route(graph, t_o, t_d, a_o) or not is_on_route(graph, t_o, t_d, a_d):
        return False
    d_pick = 0.0 if t_o == a_o else shortest_path_km(graph, t_o, a_o)
    d_drop = total_km if t_d == a_d else shortest_path_km(graph, t_o, a_d)
    if d_pick is None or d_drop is None or d_pick >= d_drop:
        return False
    active = [b for b in trip.bookings if b.occupies_seat]
    avail = seats_for_segment(graph, trip.seats_total, active, t_o, t_d, a_o, a_d)
    return avail >= alert.seats


def notify_matching_alerts(db: Session, trip: models.Trip) -> None:
    """
    Called right after a new trip is created.
    Finds every active alert that matches the trip and sends one email per alert.

    Matching mirrors search via _alert_matches_trip(): both alert cities on the
    trip's route (right direction), segment availability on the alert's leg, and
    mid-route matches only on segment-enabled trips. Plus: same-day if the alert
    is dated, not the driver's own trip, and throttled to one email/alert/hour.
    """
    try:
        alerts = (
            db.query(models.RideAlert)
            .filter(models.RideAlert.is_active == True)   # noqa: E712
            .all()
        )
        now = datetime.utcnow()
        graph = build_route_graph(db)
        notified = 0
        for alert in alerts:
            # Don't notify the driver about their own trip
            if alert.user_id and alert.user_id == trip.driver_id:
                continue
            # Date: expire past-dated alerts; otherwise require a same-day trip
            if alert.travel_date:
                if alert.travel_date < now.date():
                    alert.is_active = False
                    continue
                if trip.departure_datetime.date() != alert.travel_date:
                    continue
            # Throttle: at most one notification per alert per hour
            if alert.last_notified_at and (now - alert.last_notified_at).total_seconds() < 3600:
                continue
            # Route + per-segment availability — same segment logic as search, so
            # mid-route matches (e.g. a Selfoss→Vík alert on a Reykjavík→Vík trip)
            # are caught and availability is checked on the alert's leg.
            if not _alert_matches_trip(graph, alert, trip):
                continue

            mailer.ride_alert_notification(alert, [trip])
            alert.last_notified_at = now
            # A public request is now answerable — drop it off the demand board.
            if alert.is_public and not alert.fulfilled_at:
                alert.fulfilled_at = now
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


# ── Public ride-request board ─────────────────────────────────────────────────

REQUEST_TTL_DAYS = 14   # undated requests drop off the board after this long


def open_ride_requests(db: Session, limit: int = 60) -> list[tuple]:
    """
    Active, public, unfulfilled, non-expired ride requests — newest first.
    Returns a list of (alert, first_name_or_None). Never exposes email.
    """
    now    = datetime.utcnow()
    cutoff = now - timedelta(days=REQUEST_TTL_DAYS)
    today  = now.date()
    rows = (
        db.query(models.RideAlert)
        .options(joinedload(models.RideAlert.user))
        .filter(
            models.RideAlert.is_public   == True,   # noqa: E712
            models.RideAlert.is_active   == True,    # noqa: E712
            models.RideAlert.fulfilled_at.is_(None),
            # not past-dated
            or_(models.RideAlert.travel_date >= today,
                models.RideAlert.travel_date.is_(None)),
            # undated requests also expire by age
            or_(models.RideAlert.travel_date.isnot(None),
                models.RideAlert.created_at >= cutoff),
        )
        .order_by(models.RideAlert.created_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for a in rows:
        first = a.user.full_name.split()[0] if (a.user and a.user.full_name) else None
        out.append((a, first))
    return out


@router.get("/requests", response_class=HTMLResponse)
def ride_requests_board(
    request: Request,
    ctx:     dict    = Depends(get_template_context),
    db:      Session = Depends(get_db),
):
    return templates.TemplateResponse("alerts/requests.html", {
        **ctx,
        "requests": open_ride_requests(db),
        "now":      datetime.utcnow(),
    })


# ── Public: create alert ──────────────────────────────────────────────────────

@router.post("/alerts")
def create_alert(
    request:     Request,
    origin:      str = Form(...),
    destination: str = Form(...),
    travel_date: str = Form(""),
    seats:       int = Form(1),
    email:       str = Form(""),       # required only for guests
    make_public: str = Form(""),       # checkbox: also show as an open request
    note:        str = Form(""),       # optional short note shown on the board
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
    is_public   = bool(make_public)
    note_clean  = (note or "").strip()[:120] or None

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

    # Guests must double-opt-in: a logged-in user owns their account email, but
    # a guest can type any address — without confirmation, alerts would let an
    # attacker email-bomb a victim with trip notifications. Guest alerts are
    # created/kept INACTIVE until the recipient clicks the email confirm link.
    needs_confirm = current_user is None

    if existing:
        existing.travel_date  = parsed_date
        existing.seats        = max(1, seats)
        existing.is_active    = not needs_confirm
        existing.is_public    = is_public
        existing.note         = note_clean
        # Re-saving revives a previously fulfilled request as fresh demand.
        existing.fulfilled_at = None
        alert = existing
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
            is_active   = not needs_confirm,
            is_public   = is_public,
            note        = note_clean,
        )
        db.add(alert)
        db.commit()

    if needs_confirm:
        try:
            mailer.ride_alert_confirm(alert)
        except Exception as exc:
            log.warning("ride_alert_confirm email failed: %s", exc)
        return RedirectResponse(
            "/trips?" + urlencode({"origin": origin, "destination": destination,
                                   "alert_confirm": "1"}),
            status_code=303,
        )

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


# ── Public: confirm (double opt-in) via token ────────────────────────────────

@router.get("/alerts/confirm/{token}", response_class=HTMLResponse)
def confirm_alert(
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
    if alert:
        alert.is_active = True
        db.commit()
    return templates.TemplateResponse("alerts/confirmed.html", {
        **ctx,
        "success": bool(alert),
        "alert":   alert,
    })


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
