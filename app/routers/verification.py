"""
Verification router.

Passengers must have an approved ID before booking.
Drivers must have an approved driver's licence before posting a trip.

Admin routes (is_admin=True) let staff review and approve / reject documents.
"""

import os
import uuid
from datetime import datetime, timedelta, date

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from sqlalchemy import or_, func
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

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
    request:  Request,
    ctx:      dict         = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:       Session      = Depends(get_db),
    doc_type: str          = Form("passport"),   # 'license' | 'passport' | 'national_id'
    document: UploadFile   = File(...),
):
    """
    Single upload handler for all identity document types.
    A driver's licence satisfies both identity and driving verification.
    A passport or national ID satisfies identity only.
    """
    if current_user.id_verification == models.VerificationStatus.approved:
        # Already identity-verified; if they're now adding a standalone licence, redirect
        if doc_type == "license" and current_user.license_verification != models.VerificationStatus.approved:
            pass  # fall through to handle licence upload
        else:
            return RedirectResponse("/verify", status_code=303)

    try:
        filename = _save_upload(document)
    except ValueError as e:
        return templates.TemplateResponse("verification/index.html", {
            **ctx, "error": str(e), "success": None,
        }, status_code=400)

    is_licence = doc_type == "license"
    approved   = models.VerificationStatus.approved
    pending    = models.VerificationStatus.pending

    # ── Identity side ────────────────────────────────────────────────────────
    current_user.id_doc_filename     = filename
    current_user.id_doc_type         = doc_type
    current_user.id_rejection_reason = None
    current_user.id_verification     = approved if settings.beta_mode else pending

    # ── Driving side — only when a licence is submitted ───────────────────
    if is_licence:
        current_user.license_doc_filename     = filename   # same physical file
        current_user.license_rejection_reason = None
        current_user.license_verification     = approved if settings.beta_mode else pending

    db.commit()

    if settings.beta_mode:
        if is_licence:
            success_msg = "Driver's licence approved — identity and driving both verified (beta mode)."
        else:
            success_msg = "Document approved — identity verified (beta mode)."
    else:
        if is_licence:
            success_msg = "Driver's licence submitted — we'll verify your identity and driving eligibility shortly."
        else:
            success_msg = "Document submitted — we'll review your identity shortly."

    return templates.TemplateResponse("verification/index.html", {
        **ctx, "error": None, "success": success_msg,
    })


@router.post("/verify/license", response_class=HTMLResponse)
def upload_license(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    document: UploadFile = File(...),
):
    """
    Standalone licence upload — only shown to users whose identity is already
    verified via passport/national ID but who still need driving verification.
    """
    if current_user.license_verification == models.VerificationStatus.approved:
        return RedirectResponse("/verify", status_code=303)

    try:
        filename = _save_upload(document)
    except ValueError as e:
        return templates.TemplateResponse("verification/index.html", {
            **ctx, "error": str(e), "success": None,
        }, status_code=400)

    current_user.license_doc_filename     = filename
    current_user.license_rejection_reason = None
    if settings.beta_mode:
        current_user.license_verification = models.VerificationStatus.approved
        success_msg = "Driver's licence approved (beta mode)."
    else:
        current_user.license_verification = models.VerificationStatus.pending
        success_msg = "Driver's licence submitted — we'll review it shortly."
    db.commit()
    return templates.TemplateResponse("verification/index.html", {
        **ctx, "error": None, "success": success_msg,
    })


# ── Admin ─────────────────────────────────────────────────────────────────────

def _require_admin(current_user: models.User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise __import__("fastapi").HTTPException(status_code=403, detail="Forbidden")
    return current_user


@router.get("/admin/test-users", response_class=HTMLResponse)
def admin_test_users(
    request: Request,
    ctx:     dict        = Depends(get_template_context),
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    # Import TEST_USERS from the seed script — single source of truth
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "seed_test_data",
        os.path.join(os.path.dirname(__file__), "..", "..", "seed_test_data.py"),
    )
    seed_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_mod)
    test_users_def = seed_mod.TEST_USERS

    # Enrich with live DB state (exists? how many trips? newsletter discount?)
    enriched = []
    for u in test_users_def:
        db_user = db.query(models.User).filter(models.User.email == u["email"]).first()
        sub = (
            db.query(models.NewsletterSubscriber)
            .filter(models.NewsletterSubscriber.email == u["email"])
            .first()
        ) if db_user else None
        enriched.append({
            **u,
            "exists":           db_user is not None,
            "user_id":          db_user.id if db_user else None,
            "trip_count":       len(db_user.trips) if db_user else 0,
            "discount_active":  sub is not None and not sub.discount_used,
            "discount_used":    sub is not None and sub.discount_used,
        })

    return templates.TemplateResponse("admin/test_users.html", {
        **ctx,
        "test_users": enriched,
    })


@router.post("/admin/test-users/seed", response_class=HTMLResponse)
def seed_test_users(
    request: Request,
    admin:   models.User = Depends(_require_admin),
):
    """Run the seed script from the admin panel."""
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "seed_test_data",
        os.path.join(os.path.dirname(__file__), "..", "..", "seed_test_data.py"),
    )
    seed_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_mod)
    seed_mod.run()
    return RedirectResponse("/admin/test-users?seeded=1", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    ctx:     dict        = Depends(get_template_context),
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    now      = datetime.utcnow()
    ago_7d   = now - timedelta(days=7)
    ago_30d  = now - timedelta(days=30)

    # ── Users ──────────────────────────────────────────────────────────────────
    total_users    = db.query(func.count(models.User.id)).scalar() or 0
    new_users_7d   = db.query(func.count(models.User.id)).filter(models.User.created_at >= ago_7d).scalar() or 0
    new_users_30d  = db.query(func.count(models.User.id)).filter(models.User.created_at >= ago_30d).scalar() or 0
    verified_users = db.query(func.count(models.User.id)).filter(
        models.User.id_verification == "approved"
    ).scalar() or 0

    # ── Trips ──────────────────────────────────────────────────────────────────
    total_trips    = db.query(func.count(models.Trip.id)).scalar() or 0
    trips_7d       = db.query(func.count(models.Trip.id)).filter(models.Trip.created_at >= ago_7d).scalar() or 0
    upcoming_trips = db.query(func.count(models.Trip.id)).filter(
        models.Trip.departure_datetime >= now,
        models.Trip.status == models.TripStatus.active,
    ).scalar() or 0

    # ── Bookings ───────────────────────────────────────────────────────────────
    confirmed_bookings = db.query(func.count(models.Booking.id)).filter(
        models.Booking.status == models.BookingStatus.confirmed
    ).scalar() or 0
    pending_bookings   = db.query(func.count(models.Booking.id)).filter(
        models.Booking.status == models.BookingStatus.pending
    ).scalar() or 0
    bookings_7d        = db.query(func.count(models.Booking.id)).filter(
        models.Booking.status == models.BookingStatus.confirmed,
        models.Booking.created_at >= ago_7d,
    ).scalar() or 0

    # ── Revenue ────────────────────────────────────────────────────────────────
    total_gmv = db.query(func.sum(models.Booking.total_price)).filter(
        models.Booking.status == models.BookingStatus.confirmed
    ).scalar() or 0
    total_fees = db.query(func.sum(models.Booking.service_fee)).filter(
        models.Booking.status == models.BookingStatus.confirmed
    ).scalar() or 0
    fees_7d = db.query(func.sum(models.Booking.service_fee)).filter(
        models.Booking.status == models.BookingStatus.confirmed,
        models.Booking.created_at >= ago_7d,
    ).scalar() or 0

    # ── Newsletter ─────────────────────────────────────────────────────────────
    total_subscribers  = db.query(func.count(models.NewsletterSubscriber.id)).scalar() or 0
    discounts_used     = db.query(func.count(models.NewsletterSubscriber.id)).filter(
        models.NewsletterSubscriber.discount_used == True  # noqa: E712
    ).scalar() or 0

    # ── Popular routes (top 5) ─────────────────────────────────────────────────
    popular_routes = (
        db.query(
            models.Trip.origin,
            models.Trip.destination,
            func.count(models.Trip.id).label("trip_count"),
            func.sum(
                db.query(func.count(models.Booking.id))
                .filter(
                    models.Booking.trip_id == models.Trip.id,
                    models.Booking.status  == models.BookingStatus.confirmed,
                )
                .correlate(models.Trip)
                .scalar_subquery()
            ).label("booking_count"),
        )
        .group_by(models.Trip.origin, models.Trip.destination)
        .order_by(func.count(models.Trip.id).desc())
        .limit(6)
        .all()
    )

    # ── Recent confirmed bookings ──────────────────────────────────────────────
    recent_bookings = (
        db.query(models.Booking)
        .options(
            joinedload(models.Booking.trip),
            joinedload(models.Booking.passenger),
        )
        .filter(models.Booking.status == models.BookingStatus.confirmed)
        .order_by(models.Booking.created_at.desc())
        .limit(8)
        .all()
    )

    # ── Annual pricing policy reminder (December only) ────────────────────────
    # Show a banner in December when no PricingPolicy row has been entered for
    # the coming year yet.  The banner disappears automatically once a row with
    # effective_from >= Jan 1 of next year exists in the DB.
    today     = date.today()
    next_year = today.year + 1
    pricing_reminder = (
        today.month == 12
        and not db.query(models.PricingPolicy)
            .filter(models.PricingPolicy.effective_from >= date(next_year, 1, 1))
            .first()
    )

    return templates.TemplateResponse("admin/dashboard.html", {
        **ctx,
        # users
        "total_users":    total_users,
        "new_users_7d":   new_users_7d,
        "new_users_30d":  new_users_30d,
        "verified_users": verified_users,
        # trips
        "total_trips":    total_trips,
        "trips_7d":       trips_7d,
        "upcoming_trips": upcoming_trips,
        # bookings
        "confirmed_bookings": confirmed_bookings,
        "pending_bookings":   pending_bookings,
        "bookings_7d":        bookings_7d,
        # revenue
        "total_gmv":   total_gmv,
        "total_fees":  total_fees,
        "fees_7d":     fees_7d,
        # newsletter
        "total_subscribers": total_subscribers,
        "discounts_used":    discounts_used,
        # tables
        "popular_routes":   popular_routes,
        "recent_bookings":  recent_bookings,
        # annual pricing reminder
        "pricing_reminder": pricing_reminder,
        "next_year":        next_year,
    })


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


@router.post("/admin/users/{user_id}/suspend", response_class=HTMLResponse)
def suspend_user(
    user_id: int,
    reason:  str         = Form(""),
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user and user.id != admin.id and not user.deleted_at:
        user.is_active         = False
        user.suspension_reason = reason.strip() or None
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/reactivate", response_class=HTMLResponse)
def reactivate_user(
    user_id: int,
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user and user.id != admin.id and not user.deleted_at:
        user.is_active         = True
        user.suspension_reason = None
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
        user.id_verification     = models.VerificationStatus.approved
        user.id_rejection_reason = None
        # If a driver's licence was used for identity, it also covers driving
        if user.id_doc_type == "license":
            user.license_verification     = models.VerificationStatus.approved
            user.license_rejection_reason = None
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
        user.id_verification     = models.VerificationStatus.rejected
        user.id_rejection_reason = reason or "Document could not be verified."
        user.id_doc_filename     = None
        # If this was a dual-use licence, reset the driving status too
        if user.id_doc_type == "license":
            user.license_verification     = models.VerificationStatus.rejected
            user.license_rejection_reason = reason or "Document could not be verified."
            user.license_doc_filename     = None
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

    if not s.resend_api_key:
        return RedirectResponse("/admin/users?flash=RESEND_API_KEY+is+not+set+in+Railway", status_code=303)

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
                "User-Agent":    "SameFare/1.0",
                "Accept":        "application/json",
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
