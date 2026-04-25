from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.dependencies import get_current_user, get_template_context

templates = Jinja2Templates(directory="templates")
router = APIRouter(tags=["users"])


@router.get("/profile", response_class=HTMLResponse)
def profile(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Trips the current user is driving
    my_trips = (
        db.query(models.Trip)
        .filter(models.Trip.driver_id == current_user.id)
        .order_by(models.Trip.departure_datetime.desc())
        .all()
    )

    # Pending bookings on the driver's trips (requests to accept/reject)
    pending_bookings = []
    for trip in my_trips:
        for b in trip.bookings:
            if b.status == models.BookingStatus.pending:
                pending_bookings.append(b)

    return templates.TemplateResponse("profile.html", {
        **ctx,
        "my_trips": my_trips,
        "pending_bookings": pending_bookings,
    })


@router.post("/profile/edit", response_class=HTMLResponse)
def edit_profile(
    request: Request,
    ctx: dict = Depends(get_template_context),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    full_name: str = Form(...),
    phone: str = Form(""),
    bio: str = Form(""),
):
    current_user.full_name = full_name
    current_user.phone = phone or None
    current_user.bio = bio or None
    db.commit()
    return RedirectResponse("/profile", status_code=303)
