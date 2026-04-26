"""
Transactional email via Resend (https://resend.com).

All public functions are fire-and-forget: they catch every exception so a
mail failure never breaks the booking/payment flow.
"""

import json
import logging
import urllib.request
import urllib.error

from app.config import get_settings

log = logging.getLogger(__name__)


# ── Low-level sender ──────────────────────────────────────────────────────────

def _send(to: str, subject: str, html: str) -> None:
    """Send a single HTML email via Resend REST API. Silently logs on failure."""
    s = get_settings()
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
              <a href="mailto:support@samefare.com" style="color:#006C5B;">support@samefare.com</a>.
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
    return (f'<p style="margin:0 0 4px;font-size:1.1rem;font-weight:700;color:#1A2B3C;">'
            f'{origin} → {destination}</p>'
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
        _btn("Complete payment", f"{s.base_url}/payments/checkout/{booking.id}")
    )
    _send(booking.passenger.email, f"Request approved — {trip.origin} → {trip.destination}", _wrap(body))


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
    refund = booking.payment.refund_amount if booking.payment else 0
    if refund > 0:
        refund_line = _p(f"Refund of <strong>{refund:,} ISK</strong> will be returned to your "
                         f"original payment method within 5–10 business days.")
    else:
        refund_line = _p("No refund applies for this cancellation.")

    body = (
        _h1("Booking cancelled") +
        _p("Your booking has been cancelled:") +
        f'<div style="background:#F7FAF9;border:1px solid #DDE8E5;border-radius:8px;'
        f'padding:16px;margin:8px 0;">'
        f'{_route_line(trip.origin, trip.destination, trip.departure_datetime)}</div>' +
        _divider() +
        refund_line +
        _btn("Find another ride", f"{s.base_url}/trips?origin={trip.origin}&destination={trip.destination}")
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
        _btn("Find another ride", f"{s.base_url}/trips?origin={trip.origin}&destination={trip.destination}")
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
