import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app import models
from app.database import get_db
from app.dependencies import get_current_user, get_template_context

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["reports"])

_REASON_LABELS = {
    "harassment": "Harassment or abusive behaviour",
    "safety":     "Safety concern during a trip",
    "fraud":      "Fraud or scam",
    "no_show":    "Repeated no-shows / unreliable",
    "spam":       "Spam or unwanted messages",
    "other":      "Other",
}


@router.get("/report/{user_id}", response_class=HTMLResponse)
def report_form(
    user_id: int,
    request: Request,
    ctx:          dict         = Depends(get_template_context),
    current_user: models.User  = Depends(get_current_user),
    db:           Session      = Depends(get_db),
):
    reported = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.is_active == True,
    ).first()
    if not reported or reported.id == current_user.id:
        return RedirectResponse("/trips", status_code=303)

    booking_id = request.query_params.get("booking_id")
    booking = None
    if booking_id and booking_id.strip():
        try:
            bk_id_raw = int(booking_id.strip())
        except ValueError:
            bk_id_raw = None
        if bk_id_raw is not None:
            bk = (
                db.query(models.Booking)
                .options(joinedload(models.Booking.trip))
                .filter(models.Booking.id == bk_id_raw)
                .first()
            )
            if bk:
                reporter_is_passenger = bk.passenger_id == current_user.id
                reporter_is_driver    = bk.trip.driver_id == current_user.id
                reported_is_passenger = bk.passenger_id == user_id
                reported_is_driver    = bk.trip.driver_id == user_id
                if (reporter_is_passenger or reporter_is_driver) and \
                   (reported_is_passenger or reported_is_driver):
                    booking = bk

    return templates.TemplateResponse("reports/new.html", {
        **ctx,
        "reported":      reported,
        "booking":       booking,
        "reason_labels": _REASON_LABELS,
    })


@router.post("/report/{user_id}", response_class=HTMLResponse)
def submit_report(
    user_id:    int,
    request:    Request,
    ctx:        dict        = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:         Session     = Depends(get_db),
    reason:     str         = Form(...),
    comment:    str         = Form(""),
    booking_id: str         = Form(""),
):
    reported = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.is_active == True,
    ).first()
    if not reported or reported.id == current_user.id:
        return RedirectResponse("/trips", status_code=303)

    try:
        reason_enum = models.ReportReason(reason)
    except ValueError:
        return RedirectResponse(f"/report/{user_id}", status_code=303)

    # Validate the booking reference defensively.
    # Parse the raw form value, then reload from DB and confirm the current
    # user is a participant and the reported user is the other participant.
    # Any mismatch silently drops the booking link — the report is still filed.
    bk_id = None
    if booking_id.strip():
        try:
            bk_id_raw = int(booking_id.strip())
        except ValueError:
            bk_id_raw = None
        if bk_id_raw is not None:
            bk = (
                db.query(models.Booking)
                .options(joinedload(models.Booking.trip))
                .filter(models.Booking.id == bk_id_raw)
                .first()
            )
            if bk:
                reporter_is_passenger = bk.passenger_id == current_user.id
                reporter_is_driver    = bk.trip.driver_id == current_user.id
                reported_is_passenger = bk.passenger_id == user_id
                reported_is_driver    = bk.trip.driver_id == user_id
                if (reporter_is_passenger or reporter_is_driver) and \
                   (reported_is_passenger or reported_is_driver):
                    bk_id = bk.id

    report = models.UserReport(
        reporter_id = current_user.id,
        reported_id = user_id,
        booking_id  = bk_id,
        reason      = reason_enum,
        comment     = comment.strip() or None,
    )
    db.add(report)
    db.commit()
    log.info("User %d reported user %d for %s", current_user.id, user_id, reason)

    return RedirectResponse(f"/report/{user_id}/thanks", status_code=303)


@router.get("/report/{user_id}/thanks", response_class=HTMLResponse)
def report_thanks(
    user_id: int,
    request: Request,
    ctx:     dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db:      Session = Depends(get_db),
):
    reported = db.query(models.User).filter(models.User.id == user_id).first()
    return templates.TemplateResponse("reports/thanks.html", {
        **ctx,
        "reported": reported,
    })


# ── Admin ─────────────────────────────────────────────────────────────────────

def _require_admin(current_user: models.User = Depends(get_current_user)):
    if not current_user.is_admin:
        from fastapi import HTTPException
        raise HTTPException(status_code=403)
    return current_user


@router.get("/admin/reports", response_class=HTMLResponse)
def admin_reports(
    request: Request,
    ctx:     dict        = Depends(get_template_context),
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    reports = (
        db.query(models.UserReport)
        .options(
            joinedload(models.UserReport.reporter),
            joinedload(models.UserReport.reported),
            joinedload(models.UserReport.booking).joinedload(models.Booking.trip),
        )
        .order_by(models.UserReport.created_at.desc())
        .all()
    )
    return templates.TemplateResponse("admin/reports.html", {
        **ctx,
        "reports":       reports,
        "reason_labels": _REASON_LABELS,
    })


@router.post("/admin/reports/{report_id}/suspend", response_class=HTMLResponse)
def suspend_from_report(
    report_id: int,
    reason:    str         = Form(""),
    admin:     models.User = Depends(_require_admin),
    db:        Session     = Depends(get_db),
):
    report = db.query(models.UserReport).filter(models.UserReport.id == report_id).first()
    if report:
        user = db.query(models.User).filter(models.User.id == report.reported_id).first()
        if user and user.id != admin.id and not user.deleted_at:
            user.is_active         = False
            user.suspension_reason = reason.strip() or None
        report.reviewed_at = datetime.utcnow()
        db.commit()
    return RedirectResponse("/admin/reports", status_code=303)


@router.post("/admin/reports/{report_id}/dismiss", response_class=HTMLResponse)
def dismiss_report(
    report_id: int,
    admin:     models.User = Depends(_require_admin),
    db:        Session     = Depends(get_db),
):
    report = db.query(models.UserReport).filter(models.UserReport.id == report_id).first()
    if report:
        report.reviewed_at = datetime.utcnow()
        db.commit()
    return RedirectResponse("/admin/reports", status_code=303)


@router.get("/admin/payouts", response_class=HTMLResponse)
def admin_payouts(
    request: Request,
    ctx:     dict        = Depends(get_template_context),
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    """
    Driver payouts ready for the account holder to approve. Each is submitted to
    Blikk on demand (fresh SCA link) when Approve is clicked, so links never go
    stale. Also shows in-flight (submitted, completing) and recent batches.
    """
    ready = (
        db.query(models.DriverPayout)
        .options(
            joinedload(models.DriverPayout.driver),
            joinedload(models.DriverPayout.items),
        )
        .filter(
            models.DriverPayout.status        == models.DriverPayoutStatus.pending,
            models.DriverPayout.payout_method == models.PayoutMethod.blikk,
        )
        .order_by(models.DriverPayout.created_at.asc())
        .all()
    )
    awaiting = (
        db.query(models.DriverPayout)
        .options(joinedload(models.DriverPayout.driver))
        .filter(
            models.DriverPayout.status       == models.DriverPayoutStatus.sent,
            models.DriverPayout.approval_url  != None,  # noqa: E711
        )
        .order_by(models.DriverPayout.sent_at.asc())
        .all()
    )
    recent = (
        db.query(models.DriverPayout)
        .options(joinedload(models.DriverPayout.driver))
        .filter(models.DriverPayout.status == models.DriverPayoutStatus.confirmed)
        .order_by(models.DriverPayout.created_at.desc())
        .limit(20)
        .all()
    )
    # ── Needs attention (replaces the old admin SMS alerts) ───────────────────
    # Failed payouts: the driver wasn't paid — investigate / re-issue.
    failed = (
        db.query(models.DriverPayout)
        .options(joinedload(models.DriverPayout.driver), joinedload(models.DriverPayout.items))
        .filter(models.DriverPayout.status == models.DriverPayoutStatus.failed)
        .order_by(models.DriverPayout.failed_at.desc().nullslast())
        .all()
    )
    # Clawbacks: a driver was paid for a fare later refunded (e.g. a no-show
    # reported after payout) — recover the funds manually.
    clawbacks = (
        db.query(models.PayoutItem)
        .options(joinedload(models.PayoutItem.driver))
        .filter(models.PayoutItem.status == models.PayoutItemStatus.reversed)
        .order_by(models.PayoutItem.id.desc())
        .all()
    )
    return templates.TemplateResponse("admin/payouts.html", {
        **ctx,
        "ready":          ready,
        "ready_total":    sum(b.amount for b in ready),
        "awaiting":       awaiting,
        "recent":         recent,
        "failed":         failed,
        "clawbacks":      clawbacks,
        "clawback_total": sum(i.amount for i in clawbacks),
    })


@router.post("/admin/payouts/{batch_id}/approve")
def approve_payout(
    batch_id: int,
    admin:    models.User = Depends(_require_admin),
    db:       Session     = Depends(get_db),
):
    """
    Submit a prepared (pending) Blikk payout batch to Blikk NOW — generating a
    fresh SCA approval link — and redirect the approver straight to it. The link
    is seconds old, so it never expires before they act. Reconcile confirms the
    batch once they approve.
    """
    from app.config import get_settings
    from app.payout import send_driver_payout

    batch = (
        db.query(models.DriverPayout)
        .options(joinedload(models.DriverPayout.driver), joinedload(models.DriverPayout.items))
        .filter(models.DriverPayout.id == batch_id)
        .first()
    )
    if not batch:
        return RedirectResponse("/admin/payouts", status_code=303)

    # Already submitted and still open — just re-open the existing Blikk link.
    if batch.status == models.DriverPayoutStatus.sent and batch.approval_url:
        return RedirectResponse(batch.approval_url, status_code=303)
    # Only pending batches can be submitted; ignore anything else (idempotent).
    if batch.status != models.DriverPayoutStatus.pending:
        return RedirectResponse("/admin/payouts", status_code=303)
    # Safety: never call the provider while payouts are globally disabled.
    if not get_settings().payout_enabled:
        return RedirectResponse("/admin/payouts", status_code=303)

    send_driver_payout(db, batch)   # sets status sent + approval_url, or marks failed
    db.commit()

    if batch.status == models.DriverPayoutStatus.sent and batch.approval_url:
        return RedirectResponse(batch.approval_url, status_code=303)
    # Settled instantly (no SCA) or failed — back to the list either way.
    return RedirectResponse("/admin/payouts", status_code=303)


@router.get("/admin/city-suggestions", response_class=HTMLResponse)
def admin_city_suggestions(
    request: Request,
    ctx:     dict        = Depends(get_template_context),
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    from sqlalchemy import func
    rows = (
        db.query(
            models.CitySuggestion.city_name,
            func.count(models.CitySuggestion.id).label("count"),
            func.min(models.CitySuggestion.created_at).label("first_seen"),
            func.max(models.CitySuggestion.created_at).label("last_seen"),
            # Sample context from the most recent row
            func.max(models.CitySuggestion.context_origin).label("sample_origin"),
            func.max(models.CitySuggestion.context_destination).label("sample_destination"),
        )
        .group_by(models.CitySuggestion.city_name)
        .order_by(func.count(models.CitySuggestion.id).desc())
        .all()
    )
    return templates.TemplateResponse("admin/city_suggestions.html", {
        **ctx,
        "suggestions": rows,
    })
