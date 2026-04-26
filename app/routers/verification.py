"""
Verification router.

Passengers must have an approved ID before booking.
Drivers must have an approved driver's licence before posting a trip.

Admin routes (is_admin=True) let staff review and approve / reject documents.
"""

import os
import uuid

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from sqlalchemy import or_
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_template_context

settings = get_settings()

templates  = Jinja2Templates(directory="templates")
router     = APIRouter(tags=["verification"])

UPLOAD_DIR = "uploads/verifications"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ".heic"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def _save_upload(file: UploadFile) -> str:
    """Save an uploaded file and return the stored filename."""
    ext = os.path.splitext(file.filename or "")[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"File type {ext} not allowed.")
    stored = f"{uuid.uuid4().hex}{ext}"
    path   = os.path.join(UPLOAD_DIR, stored)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    content = file.file.read()
    if len(content) > MAX_FILE_SIZE:
        raise ValueError("File too large (max 10 MB).")
    with open(path, "wb") as f:
        f.write(content)
    return stored


# ── User-facing ───────────────────────────────────────────────────────────────

@router.get("/verify", response_class=HTMLResponse)
def verify_page(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
):
    return templates.TemplateResponse("verification/index.html", {
        **ctx, "error": None, "success": None,
    })


@router.post("/verify/identity", response_class=HTMLResponse)
def upload_identity(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    document: UploadFile = File(...),
):
    if current_user.id_verification == models.VerificationStatus.approved:
        return RedirectResponse("/verify", status_code=303)

    try:
        filename = _save_upload(document)
    except ValueError as e:
        return templates.TemplateResponse("verification/index.html", {
            **ctx, "error": str(e), "success": None,
        }, status_code=400)

    current_user.id_doc_filename     = filename
    current_user.id_rejection_reason = None
    if settings.beta_mode:
        current_user.id_verification = models.VerificationStatus.approved
        success_msg = "ID document submitted and automatically approved (beta mode)."
    else:
        current_user.id_verification = models.VerificationStatus.pending
        success_msg = "ID document submitted — we'll review it shortly."
    db.commit()
    return templates.TemplateResponse("verification/index.html", {
        **ctx,
        "error":   None,
        "success": success_msg,
    })


@router.post("/verify/license", response_class=HTMLResponse)
def upload_license(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    document: UploadFile = File(...),
):
    if current_user.license_verification == models.VerificationStatus.approved:
        return RedirectResponse("/verify", status_code=303)

    try:
        filename = _save_upload(document)
    except ValueError as e:
        return templates.TemplateResponse("verification/index.html", {
            **ctx, "error": str(e), "success": None,
        }, status_code=400)

    current_user.license_doc_filename      = filename
    current_user.license_rejection_reason  = None
    if settings.beta_mode:
        current_user.license_verification = models.VerificationStatus.approved
        success_msg = "Driver's licence submitted and automatically approved (beta mode)."
    else:
        current_user.license_verification = models.VerificationStatus.pending
        success_msg = "Driver's licence submitted — we'll review it shortly."
    db.commit()
    return templates.TemplateResponse("verification/index.html", {
        **ctx,
        "error":   None,
        "success": success_msg,
    })


# ── Admin ─────────────────────────────────────────────────────────────────────

def _require_admin(current_user: models.User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise __import__("fastapi").HTTPException(status_code=403, detail="Forbidden")
    return current_user


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    ctx:     dict         = Depends(get_template_context),
    admin:   models.User  = Depends(_require_admin),
    db:      Session      = Depends(get_db),
    q:       str          = "",
):
    query = db.query(models.User)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            or_(models.User.full_name.ilike(like), models.User.email.ilike(like))
        )
    users = query.order_by(models.User.created_at.desc()).all()

    # Attach computed stats to each user without extra queries
    for u in users:
        u._trip_count    = len(u.trips)
        u._booking_count = len(u.bookings)

    return templates.TemplateResponse("admin/users.html", {
        **ctx, "users": users, "q": q,
    })


@router.post("/admin/users/{user_id}/toggle-admin", response_class=HTMLResponse)
def toggle_admin(
    user_id: int,
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user and user.id != admin.id:   # can't remove your own admin
        user.is_admin = not user.is_admin
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/toggle-active", response_class=HTMLResponse)
def toggle_active(
    user_id: int,
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user and user.id != admin.id:
        user.is_active = not user.is_active
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/admin/verifications", response_class=HTMLResponse)
def admin_verifications(
    request: Request,
    ctx: dict = Depends(get_template_context),
    admin: models.User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    pending = (
        db.query(models.User)
        .filter(
            (models.User.id_verification      == models.VerificationStatus.pending) |
            (models.User.license_verification == models.VerificationStatus.pending)
        )
        .all()
    )
    return templates.TemplateResponse("admin/verifications.html", {
        **ctx, "pending_users": pending,
    })


@router.get("/admin/verifications/doc/{filename}")
def serve_doc(
    filename: str,
    admin: models.User = Depends(_require_admin),
):
    """Serve uploaded documents only to admins."""
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(path) or ".." in filename:
        raise __import__("fastapi").HTTPException(status_code=404)
    return FileResponse(path)


@router.post("/admin/verifications/{user_id}/approve-id")
def approve_id(
    user_id: int,
    admin: models.User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user:
        user.id_verification      = models.VerificationStatus.approved
        user.id_rejection_reason  = None
        db.commit()
    return RedirectResponse("/admin/verifications", status_code=303)


@router.post("/admin/verifications/{user_id}/reject-id")
def reject_id(
    user_id: int,
    reason: str = Form(""),
    admin: models.User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user:
        user.id_verification      = models.VerificationStatus.rejected
        user.id_rejection_reason  = reason or "Document could not be verified."
        user.id_doc_filename      = None
        db.commit()
    return RedirectResponse("/admin/verifications", status_code=303)


@router.post("/admin/verifications/{user_id}/approve-license")
def approve_license(
    user_id: int,
    admin: models.User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user:
        user.license_verification      = models.VerificationStatus.approved
        user.license_rejection_reason  = None
        db.commit()
    return RedirectResponse("/admin/verifications", status_code=303)


@router.post("/admin/verifications/{user_id}/reject-license")
def reject_license(
    user_id: int,
    reason: str = Form(""),
    admin: models.User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user:
        user.license_verification      = models.VerificationStatus.rejected
        user.license_rejection_reason  = reason or "Document could not be verified."
        user.license_doc_filename      = None
        db.commit()
    return RedirectResponse("/admin/verifications", status_code=303)


@router.post("/admin/test-email")
def admin_test_email(
    admin: models.User = Depends(_require_admin),
):
    """Send a test email to the admin's own address to verify Resend is working."""
    from app.config import get_settings
    from app import email as mailer
    s = get_settings()

    subject = "SameFare — email delivery test"
    body = mailer._wrap(
        mailer._h1("Email delivery working ✓") +
        mailer._p(f"This test email was sent to <strong>{admin.email}</strong> via Resend.") +
        mailer._p("If you're reading this, transactional emails are working correctly on this deployment.")
    )

    try:
        import json, urllib.request, urllib.error
        payload = json.dumps({
            "from":    s.email_from,
            "to":      [admin.email],
            "subject": subject,
            "html":    body,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {s.resend_api_key}",
                "Content-Type":  "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return RedirectResponse(
            "/admin/users?flash=Test+email+sent+to+" + admin.email.replace("@", "%40"),
            status_code=303,
        )
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:120]
        return RedirectResponse(
            "/admin/users?flash=Resend+error:+" + err.replace(" ", "+"),
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            "/admin/users?flash=Error:+" + str(exc)[:120].replace(" ", "+"),
            status_code=303,
        )
