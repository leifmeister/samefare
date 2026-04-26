"""
Newsletter subscription and admin management.
"""
import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.dependencies import get_current_user, get_template_context
from app.routers.verification import _require_admin

templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["newsletter"])


# ── Public: subscribe ─────────────────────────────────────────────────────────

@router.post("/newsletter/subscribe")
def subscribe(
    request: Request,
    email:   str = Form(...),
    source:  str = Form("footer"),
    db:      Session = Depends(get_db),
):
    email = email.strip().lower()
    if email:
        existing = (
            db.query(models.NewsletterSubscriber)
            .filter(models.NewsletterSubscriber.email == email)
            .first()
        )
        if not existing:
            db.add(models.NewsletterSubscriber(email=email, source=source))
            db.commit()

    # Redirect back to wherever the form was submitted from
    referer = request.headers.get("referer", "/")
    # Append ?subscribed=1 so we can show a thank-you flash
    sep = "&" if "?" in referer else "?"
    return RedirectResponse(f"{referer}{sep}subscribed=1", status_code=303)


# ── Admin: list + export ──────────────────────────────────────────────────────

@router.get("/admin/newsletter", response_class=HTMLResponse)
def admin_newsletter(
    request: Request,
    ctx:     dict        = Depends(get_template_context),
    admin:   models.User = Depends(_require_admin),
    db:      Session     = Depends(get_db),
):
    subscribers = (
        db.query(models.NewsletterSubscriber)
        .order_by(models.NewsletterSubscriber.created_at.desc())
        .all()
    )
    return templates.TemplateResponse("admin/newsletter.html", {
        **ctx,
        "subscribers": subscribers,
    })


@router.get("/admin/newsletter/export.csv")
def export_newsletter_csv(
    admin: models.User = Depends(_require_admin),
    db:    Session     = Depends(get_db),
):
    subscribers = (
        db.query(models.NewsletterSubscriber)
        .order_by(models.NewsletterSubscriber.created_at.desc())
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "source", "signed_up"])
    for s in subscribers:
        writer.writerow([s.email, s.source or "", s.created_at.strftime("%Y-%m-%d %H:%M")])
    buf.seek(0)
    filename = f"samefare_subscribers_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
