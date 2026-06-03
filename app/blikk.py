"""
Blikk client — P2P payments and Payment Channel payouts.

━━ P2P API  (https://api.blikk.tech/p2papi) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Used for passenger ↔ platform money flows where both parties are Blikk users.

API flow:
  1. POST /p2p  — debtorPhoneNumber, creditorPhoneNumber, amount, partnerRedirectUrl
  2. GET  /payment/{id} — fetch redirect URL for passenger approval
  3. GET  /payment/{id} on return — check status

P2P flows:
  Flow 1 – Service fee (passenger → platform)   create_fee_payment()
  Flow 2 – Fare (passenger → driver, deferred)  create_fare_payment()
  Flow 3 – Refund fee (platform → passenger)    refund_fee()

━━ Payment Channel API  (https://api.blikk.tech/paymentchannel) ━━━━━━━━━━━━━
Used for platform → driver payouts (Account-to-Account).
Platform's bank account is pre-configured in the Blikk payment channel.
Driver identified by kennitala + IBAN.

API flow:
  1. POST /payment — creditor.ssn, creditor.name, creditor.iban, amount, currency
  2. GET  /payment/{id} — poll until SUCCESS / CANCELLED / REJECTED / ERROR

Channel flows:
  Flow 4 – Driver payout (platform → driver)    create_channel_payout()
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_BASE          = "https://api.blikk.tech/p2papi"
_CHANNEL_BASE  = "https://api.blikk.tech/paymentchannel"

# Terminal statuses for the Payment Channel API
_CHANNEL_TERMINAL = {"SUCCESS", "CANCELLED", "REJECTED", "ERROR"}


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


def _channel_client() -> httpx.Client:
    """HTTP client for the Payment Channel API."""
    settings = get_settings()
    return httpx.Client(
        base_url=_CHANNEL_BASE,
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
    redirect_url: str,
) -> dict:
    """
    POST /p2p — create a P2P payment.

    Required fields per API spec: debtorPhoneNumber, creditorPhoneNumber,
    amount (integer ISK), partnerRedirectUrl.

    Returns {id: str}.
    """
    with _client() as c:
        resp = c.post("/p2p", json={
            "debtorPhoneNumber":   debtor_phone,
            "creditorPhoneNumber": creditor_phone,
            "amount":              amount,
            "partnerRedirectUrl":  redirect_url,
        })
    _raise_for_error(resp)
    return resp.json()


def get_payment(payment_id: str) -> dict:
    """
    GET /payment/{id} — fetch full payment object.

    Response includes: id, status, redirectUri, scaRedirectUrl,
    partnerRedirectUrl, callbackUrl, and payment details.
    """
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


def blikk_redirect_url(payment: dict) -> str | None:
    """
    Extract the redirect URL from a GET /payment/{id} response.

    Tries the documented field names in priority order.
    Returns None if no URL is present (payment may not require SCA).
    """
    for key in ("redirectUri", "scaRedirectUrl", "redirectUrl", "url", "deepLink"):
        val = payment.get(key)
        if val:
            return val
    return None


def create_fee_payment(booking, base_url: str) -> tuple[str, str | None]:
    """
    Create a service-fee P2P payment (passenger → Samefare platform).

    Flow:
      1. POST /p2p  → get payment id  (raises BlikkError on failure)
      2. GET  /payment/{id} → get redirectUri for passenger

    Returns (blikk_payment_id, redirect_url).
    redirect_url may be None if:
      - Blikk returns no URL (payment requires no SCA redirect), or
      - GET /payment/{id} times out after the P2P was already created.

    IMPORTANT: the caller must persist blikk_payment_id to the DB
    immediately after this returns, before using the redirect URL.
    If get_payment() fails after POST /p2p succeeded, the payment_id
    is still returned so it can be recorded and recovered later.
    Raises BlikkError only if POST /p2p itself fails (no payment created).
    """
    settings = get_settings()
    passenger_phone = booking.passenger.phone
    if not passenger_phone:
        raise BlikkError("Passenger has no phone number on file.")

    return_url = f"{base_url.rstrip('/')}/bookings/{booking.id}/blikk-return"

    p2p = create_p2p(
        debtor_phone   = passenger_phone,
        creditor_phone = settings.blikk_platform_phone,
        amount         = booking.service_fee,
        redirect_url   = return_url,
    )
    payment_id = p2p["id"]

    # GET /payment/{id} fetches the redirect URL — if it fails the P2P already
    # exists on Blikk's side. Return the payment_id with redirect=None so the
    # caller can save it to the DB. The blikk-pay endpoint will fetch the URL
    # on the passenger's next visit via the idempotency check.
    try:
        payment  = get_payment(payment_id)
        redirect = blikk_redirect_url(payment)
    except BlikkError:
        redirect = None

    return payment_id, redirect


def create_fare_payment(booking, driver_phone: str, base_url: str) -> tuple[str, str | None]:
    """
    Create a driver-fare P2P payment (passenger → driver, triggered at ride time).

    Returns (blikk_payment_id, redirect_url).
    """
    passenger_phone = booking.passenger.phone
    if not passenger_phone:
        raise BlikkError("Passenger has no phone number on file.")
    if not driver_phone:
        raise BlikkError("Driver has no phone number on file.")

    return_url = f"{base_url.rstrip('/')}/bookings/{booking.id}/blikk-return?flow=fare"

    p2p = create_p2p(
        debtor_phone   = passenger_phone,
        creditor_phone = driver_phone,
        amount         = booking.subtotal,
        redirect_url   = return_url,
    )
    payment_id = p2p["id"]

    try:
        payment  = get_payment(payment_id)
        redirect = blikk_redirect_url(payment)
    except BlikkError:
        redirect = None

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

    return_url = f"{get_settings().base_url.rstrip('/')}/bookings/{booking.id}/blikk-return?flow=refund"

    p2p = create_p2p(
        debtor_phone   = settings.blikk_platform_phone,
        creditor_phone = passenger_phone,
        amount         = booking.service_fee,
        redirect_url   = return_url,
    )
    return p2p["id"]


# ── Payment Channel — driver payouts (platform → driver) ─────────────────────

def create_channel_payout(
    amount:          int,
    creditor_ssn:    str,
    creditor_name:   str,
    creditor_iban:   str,
    sca_user_ssn:    str,
    reference:       str | None = None,
) -> dict:
    """
    POST /payment on the Payment Channel API.
    Initiates an A2A transfer from the platform's pre-configured bank account
    to the driver's Icelandic IBAN.

    Args:
        amount:        Transfer amount in ISK (integer).
        creditor_ssn:  Driver's kennitala (10 digits).
        creditor_name: Driver's full name.
        creditor_iban: Driver's Icelandic IBAN (IS + 24 chars).
        sca_user_ssn:  Kennitala of the entity authorising the payment.
                       Confirm with Blikk whether this is SameFare's company
                       kennitala or the driver's kennitala.
        reference:     Optional free-text reference (e.g. "Booking #42").

    Returns:
        Full payment object dict with at least {"id": ..., "status": ...}.
    Raises:
        BlikkError on API or validation errors.
    """
    payload: dict = {
        "amount":     amount,
        "currency":   "ISK",
        "scaUserSsn": sca_user_ssn,
        "creditor": {
            "ssn":  creditor_ssn,
            "name": creditor_name,
            "iban": creditor_iban.upper().replace(" ", ""),
        },
    }
    if reference:
        payload["reference"] = reference

    with _channel_client() as c:
        resp = c.post("/payment", json=payload)
    if resp.status_code not in (200, 201):
        body = resp.text[:300]
        log.error("Blikk channel payout failed %s: %s", resp.status_code, body)
        raise BlikkError(
            f"Channel payout failed ({resp.status_code})",
            status_code=resp.status_code,
            body=body,
        )
    data = resp.json()
    log.info(
        "Blikk channel payout created: id=%s status=%s amount=%s ISK → %s",
        data.get("id"), data.get("status"), amount, creditor_name,
    )
    return data


def get_channel_payment(payment_id: str) -> dict:
    """
    GET /payment/{paymentId} — poll until terminal status.
    Returns the full payment object.
    Raises BlikkError if the payment is not found or the call fails.
    """
    with _channel_client() as c:
        resp = c.get(f"/payment/{payment_id}")
    if resp.status_code == 404:
        raise BlikkError(f"Channel payment {payment_id} not found", status_code=404)
    if resp.status_code != 200:
        raise BlikkError(
            f"get_channel_payment failed ({resp.status_code})",
            status_code=resp.status_code,
        )
    return resp.json()


def channel_payment_is_terminal(payment: dict) -> bool:
    """Return True if the payment has reached a terminal state."""
    return payment.get("status", "") in _CHANNEL_TERMINAL
