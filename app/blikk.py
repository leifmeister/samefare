"""
Blikk P2P bank-transfer client.

Base URL  : https://api.blikk.tech/p2papi
Auth      : Api-Key header  (set via BLIKK_API_KEY env var)
Currency  : ISK (all amounts are integers, in full ISK)

Three flows
-----------
Flow 1 – Service fee (passenger → Samefare platform)
    Call create_fee_payment() at booking time; redirect passenger to the
    returned `redirect_url`; verify on return with get_payment().

Flow 2 – Fare (passenger → driver, at ride time)
    Driver clicks "Request payment"; call create_fare_payment().
    Passenger receives a push notification in their Blikk app.

Flow 3 – Refund fee (Samefare platform → passenger)
    Call refund_fee() when the driver rejects a booking or cancels after
    the fee has been collected.  Uses the platform phone as the debtor.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_BASE = "https://api.blikk.tech/p2papi"


class BlikkError(Exception):
    """Raised when the Blikk API returns an error response."""
    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        base_url=_BASE,
        headers={"Api-Key": settings.blikk_api_key, "Content-Type": "application/json"},
        timeout=15.0,
    )


def _raise_for_error(resp: httpx.Response) -> None:
    if resp.is_error:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise BlikkError(
            f"Blikk API error {resp.status_code}: {body}",
            status_code=resp.status_code,
            body=body,
        )


# ── Low-level API calls ────────────────────────────────────────────────────────

def create_p2p(
    debtor_phone: str,
    creditor_phone: str,
    amount: int,
    reference: str,
    description: str = "SameFare payment",
) -> dict:
    """
    POST /p2p — create a P2P payment object.

    Returns the full Blikk payment dict (id, status, …).
    """
    with _client() as c:
        resp = c.post("/p2p", json={
            "debtor":      {"phone": debtor_phone},
            "creditor":    {"phone": creditor_phone},
            "amount":      amount,
            "currency":    "ISK",
            "reference":   reference,
            "description": description,
        })
    _raise_for_error(resp)
    return resp.json()


def init_payment(payment_id: str, redirect_url: str) -> dict:
    """
    POST /payment/init/{id} — initialize a P2P payment.

    Blikk returns a dict that includes the URL to send the passenger to
    (the key may be 'redirectUrl', 'url', or similar — adapt as needed).
    """
    with _client() as c:
        resp = c.post(f"/payment/init/{payment_id}", json={
            "partnerRedirectUrl": redirect_url,
        })
    _raise_for_error(resp)
    return resp.json()


def get_payment(payment_id: str) -> dict:
    """GET /payment/{id} — fetch current payment status."""
    with _client() as c:
        resp = c.get(f"/payment/{payment_id}")
    _raise_for_error(resp)
    return resp.json()


def get_user(phone: str) -> dict | None:
    """
    GET /user/{phoneNumber} — verify that a phone is registered with Blikk.

    Returns the user dict if found, None if the phone is not registered.
    """
    with _client() as c:
        resp = c.get(f"/user/{phone}")
    if resp.status_code == 404:
        return None
    _raise_for_error(resp)
    return resp.json()


# ── High-level helpers ─────────────────────────────────────────────────────────

def is_completed(payment: dict) -> bool:
    """Return True if the Blikk payment status indicates success."""
    status = (payment.get("status") or "").upper()
    return status in {"COMPLETED", "PAID", "SUCCESS", "APPROVED"}


def is_failed(payment: dict) -> bool:
    """Return True if the Blikk payment status indicates a terminal failure."""
    status = (payment.get("status") or "").upper()
    return status in {"FAILED", "REJECTED", "EXPIRED", "CANCELLED"}


def blikk_redirect_url(payment_init_response: dict) -> str | None:
    """
    Extract the redirect URL from a /payment/init response.

    Blikk may use different key names across API versions; we try the most
    common ones and fall back to None so callers can handle the missing URL.
    """
    for key in ("redirectUrl", "url", "paymentUrl", "deepLink", "link"):
        val = payment_init_response.get(key)
        if val:
            return val
    return None


def create_fee_payment(booking, base_url: str) -> tuple[str, str | None]:
    """
    Create and initialize a service-fee P2P payment.

    debtor   = passenger's phone number
    creditor = Samefare platform phone (+3546257175)
    amount   = booking.service_fee (ISK)

    Returns (blikk_payment_id, redirect_url).
    redirect_url may be None if Blikk returns no URL in the init response;
    callers should fall back to a "payment pending" page in that case.

    Raises BlikkError on API failure.
    """
    settings = get_settings()
    passenger_phone = booking.passenger.phone
    if not passenger_phone:
        raise BlikkError("Passenger has no phone number on file.")

    reference   = f"fee-booking-{booking.id}"
    description = (
        f"SameFare service fee — "
        f"{booking.pickup_city or booking.trip.origin} → "
        f"{booking.dropoff_city or booking.trip.destination}"
    )

    p2p = create_p2p(
        debtor_phone   = passenger_phone,
        creditor_phone = settings.blikk_platform_phone,
        amount         = booking.service_fee,
        reference      = reference,
        description    = description,
    )
    payment_id = p2p["id"]

    return_url = f"{base_url.rstrip('/')}/bookings/{booking.id}/blikk-return"
    init_resp  = init_payment(payment_id, return_url)
    redirect   = blikk_redirect_url(init_resp)

    return payment_id, redirect


def create_fare_payment(booking, driver_phone: str, base_url: str) -> tuple[str, str | None]:
    """
    Create and initialize a driver-fare P2P payment (triggered at ride time).

    debtor   = passenger's phone number
    creditor = driver's phone number
    amount   = booking.subtotal (driver's cut, ISK)

    Returns (blikk_payment_id, redirect_url).
    """
    passenger_phone = booking.passenger.phone
    if not passenger_phone:
        raise BlikkError("Passenger has no phone number on file.")
    if not driver_phone:
        raise BlikkError("Driver has no phone number on file.")

    reference   = f"fare-booking-{booking.id}"
    description = (
        f"SameFare fare — "
        f"{booking.pickup_city or booking.trip.origin} → "
        f"{booking.dropoff_city or booking.trip.destination}"
    )

    p2p = create_p2p(
        debtor_phone   = passenger_phone,
        creditor_phone = driver_phone,
        amount         = booking.subtotal,
        reference      = reference,
        description    = description,
    )
    payment_id = p2p["id"]

    return_url = f"{base_url.rstrip('/')}/bookings/{booking.id}/blikk-return?flow=fare"
    init_resp  = init_payment(payment_id, return_url)
    redirect   = blikk_redirect_url(init_resp)

    return payment_id, redirect


def refund_fee(booking) -> str:
    """
    Issue a service-fee refund: platform phone → passenger phone.

    Returns the Blikk payment ID of the refund transfer.
    Raises BlikkError on API failure.
    """
    settings = get_settings()
    passenger_phone = booking.passenger.phone
    if not passenger_phone:
        raise BlikkError("Passenger has no phone number on file — cannot refund automatically.")

    reference   = f"refund-booking-{booking.id}"
    description = (
        f"SameFare service fee refund — "
        f"{booking.pickup_city or booking.trip.origin} → "
        f"{booking.dropoff_city or booking.trip.destination}"
    )

    p2p = create_p2p(
        debtor_phone   = settings.blikk_platform_phone,
        creditor_phone = passenger_phone,
        amount         = booking.service_fee,
        reference      = reference,
        description    = description,
    )
    return p2p["id"]
