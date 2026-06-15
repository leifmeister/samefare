"""
Phone number verification via SMS OTP.

Routes
------
POST /verify-phone/send     — generate + send a 6-digit code
POST /verify-phone/confirm  — validate the code, mark phone_verified
"""

import random
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse

from app import models, sms
from app.database import get_db
from app.dependencies import get_current_user
from app.i18n import detect_lang, get_translations
from app.limiter import rate_limit, limit_ok
from sqlalchemy.orm import Session

router = APIRouter(prefix="/verify-phone", tags=["phone"])

OTP_TTL_MINUTES = 10

# Maps the stable token returned by sms.send_otp to a translation key. Anything
# unmapped (incl. "unconfigured"/"no_number") falls back to the generic message.
_SMS_ERROR_KEYS = {
    "region_unsupported": "otp_sms_region_unsupported",
    "invalid_number":     "otp_sms_invalid_number",
    "optout":             "otp_sms_optout",
}


def _generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


@router.post("/send")
def send_otp(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    phone: str = Form(None),
    _rl=rate_limit(5, 600),   # 5 attempts per 10 minutes per IP
):
    """
    Generate a 6-digit OTP, store it on the user row, and send it via SMS.
    Returns JSON so the caller can update inline without a full reload.

    An optional `phone` param lets an unverified user set/update their number in
    the same step (used by the inline verify step on the booking page, so a
    passenger with no saved number can verify without a separate profile save).
    The profile page sends no `phone` and uses the already-saved number.
    """
    _t = get_translations(detect_lang(request))

    if current_user.phone_verified:
        return JSONResponse({"ok": False, "error": _t("otp_already_verified")}, status_code=400)

    if phone:
        normalized = sms.normalize_phone(phone)
        if not normalized:
            return JSONResponse(
                {"ok": False, "error": _t("otp_invalid_format")},
                status_code=400,
            )
        if normalized != current_user.phone:
            current_user.phone          = normalized
            current_user.phone_otp      = None
            current_user.phone_otp_expires = None
            db.commit()

    phone = current_user.phone
    if not phone:
        return JSONResponse({"ok": False, "error": _t("otp_no_phone")}, status_code=400)

    # Hard caps on real SMS sends (Twilio cost + anti-harassment), independent of
    # the per-IP limiter: at most 8/day per user and 5/day per destination number.
    if not limit_ok(f"otp-user:{current_user.id}", 8, 86400):
        return JSONResponse({"ok": False, "error": _t("otp_rate_user")}, status_code=429)
    if not limit_ok(f"otp-dest:{phone}", 5, 86400):
        return JSONResponse({"ok": False, "error": _t("otp_rate_dest")}, status_code=429)

    code    = _generate_otp()
    expires = datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES)

    current_user.phone_otp         = code
    current_user.phone_otp_expires = expires
    db.commit()

    sent, sms_error = sms.send_otp(phone, code)
    if not sent:
        # sms_error is a stable token (never raw Twilio output); translate it.
        return JSONResponse({
            "ok": False,
            "error": _t(_SMS_ERROR_KEYS.get(sms_error, "otp_sms_generic")),
        }, status_code=502)

    return JSONResponse({"ok": True, "message": _t("otp_sent").format(phone=phone)})


@router.post("/confirm")
def confirm_otp(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    code: str = Form(...),
    _rl=rate_limit(10, 600),
):
    """
    Validate the OTP the user typed in.  Returns JSON.
    """
    _t = get_translations(detect_lang(request))

    if current_user.phone_verified:
        return JSONResponse({"ok": True, "message": _t("otp_verified")})

    if not current_user.phone_otp:
        return JSONResponse({"ok": False, "error": _t("otp_none_sent")}, status_code=400)

    if datetime.utcnow() > current_user.phone_otp_expires:
        return JSONResponse({"ok": False, "error": _t("otp_expired")}, status_code=400)

    if code.strip() != current_user.phone_otp:
        return JSONResponse({"ok": False, "error": _t("otp_incorrect")}, status_code=400)

    # Success
    current_user.phone_verified       = True
    current_user.phone_otp            = None
    current_user.phone_otp_expires    = None
    db.commit()

    return JSONResponse({"ok": True, "message": _t("otp_verified")})
