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


# ── Phone normalisation ───────────────────────────────────────────────────────

# Default country for bare, country-code-less numbers. This is an Iceland-first
# product and every legacy/seed row missing a code is Icelandic.
_DEFAULT_DIAL_CODE = "+354"


def normalize_phone(raw: str | None) -> str | None:
    """
    Coerce a stored phone number to E.164 (e.g. '+3546184321').

    Twilio rejects anything that isn't strict E.164 with 21211 'Invalid To'.
    Stored values are messy — some have a space ('+354 6184321'), some are
    bare with no country code ('6184321'). Both must end up as '+3546184321'.

    - strips spaces and hyphens (dial codes like '+1-809' become '+1809')
    - '00' international prefix → '+'
    - no leading '+' → assume the default country code
    """
    if not raw:
        return raw
    p = raw.strip().replace(" ", "").replace("-", "")
    if not p:
        return None
    if p.startswith("00"):
        p = "+" + p[2:]
    if not p.startswith("+"):
        p = _DEFAULT_DIAL_CODE + p
    return p


# ── Error classification ───────────────────────────────────────────────────────

# Map Twilio error codes (https://www.twilio.com/docs/api/errors) to stable,
# user-safe tokens. Callers translate the token for display; the raw Twilio body
# is only ever logged. Anything unmapped falls through to "generic".
_SMS_ERROR_CODES = {
    21408: "region_unsupported",  # messaging not enabled for the destination region
    21211: "invalid_number",      # 'To' is not a valid phone number
    21214: "invalid_number",      # 'To' failed validation
    21614: "invalid_number",      # 'To' is not a valid mobile number
    21612: "region_unsupported",  # can't route to this number from the From number
    21610: "optout",              # recipient has unsubscribed
}


def _classify_sms_error(body_err: str) -> str:
    """Reduce a raw Twilio error body to a stable, user-safe token."""
    try:
        code = int(json.loads(body_err).get("code"))
    except Exception:
        return "generic"
    return _SMS_ERROR_CODES.get(code, "generic")


# ── Low-level sender ──────────────────────────────────────────────────────────

def _send(to: str, body: str) -> tuple[bool, str]:
    """Send a single SMS via Twilio REST API.

    Returns (success, error_token). On failure the second element is a stable,
    user-safe token (see _SMS_ERROR_CODES) — never raw Twilio output.
    """
    s = get_settings()
    if not s.twilio_account_sid or not s.twilio_auth_token or not s.twilio_from_number:
        log.debug("Twilio not configured — skipping SMS to %s", to)
        return False, "unconfigured"
    if not to:
        log.debug("No phone number — skipping SMS")
        return False, "no_number"

    # Defensive: never hand Twilio a non-E.164 number — a country-code-less
    # value (e.g. '6184321') triggers a 21211 rejection.
    to = normalize_phone(to)

    # Use alphanumeric sender ID only for countries registered in Twilio.
    # Falls back to the phone number for all others so messages are never
    # silently dropped by an unregistered alphanumeric sender.
    # Add country prefixes here as you register more countries in Twilio.
    _alpha = s.twilio_sender_id.strip()
    _alpha_prefixes = ("+354",)   # Iceland — registered in Twilio
    sender = (
        _alpha
        if _alpha and to.startswith(_alpha_prefixes)
        else s.twilio_from_number
    )

    credentials = b64encode(
        f"{s.twilio_account_sid}:{s.twilio_auth_token}".encode()
    ).decode()

    payload = urllib.parse.urlencode({
        "To":   to,
        "From": sender,
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
            return True, ""
    except urllib.error.HTTPError as exc:
        body_err = exc.read().decode("utf-8", errors="replace")
        log.warning("SMS failed → %s: %s %s", to, exc.code, body_err)
        # Classify the Twilio error into a stable, user-safe token. The raw
        # JSON (carrier names, error URLs, the obfuscated 'To' number) is logged
        # above but must never reach the browser — see _classify_sms_error.
        return False, _classify_sms_error(body_err)
    except Exception as exc:
        log.warning("SMS failed → %s: %s", to, exc)
        return False, "generic"


# ── Public API ────────────────────────────────────────────────────────────────

def send_otp(phone: str, code: str) -> tuple[bool, str]:
    """Send a 6-digit OTP for phone number verification. Returns (success, error)."""
    return _send(phone, f"Your SameFare verification code is: {code}\n\nExpires in 10 minutes.")


def admin_alert(message: str) -> None:
    """
    Fire-and-forget critical operational alert to the operator phone
    (ADMIN_ALERT_PHONE). No-op when the number isn't configured.
    """
    phone = get_settings().admin_alert_phone
    if not phone:
        log.debug("admin_alert skipped — ADMIN_ALERT_PHONE not set: %s", message)
        return
    ok, err = _send(phone, message)
    if not ok:
        log.error("admin_alert SMS failed: %s — message was: %s", err, message)


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
    Passenger has 2 hours to update their payment card. No surcharge.
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
