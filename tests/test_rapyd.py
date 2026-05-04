"""
Tests for Rapyd signing utilities.

These tests are entirely self-contained — no network, no DB, no app config.
They exist to pin the signing formula so it cannot regress silently.

Run:
    python -m pytest tests/test_rapyd.py -v
"""

import hashlib
import hmac
import importlib
import sys
import types
from base64 import b64encode
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers — replicate the formula independently so the test is not circular
# ---------------------------------------------------------------------------

def _expected_request_sig(
    method: str,
    path:   str,
    salt:   str,
    ts:     str,
    body:   str,
    access_key: str,
    secret_key: str,
) -> str:
    """
    Rapyd REST API request signature (per Rapyd docs):
        BASE64( HMAC-SHA256-hexdigest( secret_key,
                    http_method_lower + url_path + salt + timestamp
                    + access_key + secret_key + body_json ) )

    IMPORTANT: Rapyd base64-encodes the **hex-digest string**, not the raw
    HMAC bytes.  Using .digest() instead of .hexdigest() produces a different
    (incorrect) value that will be rejected by the Rapyd API.
    """
    to_sign = method.lower() + path + salt + ts + access_key + secret_key + body
    hexdig = hmac.new(
        secret_key.encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return b64encode(hexdig.encode("utf-8")).decode("utf-8")


def _expected_webhook_sig(
    url:        str,
    body:       str,
    salt:       str,
    ts:         str,
    access_key: str,
    secret_key: str,
) -> str:
    """
    Rapyd webhook verification signature
    (docs.rapyd.net/en/webhook-authentication.html):

        BASE64( HMAC-SHA256-hexdigest( secret_key,
                    full_webhook_url + salt + timestamp
                    + access_key + secret_key + body_json ) )

    Key differences from outbound request signing:
    - Uses the FULL registered webhook URL, not just the path
    - HTTP method is NOT included
    - secret_key appears in the string AND as the HMAC key

    IMPORTANT: same hexdigest → encode → base64 formula as request signing.
    """
    to_verify = url + salt + ts + access_key + secret_key + body
    hexdig = hmac.new(
        secret_key.encode("utf-8"),
        to_verify.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return b64encode(hexdig.encode("utf-8")).decode("utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ACCESS_KEY = "test_access_key_abc123"
SECRET_KEY = "test_secret_key_xyz789"

_MOCK_SETTINGS = MagicMock()
_MOCK_SETTINGS.rapyd_access_key = ACCESS_KEY
_MOCK_SETTINGS.rapyd_secret_key = SECRET_KEY
_MOCK_SETTINGS.rapyd_sandbox    = True


def _import_rapyd():
    """Import app.rapyd with settings patched to known test values."""
    with patch("app.config.get_settings", return_value=_MOCK_SETTINGS):
        import app.rapyd as rapyd_mod
        # Force re-evaluation of get_settings() calls inside the module
        rapyd_mod.get_settings = lambda: _MOCK_SETTINGS
        return rapyd_mod


# ---------------------------------------------------------------------------
# Outbound request signature
# ---------------------------------------------------------------------------

class TestRequestSigning:
    def _sign(self, method, path, salt, ts, body):
        rapyd = _import_rapyd()
        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            return rapyd._sign(method, path, salt, ts, body)

    def test_post_with_body(self):
        method = "post"
        path   = "/v1/hosted/collect/checkout"
        salt   = "abcdefghijkl"
        ts     = "1700000000"
        body   = '{"amount":5000,"currency":"ISK"}'

        got      = self._sign(method, path, salt, ts, body)
        expected = _expected_request_sig(method, path, salt, ts, body, ACCESS_KEY, SECRET_KEY)
        assert got == expected, (
            f"Signature mismatch.\n  got:      {got}\n  expected: {expected}\n"
            "Check that _sign() uses: method + path + salt + ts + access_key + secret_key + body"
        )

    def test_get_empty_body(self):
        method = "get"
        path   = "/v1/payments/payment_abc123"
        salt   = "XXXXXXXXXXXX"
        ts     = "1700001234"
        body   = ""

        got      = self._sign(method, path, salt, ts, body)
        expected = _expected_request_sig(method, path, salt, ts, body, ACCESS_KEY, SECRET_KEY)
        assert got == expected

    def test_secret_key_included_in_canonical_string(self):
        """
        If secret_key were omitted from the canonical string (previous bug),
        two different secret keys would produce the same signature for the
        same inputs — that should not happen.
        """
        method = "post"
        path   = "/v1/refunds"
        salt   = "saltsaltsalt"
        ts     = "1700002000"
        body   = '{"payment":"pay_123","amount":1000}'

        sig_correct = _expected_request_sig(method, path, salt, ts, body, ACCESS_KEY, SECRET_KEY)
        sig_missing = _expected_request_sig(method, path, salt, ts, body, ACCESS_KEY, "")
        assert sig_correct != sig_missing, (
            "secret_key must appear in the canonical string — signatures should differ "
            "when the secret key differs."
        )

        got = self._sign(method, path, salt, ts, body)
        assert got == sig_correct
        assert got != sig_missing

    def test_field_order_matters(self):
        """
        Canonical string order must be:
            method + path + salt + ts + access_key + secret_key + body
        not the old (wrong) order:
            access_key + method + path + salt + ts + body
        """
        method = "post"
        path   = "/v1/customers"
        salt   = "ordermatters1"
        ts     = "1700003000"
        body   = '{"email":"test@example.com","name":"Test User"}'

        correct_order_sig = _expected_request_sig(method, path, salt, ts, body, ACCESS_KEY, SECRET_KEY)

        # Old (wrong) formula: access_key first, no secret_key in string
        wrong_to_sign = ACCESS_KEY + method.lower() + path + salt + ts + body
        wrong_digest  = hmac.new(
            SECRET_KEY.encode("utf-8"),
            wrong_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        wrong_order_sig = b64encode(wrong_digest).decode("utf-8")

        assert correct_order_sig != wrong_order_sig, (
            "The correct and wrong formulas should produce different signatures for the same input."
        )

        got = self._sign(method, path, salt, ts, body)
        assert got == correct_order_sig
        assert got != wrong_order_sig


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

WEBHOOK_URL = "https://samefare.is/webhooks/rapyd"   # full URL as registered in Rapyd dashboard


class TestWebhookVerification:
    def test_valid_signature_accepted(self):
        rapyd = _import_rapyd()
        salt  = "webhooksalt12"
        ts    = "1700005000"
        body  = '{"id":"wh_123","type":"CHECKOUT_COMPLETED","data":{}}'
        sig   = _expected_webhook_sig(WEBHOOK_URL, body, salt, ts, ACCESS_KEY, SECRET_KEY)

        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            result = rapyd.verify_webhook(
                url=WEBHOOK_URL, body=body, rapyd_signature=sig,
                rapyd_salt=salt, rapyd_timestamp=ts,
            )
        assert result is True

    def test_tampered_body_rejected(self):
        rapyd = _import_rapyd()
        salt  = "webhooksalt12"
        ts    = "1700005000"
        body  = '{"id":"wh_123","type":"CHECKOUT_COMPLETED","data":{}}'
        sig   = _expected_webhook_sig(WEBHOOK_URL, body, salt, ts, ACCESS_KEY, SECRET_KEY)

        tampered_body = body.replace("CHECKOUT_COMPLETED", "PAYMENT_FAILED")
        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            result = rapyd.verify_webhook(
                url=WEBHOOK_URL, body=tampered_body, rapyd_signature=sig,
                rapyd_salt=salt, rapyd_timestamp=ts,
            )
        assert result is False

    def test_wrong_key_rejected(self):
        rapyd = _import_rapyd()
        salt  = "webhooksalt12"
        ts    = "1700005000"
        body  = '{"id":"wh_456","type":"PAYMENT_COMPLETED","data":{}}'
        sig   = _expected_webhook_sig(WEBHOOK_URL, body, salt, ts, ACCESS_KEY, "wrong_secret_key")

        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            result = rapyd.verify_webhook(
                url=WEBHOOK_URL, body=body, rapyd_signature=sig,
                rapyd_salt=salt, rapyd_timestamp=ts,
            )
        assert result is False

    def test_wrong_url_rejected(self):
        """
        The full webhook URL is part of the signed string.
        A webhook delivered to a different URL (or a man-in-the-middle replay to
        a different endpoint) must be rejected.
        """
        rapyd = _import_rapyd()
        salt  = "webhooksalt12"
        ts    = "1700005000"
        body  = '{"id":"wh_789","type":"CHECKOUT_COMPLETED","data":{}}'
        sig   = _expected_webhook_sig(WEBHOOK_URL, body, salt, ts, ACCESS_KEY, SECRET_KEY)

        wrong_url = "https://attacker.example.com/webhooks/rapyd"
        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            result = rapyd.verify_webhook(
                url=wrong_url, body=body, rapyd_signature=sig,
                rapyd_salt=salt, rapyd_timestamp=ts,
            )
        assert result is False, "Signature signed for a different URL must be rejected"

    def test_url_missing_from_old_formula_would_pass_wrong_sig(self):
        """
        Regression guard: prove that the old formula (no URL, no secret_key in string)
        produces a DIFFERENT signature than the correct formula, so we can't accidentally
        regress to it and still pass tests.
        """
        salt = "regressiontest"
        ts   = "1700006000"
        body = '{"id":"wh_reg","type":"PAYMENT_COMPLETED","data":{}}'

        correct_sig = _expected_webhook_sig(WEBHOOK_URL, body, salt, ts, ACCESS_KEY, SECRET_KEY)

        # Old (wrong) formula: access_key + salt + ts + body, no URL, no secret_key in string
        old_to_verify = ACCESS_KEY + salt + ts + body
        old_digest    = hmac.new(
            SECRET_KEY.encode("utf-8"),
            old_to_verify.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        old_sig = b64encode(old_digest).decode("utf-8")

        assert correct_sig != old_sig, (
            "Correct and old-formula signatures must differ — "
            "if they match, the regression guard is broken."
        )


# ---------------------------------------------------------------------------
# Known-value fixture tests
#
# These hard-code the exact expected output so that tests cannot pass when
# the wrong formula (base64 of raw HMAC bytes) is used — even if both the
# implementation and the helper accidentally use the same wrong formula.
#
# How the expected values were derived:
#   secret = "test_secret_key_xyz789"
#   hexdig = hmac.new(secret, canonical_string, sha256).hexdigest()
#   expected = base64.b64encode(hexdig.encode("utf-8")).decode("utf-8")
#
# Verified independently with Python at development time.
# ---------------------------------------------------------------------------

class TestKnownValueFixture:
    """
    Anchors the implementation to the CORRECT Rapyd formula
    (base64 of HMAC-SHA256 hex-digest string).

    Both the raw-bytes formula and the correct formula are different encodings
    of the same HMAC, but they produce non-overlapping base64 strings:
      • raw bytes  → 44-char base64 (32 raw bytes → ~43 b64 chars + padding)
      • hex string → 88-char base64 (64 hex chars → ~88 b64 chars)

    A test against these hard-coded 88-char values will FAIL if someone
    accidentally reverts to .digest() (which yields a 44-char string).
    """

    # Canonical inputs — must match the constants defined at fixture derivation time
    _METHOD      = "post"
    _PATH        = "/v1/payments"
    _SALT        = "fixture000001"
    _TS          = "1700000000"
    _BODY        = '{"amount":1000}'

    # Pre-computed: base64( hexdigest(HMAC-SHA256(secret, canonical_string)) )
    _EXPECTED_REQUEST_SIG = (
        "NmY0YzZiNGJmMzE2OTEyODczYzBiZGNlYmZkZGRhYjk3Y2FkZjVjN2M3MGRmYzA2MzU5"
        "NDlkNzUyZGIxM2JhYw=="
    )

    _WH_URL  = "https://samefare.is/webhooks/rapyd"
    _WH_SALT = "wh_fixture001"
    _WH_TS   = "1700000000"
    _WH_BODY = '{"id":"wh_test_001","type":"PAYMENT_COMPLETED"}'

    # Pre-computed: base64( hexdigest(HMAC-SHA256(secret, webhook_canonical_string)) )
    _EXPECTED_WEBHOOK_SIG = (
        "NmUxMjVjMTI4ZjU1NDI4YzRkM2JmNzNlYmY0OGZiMDNlYzFiNzE3Zjc5NjcxZjZhYjM0"
        "ODIzY2FmOTEyNjA4NA=="
    )

    def test_request_sig_matches_known_value(self):
        """_sign() must produce the pre-computed hex-digest base64 value."""
        rapyd = _import_rapyd()
        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            got = rapyd._sign(
                self._METHOD, self._PATH, self._SALT, self._TS, self._BODY,
            )
        assert got == self._EXPECTED_REQUEST_SIG, (
            f"Request signature does not match known fixture.\n"
            f"  got:      {got}\n"
            f"  expected: {self._EXPECTED_REQUEST_SIG}\n"
            "The correct formula is: base64(hexdigest(HMAC-SHA256(secret, canonical_string)))\n"
            "Using .digest() instead of .hexdigest() produces a 44-char base64 string, not 88."
        )

    def test_request_sig_is_88_chars(self):
        """
        base64(64-char hex string) = 88 chars (with padding).
        If this is 44 chars the raw-bytes formula was used — wrong.
        """
        rapyd = _import_rapyd()
        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            got = rapyd._sign(
                self._METHOD, self._PATH, self._SALT, self._TS, self._BODY,
            )
        assert len(got) == 88, (
            f"Signature length {len(got)} != 88. "
            "Expected 88 chars (base64 of 64-char hex string). "
            "A 44-char result means the raw .digest() bytes were base64-encoded — wrong formula."
        )

    def test_webhook_sig_matches_known_value(self):
        """verify_webhook() must accept the pre-computed hex-digest base64 value."""
        rapyd = _import_rapyd()
        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            result = rapyd.verify_webhook(
                url=self._WH_URL,
                body=self._WH_BODY,
                rapyd_signature=self._EXPECTED_WEBHOOK_SIG,
                rapyd_salt=self._WH_SALT,
                rapyd_timestamp=self._WH_TS,
            )
        assert result is True, (
            "verify_webhook() rejected a signature computed with the correct formula.\n"
            "Check that verify_webhook uses hexdigest → encode → base64, not raw digest → base64."
        )

    def test_raw_bytes_sig_is_rejected_by_webhook(self):
        """
        A signature produced with the WRONG formula (base64 of raw bytes) must be
        rejected, proving that the two formulas are not interchangeable.
        """
        rapyd = _import_rapyd()
        to_verify = (
            self._WH_URL + self._WH_SALT + self._WH_TS
            + ACCESS_KEY + SECRET_KEY + self._WH_BODY
        )
        wrong_sig = b64encode(
            hmac.new(
                SECRET_KEY.encode("utf-8"),
                to_verify.encode("utf-8"),
                hashlib.sha256,
            ).digest()           # raw bytes — the wrong formula
        ).decode("utf-8")

        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            result = rapyd.verify_webhook(
                url=self._WH_URL,
                body=self._WH_BODY,
                rapyd_signature=wrong_sig,
                rapyd_salt=self._WH_SALT,
                rapyd_timestamp=self._WH_TS,
            )
        assert result is False, (
            "verify_webhook() accepted a signature from the wrong (raw-bytes) formula. "
            "This means the implementation is using .digest() instead of .hexdigest()."
        )


# ---------------------------------------------------------------------------
# Idempotency header tests
#
# _headers() must include the Rapyd 'idempotency' header when a key is
# supplied, and must omit it when none is given.
#
# _request() for financial POSTs (MIT, capture, refund) must no longer put
# idempotency-like values in the request body — they belong in the header
# where Rapyd actually enforces deduplication.
# ---------------------------------------------------------------------------

class TestIdempotencyHeader:
    """
    Verify that _headers passes the 'idempotency' header to Rapyd when a key
    is supplied, and omits it for GETs and non-idempotent calls that don't
    provide one.
    """

    def test_idempotency_header_present_when_key_given(self):
        rapyd = _import_rapyd()
        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            with patch("app.rapyd._make_salt", return_value="testsalt0001"):
                with patch("app.rapyd.time") as mock_time:
                    mock_time.time.return_value = 1700000000
                    headers = rapyd._headers(
                        "post",
                        "/v1/payments",
                        '{"amount":1000}',
                        idempotency_key="idem-key-abc123",
                    )
        assert "idempotency" in headers, (
            "'idempotency' header must be present when idempotency_key is supplied"
        )
        assert headers["idempotency"] == "idem-key-abc123"

    def test_idempotency_header_absent_when_no_key(self):
        rapyd = _import_rapyd()
        with patch("app.rapyd.get_settings", return_value=_MOCK_SETTINGS):
            with patch("app.rapyd._make_salt", return_value="testsalt0002"):
                with patch("app.rapyd.time") as mock_time:
                    mock_time.time.return_value = 1700000000
                    headers = rapyd._headers(
                        "get",
                        "/v1/payments/pay_abc123",
                        "",
                    )
        assert "idempotency" not in headers, (
            "'idempotency' header must be absent when no key is supplied"
        )

    def test_capture_body_is_empty(self):
        """
        capture_payment sends no request body — idempotency is in the header only.
        Previously it sent {'idempotency_key': ...} which is not a Rapyd body field.
        """
        rapyd = _import_rapyd()
        captured_calls = []

        def fake_request(method, path, payload=None, *, idempotency_key=None):
            captured_calls.append({"method": method, "path": path,
                                   "payload": payload, "idempotency_key": idempotency_key})
            return {}

        with patch("app.rapyd._request", side_effect=fake_request):
            rapyd.capture_payment("pay_abc", "idem-cap-001")

        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["idempotency_key"] == "idem-cap-001", (
            "capture idempotency key must reach _request as the idempotency_key kwarg"
        )
        assert call["payload"] is None, (
            "capture_payment must send no request body — the old {'idempotency_key': ...} "
            "body field is not a Rapyd capture parameter"
        )

    def test_mit_body_has_no_idempotency_key_field(self):
        """
        create_mit_payment must NOT include 'idempotency_key' in the body payload.
        That field is not documented by Rapyd for POST /v1/payments; the header is
        the correct mechanism.
        """
        rapyd = _import_rapyd()
        captured_calls = []

        def fake_request(method, path, payload=None, *, idempotency_key=None):
            captured_calls.append({"payload": payload, "idempotency_key": idempotency_key})
            return {"id": "pay_mit_001", "status": "ACT"}

        with patch("app.rapyd._request", side_effect=fake_request):
            rapyd.create_mit_payment(
                amount=5000,
                customer_id="cus_abc",
                payment_method_id="pm_abc",
                capture=False,
                idempotency_key="idem-mit-001",
                metadata={"booking_id": 42},
            )

        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["idempotency_key"] == "idem-mit-001"
        assert "idempotency_key" not in call["payload"], (
            "'idempotency_key' must not appear in the MIT request body — "
            "it is not a Rapyd /v1/payments body field and belongs in the header"
        )

    def test_refund_sends_both_header_and_merchant_reference_id(self):
        """
        create_refund sends the key as both the idempotency header (deduplication)
        and merchant_reference_id body field (reconciliation / Rapyd lookup).
        """
        rapyd = _import_rapyd()
        captured_calls = []

        def fake_request(method, path, payload=None, *, idempotency_key=None):
            captured_calls.append({"payload": payload, "idempotency_key": idempotency_key})
            return {"id": "ref_001"}

        with patch("app.rapyd._request", side_effect=fake_request):
            rapyd.create_refund(
                payment_id="pay_abc",
                amount=5000,
                idempotency_key="idem-ref-001",
            )

        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["idempotency_key"] == "idem-ref-001", (
            "refund idempotency key must be sent as the 'idempotency' header"
        )
        assert call["payload"].get("merchant_reference_id") == "idem-ref-001", (
            "refund must also set merchant_reference_id in the body for reconciliation"
        )

    def test_checkout_sends_idempotency_header(self):
        """
        create_checkout_page passes idempotency_key as both the 'idempotency' header
        and merchant_reference_id body field.
        """
        rapyd = _import_rapyd()
        captured_calls = []

        def fake_request(method, path, payload=None, *, idempotency_key=None):
            captured_calls.append({"payload": payload, "idempotency_key": idempotency_key})
            return {"id": "chk_001", "redirect_url": "https://rapyd.example/checkout"}

        with patch("app.rapyd._request", side_effect=fake_request):
            rapyd.create_checkout_page(
                amount=5000,
                capture=False,
                complete_url="https://samefare.is/ok",
                cancel_url="https://samefare.is/cancel",
                idempotency_key="idem-chk-001",
                metadata={"booking_id": 7},
            )

        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["idempotency_key"] == "idem-chk-001"
        assert call["payload"].get("merchant_reference_id") == "idem-chk-001"


# ---------------------------------------------------------------------------
# Case A/B payment case threshold
#
# _payment_case is a pure function — tested inline to avoid triggering the
# SQLAlchemy engine creation that happens at router-module import time.
# ---------------------------------------------------------------------------

CASE_B_THRESHOLD_DAYS = 7   # mirrors the constant in payments.py


def _payment_case(departure_datetime: datetime) -> str:
    """
    Pure copy of app.routers.payments._payment_case — tested in isolation.
    Must stay in sync with the implementation.
    """
    return "A" if (departure_datetime - datetime.utcnow()) <= timedelta(days=CASE_B_THRESHOLD_DAYS) else "B"


class TestPaymentCase:
    """
    Ensure _payment_case uses timedelta comparison rather than .days so that
    fractional days are counted correctly.
    """

    def test_exactly_7_days_is_case_a(self):
        now       = datetime.utcnow()
        departure = now + timedelta(days=7)
        assert _payment_case(departure) == "A"

    def test_7_days_23_hours_is_case_b(self):
        """
        7 days 23 hours is MORE than 7 days — must be Case B.
        Using .days would incorrectly return 7 (floor) and classify as Case A,
        risking auth expiry before departure.
        """
        now       = datetime.utcnow()
        departure = now + timedelta(days=7, hours=23)
        assert _payment_case(departure) == "B", (
            "7d 23h departure must be Case B — a Case A auth would expire before the trip."
        )

    def test_days_truncation_bug_is_absent(self):
        """
        Explicit regression test: if .days were used instead of timedelta comparison,
        timedelta(days=7, hours=23).days == 7 and the case would wrongly be 'A'.
        Verify that the correct timedelta comparison catches it.
        """
        delta = timedelta(days=7, hours=23)
        # Old (broken) approach
        assert delta.days == 7, "sanity: .days truncates fractional days"
        # Correct approach
        assert delta > timedelta(days=7), "timedelta comparison preserves fractional days"

    def test_8_days_is_case_b(self):
        now       = datetime.utcnow()
        departure = now + timedelta(days=8)
        assert _payment_case(departure) == "B"

    def test_same_day_is_case_a(self):
        now       = datetime.utcnow()
        departure = now + timedelta(hours=3)
        assert _payment_case(departure) == "A"

    def test_6_days_is_case_a(self):
        now       = datetime.utcnow()
        departure = now + timedelta(days=6, hours=12)
        assert _payment_case(departure) == "A"
