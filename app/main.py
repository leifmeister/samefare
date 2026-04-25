from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from sqlalchemy.orm import joinedload

from app.config import get_settings
from app.database import Base, engine, SessionLocal
from app.dependencies import get_current_user_optional
from app import models  # noqa: F401 — register models before create_all
from app.routers import auth, bookings, language, messages, payments, trips, users, verification

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="SameFare",
    description="Icelandic ridesharing — share the journey across Iceland",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth.router)
app.include_router(trips.router)
app.include_router(bookings.router)
app.include_router(payments.router)
app.include_router(users.router)
app.include_router(language.router)
app.include_router(verification.router)
app.include_router(messages.router)

templates = Jinja2Templates(directory="templates")


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    db = SessionLocal()
    try:
        current_user = get_current_user_optional(request, db)
    finally:
        db.close()
    return templates.TemplateResponse("legal/terms.html", {"request": request, "current_user": current_user})


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    db = SessionLocal()
    try:
        current_user = get_current_user_optional(request, db)
    finally:
        db.close()
    return templates.TemplateResponse("legal/privacy.html", {"request": request, "current_user": current_user})


@app.get("/offer-ride", response_class=HTMLResponse)
def offer_ride_page(request: Request):
    db = SessionLocal()
    try:
        current_user = get_current_user_optional(request, db)
        if current_user:
            return RedirectResponse("/trips/new", status_code=303)
    finally:
        db.close()
    return templates.TemplateResponse("offer_ride.html", {"request": request, "current_user": None})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    db: Session = SessionLocal()
    try:
        current_user = get_current_user_optional(request, db)

        upcoming_trips = (
            db.query(models.Trip)
            .options(joinedload(models.Trip.driver).joinedload(models.User.reviews_received))
            .filter(
                models.Trip.status == models.TripStatus.active,
                models.Trip.departure_datetime >= datetime.utcnow(),
                models.Trip.seats_available > 0,
            )
            .order_by(models.Trip.departure_datetime)
            .limit(6)
            .all()
        )

        stats = {
            "trips":      db.query(models.Trip).count(),
            "passengers": db.query(models.Booking)
                           .filter(models.Booking.status == models.BookingStatus.confirmed)
                           .count(),
            "drivers":    db.query(models.User).count(),
        }
    finally:
        db.close()

    return templates.TemplateResponse("index.html", {
        "request":       request,
        "current_user":  current_user,
        "upcoming_trips": upcoming_trips,
        "stats":         stats,
    })


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return templates.TemplateResponse(
        "errors/404.html",
        {"request": request, "current_user": None},
        status_code=404,
    )


@app.exception_handler(500)
async def server_error(request: Request, exc):
    return templates.TemplateResponse(
        "errors/500.html",
        {"request": request, "current_user": None},
        status_code=500,
    )
