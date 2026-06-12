"""
Blikk Payment Channel client.

Base URL  : https://api.blikk.tech/paymentchannel
Auth      : Api-Key header (BLIKK_CHANNEL_API_KEY env var)
            — separate key from P2P; tied to SameFare's bank account in Blikk.
Currency  : ISK (integer, full ISK — no subunit)

API flow
--------
1. POST /payment  → {id, status: "DRAFT"|"PENDING"|...}
2. GET  /payment/{id} → poll until terminal status

Status lifecycle
----------------
DRAFT       Created but not yet initialised
PENDING     Initialised, processing
SCA_REQUIRED  User must complete SCA at redirectUrl
SUCCESS     Completed
CANCELLED   Cancelled before completion
REJECTED    Rejected by bank/provider
ERROR       Processing error

Terminal statuses: SUCCESS, CANCELLED, REJECTED, ERROR

Usage
-----
Used exclusively for platform → driver payouts after trip completion.
Driver's kennitala + IBAN must be on file (set in profile → Payout details).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_CHANNEL_BASE     = "https://api.blikk.tech/paymentchannel"
_TERMINAL_STATUSES = frozenset({"SUCCESS", "CANCELLED", "REJECTED", "ERROR"})


class BlikkError(Exception):
    """Raised when the Blikk API returns an error or is misconfigured."""
    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _client() -> httpx.Client:
    s = get_settings()
    if not s.blikk_channel_api_key:
        raise BlikkError("BLIKK_CHANNEL_API_KEY is not set — required for driver payouts.")
    return httpx.Client(
        base_url=s.blikk_channel_base_url or _CHANNEL_BASE,
        headers={"Api-Key": s.blikk_channel_api_key, "Content-Type": "application/json"},
        timeout=15.0,
    )


def create_channel_payout(
    amount:        int,
    creditor_ssn:  str,
    creditor_name: str,
    creditor_iban: str,
    sca_user_ssn:  str,
    reference:     str | None = None,
) -> dict:
    """
    POST /payment — initiate an A2A transfer from SameFare's bank account to
    the driver's Icelandic IBAN.

    Args:
        amount:        ISK amount (integer).
        creditor_ssn:  Driver's kennitala (10 digits).
        creditor_name: Driver's full name.
        creditor_iban: Driver's Icelandic IBAN (IS + 24 chars).
        sca_user_ssn:  Kennitala authorising the payment. Use BLIKK_COMPANY_KENNITALA
                       (SameFare's company kennitala) once confirmed with Blikk.
        reference:     Optional free-text reference shown on bank statement.

    Returns:
        Full payment dict including at least {"id": ..., "status": ...}.
    Raises:
        BlikkError on API errors or missing config.
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
    # NB: the Payment Channel POST /payment body is strict (additionalProperties:
    # false) and has no reference/description field, so we must NOT send one —
    # including it gets the whole request rejected. `reference` is accepted as a
    # param for call-site compatibility but is intentionally not transmitted.
    _ = reference

    with _client() as c:
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
    GET /payment/{paymentId} — fetch current status of a payout.
    Poll this until channel_payment_is_terminal() returns True.

    Raises BlikkError if not found or the call fails.
    """
    with _client() as c:
        resp = c.get(f"/payment/{payment_id}")

    if resp.status_code == 404:
        raise BlikkError(f"Channel payment {payment_id} not found.", status_code=404)
    if resp.status_code != 200:
        raise BlikkError(
            f"get_channel_payment failed ({resp.status_code})",
            status_code=resp.status_code,
        )
    return resp.json()


def channel_payment_is_terminal(payment: dict) -> bool:
    """Return True if the payment has reached a terminal state."""
    return payment.get("status", "") in _TERMINAL_STATUSES


def channel_payment_succeeded(payment: dict) -> bool:
    """Return True if the payment completed successfully."""
    return payment.get("status") == "SUCCESS"
