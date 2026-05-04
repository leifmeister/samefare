"""
SMS via Twilio (https://twilio.com).

Usage matches BlaBlaCar's approach:
  1. Phone OTP verification
  2. Day-before trip reminder to driver + all confirmed passengers

All functions are fire-and-forget — exceptions are logged, never raised.

Required env vars:
    TWILIO_ACCOUNT_SID   ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    TWILIO_AUTH_TOKEN    your_auth_token
    TWILIO_FROM_NUMBER   +15551234567
"""

import json
import logging
import urllib.parse
import urllib.request
import urllib.error
from base64 import b64encode

from app.config import get_settings

log = logging.getLogger(__name__)


# ── Low-level sender ──────────────────────────────────────────────────────────

def _send(to: str, body: str) -> None:
    """Send a single SMS via Twilio REST API. Silently logs on failure."""
    s = get_settings()
    if not s.twilio_account_sid or not s.twilio_auth_token or not s.twilio_from_number:
        log.debug("Twilio not configured — skipping SMS to %s", to)
        return
    if not to:
        log.debug("No phone number — skipping SMS")
        return

    credentials = b64encode(
        f"{s.twilio_account_sid}:{s.twilio_auth_token}".encode()
    ).decode()

    payload = urllib.parse.urlencode({
        "To":   to,
        "From": s.twilio_from_number,
        "Body": body,
    }).encode("utf-8")

    url = (f"https://api.twilio.com/2010-04-01/Accounts/"
           f"{s.twilio_account_sid}/Messages.json")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
            "User-Agent":    "SameFare/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("SMS sent → %s  status=%s", to, resp.status)
    except urllib.error.HTTPError as exc:
        body_err = exc.read().decode("utf-8", errors="replace")
        log.warning("SMS failed → %s: %s %s", to, exc.code, body_err)
    except Exception as exc:
        log.warning("SMS failed → %s: %s", to, exc)


# ── Public API ────────────────────────────────────────────────────────────────

def send_otp(phone: str, code: str) -> None:
    """Send a 6-digit OTP for phone number verification."""
    _send(phone, f"Your SameFare verification code is: {code}\n\nExpires in 10 minutes.")


def trip_cancelled_to_passenger(booking) -> None:
    """
    Sent immediately when a driver cancels a trip with confirmed passengers.
    Time-critical — passenger needs to know ASAP so they can find another ride.
    """
    if not booking.passenger.phone:
        return
    trip = booking.trip
    _send(
        booking.passenger.phone,
        f"SameFare: {trip.driver.full_name.split()[0]} has cancelled the trip "
        f"{trip.origin} → {trip.destination} on {trip.departure_datetime.strftime('%-d %b')}. "
        f"Full refund issued. Find another ride: samefare.com/trips",
    )


def trip_reminder_to_driver(trip, passenger_count: int) -> None:
    """
    Day-before reminder to the driver.
    Sent ~20:00 the evening before departure.
    """
    if not trip.driver.phone:
        return
    departure = trip.departure_datetime.strftime("%H:%M")
    _send(
        trip.driver.phone,
        f"SameFare reminder: you have {passenger_count} passenger"
        f"{'s' if passenger_count != 1 else ''} tomorrow for "
        f"{trip.origin} → {trip.destination} at {departure}. "
        f"Safe travels! samefare.com/my-trips",
    )


def mit_auth_failed_to_passenger(booking, retry_deadline) -> None:
    """
    Case B: MIT authorisation declined 24 h before departure.
    Passenger has 2 hours to update their payment card.
    Includes the +5 % surcharge notice.
    """
    if not booking.passenger.phone:
        return
    trip     = booking.trip
    deadline = retry_deadline.strftime("%H:%M") if retry_deadline else "soon"
    s        = get_settings()
    _send(
        booking.passenger.phone,
        f"SameFare: your payment for {trip.origin} → {trip.destination} "
        f"({trip.departure_datetime.strftime('%-d %b')}) could not be authorised. "
        f"Please update your card by {deadline} to keep your seat. "
        f"Note: a 5% late fee now applies. "
        f"{s.base_url}/payments/auth-failed/{booking.id}",
    )


def mit_auth_failed_to_driver(booking) -> None:
    """
    Case B: notify driver that a passenger's payment is at risk.
    Driver does not need to act, but should know the seat may be released.
    """
    if not booking.trip.driver.phone:
        return
    trip = booking.trip
    pax  = booking.passenger.full_name.split()[0]
    _send(
        trip.driver.phone,
        f"SameFare: {pax}'s payment for your trip "
        f"{trip.origin} → {trip.destination} "
        f"({trip.departure_datetime.strftime('%-d %b')}) failed. "
        f"They have 2 hours to update their card — if not resolved their seat will be released. "
        f"samefare.com/my-trips",
    )


def retry_expired_to_driver(booking) -> None:
    """
    The passenger's 2-hour retry window expired without them updating their card.
    Driver is notified that the seat has been released.
    """
    if not booking.trip.driver.phone:
        return
    trip = booking.trip
    pax  = booking.passenger.full_name.split()[0]
    _send(
        trip.driver.phone,
        f"SameFare: {pax}'s booking on your trip "
        f"{trip.origin} → {trip.destination} "
        f"({trip.departure_datetime.strftime('%-d %b')}) has been cancelled — "
        f"payment could not be secured. The seat is now available again. "
        f"samefare.com/trips/{trip.id}",
    )


def trip_reminder_to_passenger(booking) -> None:
    """
    Day-before reminder to a confirmed passenger.
    Sent ~20:00 the evening before departure.
    """
    if not booking.passenger.phone:
        return
    trip      = booking.trip
    departure = trip.departure_datetime.strftime("%H:%M")
    driver    = trip.driver.full_name.split()[0]
    pickup    = f" Meet at: {trip.pickup_address}." if trip.pickup_address else ""
    _send(
        booking.passenger.phone,
        f"SameFare reminder: your ride with {driver} is tomorrow — "
        f"{trip.origin} → {trip.destination} at {departure}.{pickup} "
        f"samefare.com/my-trips",
    )
