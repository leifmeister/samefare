import os
import uuid

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
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
    request:          Request,
    ctx:              dict         = Depends(get_template_context),
    current_user:     models.User  = Depends(get_current_user),
    db:               Session      = Depends(get_db),
    full_name:        str          = Form(...),
    phone:            str          = Form(""),
    bio:              str          = Form(""),
    default_car_make:  str         = Form(""),
    default_car_model: str         = Form(""),
    default_car_year:  str         = Form(""),
    default_car_type:  str         = Form("sedan"),
):
    current_user.full_name = full_name
    current_user.phone     = phone or None
    current_user.bio       = bio or None
    current_user.default_car_make  = default_car_make  or None
    current_user.default_car_model = default_car_model or None
    current_user.default_car_year  = int(default_car_year) if default_car_year.strip() else None
    try:
        current_user.default_car_type = models.CarType(default_car_type)
    except ValueError:
        current_user.default_car_type = models.CarType.sedan
    db.commit()
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/avatar", response_class=HTMLResponse)
def upload_avatar(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
    photo: UploadFile = File(...),
):
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
    ext = os.path.splitext(photo.filename or "")[-1].lower()
    if ext not in allowed:
        return RedirectResponse("/profile", status_code=303)

    content = photo.file.read()
    if len(content) > 5 * 1024 * 1024:  # 5 MB limit
        return RedirectResponse("/profile", status_code=303)

    os.makedirs("static/avatars", exist_ok=True)

    # Delete old avatar file if it exists
    if current_user.avatar_url:
        old_path = current_user.avatar_url.lstrip("/")
        if os.path.exists(old_path):
            os.remove(old_path)

    filename = f"{uuid.uuid4().hex}{ext}"
    with open(f"static/avatars/{filename}", "wb") as f:
        f.write(content)

    current_user.avatar_url = f"/static/avatars/{filename}"
    db.commit()
    return RedirectResponse("/profile", status_code=303)
