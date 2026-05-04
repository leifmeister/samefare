"""
Rapyd payment client for SameFare.

Handles:
  - Request signing (HMAC-SHA256 per Rapyd spec)
  - Checkout page creation (Case A: auth; Case B: save-card / SCA CIT)
  - Payment capture
  - Merchant-initiated transactions (MIT) for Case B
  - Refunds
  - Payment lookup
  - Webhook signature verification

Environment variables required (see config.py):
    RAPYD_ACCESS_KEY    your Rapyd access key
    RAPYD_SECRET_KEY    your Rapyd secret key
    RAPYD_SANDBOX       true (default) / false

Reference: https://docs.rapyd.net
"""

import hashlib
import hmac
import json
import logging
import random
import secrets
import string
import time
import urllib.request
import urllib.error
from base64 import b64encode
from typing import Optional

from app.config import get_settings

log = logging.getLogger(__name__)

_SANDBOX_API = "https://sandboxapi.rapyd.net"
_PROD_API    = "https://api.rapyd.net"
_SANDBOX_JS  = "https://sandboxclient.rapyd.net/v1/rapyd.js"
_PROD_JS     = "https://client.rapyd.net/v1/rapyd.js"


class RapydError(Exception):
    """Raised when a Rapyd API call returns an error or cannot be made."""


# ── Internals ─────────────────────────────────────────────────────────────────

def _base_url() -> str:
    return _SANDBOX_API if get_settings().rapyd_sandbox else _PROD_API


def js_url() -> str:
    """Return the correct Rapyd.js CDN URL for the current environment."""
    return _SANDBOX_JS if get_settings().rapyd_sandbox else _PROD_JS


def _make_salt(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=length))


def _sign(method: str, path: str, salt: str, ts: str, body: str) -> str:
    """
    Build and return the base64-encoded HMAC-SHA256 signature for a Rapyd request.

    Canonical string (per Rapyd REST API docs):
        http_method_lower + url_path + salt + timestamp + access_key + secret_key + body_json

    The secret_key is both part of the signed string AND the HMAC key.
    This formula is for outbound API requests only — webhook verification uses a
    different formula (see verify_webhook).
    """
    s = get_settings()
    to_sign = (
        method.lower()
        + path
        + salt
        + ts
        + s.rapyd_access_key
        + s.rapyd_secret_key
        + body
    )
    hexdig = hmac.new(
        s.rapyd_secret_key.encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return b64encode(hexdig.encode("utf-8")).decode("utf-8")


def _headers(
    method: str,
    path:   str,
    body:   str,
    idempotency_key: Optional[str] = None,
) -> dict:
    s    = get_settings()
    salt = _make_salt()
    ts   = str(int(time.time()))
    sig  = _sign(method, path, salt, ts, body)
    headers: dict = {
        "Content-Type": "application/json",
        "access_key":   s.rapyd_access_key,
        "salt":         salt,
        "timestamp":    ts,
        "signature":    sig,
    }
    if idempotency_key is not None:
        # Rapyd deduplicates requests that share the same idempotency header value
        # within a rolling window.  This is the only reliable guard against duplicate
        # payments, captures, and refunds on retry — body fields like idempotency_key
        # or merchant_reference_id are stored for reference only, not enforced.
        headers["idempotency"] = idempotency_key
    return headers


def _request(
    method: str,
    path:   str,
    payload: Optional[dict] = None,
    *,
    idempotency_key: Optional[str] = None,
) -> dict:
    """
    Execute a signed Rapyd API call and return the parsed response body.
    Raises RapydError on HTTP errors or API-level error codes.

    Pass idempotency_key for any POST that must not be duplicated on retry
    (checkout creation, MIT, capture, refund).  Rapyd enforces deduplication
    via the 'idempotency' request header; body-level fields are not enforced.
    """
    body    = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    url     = _base_url() + path
    headers = _headers(method, path, body, idempotency_key)
    data    = body.encode("utf-8") if body else None

    req = urllib.request.Request(
        url, data=data, headers=headers, method=method.upper()
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        log.error("Rapyd %s %s → HTTP %s: %s", method.upper(), path, exc.code, raw)
        try:
            result = json.loads(raw)
        except Exception:
            raise RapydError(f"Rapyd HTTP {exc.code}: {raw}") from exc
    except Exception as exc:
        log.error("Rapyd %s %s failed: %s", method.upper(), path, exc)
        raise RapydError(str(exc)) from exc

    # Rapyd API-level errors
    status = result.get("status", {})
    err    = status.get("error_code", "")
    if err and err not in ("", "SUCCESS"):
        msg = status.get("message", err)
        log.error("Rapyd API error %s %s: %s — %s", method.upper(), path, err, msg)
        raise RapydError(f"{err}: {msg}")

    return result.get("data", result)


# ── Checkout page (embedded Rapyd.js) ─────────────────────────────────────────

def create_checkout_page(
    *,
    amount: int,
    currency: str = "ISK",
    country:  str = "IS",
    capture:  bool,
    complete_url: str,
    cancel_url:   str,
    idempotency_key: str,
    metadata: dict,
    customer_id:          Optional[str] = None,
    save_payment_method:  bool = False,
) -> dict:
    """
    Create a Rapyd hosted checkout page for embedding with Rapyd.js.
    Returns the checkout data dict (data.id = checkout_id to pass to RapydCheckoutToolkit).

    Case A (ride ≤7 days):
        amount = passenger total, capture=False
        Rapyd authorises the card; we capture later at departure.

    Case B (ride >7 days):
        amount = 0, capture=True, save_payment_method=True
        Triggers SCA-authenticated CIT; card is stored for future MIT.
        customer_id must be provided (create_customer first).
    """
    payload: dict = {
        "amount":                         amount,
        "currency":                       currency,
        "country":                        country,
        "embedded":                       True,
        "payment_method_type_categories": ["card"],
        "capture":                        capture,
        "payment_expiration":             86400,       # 24 h
        "complete_payment_url":           complete_url,
        "cancel_payment_url":             cancel_url,
        "language":                       "en",
        "metadata":                       metadata,
        "merchant_reference_id":          idempotency_key,
    }
    if save_payment_method:
        payload["save_payment_method"] = True
    if customer_id:
        payload["customer"] = customer_id

    return _request("post", "/v1/hosted/collect/checkout", payload,
                    idempotency_key=idempotency_key)


# ── Customer management ────────────────────────────────────────────────────────

def create_customer(*, email: str, name: str, idempotency_key: str) -> str:
    """
    Create a Rapyd customer object and return the customer_id.
    Used for Case B so we can attach the saved payment method later.

    Pass a stable idempotency_key (e.g. f"customer-{payment.idempotency_key}")
    so that a retry caused by a DB failure after the API call returns the
    already-created customer rather than opening a second orphaned one.
    """
    data = _request(
        "post", "/v1/customers",
        {"email": email, "name": name},
        idempotency_key=idempotency_key,
    )
    return data["id"]


# ── Merchant-initiated transaction (Case B) ───────────────────────────────────

def create_mit_payment(
    *,
    amount: int,
    currency:          str = "ISK",
    customer_id:       str,
    payment_method_id: str,
    capture:           bool = False,
    idempotency_key:   str,
    metadata:          dict,
) -> dict:
    """
    Create a merchant-initiated card payment using a previously saved payment method.

    Called by the 24-hour-before-departure scheduler for Case B bookings.
    The cardholder is not present, so we set merchant_initiated=True and skip
    3DS (the initial SCA-authenticated CIT already obtained the cardholder's
    consent per PSD2 MIT rules).
    """
    return _request("post", "/v1/payments", {
        "amount":          amount,
        "currency":        currency,
        "customer":        customer_id,
        "payment_method":  payment_method_id,
        "capture":         capture,
        "merchant_initiated": True,
        "payment_method_options": {
            "3d_required": False,
        },
        "metadata":        metadata,
    }, idempotency_key=idempotency_key)


# ── Capture ────────────────────────────────────────────────────────────────────

def capture_payment(payment_id: str, idempotency_key: str) -> dict:
    """
    Capture a previously authorised (ACT) Rapyd payment.
    Idempotent: safe to retry with the same key.

    The capture endpoint takes no meaningful request body; idempotency is
    enforced entirely via the 'idempotency' request header.
    """
    return _request("post", f"/v1/payments/{payment_id}/capture",
                    idempotency_key=idempotency_key)


# ── Refunds ────────────────────────────────────────────────────────────────────

def create_refund(
    *,
    payment_id:      str,
    amount:          int,
    reason:          str = "requested_by_customer",
    idempotency_key: str,
) -> dict:
    """
    Issue a full or partial refund against a Rapyd payment.
    Idempotent: safe to retry with the same key.
    """
    return _request("post", "/v1/refunds", {
        "payment":               payment_id,
        "amount":                amount,
        "reason":                reason,
        # merchant_reference_id is stored on the Rapyd refund object and useful
        # for reconciliation — it is distinct from (and complementary to) the
        # idempotency header that actually guards against duplicate submission.
        "merchant_reference_id": idempotency_key,
    }, idempotency_key=idempotency_key)


# ── Payment lookup ─────────────────────────────────────────────────────────────

def get_payment(payment_id: str) -> dict:
    """Fetch a Rapyd payment object by ID."""
    return _request("get", f"/v1/payments/{payment_id}")


# ── Webhook signature verification ────────────────────────────────────────────

def verify_webhook(
    *,
    url:               str,
    body:              str,
    rapyd_signature:   str,
    rapyd_salt:        str,
    rapyd_timestamp:   str,
) -> bool:
    """
    Verify the HMAC-SHA256 signature on an incoming Rapyd webhook.
    Returns True if the signature is valid, False otherwise.

    Canonical string (per docs.rapyd.net/en/webhook-authentication.html):
        full_webhook_url + salt + timestamp + access_key + secret_key + body_json

    Key differences from outbound request signing:
    - Uses the FULL webhook URL (https://...) as registered in the Rapyd dashboard
    - HTTP method is NOT included
    - secret_key appears both in the canonical string AND as the HMAC key
    - body must be compact JSON with no extra whitespace (Rapyd always sends compact JSON)
    """
    s = get_settings()
    to_verify = (
        url
        + rapyd_salt
        + rapyd_timestamp
        + s.rapyd_access_key
        + s.rapyd_secret_key
        + body
    )
    hexdig = hmac.new(
        s.rapyd_secret_key.encode("utf-8"),
        to_verify.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    expected = b64encode(hexdig.encode("utf-8")).decode("utf-8")
    return hmac.compare_digest(expected, rapyd_signature)


def generate_idempotency_key() -> str:
    """Generate a unique idempotency key for a new payment attempt."""
    return secrets.token_hex(16)
