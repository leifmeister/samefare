from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app import models, email as mailer
from app.database import get_db
from app.dependencies import get_current_user, get_template_context

templates = Jinja2Templates(directory="templates")
router    = APIRouter(prefix="/messages", tags=["messages"])


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
            models.Booking.status.in_([
                models.BookingStatus.confirmed,
                models.BookingStatus.pending,
                models.BookingStatus.awaiting_payment,
            ]),
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
            "booking":      b,
            "partner":      partner,
            "last_message": last_msg,
            "unread_count": unread,
        })

    conversations.sort(
        key=lambda c: (
            c["last_message"].created_at if c["last_message"]
            else c["booking"].created_at
        ),
        reverse=True,
    )

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

    messages = list(booking.messages)
    partner  = _other_person(booking, current_user)
    last_id  = messages[-1].id if messages else 0

    return templates.TemplateResponse("messages/conversation.html", {
        **ctx,
        "booking":  booking,
        "partner":  partner,
        "messages": messages,
        "last_id":  last_id,
    })


# ── Send ──────────────────────────────────────────────────────────────────────

@router.post("/{booking_id}", response_class=HTMLResponse)
def send_message(
    booking_id:   int,
    request:      Request,
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
    body:         str         = Form(...),
):
    booking = _load_booking(booking_id, db)
    if not booking or not _can_access(booking, current_user):
        return HTMLResponse("", status_code=403)

    body = body.strip()
    if not body:
        return HTMLResponse("", status_code=400)

    # Is this the first message in the thread? If so, email the recipient.
    existing_count = (
        db.query(models.Message)
        .filter(models.Message.booking_id == booking_id)
        .count()
    )
    first_message = existing_count == 0

    msg = models.Message(
        booking_id=booking_id,
        sender_id=current_user.id,
        body=body,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    if first_message:
        recipient = _other_person(booking, current_user)
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

    return templates.TemplateResponse("messages/_bubbles.html", {
        "request":      request,
        "messages":     new_msgs,
        "current_user": current_user,
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
