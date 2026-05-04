from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from typing import Optional

from app import models, email as mailer
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app.limiter import rate_limit
from app.routers.payments import calc_fees, _issue_rapyd_refund
from app.utils import canonical_city, build_route_graph, shortest_path_km, is_on_route, prorate_segment_price

settings = get_settings()
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/bookings", tags=["bookings"])


def _newsletter_discount(db: Session, user: models.User):
    """
    Return the NewsletterSubscriber row if this user has an unused first-ride
    discount, otherwise None.
    """
    return (
        db.query(models.NewsletterSubscriber)
        .filter(
            models.NewsletterSubscriber.email         == user.email,
            models.NewsletterSubscriber.discount_used == False,  # noqa: E712
        )
        .first()
    )


@router.get("", response_class=HTMLResponse)
def my_bookings(request: Request):
    # Consolidated into /my-trips
    params = request.query_params
    qs = f"?tab=bookings{'&' + str(params) if params else ''}"
    return RedirectResponse(f"/my-trips{qs}", status_code=301)


def _resolve_segment(
    graph: dict,
    trip: models.Trip,
    raw_pickup: str,
    raw_dropoff: str,
) -> tuple:
    """
    Canonicalize, validate, and price a segment (partial-route) booking.

    Returns (pickup_city, dropoff_city, segment_price, error_message).
    - Empty input → (None, None, None, None)      — full-route booking
    - Valid segment → (pickup, dropoff, price, None)
    - Invalid → (None, None, None, error_str)

    Validation rules (all checked against the trip's own route):
    1. Either both or neither city must be provided.
    2. pickup and dropoff must be different cities.
    3. Both cities must lie on trip.origin → trip.destination.
    4. pickup must precede dropoff in the direction of travel.
    5. Distance data must exist for the segment to compute a prorated price.
    """
    pickup  = canonical_city(raw_pickup.strip())  if raw_pickup.strip()  else None
    dropoff = canonical_city(raw_dropoff.strip()) if raw_dropoff.strip() else None

    if not pickup and not dropoff:
        return None, None, None, None

    if bool(pickup) != bool(dropoff):
        return None, None, None, "Please provide both a pickup and dropoff city for a partial route."

    if pickup == dropoff:
        return None, None, None, "Pickup and dropoff cities must be different."

    # Both cities must be on this trip's route.
    if not is_on_route(graph, trip.origin, trip.destination, pickup):
        return None, None, None, (
            f"'{pickup}' is not on this trip's route "
            f"({trip.origin} → {trip.destination})."
        )
    if not is_on_route(graph, trip.origin, trip.destination, dropoff):
        return None, None, None, (
            f"'{dropoff}' is not on this trip's route "
            f"({trip.origin} → {trip.destination})."
        )

    # pickup must precede dropoff: pickup must lie on trip.origin → dropoff.
    if not is_on_route(graph, trip.origin, dropoff, pickup):
        return None, None, None, (
            f"'{pickup}' does not come before '{dropoff}' on this route. "
            "Please check the order of your pickup and dropoff cities."
        )

    # Segment equals the full trip — no partial pricing needed.
    if pickup == trip.origin and dropoff == trip.destination:
        return None, None, None, None

    seg_km   = shortest_path_km(graph, pickup, dropoff)
    total_km = shortest_path_km(graph, trip.origin, trip.destination)
    if not seg_km or not total_km:
        return None, None, None, (
            "We don't have distance data for that segment. "
            "Please book the full route."
        )

    price = prorate_segment_price(trip.price_per_seat, seg_km, total_km)
    if price is None:
        return None, None, None, (
            "This segment is too short to book separately — the minimum fare is 200 ISK. "
            "Please book the full route or choose a longer segment."
        )
    return pickup, dropoff, price, None


@router.get("/trip/{trip_id}", response_class=HTMLResponse)
def book_trip_page(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pickup:  Optional[str] = None,   # segment: passenger's boarding city
    dropoff: Optional[str] = None,   # segment: passenger's exit city
):
    if not current_user.email_verified and not get_settings().beta_mode:
        return RedirectResponse("/check-your-email", status_code=303)
    if current_user.id_verification != models.VerificationStatus.approved:
        return RedirectResponse(f"/verify?next=book&trip={trip_id}", status_code=303)

    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        return templates.TemplateResponse("errors/404.html", {**ctx}, status_code=404)
    if trip.driver_id == current_user.id:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)
    if trip.status != models.TripStatus.active or trip.seats_available < 1:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    # Compute segment price if this is a partial-route booking
    graph = build_route_graph(db)
    segment_pickup, segment_dropoff, segment_price, _ = _resolve_segment(
        graph, trip, pickup or "", dropoff or ""
    )
    # Bad query-string params (invalid segment from a crafted URL) are silently
    # dropped — the user sees a full-route booking form without an error banner.

    has_discount = _newsletter_discount(db, current_user) is not None
    return templates.TemplateResponse("bookings/create.html", {
        **ctx, "trip": trip, "error": None, "has_discount": has_discount,
        "segment_pickup": segment_pickup, "segment_dropoff": segment_dropoff,
        "segment_price": segment_price,
    })


@router.post("/trip/{trip_id}", response_class=HTMLResponse)
def create_booking(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    seats_booked: int = Form(1),
    message: str = Form(""),
    pickup_city:  str = Form(""),   # segment booking — empty means trip.origin
    dropoff_city: str = Form(""),   # segment booking — empty means trip.destination
    _rl=rate_limit(10, 60),
):
    if not current_user.email_verified and not settings.beta_mode:
        return RedirectResponse("/check-your-email", status_code=303)
    if current_user.id_verification != models.VerificationStatus.approved:
        return RedirectResponse(f"/verify?next=book&trip={trip_id}", status_code=303)

    # Lock the trip row for the duration of this transaction so that concurrent
    # booking requests serialise here rather than racing on seats_available.
    trip = (
        db.query(models.Trip)
        .filter(models.Trip.id == trip_id)
        .with_for_update()
        .first()
    )
    if not trip:
        return templates.TemplateResponse("errors/404.html", {**ctx}, status_code=404)

    # Reject inactive or full trips — mirrors the GET guard so a direct POST
    # cannot bypass the availability check.
    if trip.status != models.TripStatus.active or trip.seats_available < 1:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    # Reject bookings on trips that have already departed.
    # Auto-complete intentionally leaves trips active for 2 hours after
    # departure, so we must check departure_datetime explicitly.
    if trip.departure_datetime <= datetime.utcnow():
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    has_discount = _newsletter_discount(db, current_user) is not None
    err_ctx = {**ctx, "trip": trip, "has_discount": has_discount}

    if trip.driver_id == current_user.id:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": "You cannot book your own trip."}, status_code=400)

    if seats_booked < 1:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": "Please select at least 1 seat."}, status_code=400)

    if seats_booked > trip.seats_available:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": f"Only {trip.seats_available} seat(s) available."}, status_code=400)

    # Check if passenger already has an active booking on this trip.
    # card_saved is included: the passenger has a Case B booking with seats
    # held and a MIT scheduled — it is just as active as a confirmed booking.
    existing = db.query(models.Booking).filter(
        models.Booking.trip_id == trip_id,
        models.Booking.passenger_id == current_user.id,
        models.Booking.status.in_([
            models.BookingStatus.pending,
            models.BookingStatus.awaiting_payment,
            models.BookingStatus.card_saved,
            models.BookingStatus.confirmed,
        ]),
    ).first()
    if existing:
        if existing.status == models.BookingStatus.awaiting_payment:
            return RedirectResponse(
                f"/payments/checkout/{existing.id}", status_code=303
            )
        if existing.status == models.BookingStatus.card_saved:
            return RedirectResponse(
                f"/payments/card-saved/{existing.id}", status_code=303
            )
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": "You already have a booking on this trip."}, status_code=400)

    # Validate and apply segment (partial-route) pricing
    graph = build_route_graph(db)
    pickup_city, dropoff_city, prorated_price, seg_err = _resolve_segment(
        graph, trip, pickup_city, dropoff_city
    )
    if seg_err:
        return templates.TemplateResponse("bookings/create.html", {
            **err_ctx,
            "error": seg_err,
            "segment_pickup": None,
            "segment_dropoff": None,
            "segment_price": None,
        }, status_code=400)
    price_per_seat = prorated_price if prorated_price is not None else trip.price_per_seat

    contribution = price_per_seat * seats_booked
    subscriber   = _newsletter_discount(db, current_user)
    if subscriber:
        service_fee = 0
        total       = contribution
    else:
        service_fee, total, _ = calc_fees(contribution)

    if trip.instant_book:
        # Instant: hold seats now, go straight to payment.
        # Cap the deadline at departure so a passenger can never sit on an
        # unpaid hold past the point the trip has left.
        payment_deadline = min(
            datetime.utcnow() + timedelta(hours=24),
            trip.departure_datetime,
        )
        initial_status   = models.BookingStatus.awaiting_payment
        trip.seats_available = max(0, trip.seats_available - seats_booked)
    else:
        # Requires approval: don't hold seats yet, wait for driver
        initial_status   = models.BookingStatus.pending
        payment_deadline = None

    booking = models.Booking(
        trip_id=trip_id,
        passenger_id=current_user.id,
        seats_booked=seats_booked,
        total_price=total,
        service_fee=service_fee,
        message=message or None,
        pickup_city=pickup_city,
        dropoff_city=dropoff_city,
        status=initial_status,
        payment_deadline=payment_deadline,
    )
    db.add(booking)
    # Do NOT mark discount_used here — do it only on successful payment so an
    # abandoned checkout doesn't permanently burn the user's first-ride discount.
    db.commit()
    db.refresh(booking)

    if trip.instant_book:
        return RedirectResponse(f"/payments/checkout/{booking.id}", status_code=303)
    else:
        # Notify driver of the pending request
        mailer.booking_request_to_driver(booking)
        return RedirectResponse("/bookings?requested=1", status_code=303)


def _refund_preview(booking) -> dict:
    """
    Calculate the refund a passenger would receive if they cancelled now.
    Returns a dict with 'amount', 'label', and 'policy'.
    Does NOT modify anything — safe to call from a GET handler.
    """
    now = datetime.utcnow()
    if not booking.payment:
        return {"amount": 0, "label": "No charge yet", "policy": "free"}

    # Case B card_saved: card tokenized for MIT but nothing charged yet
    if booking.status == models.BookingStatus.card_saved:
        return {
            "amount": 0,
            "label":  "No charge — card not billed yet",
            "policy": "card_not_charged",
        }

    departure    = booking.trip.departure_datetime
    hours_left   = (departure - now).total_seconds() / 3600
    mins_since   = (now - booking.created_at).total_seconds() / 60
    contribution = booking.payment.driver_payout
    total        = booking.payment.passenger_total

    if mins_since <= 30 and hours_left >= 24:
        return {
            "amount": total,
            "label":  f"Full refund — {total:,} ISK",
            "policy": "Within 30-minute grace period",
        }
    elif hours_left >= 24:
        return {
            "amount": contribution,
            "label":  f"Partial refund — {contribution:,} ISK",
            "policy": "Service fee is non-refundable",
        }
    elif hours_left > 0:
        half = round(contribution * 0.5)
        return {
            "amount": half,
            "label":  f"Partial refund — {half:,} ISK",
            "policy": "Less than 24 hours before departure — 50% of contribution",
        }
    else:
        return {
            "amount": 0,
            "label":  "No refund",
            "policy": "Trip has already departed",
        }


@router.get("/{booking_id}/cancel", response_class=HTMLResponse)
def cancel_booking_page(
    booking_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    cancellable = (models.BookingStatus.awaiting_payment,
                   models.BookingStatus.pending,
                   models.BookingStatus.confirmed,
                   models.BookingStatus.card_saved)
    if not booking or booking.passenger_id != current_user.id or booking.status not in cancellable:
        return RedirectResponse("/bookings", status_code=303)

    return templates.TemplateResponse("bookings/cancel_confirm.html", {
        **ctx,
        "booking": booking,
        "refund":  _refund_preview(booking),
    })


@router.post("/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if not booking or booking.passenger_id != current_user.id:
        return RedirectResponse("/bookings", status_code=303)

    cancellable = (models.BookingStatus.awaiting_payment,
                   models.BookingStatus.pending,
                   models.BookingStatus.confirmed,
                   models.BookingStatus.card_saved)
    if booking.status not in cancellable:
        return RedirectResponse("/bookings", status_code=303)

    # Capture the pre-cancellation status before mutating it so the payment
    # block below can distinguish card_saved (no MIT yet) from charged states.
    original_status = booking.status

    seats_were_held = booking.status != models.BookingStatus.pending
    if seats_were_held:
        # Lock the trip row before releasing seats so that concurrent
        # cancellations serialise here rather than racing on seats_available.
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
    booking.status = models.BookingStatus.cancelled

    if booking.payment:
        if original_status == models.BookingStatus.card_saved:
            # Card was tokenized for a future MIT but never charged.
            # Mark the payment failed (no charge to refund, no Rapyd call needed).
            booking.payment.status = models.PaymentStatus.failed
        else:
            refund = _refund_preview(booking)["amount"]
            # _issue_rapyd_refund owns refund_amount and status — do not pre-set them.
            _issue_rapyd_refund(db, booking, refund,
                                reason="requested_by_customer")

    db.commit()
    db.refresh(booking)
    if seats_were_held:
        mailer.booking_cancelled_to_driver(booking)
    mailer.booking_cancelled_to_passenger(booking)
    return RedirectResponse("/bookings?cancelled=1", status_code=303)


@router.post("/{booking_id}/confirm")
def confirm_booking(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if booking and booking.trip.driver_id == current_user.id:
        if booking.status == models.BookingStatus.pending:
            # Lock the trip row before reading/writing seats_available so that
            # concurrent approvals on the same trip serialise here.
            trip = (
                db.query(models.Trip)
                .filter(models.Trip.id == booking.trip_id)
                .with_for_update()
                .first()
            )
            if (
                trip
                and trip.status == models.TripStatus.active
                and trip.departure_datetime > datetime.utcnow()
                and trip.seats_available >= booking.seats_booked
            ):
                trip.seats_available     = trip.seats_available - booking.seats_booked
                booking.status           = models.BookingStatus.awaiting_payment
                # Cap at departure so the passenger can never hold an unpaid
                # seat past the point the trip has left.
                booking.payment_deadline = min(
                    datetime.utcnow() + timedelta(hours=24),
                    trip.departure_datetime,
                )
                db.commit()
                db.refresh(booking)
                mailer.booking_approved_to_passenger(booking)
            # If trip is not active, in the past, or has insufficient seats,
            # do nothing — driver sees the booking still pending
    return RedirectResponse("/my-trips?tab=rides", status_code=303)


@router.post("/{booking_id}/no-show")
def mark_passenger_no_show(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _rl=rate_limit(5, 60),
):
    """Driver marks a confirmed passenger as a no-show (only after departure)."""
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if (not booking
            or booking.trip.driver_id != current_user.id
            or booking.status != models.BookingStatus.confirmed
            or datetime.utcnow() < booking.trip.departure_datetime + timedelta(minutes=15)):
        return RedirectResponse("/my-trips?tab=rides", status_code=303)

    booking.status = models.BookingStatus.no_show
    # Passenger forfeits their contribution — no refund issued
    db.commit()
    return RedirectResponse(f"/trips/{booking.trip_id}?no_show=1", status_code=303)


@router.post("/{booking_id}/driver-no-show")
def report_driver_no_show(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _rl=rate_limit(5, 60),
):
    """Passenger reports the driver as a no-show (only after departure). Issues full refund."""
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip), joinedload(models.Booking.payment))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if (not booking
            or booking.passenger_id != current_user.id
            or booking.status != models.BookingStatus.confirmed
            or datetime.utcnow() < booking.trip.departure_datetime + timedelta(minutes=15)):
        return RedirectResponse("/my-trips?tab=bookings", status_code=303)

    # Flag the trip so the driver can be penalised by auto-ratings
    booking.trip.driver_no_show = True
    # Cancel the booking and issue a full refund via Rapyd.
    # _issue_rapyd_refund owns refund_amount and status — do not pre-set them.
    booking.status = models.BookingStatus.cancelled
    if booking.payment:
        _issue_rapyd_refund(
            db, booking, booking.payment.passenger_total,
            reason="driver_no_show",
        )

    # Issue an immediate 1-star auto-review for the driver (no grace period for no-shows)
    existing_review = (
        db.query(models.Review)
        .filter(
            models.Review.booking_id  == booking.id,
            models.Review.review_type == models.ReviewType.passenger_to_driver,
        )
        .first()
    )
    if not existing_review:
        db.add(models.Review(
            booking_id  = booking.id,
            trip_id     = booking.trip_id,
            reviewer_id = current_user.id,
            reviewee_id = booking.trip.driver_id,
            review_type = models.ReviewType.passenger_to_driver,
            rating      = 1,
            is_auto     = True,
        ))

    db.commit()
    return RedirectResponse("/my-trips?tab=bookings&driver_no_show=1", status_code=303)


@router.post("/{booking_id}/reject")
def reject_booking(
    booking_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.trip))
        .filter(models.Booking.id == booking_id)
        .first()
    )
    if booking and booking.trip.driver_id == current_user.id:
        if booking.status == models.BookingStatus.pending:
            # Pending on manual-approval trips never held seats — nothing to release
            booking.status = models.BookingStatus.rejected
            db.commit()
    return RedirectResponse("/my-trips?tab=rides", status_code=303)
