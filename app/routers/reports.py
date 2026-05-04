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
    if booking_id:
        booking = db.query(models.Booking).filter(
            models.Booking.id == int(booking_id),
            (models.Booking.passenger_id == current_user.id) |
            (models.Booking.trip.has(models.Trip.driver_id == current_user.id)),
        ).first()

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

    bk_id = int(booking_id) if booking_id.strip() else None

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
