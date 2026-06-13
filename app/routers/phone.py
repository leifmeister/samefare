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
from app.limiter import rate_limit
from sqlalchemy.orm import Session

router = APIRouter(prefix="/verify-phone", tags=["phone"])

OTP_TTL_MINUTES = 10


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
    if current_user.phone_verified:
        return JSONResponse({"ok": False, "error": "Phone already verified."}, status_code=400)

    if phone:
        normalized = sms.normalize_phone(phone)
        if not normalized:
            return JSONResponse(
                {"ok": False, "error": "That doesn't look like a valid phone number."},
                status_code=400,
            )
        if normalized != current_user.phone:
            current_user.phone          = normalized
            current_user.phone_otp      = None
            current_user.phone_otp_expires = None
            db.commit()

    phone = current_user.phone
    if not phone:
        return JSONResponse({"ok": False, "error": "No phone number saved."}, status_code=400)

    code    = _generate_otp()
    expires = datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES)

    current_user.phone_otp         = code
    current_user.phone_otp_expires = expires
    db.commit()

    sent, sms_error = sms.send_otp(phone, code)
    if not sent:
        return JSONResponse({
            "ok": False,
            "error": f"Could not send SMS: {sms_error}"
        }, status_code=502)

    return JSONResponse({"ok": True, "message": f"Code sent to {phone}."})


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
    if current_user.phone_verified:
        return JSONResponse({"ok": True, "message": "Already verified."})

    if not current_user.phone_otp:
        return JSONResponse({"ok": False, "error": "No code was sent. Request a new one."}, status_code=400)

    if datetime.utcnow() > current_user.phone_otp_expires:
        return JSONResponse({"ok": False, "error": "Code expired. Request a new one."}, status_code=400)

    if code.strip() != current_user.phone_otp:
        return JSONResponse({"ok": False, "error": "Incorrect code. Please try again."}, status_code=400)

    # Success
    current_user.phone_verified       = True
    current_user.phone_otp            = None
    current_user.phone_otp_expires    = None
    db.commit()

    return JSONResponse({"ok": True, "message": "Phone verified!"})
