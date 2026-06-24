"""
Didit KYC/AML client.

API docs: https://docs.didit.me
Base URL: https://verification.didit.me/v3/

Usage:
  session = didit.create_session(
      api_key=settings.didit_api_key,
      workflow_id=settings.didit_workflow_id_licence,
      user_id=current_user.id,
      verification_type="licence",
      callback_url=f"{settings.base_url}/verify/didit/callback",
  )
  redirect to session["url"]

Webhook at POST /webhooks/didit validates X-Signature-V2 using verify_webhook_signature().
"""

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Literal

log = logging.getLogger(__name__)

BASE_URL = "https://verification.didit.me/v3"

VerificationType = Literal["identity", "licence"]

# Didit session statuses we act on
STATUS_APPROVED  = "Approved"
STATUS_DECLINED  = "Declined"
STATUS_IN_REVIEW = "In Review"
STATUS_ABANDONED = "Abandoned"
STATUS_EXPIRED   = "Expired"


def create_session(
    *,
    api_key: str,
    workflow_id: str,
    user_id: int,
    verification_type: VerificationType,
    callback_url: str,
    language: str = "en",
) -> dict:
    """
    Create a Didit verification session.

    Returns the full session object; the ``url`` field is where
    the user should be redirected to complete verification.

    Raises urllib.error.HTTPError on API errors (400, 403, 429…).
    """
    payload = json.dumps({
        "workflow_id": workflow_id,
        "vendor_data": f"{user_id}:{verification_type}",
        "callback":    callback_url,
        "language":    language,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{BASE_URL}/session/",
        data=payload,
        headers={
            "x-api-key":    api_key,
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "User-Agent":   "SameFare/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def retrieve_decision(*, api_key: str, session_id: str) -> dict:
    """
    Fetch the full decision object for a session (read-only).

    Used as an authoritative fallback when a webhook payload doesn't carry the
    verified document details — so we can confirm what document was actually
    checked (e.g. driver's licence vs passport).
    """
    req = urllib.request.Request(
        f"{BASE_URL}/session/{session_id}/decision/",
        headers={
            "x-api-key":  api_key,
            "Accept":     "application/json",
            "User-Agent": "SameFare/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def primary_document(decision: dict) -> dict:
    """The first verified ID document in a Didit decision (webhook or API shape)."""
    checks = (decision or {}).get("id_verifications") or []
    return (checks[0] or {}) if checks else {}


def is_drivers_license(doc: dict) -> bool:
    """
    True when Didit actually verified a driver's licence (not a passport / national
    ID / etc.). Real values seen: document_type "Driver's License",
    document_subtype "DRIVER_LICENSE_GENERIC".
    """
    subtype = (doc.get("document_subtype") or "").upper()
    dtype   = (doc.get("document_type")    or "").upper()
    return subtype.startswith("DRIVER") or "DRIVER" in dtype or "DRIVING" in dtype


def verify_webhook_signature(
    *,
    payload_bytes: bytes,
    signature: str,
    secret: str,
    max_age_seconds: int = 300,
) -> dict:
    """
    Verify a Didit webhook using X-Signature-V2 (recommended).

    Algorithm: HMAC-SHA256 over the JSON payload with keys sorted alphabetically
    and Unicode characters preserved (not escaped to \\uXXXX).

    Raises ValueError if:
      - the HMAC signature does not match
      - the timestamp is older than max_age_seconds or more than 30 s in the future

    Returns the parsed payload dict on success.
    """
    payload = json.loads(payload_bytes.decode("utf-8"))

    # Timestamp freshness check
    timestamp = payload.get("timestamp") or payload.get("created_at")
    if timestamp is not None:
        age = int(time.time()) - int(timestamp)
        if age > max_age_seconds or age < -30:
            raise ValueError(
                f"Webhook timestamp out of acceptable range (age={age}s)"
            )

    # Didit's X-Signature-V2 is HMAC-SHA256 over the JSON payload RE-SERIALISED
    # with keys sorted alphabetically, compact separators, and Unicode preserved
    # — NOT the raw request body. (Confirmed in prod via diagnostic: raw=False,
    # sorted=True.) .strip() guards a trailing newline in the Railway-pasted secret.
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    expected = hmac.new(
        secret.strip().encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature.strip()):
        raise ValueError("Webhook signature mismatch")

    return payload
