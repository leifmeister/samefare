from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import models, email as mailer
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app.limiter import rate_limit

templates = Jinja2Templates(directory="templates")
router    = APIRouter(prefix="/messages", tags=["messages"])

# Don't email the recipient for a new message if the thread has had activity in
# the last N minutes — debounces an active back-and-forth into at most one email
# per lull (see send_message).
NOTIFY_DEBOUNCE_MINUTES = 15

# ── Jinja2 filters ────────────────────────────────────────────────────────────

# Country codes sorted longest-first so "+358" matches before "+35", etc.
_PHONE_CODES = sorted([
    '+1-809','+1-876','+1-787',
    '+358','+354','+353','+352','+351','+383','+382','+381','+380','+378',
    '+376','+375','+374','+373','+372','+371','+370','+886','+880','+856',
    '+855','+852','+850','+974','+973','+972','+971','+970','+968','+967',
    '+966','+965','+964','+962','+961','+976','+977','+975','+960',
    '+298','+350','+355','+357','+359','+385','+387','+389','+420','+421',
    '+423','+670','+673','+675','+679','+995','+994','+993','+992',
    '+238','+239','+240','+241','+242','+243','+244','+245','+246','+247',
    '+248','+249','+250','+251','+252','+253','+254','+255','+256','+257',
    '+258','+260','+261','+262','+263','+264','+265','+266','+267','+268',
    '+269','+211','+212','+213','+216','+218','+220','+221','+222','+223',
    '+224','+225','+226','+227','+228','+229','+230','+231','+232','+233',
    '+234','+235','+236','+237','+291',
    '+43','+44','+45','+46','+47','+48','+49',
    '+30','+31','+32','+33','+34','+36','+39','+40','+41',
    '+51','+52','+53','+54','+55','+56','+57',
    '+60','+61','+62','+63','+64','+65','+66',
    '+81','+82','+84','+86','+90','+91','+92','+93','+94','+95',
    '+20','+27','+7','+1',
], key=lambda c: -len(c.replace('-', '')))


def _format_phone(phone: str | None) -> str:
    """Ensure a space between country code and local number for display.
    Handles legacy numbers stored without a space, e.g. '+4796019308'."""
    if not phone:
        return ''
    if ' ' in phone:
        return phone          # already formatted
    if not phone.startswith('+'):
        return phone          # no country code prefix — leave as-is
    for code in _PHONE_CODES:
        plain = code.replace('-', '')   # '+1-809' → '+1809' (not used as stored)
        if phone.startswith(plain):
            return plain + ' ' + phone[len(plain):]  # non-breaking space
    return phone


templates.env.filters['format_phone'] = _format_phone


# ── Helpers ───────────────────────────────────────────────────────────────────

def _other_person(booking: models.Booking, me: models.User) -> models.User:
    """Return the conversation partner (driver or passenger)."""
    return booking.trip.driver if booking.passenger_id == me.id else booking.passenger


def _can_access(booking: models.Booking, me: models.User) -> bool:
    return booking.passenger_id == me.id or booking.trip.driver_id == me.id


# ── Inbox ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def inbox(
    request: Request,
    ctx:          dict         = Depends(get_template_context),
    current_user: models.User  = Depends(get_current_user),
    db:           Session      = Depends(get_db),
):
    bookings = (
        db.query(models.Booking)
        .join(models.Trip, models.Booking.trip_id == models.Trip.id)
        .options(
            joinedload(models.Booking.trip).joinedload(models.Trip.driver),
            joinedload(models.Booking.passenger),
            joinedload(models.Booking.messages).joinedload(models.Message.sender),
        )
        .filter(
            # Active bookings always show (so you can coordinate even before any
            # message is sent). Past bookings — cancelled / rejected / completed /
            # no_show — show only if a conversation actually happened, so the
            # history survives the ride instead of vanishing the moment the
            # booking leaves an active state (which also left the nav unread
            # badge pointing at conversations the inbox refused to display).
            or_(
                models.Booking.status.in_([
                    models.BookingStatus.confirmed,
                    models.BookingStatus.pending,
                    models.BookingStatus.awaiting_payment,
                    models.BookingStatus.card_saved,
                ]),
                models.Booking.messages.any(),
            ),
            or_(
                models.Booking.passenger_id == current_user.id,
                models.Trip.driver_id       == current_user.id,
            ),
        )
        .order_by(models.Booking.created_at.desc())
        .all()
    )

    conversations = []
    for b in bookings:
        partner  = _other_person(b, current_user)
        msgs     = list(b.messages)   # already ordered by created_at via relationship
        last_msg = msgs[-1] if msgs else None
        unread   = sum(1 for m in msgs
                       if m.sender_id != current_user.id and not m.is_read)
        conversations.append({
            "kind":         "booking",
            "url":          f"/messages/{b.id}",
            "partner":      partner,
            "last_message": last_msg,
            "unread_count": unread,
            "origin":       b.pickup_city or b.trip.origin,
            "destination":  b.dropoff_city or b.trip.destination,
            "dep_dt":       b.trip.departure_datetime,
            "status":       b.status.value,
            "sort_dt":      last_msg.created_at if last_msg else b.created_at,
        })

    # Pre-booking inquiry threads — only those that actually have a message, so an
    # opened-but-empty thread doesn't clutter the inbox.
    inquiries = (
        db.query(models.Inquiry)
        .join(models.Trip, models.Inquiry.trip_id == models.Trip.id)
        .options(
            joinedload(models.Inquiry.trip).joinedload(models.Trip.driver),
            joinedload(models.Inquiry.passenger),
            joinedload(models.Inquiry.messages).joinedload(models.Message.sender),
        )
        .filter(
            models.Inquiry.messages.any(),
            or_(
                models.Inquiry.passenger_id == current_user.id,
                models.Trip.driver_id        == current_user.id,
            ),
        )
        .all()
    )
    for inq in inquiries:
        partner  = _inquiry_partner(inq, current_user)
        msgs     = list(inq.messages)
        last_msg = msgs[-1] if msgs else None
        unread   = sum(1 for m in msgs
                       if m.sender_id != current_user.id and not m.is_read)
        conversations.append({
            "kind":         "inquiry",
            "url":          f"/inquiries/{inq.id}",
            "partner":      partner,
            "last_message": last_msg,
            "unread_count": unread,
            "origin":       inq.trip.origin,
            "destination":  inq.trip.destination,
            "dep_dt":       inq.trip.departure_datetime,
            "status":       "inquiry",
            "sort_dt":      last_msg.created_at if last_msg else inq.created_at,
        })

    conversations.sort(key=lambda c: c["sort_dt"], reverse=True)

    return templates.TemplateResponse("messages/inbox.html", {
        **ctx,
        "conversations": conversations,
    })


# ── Conversation ──────────────────────────────────────────────────────────────

@router.get("/{booking_id}", response_class=HTMLResponse)
def conversation(
    booking_id:   int,
    request:      Request,
    ctx:          dict        = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    booking = _load_booking(booking_id, db)
    if not booking or not _can_access(booking, current_user):
        return RedirectResponse("/messages", status_code=303)

    # Mark incoming messages as read
    changed = False
    for m in booking.messages:
        if m.sender_id != current_user.id and not m.is_read:
            m.is_read = True
            changed   = True
    if changed:
        db.commit()

    messages  = list(booking.messages)
    partner   = _other_person(booking, current_user)
    last_id   = messages[-1].id if messages else 0
    today     = date.today()
    yesterday = today - timedelta(days=1)

    return templates.TemplateResponse("messages/conversation.html", {
        **ctx,
        "booking":   booking,
        "partner":   partner,
        "messages":  messages,
        "last_id":   last_id,
        "prev_date": None,
        "today":     today,
        "yesterday": yesterday,
    })


# ── Send ──────────────────────────────────────────────────────────────────────

@router.post("/{booking_id}", response_class=HTMLResponse)
def send_message(
    booking_id:   int,
    request:      Request,
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
    body:         str         = Form(...),
    _rl=rate_limit(30, 60),
):
    booking = _load_booking(booking_id, db)
    if not booking or not _can_access(booking, current_user):
        return HTMLResponse("", status_code=403)

    body = body.strip()[:2000]
    if not body:
        return HTMLResponse("", status_code=400)

    # Decide whether to email the recipient — computed BEFORE inserting the new
    # message. Debounced notifications: email only when the recipient is caught
    # up (no unread messages from us in this thread) AND there hasn't been any
    # message in the last NOTIFY_DEBOUNCE_MINUTES. This still emails the first
    # message and re-notifies after a lull, but stays quiet during an active
    # back-and-forth and sends at most one email to an away recipient until they
    # read it (a later reply finds prior messages still unread → no second email).
    recipient = _other_person(booking, current_user)
    recipient_unread = (
        db.query(models.Message)
        .filter(
            models.Message.booking_id == booking_id,
            models.Message.sender_id  != recipient.id,   # messages addressed to the recipient
            models.Message.is_read    == False,          # noqa: E712
        )
        .count()
    )
    last_at = (
        db.query(models.Message.created_at)
        .filter(models.Message.booking_id == booking_id)
        .order_by(models.Message.created_at.desc())
        .first()
    )
    should_notify = recipient_unread == 0 and (
        last_at is None
        or (datetime.utcnow() - last_at[0]) > timedelta(minutes=NOTIFY_DEBOUNCE_MINUTES)
    )

    msg = models.Message(
        booking_id=booking_id,
        sender_id=current_user.id,
        body=body,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    if should_notify:
        mailer.new_message_to_recipient(msg, recipient)

    # Return empty — the client-side poll will fetch and render the new message,
    # preventing the double-append race condition between POST and poll.
    return HTMLResponse("")


# ── Poll ──────────────────────────────────────────────────────────────────────

@router.get("/{booking_id}/poll", response_class=HTMLResponse)
def poll(
    booking_id:   int,
    request:      Request,
    after:        int         = 0,
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    booking = _load_booking(booking_id, db)
    if not booking or not _can_access(booking, current_user):
        return HTMLResponse("", status_code=403)

    new_msgs = (
        db.query(models.Message)
        .filter(
            models.Message.booking_id == booking_id,
            models.Message.id         >  after,
        )
        .order_by(models.Message.created_at)
        .all()
    )

    # Mark other person's new messages as read on poll
    changed = False
    for m in new_msgs:
        if m.sender_id != current_user.id and not m.is_read:
            m.is_read = True
            changed   = True
    if changed:
        db.commit()

    # Work out the date of the last already-displayed message so the template
    # can decide whether to emit a date separator at the top of the new batch.
    prev_msg = (
        db.query(models.Message)
        .filter(models.Message.id == after)
        .first()
    ) if after else None
    today     = date.today()
    yesterday = today - timedelta(days=1)

    return templates.TemplateResponse("messages/_bubbles.html", {
        "request":      request,
        "messages":     new_msgs,
        "current_user": current_user,
        "prev_date":    prev_msg.created_at.date() if prev_msg else None,
        "today":        today,
        "yesterday":    yesterday,
    })


# ── Private ───────────────────────────────────────────────────────────────────

def _load_booking(booking_id: int, db: Session) -> models.Booking | None:
    return (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.trip).joinedload(models.Trip.driver),
            joinedload(models.Booking.passenger),
            joinedload(models.Booking.messages).joinedload(models.Message.sender),
        )
        .filter(models.Booking.id == booking_id)
        .first()
    )


# ════════════════════════════════════════════════════════════════════════════
#  Pre-booking inquiry threads
#  A passenger can message a driver about a trip BEFORE booking. These threads
#  reserve no seat and never touch the booking state machine — they live in the
#  separate `inquiries` table and are surfaced in the same inbox.
# ════════════════════════════════════════════════════════════════════════════

inquiries_router = APIRouter(prefix="/inquiries", tags=["inquiries"])


def _load_inquiry(inquiry_id: int, db: Session) -> models.Inquiry | None:
    return (
        db.query(models.Inquiry)
        .options(
            joinedload(models.Inquiry.trip).joinedload(models.Trip.driver),
            joinedload(models.Inquiry.passenger),
            joinedload(models.Inquiry.messages).joinedload(models.Message.sender),
        )
        .filter(models.Inquiry.id == inquiry_id)
        .first()
    )


def _inquiry_partner(inq: models.Inquiry, me: models.User) -> models.User:
    return inq.trip.driver if inq.passenger_id == me.id else inq.passenger


def _inquiry_can_access(inq: models.Inquiry, me: models.User) -> bool:
    return inq.passenger_id == me.id or inq.trip.driver_id == me.id


@inquiries_router.post("/start")
def start_inquiry(
    request:      Request,
    trip_id:      int          = Form(...),
    current_user: models.User  = Depends(get_current_user),
    db:           Session      = Depends(get_db),
    _rl=rate_limit(20, 60),
):
    """Open (or create) the pre-booking thread for this rider + trip."""
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        return RedirectResponse("/trips", status_code=303)
    # A driver can't inquire about their own ride.
    if trip.driver_id == current_user.id:
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    inq = (
        db.query(models.Inquiry)
        .filter(
            models.Inquiry.trip_id      == trip_id,
            models.Inquiry.passenger_id == current_user.id,
        )
        .first()
    )
    if not inq:
        inq = models.Inquiry(trip_id=trip_id, passenger_id=current_user.id)
        db.add(inq)
        try:
            db.commit()
            db.refresh(inq)
        except IntegrityError:
            # Lost a race against a concurrent first message — reuse the winner.
            db.rollback()
            inq = (
                db.query(models.Inquiry)
                .filter(
                    models.Inquiry.trip_id      == trip_id,
                    models.Inquiry.passenger_id == current_user.id,
                )
                .first()
            )
    return RedirectResponse(f"/inquiries/{inq.id}", status_code=303)


@inquiries_router.get("/{inquiry_id}", response_class=HTMLResponse)
def inquiry_conversation(
    inquiry_id:   int,
    request:      Request,
    ctx:          dict        = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    inq = _load_inquiry(inquiry_id, db)
    if not inq or not _inquiry_can_access(inq, current_user):
        return RedirectResponse("/messages", status_code=303)

    changed = False
    for m in inq.messages:
        if m.sender_id != current_user.id and not m.is_read:
            m.is_read = True
            changed   = True
    if changed:
        db.commit()

    messages = list(inq.messages)
    partner  = _inquiry_partner(inq, current_user)
    last_id  = messages[-1].id if messages else 0
    # The passenger sees a "Book this ride" CTA; the driver does not.
    can_book = (
        inq.passenger_id == current_user.id
        and inq.trip.status == models.TripStatus.active
        and inq.trip.seats_available > 0
    )

    return templates.TemplateResponse("messages/inquiry.html", {
        **ctx,
        "inquiry":   inq,
        "trip":      inq.trip,
        "partner":   partner,
        "messages":  messages,
        "last_id":   last_id,
        "can_book":  can_book,
        "is_passenger": inq.passenger_id == current_user.id,
        "today":     date.today(),
        "yesterday": date.today() - timedelta(days=1),
    })


@inquiries_router.post("/{inquiry_id}", response_class=HTMLResponse)
def send_inquiry_message(
    inquiry_id:   int,
    request:      Request,
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
    body:         str         = Form(...),
    _rl=rate_limit(30, 60),
):
    inq = _load_inquiry(inquiry_id, db)
    if not inq or not _inquiry_can_access(inq, current_user):
        return HTMLResponse("", status_code=403)

    body = body.strip()[:2000]
    if not body:
        return HTMLResponse("", status_code=400)

    # Same debounced-notification logic as booking threads (see send_message).
    recipient = _inquiry_partner(inq, current_user)
    recipient_unread = (
        db.query(models.Message)
        .filter(
            models.Message.inquiry_id == inquiry_id,
            models.Message.sender_id  != recipient.id,
            models.Message.is_read    == False,  # noqa: E712
        )
        .count()
    )
    last_at = (
        db.query(models.Message.created_at)
        .filter(models.Message.inquiry_id == inquiry_id)
        .order_by(models.Message.created_at.desc())
        .first()
    )
    should_notify = recipient_unread == 0 and (
        last_at is None
        or (datetime.utcnow() - last_at[0]) > timedelta(minutes=NOTIFY_DEBOUNCE_MINUTES)
    )

    msg = models.Message(inquiry_id=inquiry_id, sender_id=current_user.id, body=body)
    inq.updated_at = datetime.utcnow()   # bump so the inbox re-sorts this thread up
    db.add(msg)
    db.commit()
    db.refresh(msg)

    if should_notify:
        mailer.new_inquiry_message(msg, recipient)

    return HTMLResponse("")


@inquiries_router.get("/{inquiry_id}/poll", response_class=HTMLResponse)
def inquiry_poll(
    inquiry_id:   int,
    request:      Request,
    after:        int         = 0,
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    inq = _load_inquiry(inquiry_id, db)
    if not inq or not _inquiry_can_access(inq, current_user):
        return HTMLResponse("", status_code=403)

    new_msgs = (
        db.query(models.Message)
        .filter(
            models.Message.inquiry_id == inquiry_id,
            models.Message.id         >  after,
        )
        .order_by(models.Message.created_at)
        .all()
    )

    changed = False
    for m in new_msgs:
        if m.sender_id != current_user.id and not m.is_read:
            m.is_read = True
            changed   = True
    if changed:
        db.commit()

    prev_msg = (
        db.query(models.Message).filter(models.Message.id == after).first()
    ) if after else None

    return templates.TemplateResponse("messages/_bubbles.html", {
        "request":      request,
        "messages":     new_msgs,
        "current_user": current_user,
        "prev_date":    prev_msg.created_at.date() if prev_msg else None,
        "today":        date.today(),
        "yesterday":    date.today() - timedelta(days=1),
    })
