"""
Transactional email via Resend (https://resend.com).

All public functions are fire-and-forget: they catch every exception so a
mail failure never breaks the booking/payment flow.
"""

import html
import json
import logging
import urllib.parse
import urllib.request
import urllib.error

from app.config import get_settings

log = logging.getLogger(__name__)


# ── Low-level sender ──────────────────────────────────────────────────────────

def _send(to: str, subject: str, html: str) -> None:
    """Send a single HTML email via Resend REST API. Silently logs on failure."""
    s = get_settings()
    if s.beta_mode:
        log.debug("Beta mode — suppressing email to %s: %s", to, subject)
        return
    if not s.resend_api_key:
        log.debug("Resend not configured — skipping email to %s: %s", to, subject)
        return

    payload = json.dumps({
        "from":    s.email_from,
        "to":      [to],
        "subject": subject,
        "html":    html,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {s.resend_api_key}",
            "Content-Type":  "application/json",
            "User-Agent":    "SameFare/1.0",
            "Accept":        "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("Email sent → %s  [%s] status=%s", to, subject, resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        log.warning("Email failed → %s  [%s]: %s %s", to, subject, exc.code, body)
    except Exception as exc:
        log.warning("Email failed → %s  [%s]: %s", to, subject, exc)


# ── Shared layout ─────────────────────────────────────────────────────────────

def _wrap(body: str) -> str:
    s = get_settings()
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
</head>
<body style="margin:0;padding:0;background:#F7FAF9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F7FAF9;padding:32px 16px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:12px;overflow:hidden;
                    border:1px solid #DDE8E5;max-width:560px;width:100%;">

        <!-- Header -->
        <tr>
          <td style="background:#006C5B;padding:24px 32px;">
            <a href="{s.base_url}" style="text-decoration:none;">
              <span style="color:#ffffff;font-size:1.25rem;font-weight:700;letter-spacing:-.02em;">
                SameFare
              </span>
            </a>
          </td>
        </tr>

        <!-- Body -->
        <tr><td style="padding:32px 32px 24px;">{body}</td></tr>

        <!-- Footer -->
        <tr>
          <td style="background:#F7FAF9;padding:20px 32px;border-top:1px solid #DDE8E5;">
            <p style="margin:0;font-size:.75rem;color:#64748B;line-height:1.6;">
              You're receiving this because you have an account on
              <a href="{s.base_url}" style="color:#006C5B;">{s.base_url.replace('https://','')}</a>.
              Questions? Contact
              <a href="mailto:samefare@samefare.com" style="color:#006C5B;">samefare@samefare.com</a>.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _btn(label: str, url: str) -> str:
    return (f'<a href="{url}" style="display:inline-block;margin-top:20px;'
            f'padding:12px 24px;background:#006C5B;color:#ffffff;text-decoration:none;'
            f'border-radius:8px;font-weight:600;font-size:.9375rem;">{label}</a>')


def _route_line(origin: str, destination: str, dt) -> str:
    date_str = dt.strftime("%-d %B %Y, %H:%M")
    o, d = html.escape(origin), html.escape(destination)
    return (f'<p style="margin:0 0 4px;font-size:1.1rem;font-weight:700;color:#1A2B3C;">'
            f'{o} → {d}</p>'
            f'<p style="margin:0;font-size:.875rem;color:#64748B;">{date_str}</p>')


def _h1(text: str) -> str:
    return f'<h1 style="margin:0 0 16px;font-size:1.375rem;font-weight:800;color:#1A2B3C;">{text}</h1>'


def _p(text: str) -> str:
    return f'<p style="margin:0 0 12px;font-size:.9375rem;color:#475569;line-height:1.6;">{text}</p>'


def _divider() -> str:
    return '<hr style="border:none;border-top:1px solid #DDE8E5;margin:20px 0;"/>'


# ── Public API ────────────────────────────────────────────────────────────────

def booking_request_to_driver(booking) -> None:
    s    = get_settings()
    trip = booking.trip
    pax  = booking.passenger
    body = (
        _h1("New booking request") +
        _p(f"<strong>{pax.full_name}</strong> is requesting "
           f"<strong>{booking.seats_booked} seat{'s' if booking.seats_booked != 1 else ''}</strong> "
           f"on your trip:") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0 4px;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}</div>' +
        (f'<p style="margin:12px 0 0;font-size:.875rem;font-style:italic;color:#475569;">'
         f'"{booking.message}"</p>' if booking.message else '') +
        _divider() +
        _p("Review and accept or decline this request on your trips page.") +
        _btn("Review request", f"{s.base_url}/my-trips?tab=rides")
    )
    _send(trip.driver.email, f"New booking request — {trip.origin} → {trip.destination}", _wrap(body))


def booking_confirmed_to_driver(booking) -> None:
    s    = get_settings()
    trip = booking.trip
    pax  = booking.passenger
    body = (
        _h1("New confirmed passenger") +
        _p(f"<strong>{pax.full_name}</strong> has confirmed "
           f"<strong>{booking.seats_booked} seat{'s' if booking.seats_booked != 1 else ''}</strong> "
           f"on your trip:") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}</div>' +
        _divider() +
        _btn("View trip", f"{s.base_url}/trips/{trip.id}")
    )
    _send(trip.driver.email, f"Passenger confirmed — {trip.origin} → {trip.destination}", _wrap(body))


def booking_confirmed_to_passenger(booking) -> None:
    s    = get_settings()
    trip = booking.trip
    body = (
        _h1("You're booked! 🎉") +
        _p("Your seat is confirmed. Here are your trip details:") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}' +
        (f'<p style="margin:12px 0 0;font-size:.875rem;color:#475569;">'
         f'📍 Pick up: {trip.pickup_address}</p>' if trip.pickup_address else '') +
        f'</div>' +
        _divider() +
        _p(f"Driver: <strong>{trip.driver.full_name}</strong>") +
        _p(f"Total paid: <strong>{booking.total_price:,} ISK</strong>") +
        _btn("View my booking", f"{s.base_url}/my-trips?tab=bookings")
    )
    _send(booking.passenger.email, f"Booking confirmed — {trip.origin} → {trip.destination}", _wrap(body))


def booking_approved_to_passenger(booking) -> None:
    s    = get_settings()
    trip = booking.trip
    # Route the payment CTA to the appropriate checkout depending on payment rail
    from app.models import TripPaymentMethod  # local import avoids circular
    if trip.payment_method == TripPaymentMethod.blikk:
        payment_url  = f"{s.base_url}/bookings/{booking.id}/blikk-pay"
        payment_note = "Complete your payment via Blikk bank transfer"
    else:
        payment_url  = f"{s.base_url}/payments/checkout/{booking.id}"
        payment_note = "Complete your payment"
    body = (
        _h1("Your request was approved!") +
        _p(f"<strong>{trip.driver.full_name}</strong> has accepted your booking request for:") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}</div>' +
        _divider() +
        _p(f"To confirm your seat, complete your payment of "
           f"<strong>{booking.total_price:,} ISK</strong> within 24 hours, "
           f"otherwise your spot may be released.") +
        _btn(payment_note, payment_url)
    )
    _send(booking.passenger.email, f"Request approved — {trip.origin} → {trip.destination}", _wrap(body))


def booking_cancelled_charged(booking) -> None:
    """
    Notify the driver that a passenger cancelled within 24 hours of departure.
    The pre-auth was already in place so the full amount will still be captured
    and paid out — the driver keeps their contribution.
    """
    s    = get_settings()
    trip = booking.trip
    pax  = booking.passenger
    amount = booking.subtotal
    body = (
        _h1("Passenger cancelled — you will still be paid") +
        _p(f"<strong>{pax.full_name}</strong> cancelled their booking, but because "
           f"the cancellation was made within 24 hours of departure the full amount "
           f"will still be captured and your contribution paid out.") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}</div>' +
        _p(f"Your contribution: <strong>{amount:,} ISK</strong>") +
        _p("The seat has been released and will not be rebooked. "
           "No action is required from you.") +
        _btn("View trip", f"{s.base_url}/trips/{trip.id}")
    )
    _send(
        trip.driver.email,
        f"Passenger cancelled (you're still paid) — {trip.origin} → {trip.destination}",
        _wrap(body),
    )


def booking_cancelled_to_driver(booking) -> None:
    s    = get_settings()
    trip = booking.trip
    pax  = booking.passenger
    body = (
        _h1("Booking cancelled") +
        _p(f"<strong>{pax.full_name}</strong> has cancelled their booking on your trip:") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}</div>' +
        _p(f"Seat{'s' if booking.seats_booked != 1 else ''} released: <strong>{booking.seats_booked}</strong>") +
        _btn("View trip", f"{s.base_url}/trips/{trip.id}")
    )
    _send(trip.driver.email, f"Booking cancelled — {trip.origin} → {trip.destination}", _wrap(body))


def booking_cancelled_to_passenger(booking) -> None:
    s    = get_settings()
    trip = booking.trip
    p    = booking.payment

    # Determine whether the pre-auth was in place at the time of cancellation.
    # If capture_at has been moved to now/past it means cancel_booking triggered
    # an immediate capture (≤24h policy).
    pre_auth_captured = (
        p is not None
        and p.status in (
            models.PaymentStatus.authorised,
            models.PaymentStatus.capture_requested,
            models.PaymentStatus.captured,
        )
    )
    card_not_charged = not p or p.status in (
        models.PaymentStatus.pending,
        models.PaymentStatus.failed,
    )

    if card_not_charged:
        charge_line = _p(
            "Your card was <strong>not charged</strong>. "
            "Your seat has been released."
        )
    elif pre_auth_captured:
        charge_line = _p(
            f"Because you cancelled within 24 hours of departure, the full amount of "
            f"<strong>{booking.total_price:,} ISK</strong> has been charged. "
            "No refund applies — the seat reservation is considered fulfilled per our "
            "<a href='https://samefare.com/terms' style='color:#006C5B;'>cancellation policy</a>."
        )
    else:
        charge_line = _p("No charge applies for this cancellation.")

    body = (
        _h1("Booking cancelled") +
        _p("Your booking has been cancelled:") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}</div>' +
        _divider() +
        charge_line +
        _btn("Find another ride", f"{s.base_url}/trips?" + urllib.parse.urlencode({"origin": trip.origin, "destination": trip.destination}))
    )
    _send(booking.passenger.email, f"Booking cancelled — {trip.origin} → {trip.destination}", _wrap(body))


def trip_cancelled_to_passenger(booking) -> None:
    s    = get_settings()
    trip = booking.trip
    body = (
        _h1("Trip cancelled") +
        _p(f"We're sorry — <strong>{trip.driver.full_name}</strong> has cancelled the following trip:") +
        f'<div style="background:#FEE2E2;border:1px solid #FCA5A5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}</div>' +
        _divider() +
        _p("A <strong>full refund</strong> (including service fee) will be returned to your "
           "original payment method within 5–10 business days.") +
        _btn("Find another ride", f"{s.base_url}/trips?" + urllib.parse.urlencode({"origin": trip.origin, "destination": trip.destination}))
    )
    _send(booking.passenger.email, f"Trip cancelled — {trip.origin} → {trip.destination}", _wrap(body))


def new_message_to_recipient(message, recipient) -> None:
    s       = get_settings()
    booking = message.booking
    trip    = booking.trip
    sender  = message.sender
    body = (
        _h1(f"New message from {sender.full_name.split()[0]}") +
        f'<div style="background:#F7FAF9;border-left:3px solid #006C5B;'
        f'padding:12px 16px;margin:8px 0 16px;border-radius:0 8px 8px 0;">'
        f'<p style="margin:0;font-size:.9375rem;color:#1A2B3C;">{message.body}</p></div>' +
        _p(f"Regarding your trip: <strong>{trip.origin} → {trip.destination}</strong>, "
           f"{trip.departure_datetime.strftime('%-d %B %Y')}") +
        _btn("Reply", f"{s.base_url}/messages/{booking.id}")
    )
    _send(recipient.email, f"New message from {sender.full_name.split()[0]} — SameFare", _wrap(body))


def email_verification(user, token: str) -> None:
    s   = get_settings()
    url = f"{s.base_url}/verify-email?token={token}"
    body = (
        _h1("Verify your email address") +
        _p(f"Hi {user.full_name.split()[0]}, thanks for joining SameFare! "
           f"Please verify your email address to start booking rides.") +
        _btn("Verify email", url) +
        _divider() +
        _p("This link expires in <strong>24 hours</strong>. "
           "If you didn't create a SameFare account, you can safely ignore this email.")
    )
    _send(user.email, "Verify your SameFare email address", _wrap(body))


def ride_alert_notification(alert, trips: list) -> None:
    """
    Notify a user (or guest) that one or more rides matching their alert
    have just been posted.

    *alert* is a RideAlert model instance.
    *trips* is a list of Trip instances.
    """
    s = get_settings()
    if not trips:
        return

    route_label = f"{html.escape(alert.origin)} → {html.escape(alert.destination)}"

    # Build a card for each matching trip
    trip_cards = ""
    for trip in trips[:5]:          # cap at 5 so the email doesn't become a wall
        date_str = trip.departure_datetime.strftime("%-d %B %Y, %H:%M")
        t_origin = html.escape(trip.origin)
        t_dest   = html.escape(trip.destination)
        trip_cards += (
            f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
            f'padding:16px;margin:0 0 12px;">'
            f'<p style="margin:0 0 4px;font-size:1rem;font-weight:700;color:#1A2B3C;">'
            f'{t_origin} → {t_dest}</p>'
            f'<p style="margin:0 0 8px;font-size:.875rem;color:#64748B;">{date_str}</p>'
            f'<p style="margin:0;font-size:.875rem;color:#475569;">'
            f'<strong>{trip.price_per_seat:,} ISK</strong> per seat &nbsp;·&nbsp; '
            f'{trip.seats_available} seat{"s" if trip.seats_available != 1 else ""} available</p>'
            f'</div>'
            f'{_btn("View ride", f"{s.base_url}/trips/{trip.id}")}<br/><br/>'
        )

    unsubscribe_url = f"{s.base_url}/alerts/unsubscribe/{alert.token}"

    body = (
        _h1(f"A ride is available: {route_label} 🎉") +
        _p(f"Good news — {'a new ride' if len(trips) == 1 else str(len(trips)) + ' new rides'} "
           f"matching your alert just {'was' if len(trips) == 1 else 'were'} posted:") +
        trip_cards +
        _divider() +
        f'<p style="margin:0;font-size:.8125rem;color:#94A3B8;line-height:1.6;">'
        f'You set up a ride alert for <strong>{route_label}</strong>. '
        f'<a href="{unsubscribe_url}" style="color:#94A3B8;">Unsubscribe from this alert</a>.'
        f'</p>'
    )
    _send(alert.email,
          f"Ride available: {route_label} — SameFare",
          _wrap(body))


def card_saved_to_passenger(booking) -> None:
    """
    Case B: card tokenised successfully, MIT scheduled 24 h before departure.
    """
    s    = get_settings()
    trip = booking.trip
    from datetime import timedelta as _td
    auth_time = trip.departure_datetime - _td(hours=24)
    body = (
        _h1("Card saved — you're all set ✓") +
        _p("Your card has been securely saved. We'll authorise the payment "
           "24 hours before your trip — no action needed from you.") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}'
        f'<p style="margin:12px 0 0;font-size:.875rem;color:#475569;">'
        f'Card will be charged: <strong>{auth_time.strftime("%-d %B %Y at %H:%M")}</strong>'
        f'</p></div>' +
        _divider() +
        _p(f"Amount to be charged: <strong>{booking.total_price:,} ISK</strong>") +
        _p("You may cancel for free any time more than 24 hours before departure.") +
        _btn("View my booking", f"{s.base_url}/my-trips?tab=bookings")
    )
    _send(
        booking.passenger.email,
        f"Card saved — {trip.origin} → {trip.destination}",
        _wrap(body),
    )


def mit_auth_failed_to_passenger(booking, retry_deadline) -> None:
    """
    Case B: MIT authorisation declined 24 h before departure.
    Passenger has 2 hours to update card.  +5 % surcharge notice included.
    """
    import html as _html
    s    = get_settings()
    trip = booking.trip
    deadline_str = retry_deadline.strftime("%-d %B %Y at %H:%M") if retry_deadline else "soon"
    body = (
        _h1("Action required: payment authorisation failed ⚠") +
        _p(f"We were unable to authorise your card for the trip "
           f"<strong>{_html.escape(trip.origin)} → {_html.escape(trip.destination)}</strong> "
           f"on <strong>{trip.departure_datetime.strftime('%-d %B %Y')}</strong>.") +
        f'<div style="background:#FEF3C7;border:1px solid #F59E0B;border-radius:8px;'
        f'padding:16px;margin:8px 0 16px;">'
        f'<p style="margin:0;font-size:.9375rem;font-weight:600;color:#92400E;">'
        f'Please update your payment card by <strong>{deadline_str}</strong> '
        f'to keep your seat.</p>'
        f'<p style="margin:8px 0 0;font-size:.875rem;color:#78350F;">'
        f'A 5% late-authorisation fee now applies. New total: '
        f'<strong>{booking.total_price:,} ISK</strong>.</p>'
        f'</div>' +
        _p("If you don't update your card within 2 hours, your booking will be "
           "cancelled and your seat released to other passengers.") +
        _btn("Update my card now", f"{s.base_url}/payments/auth-failed/{booking.id}") +
        _divider() +
        _p("If you no longer need this seat, no action is needed — your booking "
           "will be cancelled automatically when the window expires.")
    )
    _send(
        booking.passenger.email,
        f"Action required: payment failed — {trip.origin} → {trip.destination}",
        _wrap(body),
    )


def trip_reminder_to_driver(trip, passenger_count: int) -> None:
    s          = get_settings()
    departure  = trip.departure_datetime.strftime("%-d %B %Y at %H:%M")
    pax_label  = f"{passenger_count} passenger{'s' if passenger_count != 1 else ''}"
    body = (
        _h1("Your trip is tomorrow") +
        _p(f"Hi {trip.driver.full_name.split()[0]}, just a heads-up — you have "
           f"<strong>{pax_label}</strong> confirmed for tomorrow's trip:") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}'
        f'</div>' +
        _divider() +
        _p("Have a safe trip!") +
        _btn("View trip", f"{s.base_url}/my-trips?tab=rides")
    )
    _send(
        trip.driver.email,
        f"Trip reminder — {trip.origin} → {trip.destination} · {departure}",
        _wrap(body),
    )


def trip_reminder_to_passenger(booking) -> None:
    s        = get_settings()
    trip     = booking.trip
    driver   = trip.driver.full_name.split()[0]
    origin   = booking.pickup_city or trip.origin
    dest     = booking.dropoff_city or trip.destination
    departure = trip.departure_datetime.strftime("%-d %B %Y at %H:%M")
    body = (
        _h1("Your ride is tomorrow") +
        _p(f"Hi {booking.passenger.full_name.split()[0]}, just a reminder — "
           f"your ride with <strong>{driver}</strong> is tomorrow:") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(origin, dest, trip.departure_datetime)}' +
        (f'<p style="margin:12px 0 0;font-size:.875rem;color:#475569;">'
         f'📍 Pickup: {trip.pickup_address}</p>' if trip.pickup_address else '') +
        f'</div>' +
        _divider() +
        _p(f"Message {driver} if you need to confirm the exact pickup point.") +
        _btn("Open conversation", f"{s.base_url}/messages/{booking.id}")
    )
    _send(
        booking.passenger.email,
        f"Ride reminder — {origin} → {dest} · {departure}",
        _wrap(body),
    )


def verification_approved(user, verification_type: str) -> None:
    """Notify a user that their identity or driver's licence has been approved."""
    s = get_settings()
    if verification_type == "licence":
        title   = "Driver's licence verified ✓"
        message = (
            "Your driver's licence has been verified. "
            "You can now post rides on SameFare."
        )
        if user.id_verification != "approved":
            message = (
                "Your driver's licence has been verified — this also confirms your identity. "
                "You can now post and book rides on SameFare."
            )
    else:
        title   = "Identity verified ✓"
        message = (
            "Your identity has been verified. "
            "You can now book rides on SameFare."
        )
    body = (
        _h1(title) +
        _p(f"Hi {user.full_name.split()[0]},") +
        _p(message) +
        _btn("Go to SameFare", s.base_url)
    )
    _send(user.email, title, _wrap(body))


def verification_rejected(user, verification_type: str, reason: str | None = None) -> None:
    """Notify a user that their document submission was rejected."""
    if verification_type == "licence":
        title    = "Driver's licence could not be verified"
        doc_name = "driver's licence"
    else:
        title    = "Identity document could not be verified"
        doc_name = "identity document"

    s = get_settings()
    reason_line = (
        f'<p style="margin:12px 0;padding:12px 16px;background:#FEF2F2;'
        f'border-left:3px solid #DC2626;border-radius:4px;'
        f'font-size:.875rem;color:#7F1D1D;">'
        f'Reason: {html.escape(reason)}</p>'
    ) if reason else ""

    body = (
        _h1(title) +
        _p(f"Hi {user.full_name.split()[0]},") +
        _p(
            f"Unfortunately we were unable to verify your {doc_name}. "
            "Please resubmit using a clear, unobstructed photo of a valid document."
        ) +
        reason_line +
        _btn("Resubmit document", f"{s.base_url}/verify")
    )
    _send(user.email, title, _wrap(body))


def password_reset(user, token: str) -> None:
    s    = get_settings()
    url  = f"{s.base_url}/reset-password?token={token}"
    body = (
        _h1("Reset your password") +
        _p(f"Hi {user.full_name.split()[0]}, we received a request to reset your SameFare password.") +
        _btn("Reset password", url) +
        _divider() +
        _p('This link expires in <strong>1 hour</strong>. '
           'If you didn\'t request a reset, you can safely ignore this email — '
           'your password won\'t change.')
    )
    _send(user.email, "Reset your SameFare password", _wrap(body))


def licence_expiry_warning(user, days_left: int) -> None:
    """
    Warn a driver that their licence expires within 30 days.
    Sent by the daily _run_licence_expiry_check() task.
    """
    import html as _html
    name     = _html.escape(user.full_name.split()[0] if user.full_name else "there")
    expiry   = user.licence_expiry.strftime("%-d %B %Y") if user.licence_expiry else "soon"
    urgency  = "⚠️" if days_left > 14 else "🚨"

    body = (
        _h1(f"{urgency} Your driver's licence expires in {days_left} day{'s' if days_left != 1 else ''}") +
        _p(f"Hi {name},") +
        _p(
            f"Your verified driver's licence on SameFare expires on <strong>{expiry}</strong>. "
            f"Once it expires, your account will be automatically suspended from posting new trips "
            f"until you re-verify with an updated licence."
        ) +
        _p(
            f"To keep posting trips without interruption, please re-verify your licence "
            f"before <strong>{expiry}</strong>."
        ) +
        _btn("Re-verify my licence", f"{get_settings().base_url}/verify") +
        _divider() +
        _p(
            "Re-verification takes about 2 minutes. You'll need your current, valid driver's licence."
        )
    )
    _send(
        user.email,
        f"Action required — your driver's licence expires in {days_left} day{'s' if days_left != 1 else ''}",
        _wrap(body),
    )


def licence_expired_suspension(user) -> None:
    """
    Notify a driver that their licence has expired, their account has been
    suspended from posting, and they must re-verify to continue.
    Sent by the daily _run_licence_expiry_check() task.
    """
    import html as _html
    name   = _html.escape(user.full_name.split()[0] if user.full_name else "there")
    expiry = user.licence_expiry.strftime("%-d %B %Y") if user.licence_expiry else "recently"

    body = (
        _h1("🚫 Your driver's licence has expired — account suspended") +
        _p(f"Hi {name},") +
        _p(
            f"Your verified driver's licence expired on <strong>{expiry}</strong>. "
            f"Your account has been automatically suspended from posting new trips "
            f"to comply with our driver verification requirements."
        ) +
        _p(
            "To resume posting trips, please re-verify your licence with a valid, "
            "current document. Re-verification takes about 2 minutes."
        ) +
        _btn("Re-verify my licence", f"{get_settings().base_url}/verify") +
        _divider() +
        _p(
            "Your existing bookings as a passenger are not affected. "
            "If you believe this is an error, please contact "
            "<a href='mailto:samefare@samefare.com' style='color:#006C5B;'>samefare@samefare.com</a>."
        )
    )
    _send(
        user.email,
        "Your SameFare account has been suspended — licence expired",
        _wrap(body),
    )


def aml_monitoring_alert(flags: list[dict], report_date) -> None:
    """
    Daily AML monitoring digest — sent to the admin inbox only when ≥1 red flag
    is raised by _run_aml_monitoring().  Each flag is a dict with keys:
        flag     — short label (str)
        user_id  — platform user ID (int)
        name     — display name (str)
        detail   — one-line human-readable detail (str)
    """
    import html as _html
    s = get_settings()
    date_str = report_date.strftime("%-d %B %Y") if hasattr(report_date, "strftime") else str(report_date)
    n = len(flags)

    rows_html = ""
    for f in flags:
        flag_label  = _html.escape(str(f.get("flag", "")))
        user_id     = _html.escape(str(f.get("user_id", "")))
        name        = _html.escape(str(f.get("name", "")))
        detail      = _html.escape(str(f.get("detail", "")))
        rows_html += (
            f'<tr>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #DDE8E5;color:#DC2626;font-weight:600;">{flag_label}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #DDE8E5;">{name} (ID {user_id})</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #DDE8E5;color:#475569;font-size:.85rem;">{detail}</td>'
            f'</tr>'
        )

    body = (
        _h1(f"⚠️ AML Monitoring Alert — {date_str}") +
        _p(f"The nightly AML sweep raised <strong>{n} flag{'s' if n != 1 else ''}</strong> "
           f"requiring review. Each item should be assessed against the SAR procedure.") +
        _divider() +
        f'<table style="width:100%;border-collapse:collapse;font-size:.875rem;">'
        f'<thead>'
        f'<tr style="background:#F7FAF9;">'
        f'<th style="padding:8px 10px;text-align:left;color:#64748B;font-weight:600;border-bottom:2px solid #DDE8E5;">Flag</th>'
        f'<th style="padding:8px 10px;text-align:left;color:#64748B;font-weight:600;border-bottom:2px solid #DDE8E5;">User</th>'
        f'<th style="padding:8px 10px;text-align:left;color:#64748B;font-weight:600;border-bottom:2px solid #DDE8E5;">Detail</th>'
        f'</tr>'
        f'</thead>'
        f'<tbody>{rows_html}</tbody>'
        f'</table>' +
        _divider() +
        _p("Review each flag against your <strong>SAR &amp; AML Internal Procedure</strong>. "
           "Document your decision in the SAR Decision Log regardless of outcome. "
           "If reasonable suspicion exists, file a SAR before taking action on the account.")
    )
    _send(
        s.email_from,
        f"[AML Alert] {n} flag{'s' if n != 1 else ''} raised — {date_str}",
        _wrap(body),
    )


def driver_no_show_admin_alert(booking, driver) -> None:
    """
    Notify the SameFare admin inbox when a passenger reports a driver no-show.
    Sent immediately after the report is filed so admin can investigate and
    process the payment void / refund via the payment provider dashboard.
    """
    import html as _html
    s    = get_settings()
    trip = booking.trip
    passenger = booking.passenger

    driver_name   = _html.escape(driver.full_name)
    passenger_name = _html.escape(passenger.full_name)
    route         = f"{_html.escape(trip.origin)} → {_html.escape(trip.destination)}"
    dep           = trip.departure_datetime.strftime("%-d %b %Y at %H:%M")
    amount        = f"{booking.total_price:,} ISK"

    body = (
        _h1("⚠️ Driver no-show reported") +
        _p(f"A passenger has reported that the driver did not show up for their ride.") +
        _divider() +
        f'<table style="width:100%;border-collapse:collapse;font-size:.9rem;">'
        f'<tr><td style="padding:6px 0;color:#64748B;width:40%;">Driver</td>'
        f'<td style="padding:6px 0;font-weight:600;">{driver_name} (ID {driver.id})</td></tr>'
        f'<tr><td style="padding:6px 0;color:#64748B;">Passenger</td>'
        f'<td style="padding:6px 0;font-weight:600;">{passenger_name} (ID {passenger.id})</td></tr>'
        f'<tr><td style="padding:6px 0;color:#64748B;">Route</td>'
        f'<td style="padding:6px 0;">{route}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#64748B;">Departure</td>'
        f'<td style="padding:6px 0;">{dep}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#64748B;">Booking ID</td>'
        f'<td style="padding:6px 0;">#{booking.id}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#64748B;">Amount</td>'
        f'<td style="padding:6px 0;">{amount}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#64748B;">Driver no-shows</td>'
        f'<td style="padding:6px 0;color:#DC2626;font-weight:700;">{driver.no_shows_confirmed}</td></tr>'
        f'</table>' +
        _divider() +
        _p(f'Driver account has been <strong>automatically suspended</strong> from posting new trips. '
           f'Reinstate via the admin panel once investigation is complete.') +
        _p(f'Action required: void or refund the pre-auth / capture for booking #{booking.id} '
           f'via the payment provider dashboard.')
    )
    _send(
        s.email_from,   # admin inbox — same address the platform sends from
        f"[Action required] Driver no-show — {driver_name} — booking #{booking.id}",
        _wrap(body),
    )
