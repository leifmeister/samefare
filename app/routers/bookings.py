import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request

log = logging.getLogger(__name__)
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
from app.utils import (
    build_route_graph, resolve_segment,
    seats_for_segment, recompute_seats_available,
)

settings = get_settings()
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/bookings", tags=["bookings"])

# Statuses that represent a booking occupying seats on the trip.
# Must stay in sync with the equivalent constant in trips.py.
_SEAT_HOLDING_STATUSES = frozenset({
    models.BookingStatus.awaiting_payment,
    models.BookingStatus.confirmed,
    models.BookingStatus.card_saved,
})


def _refresh_seats(trip: "models.Trip", db: "Session") -> None:
    """Recompute trip.seats_available from current active bookings (segment-aware)."""
    graph = build_route_graph(db)
    active = [b for b in trip.bookings if b.occupies_seat]
    trip.seats_available = recompute_seats_available(
        graph, trip.seats_total, active, trip.origin, trip.destination,
    )


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
    # Passengers ride on a verified phone (the accountability anchor) + a real
    # card at checkout. Full ID is optional (earns an "ID verified" badge), not a
    # gate — matching BlaBlaCar, and removing the heavy friction for one-off /
    # tourist riders. Drivers still need licence verification to post.
    #
    # We collect the phone INLINE on this page (no bounce to /profile, so the
    # passenger never loses the trip they were booking). `needs_phone` swaps the
    # booking form for a verify step; on success the page reloads and the form
    # appears. The POST handler keeps the same gate as a server-side backstop.
    needs_phone = not current_user.phone_verified and not get_settings().beta_mode

    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        return templates.TemplateResponse("errors/404.html", {**ctx}, status_code=404)
    if trip.driver_id == current_user.id:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)
    if trip.status != models.TripStatus.active:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)
    # Don't render the booking form for a suspended/deactivated driver's trip
    # (the POST is already guarded; this stops the form showing from a stale link).
    # The detail page shows the "unavailable" state.
    if not trip.driver.is_active or trip.driver.posting_suspended:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    # Block bookings within 1 hour of departure.
    if trip.departure_datetime <= datetime.utcnow() + timedelta(hours=1):
        return RedirectResponse(f"/trips/{trip_id}?booking_closed=1", status_code=303)

    # Resolve the segment early so we can detect origin→midpoint intent
    # (pickup == trip.origin but dropoff != trip.destination) before the
    # availability guard.  Invalid/missing params resolve to (None, None, …).
    graph = build_route_graph(db)
    segment_pickup, segment_dropoff, segment_price, _ = resolve_segment(
        graph, trip, pickup or "", dropoff or ""
    )

    # Segment intent: either pickup OR dropoff differs from the trip endpoints.
    # Checking only pickup misses the origin→midpoint case (Bug 3).
    is_segment_request = bool(segment_pickup or segment_dropoff)

    if not is_segment_request:
        # Full-route request — whole-trip minimum is the correct guard.
        if trip.seats_available < 1:
            return RedirectResponse(f"/trips/{trip_id}", status_code=303)
        booking_available_seats = trip.seats_available
    else:
        # Per-segment availability — peak occupancy on this specific leg.
        active = [b for b in trip.bookings if b.occupies_seat]
        seg_p  = segment_pickup  or trip.origin
        seg_d  = segment_dropoff or trip.destination
        booking_available_seats = seats_for_segment(
            graph, trip.seats_total, active,
            trip.origin, trip.destination, seg_p, seg_d,
        )
        if booking_available_seats < 1:
            return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    has_discount   = _newsletter_discount(db, current_user) is not None
    less_than_24h  = trip.departure_datetime <= datetime.utcnow() + timedelta(hours=24)
    return templates.TemplateResponse("bookings/create.html", {
        **ctx, "trip": trip, "error": None, "has_discount": has_discount,
        "segment_pickup": segment_pickup, "segment_dropoff": segment_dropoff,
        "segment_price": segment_price, "less_than_24h": less_than_24h,
        "instant_book": trip.instant_book,
        "booking_available_seats": booking_available_seats,
        "blikk_payments": settings.blikk_payments,
        "needs_phone": needs_phone,
    })


@router.post("/trip/{trip_id}", response_class=HTMLResponse)
def create_booking(
    trip_id: int,
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    seats_booked:   int = Form(1),
    message:        str = Form(""),
    pickup_city:    str = Form(""),   # segment booking — empty means trip.origin
    dropoff_city:   str = Form(""),   # segment booking — empty means trip.destination
    payment_method: str = Form("card"),
    _rl=rate_limit(10, 60),
):
    if not current_user.email_verified and not settings.beta_mode:
        return RedirectResponse("/check-your-email", status_code=303)
    # Verified phone (+ card at checkout) is the booking gate; full ID is optional.
    if not current_user.phone_verified and not settings.beta_mode:
        return RedirectResponse("/profile?msg=phone_required#phone", status_code=303)

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

    # Detect segment intent early — needed to gate the availability guards below.
    # pickup_city/dropoff_city come from the submitted form fields.
    is_segment = (pickup_city and pickup_city != trip.origin) or \
                 (dropoff_city and dropoff_city != trip.destination)

    # Reject inactive trips.  For full-route requests also enforce the
    # whole-trip availability floor — seats_available is the minimum remaining
    # capacity across all legs and is the correct guard for passengers who need
    # a seat for the entire journey.  For segment requests, skip this guard:
    # seats_available can be 0 because a different leg is full while the
    # requested leg still has room.  Per-segment availability is checked below.
    if trip.status != models.TripStatus.active:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)
    # Block bookings on trips from a suspended/deactivated driver. Search already
    # hides these, but this is the authoritative guard against a direct/stale link.
    if not trip.driver.is_active or trip.driver.posting_suspended:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)
    if not is_segment and trip.seats_available < 1:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    # Reject bookings on trips that have already departed.
    # Auto-complete intentionally leaves trips active for 2 hours after
    # departure, so we must check departure_datetime explicitly.
    if trip.departure_datetime <= datetime.utcnow():
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    # Block bookings within 1 hour of departure.
    if trip.departure_datetime <= datetime.utcnow() + timedelta(hours=1):
        return RedirectResponse(f"/trips/{trip_id}?booking_closed=1", status_code=303)

    has_discount = _newsletter_discount(db, current_user) is not None
    err_ctx = {**ctx, "trip": trip, "has_discount": has_discount,
               "blikk_payments": settings.blikk_payments}

    if trip.driver_id == current_user.id:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": "You cannot book your own trip."}, status_code=400)

    if seats_booked < 1:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": "Please select at least 1 seat."}, status_code=400)

    # For full-route bookings, the whole-trip minimum is the right ceiling.
    # For segment bookings, skip this — the per-segment check below is authoritative.
    if not is_segment and seats_booked > trip.seats_available:
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

    # Block segment bookings when the driver hasn't opted in
    if is_segment and not trip.allow_segments:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": "This driver only accepts full-route bookings."}, status_code=400)

    # Build route graph only when needed (segment bookings only) — full-route
    # bookings don't use it, so building it unconditionally wastes a DB query
    # on the majority of booking requests.
    active_bookings = [b for b in trip.bookings if b.occupies_seat]
    if is_segment:
        graph = build_route_graph(db)
        pickup_city, dropoff_city, prorated_price, seg_err = resolve_segment(
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
        seg_pickup  = pickup_city  or trip.origin
        seg_dropoff = dropoff_city or trip.destination
        available_on_segment = seats_for_segment(
            graph, trip.seats_total, active_bookings,
            trip.origin, trip.destination, seg_pickup, seg_dropoff,
        )
    else:
        graph            = None
        prorated_price   = None
        available_on_segment = trip.seats_available

    price_per_seat = prorated_price if prorated_price is not None else trip.price_per_seat

    # Definitive availability check.
    # Segment booking: count only bookings overlapping this specific leg.
    # Full-route booking: use trip.seats_available (peak concurrent occupancy
    #   minimum) — do NOT call seats_for_segment for the full route, it
    #   overcounts when non-overlapping segment bookings are present.
    if seats_booked > available_on_segment:
        return templates.TemplateResponse("bookings/create.html",
            {**err_ctx, "error": f"Only {available_on_segment} seat(s) available on that leg."}, status_code=400)

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
        # Recompute seats_available as true peak occupancy.
        # Segment bookings use the full route graph; full-route bookings
        # simply subtract the new booking from the current available count.
        if is_segment and graph is not None:
            trip.seats_available = recompute_seats_available(
                graph, trip.seats_total,
                active_bookings + [type('_B', (), {
                    'pickup_city': pickup_city, 'dropoff_city': dropoff_city,
                    'seats_booked': seats_booked,
                })()],
                trip.origin, trip.destination,
            )
        else:
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
        payment_method=models.TripPaymentMethod.card,
        status=initial_status,
        payment_deadline=payment_deadline,
    )
    db.add(booking)
    # Do NOT mark discount_used here — do it only on successful payment so an
    # abandoned checkout doesn't permanently burn the user's first-ride discount.
    db.commit()
    db.refresh(booking)

    # Surface the passenger's note to the driver as a real chat message, so it
    # shows in the conversation thread (and counts as unread for the driver),
    # not only on the pending-request card.
    if message:
        db.add(models.Message(
            booking_id=booking.id,
            sender_id=current_user.id,
            body=message,
        ))
        db.commit()

    if trip.instant_book:
        return RedirectResponse(f"/payments/checkout/{booking.id}", status_code=303)
    else:
        # Notify driver of the pending request, then collect card details upfront.
        # Card is saved now (amount=0); MIT fires the moment driver accepts.
        # No return visit needed from the passenger.
        mailer.booking_request_to_driver(booking)
        return RedirectResponse(f"/payments/checkout/{booking.id}", status_code=303)


def _refund_preview(booking) -> dict:
    """
    Calculate the outcome for the passenger if they cancelled now.
    Returns a dict with 'amount', 'label', and 'policy'.
    Does NOT modify anything — safe to call from a GET handler.

    Card (Rapyd) policy:
    • Before pre-auth (card_saved / pending / no payment): no charge.
    • After pre-auth: no refund — full amount captured immediately.
    """
    if not booking.payment:
        return {"amount": 0, "label": "No charge — booking not yet paid", "policy": "free"}

    if booking.payment.status in (
        models.PaymentStatus.pending,
        models.PaymentStatus.failed,
    ) or booking.status == models.BookingStatus.card_saved:
        return {
            "amount": 0,
            "label":  "No charge — card not billed yet",
            "policy": "card_not_charged",
        }

    return {
        "amount": 0,
        "label":  "No refund — full amount will be captured",
        "policy": "pre_auth_placed",
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

    # Block cancellation once the ride has departed, or once the payment's money
    # state is provider-confirmed / in-flight. Capture fires at departure (the
    # capture task) or earlier via a Rapyd CLO webhook, so a confirmed booking can
    # already be `captured`. Letting the no-charge branch below run on such a
    # payment would overwrite `captured` → `failed`, erasing provider-confirmed
    # money and preventing the driver payout item. After departure there is also
    # nothing left to cancel. (Mainly guards a direct/stale cancel POST; the UI
    # hides cancel after departure.)
    money_locked = booking.payment and booking.payment.status in (
        models.PaymentStatus.captured,
        models.PaymentStatus.capture_requested,
        models.PaymentStatus.refund_requested,
        models.PaymentStatus.refund_failed,
        models.PaymentStatus.refunded,
        models.PaymentStatus.partial_refund,
    )
    if booking.trip.departure_datetime <= datetime.utcnow() or money_locked:
        return RedirectResponse("/bookings?cancel_unavailable=1", status_code=303)

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
            booking.status = models.BookingStatus.cancelled  # mark first so refresh excludes it
            db.flush()
            _refresh_seats(trip, db)
    booking.status = models.BookingStatus.cancelled

    pre_auth_placed = (
        booking.payment
        and booking.payment.status in (
            models.PaymentStatus.authorised,
            models.PaymentStatus.capture_requested,
        )
    )

    if booking.payment:
        if original_status == models.BookingStatus.card_saved or not pre_auth_placed:
            # Card tokenised but pre-auth not yet fired — no charge, mark failed.
            booking.payment.status = models.PaymentStatus.failed
        else:
            # Pre-auth is in place: trigger immediate capture.
            # _run_capture_payments (every 10 min) picks this up.
            # No refund — full amount forfeited per cancellation policy.
            booking.payment.capture_at = datetime.utcnow()
            # Mark this as a passenger forfeit so the payout sweep pays the driver
            # their contribution (create_payout_item_for_payment keys off this).
            booking.cancellation_reason = "late_forfeit"

    db.commit()
    db.refresh(booking)
    if seats_were_held:
        if pre_auth_placed:
            mailer.booking_cancelled_charged(booking)   # driver: cancelled but you'll be paid
        else:
            mailer.booking_cancelled_to_driver(booking)
    mailer.booking_cancelled_to_passenger(booking)

    # Tell the banner the truth: a passenger self-cancel either captures the full
    # fare (pre-auth already placed → forfeit, no refund) or never charged the card
    # (card_saved / pending → token voided). It never produces a refund.
    charged = bool(
        booking.payment
        and pre_auth_placed
        and original_status != models.BookingStatus.card_saved
    )
    return RedirectResponse(
        f"/bookings?cancelled={'charged' if charged else 'nocharge'}",
        status_code=303,
    )


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
            ):
                graph   = build_route_graph(db)
                active  = [b for b in trip.bookings if b.occupies_seat]
                seg_p   = booking.pickup_city  or trip.origin
                seg_d   = booking.dropoff_city or trip.destination
                avail   = seats_for_segment(
                    graph, trip.seats_total, active,
                    trip.origin, trip.destination, seg_p, seg_d,
                )
                if avail < booking.seats_booked:
                    # No room on this leg — leave pending and tell the driver why,
                    # rather than silently no-op'ing the Accept button.
                    db.commit()
                    return RedirectResponse("/my-trips?tab=rides&accept_full=1", status_code=303)
                # NB: seats are recomputed AFTER the booking moves to a
                # seat-holding state (below), not here — a still-pending booking
                # is excluded from _refresh_seats, so recomputing now wouldn't
                # count the seats we're approving.

                # Card already saved upfront — confirm the booking now.
                # MIT always fires at T-24h via the background task, never
                # immediately on acceptance, so the passenger's card is not
                # frozen days before departure.
                if (booking.payment
                        and booking.payment.status == models.PaymentStatus.card_saved
                        and booking.payment.rapyd_customer_id
                        and booking.payment.rapyd_payment_method_id):

                    booking.status      = models.BookingStatus.confirmed
                    booking.accepted_at = datetime.utcnow()

                    # In beta mode: no Rapyd, mark authorised directly.
                    if settings.beta_mode:
                        booking.payment.status     = models.PaymentStatus.authorised
                        booking.payment.capture_at = trip.departure_datetime
                    else:
                        # Ensure auth_scheduled_for is set (may not be for pending
                        # bookings whose payment was created before driver accepted).
                        if not booking.payment.auth_scheduled_for:
                            scheduled = trip.departure_datetime - timedelta(hours=24)
                            # If T-24h has already passed, schedule for now so the
                            # task picks it up on the next run (within minutes).
                            booking.payment.auth_scheduled_for = max(
                                scheduled, datetime.utcnow()
                            )

                    # Now that the booking holds a seat, recompute availability so
                    # the approved seats are subtracted from the trip inventory.
                    _refresh_seats(trip, db)
                    db.commit()
                    db.refresh(booking)
                    mailer.booking_confirmed_to_passenger(booking)
                    mailer.booking_confirmed_to_driver(booking)
                else:
                    # No card saved yet — passenger still needs to pay
                    booking.status           = models.BookingStatus.awaiting_payment
                    booking.accepted_at      = datetime.utcnow()
                    booking.payment_deadline = min(
                        datetime.utcnow() + timedelta(hours=24),
                        trip.departure_datetime,
                    )
                    # awaiting_payment holds a seat — recompute so the approved
                    # seats are subtracted from the trip inventory.
                    _refresh_seats(trip, db)
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
    now = datetime.utcnow()
    if (not booking
            or booking.trip.driver_id != current_user.id
            or booking.status != models.BookingStatus.confirmed):
        return RedirectResponse("/my-trips?tab=rides", status_code=303)

    # A no-show is determined at the pickup, so the action is only open for a
    # grace window after departure — from +15 min (allow for a late passenger) up
    # to +4 h. The +4 h upper bound mirrors the driver-no-show report window and
    # is when the trip auto-completes; beyond it the booking has effectively
    # settled and the driver could otherwise mark a no-show midway through (or
    # even after) a long ride.
    no_show_open_from  = booking.trip.departure_datetime + timedelta(minutes=15)
    no_show_open_until = booking.trip.departure_datetime + timedelta(hours=4)
    if now < no_show_open_from or now > no_show_open_until:
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
    now = datetime.utcnow()
    if (not booking
            or booking.passenger_id != current_user.id
            or booking.status != models.BookingStatus.confirmed):
        return RedirectResponse("/my-trips?tab=bookings", status_code=303)

    report_open_from  = booking.trip.departure_datetime + timedelta(minutes=15)
    report_open_until = booking.trip.departure_datetime + timedelta(hours=4)
    if now < report_open_from or now > report_open_until:
        return RedirectResponse("/my-trips?tab=bookings", status_code=303)

    # ── Driver no-show voids the ENTIRE trip ─────────────────────────────────
    # A confirmed no-show means nobody got their ride. Refund every confirmed
    # passenger — not just the reporter — and freeze the trip so auto-complete
    # can never turn the remaining bookings into driver payables. The
    # create_payout_item chokepoint also refuses any payout on a no-show trip.
    trip = booking.trip
    trip.driver_no_show = True
    trip.status         = models.TripStatus.cancelled

    # _issue_rapyd_refund records a durable refund_requested intent in the same
    # db.commit() as the cancellation. _run_retry_refunds (background task)
    # submits it to Rapyd and calls handle_refund_payout_impact only after Rapyd
    # confirms — which also cancels/reverses the driver's PayoutItem if one slipped
    # through. cancellation_reason is "driver_no_show" (deliberately NOT
    # "late_forfeit") so the captured fare is never treated as owed to the driver.
    from app.routers.payments import _issue_rapyd_refund

    affected = [b for b in trip.bookings
                if b.status == models.BookingStatus.confirmed]
    if booking not in affected and booking.status == models.BookingStatus.confirmed:
        affected.append(booking)

    for b in affected:
        b.status              = models.BookingStatus.cancelled
        b.cancellation_reason = "driver_no_show"
        if b.payment:
            _issue_rapyd_refund(db, b, (b.payment.passenger_total or 0),
                                reason="requested_by_customer")
        # Notify the other passengers their ride is off and they're refunded
        # (the reporter gets the on-screen flash instead).
        if b.id != booking.id:
            try:
                mailer.trip_cancelled_to_passenger(b)
            except Exception:
                pass  # never block on a notification failure

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

    # ── Driver accountability: zero-tolerance for confirmed no-shows ───────────
    driver = booking.trip.driver
    driver.no_shows_confirmed += 1
    if not driver.posting_suspended:
        driver.posting_suspended = True
        driver.suspension_reason = (
            f"Driver no-show confirmed (booking #{booking.id}). "
            f"Total confirmed no-shows: {driver.no_shows_confirmed}."
        )

    db.commit()

    # Notify admin — for awareness; refund is already queued automatically.
    try:
        mailer.driver_no_show_admin_alert(booking, driver)
    except Exception:
        pass  # never block the user flow on an admin notification failure

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
