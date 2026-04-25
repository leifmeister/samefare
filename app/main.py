import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import joinedload, Session

from app.config import get_settings
from app.database import Base, engine, SessionLocal
from app.dependencies import get_current_user_optional
from app import models  # noqa: F401 — register models before create_all
from app.routers import auth, bookings, language, messages, payments, reviews, trips, users, verification
from app.tasks import auto_complete_loop, _run_auto_complete

settings = get_settings()

# ── Schema migrations (idempotent — safe to run on every startup) ─────────────
# Covers every column in every table. ADD COLUMN IF NOT EXISTS is a no-op when
# the column already exists, so this is safe regardless of DB state.
_MIGRATIONS = [

    # ── Enum types ────────────────────────────────────────────────────────────
    """DO $$ BEGIN CREATE TYPE userrole AS ENUM ('driver','passenger','both');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN CREATE TYPE cartype AS ENUM ('sedan','suv','van','electric','4x4','camper');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN CREATE TYPE tripstatus AS ENUM ('active','completed','cancelled');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN CREATE TYPE bookingstatus AS ENUM
           ('awaiting_payment','pending','confirmed','rejected','cancelled','completed');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN CREATE TYPE paymentstatus AS ENUM
           ('authorised','captured','refunded','partial_refund');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN CREATE TYPE reviewtype AS ENUM
           ('passenger_to_driver','driver_to_passenger');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN CREATE TYPE verificationstatus AS ENUM
           ('unverified','pending','approved','rejected');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",

    # ── users ─────────────────────────────────────────────────────────────────
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone          VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active      BOOLEAN   NOT NULL DEFAULT TRUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin       BOOLEAN   NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url     VARCHAR(512)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio            TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at     TIMESTAMP NOT NULL DEFAULT now()",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified      BOOLEAN   NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_otp           VARCHAR(6)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_otp_expires   TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token         VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_verification      verificationstatus NOT NULL DEFAULT 'unverified'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS license_verification verificationstatus NOT NULL DEFAULT 'unverified'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_doc_filename      VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS license_doc_filename VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_rejection_reason      TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS license_rejection_reason TEXT",

    # ── trips ─────────────────────────────────────────────────────────────────
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS car_make      VARCHAR(100)",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS car_model     VARCHAR(100)",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS car_year      INTEGER",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS description   TEXT",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS pickup_address  VARCHAR(255)",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS dropoff_address VARCHAR(255)",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS allows_luggage BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS allows_pets    BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS smoking        BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS instant_book   BOOLEAN NOT NULL DEFAULT TRUE",

    # ── bookings ──────────────────────────────────────────────────────────────
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS message     TEXT",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS service_fee INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMP NOT NULL DEFAULT now()",

    # ── payments ──────────────────────────────────────────────────────────────
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_amount INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS card_last4   VARCHAR(4)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS card_brand   VARCHAR(20)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMP NOT NULL DEFAULT now()",

    # ── messages table ────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS messages (
        id         SERIAL  PRIMARY KEY,
        booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
        sender_id  INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
        body       TEXT    NOT NULL,
        is_read    BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_messages_booking_id ON messages(booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_messages_sender_id  ON messages(sender_id)",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create any brand-new tables defined in models
    Base.metadata.create_all(bind=engine)
    # Apply column-level migrations that create_all() won't handle
    with engine.begin() as conn:
        for stmt in _MIGRATIONS:
            conn.execute(text(stmt))
    # Run once immediately on startup, then every 10 minutes
    _run_auto_complete()
    task = asyncio.create_task(auto_complete_loop())
    yield
    task.cancel()


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
app.include_router(reviews.router)

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
