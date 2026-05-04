import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import joinedload, selectinload, Session

from app.config import get_settings
from app.database import Base, engine, SessionLocal
from app.dependencies import get_current_user_optional
from app import models  # noqa: F401 — register models before create_all
from app.routers import alerts, auth, bookings, language, messages, newsletter, payments, phone, reviews, trips, users, verification, webhooks
from app.tasks import (
    auto_complete_loop,
    _run_auto_complete, _run_auto_ratings, _run_trip_reminders,
    _run_capture_payments,
    _run_retry_refunds,
    _run_create_payout_items, _run_advance_payout_items,
    _run_refresh_fuel_price,
)

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
           ('authorised','captured','refunded','partial_refund',
            'pending','card_saved','failed','auth_expired','retry_pending');
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
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_doc_type          VARCHAR(20)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS license_doc_filename VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_rejection_reason      TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS license_rejection_reason TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_car_make  VARCHAR(100)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_car_model VARCHAR(100)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_car_year  INTEGER",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_car_type  cartype NOT NULL DEFAULT 'sedan'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified     BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_token VARCHAR(64)",
    # Mark all users registered before email verification was introduced as already verified
    "UPDATE users SET email_verified = TRUE WHERE email_verified = FALSE AND email_verify_token IS NULL",

    # ── bookings ──────────────────────────────────────────────────────────────
    "ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'no_show'",
    "ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'card_saved'",

    # ── trips ─────────────────────────────────────────────────────────────────
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS driver_no_show  BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS reminder_sent   BOOLEAN NOT NULL DEFAULT FALSE",

    # ── reviews ───────────────────────────────────────────────────────────────
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS is_auto BOOLEAN NOT NULL DEFAULT FALSE",

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
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS quiet_ride      BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS large_luggage   BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS chat_ok         BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS winter_ready    BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS child_seat      BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS flexible_pickup BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS instant_book    BOOLEAN NOT NULL DEFAULT TRUE",
    # Chattiness scale — replaces the separate quiet_ride / chat_ok booleans
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS chattiness VARCHAR(10)",
    "UPDATE trips SET chattiness = 'quiet'  WHERE quiet_ride = TRUE  AND chattiness IS NULL",
    "UPDATE trips SET chattiness = 'chatty' WHERE chat_ok = TRUE AND quiet_ride = FALSE AND chattiness IS NULL",

    # ── bookings ──────────────────────────────────────────────────────────────
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS message          TEXT",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS service_fee      INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMP NOT NULL DEFAULT now()",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_deadline TIMESTAMP",
    # Segment booking — passenger boards/exits at a stop that differs from the trip endpoints
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS pickup_city  VARCHAR(150)",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS dropoff_city VARCHAR(150)",

    # ── payments ──────────────────────────────────────────────────────────────
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_amount INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS card_last4   VARCHAR(4)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS card_brand   VARCHAR(20)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMP NOT NULL DEFAULT now()",
    # PaymentStatus enum — add new values idempotently
    "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'pending'",
    "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'card_saved'",
    "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'failed'",
    "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'auth_expired'",
    "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'retry_pending'",
    # Refund lifecycle states — separate requested/failed/succeeded
    "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'refund_requested'",
    "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'refund_failed'",
    # Capture lifecycle — intermediate state between API call and webhook confirmation
    "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'capture_requested'",
    # Rapyd-specific columns on payments
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS rapyd_payment_id        VARCHAR(255)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS rapyd_customer_id       VARCHAR(255)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS rapyd_payment_method_id VARCHAR(255)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS rapyd_checkout_id       VARCHAR(255)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS idempotency_key         VARCHAR(64)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_case            VARCHAR(1)",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS auth_expires_at         TIMESTAMP",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS capture_at              TIMESTAMP",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS auth_scheduled_for      TIMESTAMP",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS retry_deadline          TIMESTAMP",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS retry_fee_applied       BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS seen_webhook_ids        TEXT",

    # ── newsletter_subscribers table ─────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS newsletter_subscribers (
        id             SERIAL  PRIMARY KEY,
        email          VARCHAR(255) NOT NULL UNIQUE,
        source         VARCHAR(50),
        discount_used  BOOLEAN NOT NULL DEFAULT FALSE,
        created_at     TIMESTAMP NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_newsletter_email ON newsletter_subscribers(email)",
    "ALTER TABLE newsletter_subscribers ADD COLUMN IF NOT EXISTS discount_used BOOLEAN NOT NULL DEFAULT FALSE",

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

    # ── ride_alerts table ─────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS ride_alerts (
        id               SERIAL  PRIMARY KEY,
        user_id          INTEGER REFERENCES users(id) ON DELETE CASCADE,
        email            VARCHAR(255) NOT NULL,
        origin           VARCHAR(150) NOT NULL,
        destination      VARCHAR(150) NOT NULL,
        travel_date      DATE,
        seats            SMALLINT NOT NULL DEFAULT 1,
        token            VARCHAR(64) NOT NULL UNIQUE,
        is_active        BOOLEAN NOT NULL DEFAULT TRUE,
        last_notified_at TIMESTAMP,
        created_at       TIMESTAMP NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_ride_alerts_email   ON ride_alerts(email)",
    "CREATE INDEX IF NOT EXISTS ix_ride_alerts_user_id ON ride_alerts(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_ride_alerts_active  ON ride_alerts(is_active)",

    # ── Payout ledger — enum types ────────────────────────────────────────────
    """DO $$ BEGIN CREATE TYPE payoutmethod AS ENUM ('blikk','stripe_connect');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN CREATE TYPE payoutitemstatus AS ENUM
           ('pending','payout_ready','payout_sent','payout_confirmed',
            'payout_failed','retry_ready','reversed','cancelled');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN CREATE TYPE driverpayoutstatus AS ENUM
           ('pending','sent','confirmed','failed','reversed');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN CREATE TYPE ledgerentrytype AS ENUM
           ('driver_payable_created','platform_fee_retained',
            'driver_payout_ready','driver_payout_batched',
            'driver_payout_sent','driver_payout_confirmed',
            'driver_payout_failed','driver_payout_reversed',
            'driver_balance_adjustment','payout_item_cancelled');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",

    # ── users — payout configuration columns ─────────────────────────────────
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_method      payoutmethod",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS blikk_account_iban VARCHAR(34)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_account_id  VARCHAR(255)",

    # ── driver_payouts table (created before payout_items which FK to it) ────
    """CREATE TABLE IF NOT EXISTS driver_payouts (
        id                  SERIAL       PRIMARY KEY,
        driver_id           INTEGER      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        amount              INTEGER      NOT NULL,
        currency            VARCHAR(3)   NOT NULL DEFAULT 'ISK',
        payout_method       payoutmethod,
        status              driverpayoutstatus NOT NULL DEFAULT 'pending',
        idempotency_key     VARCHAR(64)  NOT NULL UNIQUE,
        provider_payout_id  VARCHAR(255),
        provider_response   TEXT,
        failure_reason      TEXT,
        sent_at             TIMESTAMP,
        confirmed_at        TIMESTAMP,
        failed_at           TIMESTAMP,
        created_at          TIMESTAMP    NOT NULL DEFAULT now(),
        updated_at          TIMESTAMP    NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_driver_payouts_driver_id ON driver_payouts(driver_id)",
    "CREATE INDEX IF NOT EXISTS ix_driver_payouts_status    ON driver_payouts(status)",
    # Repair migration for deployments that ran the original CREATE TABLE before
    # the payout_method type was corrected (it was wrongly driverpayoutstatus).
    # Only executes when the column has the wrong type; fails loudly if the ALTER
    # cannot complete (e.g. incompatible non-null values) so the problem is not
    # silently swallowed at startup.
    """DO $$ BEGIN
         IF EXISTS (
             SELECT 1 FROM information_schema.columns
             WHERE table_name  = 'driver_payouts'
               AND column_name = 'payout_method'
               AND udt_name    = 'driverpayoutstatus'
         ) THEN
             ALTER TABLE driver_payouts
                 ALTER COLUMN payout_method TYPE payoutmethod
                 USING payout_method::text::payoutmethod;
         END IF;
       END $$""",

    # ── payout_items table ────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS payout_items (
        id               SERIAL      PRIMARY KEY,
        payment_id       INTEGER     NOT NULL UNIQUE REFERENCES payments(id) ON DELETE RESTRICT,
        booking_id       INTEGER     NOT NULL REFERENCES bookings(id)  ON DELETE RESTRICT,
        driver_id        INTEGER     NOT NULL REFERENCES users(id)     ON DELETE RESTRICT,
        driver_payout_id INTEGER     REFERENCES driver_payouts(id)     ON DELETE SET NULL,
        amount           INTEGER     NOT NULL,
        platform_fee     INTEGER     NOT NULL,
        passenger_total  INTEGER     NOT NULL,
        payout_method    payoutmethod,
        status           payoutitemstatus NOT NULL DEFAULT 'pending',
        idempotency_key  VARCHAR(64) NOT NULL UNIQUE,
        created_at       TIMESTAMP   NOT NULL DEFAULT now(),
        updated_at       TIMESTAMP   NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_payout_items_driver_id ON payout_items(driver_id)",
    "CREATE INDEX IF NOT EXISTS ix_payout_items_status    ON payout_items(status)",

    # ── payout_ledger table ───────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS payout_ledger (
        id               SERIAL      PRIMARY KEY,
        entry_type       ledgerentrytype NOT NULL,
        payment_id       INTEGER     REFERENCES payments(id),
        payout_item_id   INTEGER     REFERENCES payout_items(id),
        driver_payout_id INTEGER     REFERENCES driver_payouts(id),
        booking_id       INTEGER     REFERENCES bookings(id),
        driver_id        INTEGER     REFERENCES users(id),
        amount           INTEGER     NOT NULL,
        currency         VARCHAR(3)  NOT NULL DEFAULT 'ISK',
        note             TEXT,
        created_at       TIMESTAMP   NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_payout_ledger_driver_id  ON payout_ledger(driver_id)",
    "CREATE INDEX IF NOT EXISTS ix_payout_ledger_payment_id ON payout_ledger(payment_id)",
    "CREATE INDEX IF NOT EXISTS ix_payout_ledger_entry_type ON payout_ledger(entry_type)",
    "CREATE INDEX IF NOT EXISTS ix_payout_ledger_created_at ON payout_ledger(created_at)",

    # ── Ledger entry type additions ───────────────────────────────────────────
    # passenger_refund_confirmed was added after the initial ledgerentrytype CREATE,
    # so it must be added via ALTER TYPE for existing deployments.
    "ALTER TYPE ledgerentrytype ADD VALUE IF NOT EXISTS 'passenger_refund_confirmed'",

    # ── Pricing module ────────────────────────────────────────────────────────
    """DO $$ BEGIN CREATE TYPE fueltype AS ENUM ('petrol','diesel','electric','hybrid');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",

    # ── fuel_price_cache — audit log of apis.is fetches ───────────────────────
    """CREATE TABLE IF NOT EXISTS fuel_price_cache (
        id            SERIAL      PRIMARY KEY,
        fuel_type     VARCHAR(20) NOT NULL DEFAULT 'petrol',
        p80_price     FLOAT       NOT NULL,
        median_price  FLOAT,
        station_count INTEGER,
        source        VARCHAR(50) NOT NULL DEFAULT 'apis_is',
        fetched_at    TIMESTAMP   NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_fuel_price_cache_fuel_type  ON fuel_price_cache(fuel_type)",
    "CREATE INDEX IF NOT EXISTS ix_fuel_price_cache_fetched_at ON fuel_price_cache(fetched_at)",

    # ── pricing_policy — versioned cost constants ─────────────────────────────
    """CREATE TABLE IF NOT EXISTS pricing_policy (
        id                                SERIAL    PRIMARY KEY,
        effective_from                    DATE      NOT NULL,
        effective_to                      DATE,
        kilometragjald_standard           FLOAT     NOT NULL,
        kilometragjald_heavy              FLOAT,
        consumption_small                 FLOAT     NOT NULL,
        consumption_standard              FLOAT     NOT NULL,
        consumption_suv                   FLOAT     NOT NULL,
        consumption_van                   FLOAT     NOT NULL,
        ev_consumption_standard           FLOAT     NOT NULL,
        ev_consumption_suv                FLOAT     NOT NULL,
        electricity_price_isk_per_kwh     FLOAT     NOT NULL,
        wear_and_tear_isk_per_km          FLOAT     NOT NULL,
        real_depreciation_isk_per_km      FLOAT     NOT NULL,
        depreciation_factor               FLOAT     NOT NULL,
        platform_cost_cap_isk_per_km      FLOAT     NOT NULL,
        rounding_unit                     INTEGER   NOT NULL DEFAULT 50,
        fuel_price_fallback_isk_per_liter FLOAT     NOT NULL,
        fuel_price_min_isk_per_liter      FLOAT     NOT NULL,
        fuel_price_max_isk_per_liter      FLOAT     NOT NULL,
        created_at                        TIMESTAMP NOT NULL DEFAULT now(),
        notes                             TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_pricing_policy_effective_from ON pricing_policy(effective_from)",

    # ── routes — canonical city-pair distances ────────────────────────────────
    """CREATE TABLE IF NOT EXISTS routes (
        id               SERIAL       PRIMARY KEY,
        origin           VARCHAR(150) NOT NULL,
        destination      VARCHAR(150) NOT NULL,
        distance_km      FLOAT        NOT NULL,
        duration_min     INTEGER,
        polyline         TEXT,
        source           VARCHAR(50),
        last_verified_at TIMESTAMP,
        is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
        created_at       TIMESTAMP    NOT NULL DEFAULT now(),
        CONSTRAINT uq_routes_origin_destination UNIQUE (origin, destination)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_routes_origin      ON routes(origin)",
    "CREATE INDEX IF NOT EXISTS ix_routes_destination ON routes(destination)",
    "CREATE INDEX IF NOT EXISTS ix_routes_active      ON routes(is_active)",

    # ── trips — pricing module columns ────────────────────────────────────────
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS fuel_type      fueltype",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS price_snapshot TEXT",

    # ── pricing_policy seed — 2026 Iceland baseline ───────────────────────────
    # Only inserts when the table is empty, so re-running migrations is safe.
    # Sources:
    #   kílómetragjald: island.is/kilometragjald (standard cars < 3.5 t: 6.95 ISK/km)
    #   consumption:    Icelandic Transport Authority averages by class
    #   wear/tear:      Conservative fleet-cost estimate (tyres, brakes, filters)
    #   depreciation:   40 % of marginal usage depreciation (driver owns car)
    #   electricity:    ~25 ISK/kWh national average (Orkusalan / ON Power)
    #   fuel fallback:  290 ISK/L — conservative 2026 petrol estimate
    """INSERT INTO pricing_policy (
        effective_from, effective_to,
        kilometragjald_standard, kilometragjald_heavy,
        consumption_small, consumption_standard, consumption_suv, consumption_van,
        ev_consumption_standard, ev_consumption_suv, electricity_price_isk_per_kwh,
        wear_and_tear_isk_per_km, real_depreciation_isk_per_km, depreciation_factor,
        platform_cost_cap_isk_per_km, rounding_unit,
        fuel_price_fallback_isk_per_liter,
        fuel_price_min_isk_per_liter, fuel_price_max_isk_per_liter,
        created_at, notes
    ) SELECT
        '2026-01-01', NULL,
        6.95, 9.50,
        6.5, 8.0, 10.5, 12.0,
        18.0, 22.0, 25.0,
        7.0, 4.0, 0.40,
        45.0, 50,
        290.0, 150.0, 600.0,
        now(),
        '2026 baseline — kílómetragjald 6.95 ISK/km per island.is/kilometragjald; '
        'consumption defaults from Samgöngustofa class averages; '
        'depreciation factor 40 % of marginal usage cost; '
        'full methodology at /pricing/how-it-works'
    WHERE NOT EXISTS (SELECT 1 FROM pricing_policy)""",

    # ── routes seed — Iceland major corridors (approximate, needs API verification) ──
    # Distances are road distances in km, rounded to nearest km.
    # source='seeded_approximate' and last_verified_at=NULL flags these for
    # later verification against a live routing API.
    # Both directions are seeded; durations are approximate (not traffic-aware).
    # City names must exactly match ICELANDIC_CITIES in app/routers/trips.py.
    """INSERT INTO routes (origin, destination, distance_km, duration_min, source, is_active, created_at) VALUES
        -- Capital area out
        ('Reykjavík', 'Keflavík',             51,  38, 'seeded_approximate', TRUE, now()),
        ('Keflavík',  'Reykjavík',             51,  38, 'seeded_approximate', TRUE, now()),
        -- South
        ('Reykjavík', 'Hveragerði',            45,  38, 'seeded_approximate', TRUE, now()),
        ('Hveragerði','Reykjavík',             45,  38, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Selfoss',               61,  50, 'seeded_approximate', TRUE, now()),
        ('Selfoss',   'Reykjavík',             61,  50, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Hella',                119,  95, 'seeded_approximate', TRUE, now()),
        ('Hella',     'Reykjavík',            119,  95, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Vík',                  187, 150, 'seeded_approximate', TRUE, now()),
        ('Vík',       'Reykjavík',            187, 150, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Kirkjubæjarklaustur',  265, 215, 'seeded_approximate', TRUE, now()),
        ('Kirkjubæjarklaustur','Reykjavík',   265, 215, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Höfn',                 445, 355, 'seeded_approximate', TRUE, now()),
        ('Höfn',      'Reykjavík',            445, 355, 'seeded_approximate', TRUE, now()),
        -- South cross
        ('Selfoss',   'Höfn',                 257, 225, 'seeded_approximate', TRUE, now()),
        ('Höfn',      'Selfoss',              257, 225, 'seeded_approximate', TRUE, now()),
        ('Selfoss',   'Vík',                  126, 100, 'seeded_approximate', TRUE, now()),
        ('Vík',       'Selfoss',              126, 100, 'seeded_approximate', TRUE, now()),
        ('Vík',       'Kirkjubæjarklaustur',   75,  60, 'seeded_approximate', TRUE, now()),
        ('Kirkjubæjarklaustur','Vík',          75,  60, 'seeded_approximate', TRUE, now()),
        ('Kirkjubæjarklaustur','Höfn',        185, 150, 'seeded_approximate', TRUE, now()),
        ('Höfn',      'Kirkjubæjarklaustur',  185, 150, 'seeded_approximate', TRUE, now()),
        -- West
        ('Reykjavík', 'Borgarnes',             73,  58, 'seeded_approximate', TRUE, now()),
        ('Borgarnes', 'Reykjavík',             73,  58, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Stykkishólmur',        175, 145, 'seeded_approximate', TRUE, now()),
        ('Stykkishólmur','Reykjavík',         175, 145, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Ólafsvík',             195, 165, 'seeded_approximate', TRUE, now()),
        ('Ólafsvík',  'Reykjavík',            195, 165, 'seeded_approximate', TRUE, now()),
        ('Borgarnes', 'Stykkishólmur',        100,  80, 'seeded_approximate', TRUE, now()),
        ('Stykkishólmur','Borgarnes',         100,  80, 'seeded_approximate', TRUE, now()),
        -- Ring Road north
        ('Reykjavík', 'Blönduós',             193, 160, 'seeded_approximate', TRUE, now()),
        ('Blönduós',  'Reykjavík',            193, 160, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Sauðárkrókur',         256, 215, 'seeded_approximate', TRUE, now()),
        ('Sauðárkrókur','Reykjavík',          256, 215, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Akureyri',             391, 290, 'seeded_approximate', TRUE, now()),
        ('Akureyri',  'Reykjavík',            391, 290, 'seeded_approximate', TRUE, now()),
        -- West Fjords
        ('Reykjavík', 'Ísafjörður',           459, 420, 'seeded_approximate', TRUE, now()),
        ('Ísafjörður','Reykjavík',            459, 420, 'seeded_approximate', TRUE, now()),
        -- North from Akureyri
        ('Akureyri',  'Húsavík',               89,  72, 'seeded_approximate', TRUE, now()),
        ('Húsavík',   'Akureyri',              89,  72, 'seeded_approximate', TRUE, now()),
        ('Akureyri',  'Mývatn',               100,  88, 'seeded_approximate', TRUE, now()),
        ('Mývatn',    'Akureyri',             100,  88, 'seeded_approximate', TRUE, now()),
        ('Akureyri',  'Siglufjörður',          75,  65, 'seeded_approximate', TRUE, now()),
        ('Siglufjörður','Akureyri',            75,  65, 'seeded_approximate', TRUE, now()),
        ('Akureyri',  'Sauðárkrókur',         116, 100, 'seeded_approximate', TRUE, now()),
        ('Sauðárkrókur','Akureyri',           116, 100, 'seeded_approximate', TRUE, now()),
        ('Akureyri',  'Blönduós',             148, 125, 'seeded_approximate', TRUE, now()),
        ('Blönduós',  'Akureyri',             148, 125, 'seeded_approximate', TRUE, now()),
        -- East
        ('Akureyri',  'Egilsstaðir',          261, 220, 'seeded_approximate', TRUE, now()),
        ('Egilsstaðir','Akureyri',            261, 220, 'seeded_approximate', TRUE, now()),
        ('Reykjavík', 'Egilsstaðir',          697, 560, 'seeded_approximate', TRUE, now()),
        ('Egilsstaðir','Reykjavík',           697, 560, 'seeded_approximate', TRUE, now())
    ON CONFLICT (origin, destination) DO NOTHING""",

    # ── Intermediate-corridor routes (needed for partial-stop matching) ────────
    # These connect cities that are *between* the existing long-haul endpoints.
    # All distances are derived by subtracting overlapping seeded legs so the
    # triangle inequality holds within the 10 % tolerance used by is_on_route().
    # ON CONFLICT DO NOTHING makes this fully idempotent.
    """INSERT INTO routes
         (origin, destination, distance_km, duration_min, source, is_active, created_at)
       VALUES
         -- Ring Road west/north: Reykjavík ─ Borgarnes ─ Blönduós ─ Sauðárkrókur ─ Akureyri
         ('Borgarnes',    'Blönduós',      120, 100, 'seeded_approximate', TRUE, now()),
         ('Blönduós',     'Borgarnes',     120, 100, 'seeded_approximate', TRUE, now()),
         ('Borgarnes',    'Sauðárkrókur',  183, 155, 'seeded_approximate', TRUE, now()),
         ('Sauðárkrókur', 'Borgarnes',     183, 155, 'seeded_approximate', TRUE, now()),
         ('Borgarnes',    'Akureyri',      318, 230, 'seeded_approximate', TRUE, now()),
         ('Akureyri',     'Borgarnes',     318, 230, 'seeded_approximate', TRUE, now()),
         ('Blönduós',     'Sauðárkrókur',   63,  55, 'seeded_approximate', TRUE, now()),
         ('Sauðárkrókur', 'Blönduós',       63,  55, 'seeded_approximate', TRUE, now()),
         -- Ring Road east: Akureyri ─ Mývatn ─ Egilsstaðir ─ Höfn
         ('Mývatn',       'Egilsstaðir',   161, 130, 'seeded_approximate', TRUE, now()),
         ('Egilsstaðir',  'Mývatn',        161, 130, 'seeded_approximate', TRUE, now()),
         ('Egilsstaðir',  'Höfn',          252, 205, 'seeded_approximate', TRUE, now()),
         ('Höfn',         'Egilsstaðir',   252, 205, 'seeded_approximate', TRUE, now()),
         -- South coast: Höfn ─ Vík ─ Kirkjubæjarklaustur ─ Selfoss ─ Reykjavík
         ('Höfn',         'Vík',           260, 210, 'seeded_approximate', TRUE, now()),
         ('Vík',          'Höfn',          260, 210, 'seeded_approximate', TRUE, now()),
         ('Kirkjubæjarklaustur', 'Selfoss', 201, 160, 'seeded_approximate', TRUE, now()),
         ('Selfoss',      'Kirkjubæjarklaustur', 201, 160, 'seeded_approximate', TRUE, now()),
         -- Hveragerði ─ Selfoss short hop (Hveragerði is between Reykjavík and Selfoss)
         ('Hveragerði',   'Selfoss',        16,  15, 'seeded_approximate', TRUE, now()),
         ('Selfoss',      'Hveragerði',     16,  15, 'seeded_approximate', TRUE, now()),
         -- Snæfellsnes peninsula: Borgarnes ─ Stykkishólmur ─ Ólafsvík
         ('Borgarnes',    'Ólafsvík',      122, 105, 'seeded_approximate', TRUE, now()),
         ('Ólafsvík',     'Borgarnes',     122, 105, 'seeded_approximate', TRUE, now()),
         ('Stykkishólmur','Ólafsvík',       20,  20, 'seeded_approximate', TRUE, now()),
         ('Ólafsvík',     'Stykkishólmur',  20,  20, 'seeded_approximate', TRUE, now()),
         -- North Iceland: Mývatn ─ Húsavík
         ('Mývatn',       'Húsavík',        60,  55, 'seeded_approximate', TRUE, now()),
         ('Húsavík',      'Mývatn',         60,  55, 'seeded_approximate', TRUE, now())
       ON CONFLICT (origin, destination) DO NOTHING""",

    # ── rename legacy route names to match city list ──────────────────────────
    # Hvolsvöllur was the original seed name; Hella is the city in the dropdown.
    # Snæfellsnes is a peninsula, not a city; Stykkishólmur is the main town.
    # These UPDATEs are idempotent — if the rows don't exist they affect 0 rows.
    "UPDATE routes SET origin      = 'Hella'         WHERE origin      = 'Hvolsvöllur'",
    "UPDATE routes SET destination = 'Hella'         WHERE destination = 'Hvolsvöllur'",
    "UPDATE routes SET origin      = 'Stykkishólmur' WHERE origin      = 'Snæfellsnes'",
    "UPDATE routes SET destination = 'Stykkishólmur' WHERE destination = 'Snæfellsnes'",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create any brand-new tables defined in models
    Base.metadata.create_all(bind=engine)
    # Apply column-level migrations that create_all() won't handle
    with engine.begin() as conn:
        for stmt in _MIGRATIONS:
            conn.execute(text(stmt))
    # Startup sweep — same ordering as the periodic loop (see tasks.py).
    # Capture first so overdue auths are settled before bookings are completed.
    # Payout tasks last so they see the freshly-completed booking statuses.
    _run_capture_payments()
    _run_retry_refunds()
    _run_auto_complete()
    _run_auto_ratings()
    _run_create_payout_items()
    _run_advance_payout_items()
    _run_refresh_fuel_price()   # prime the fuel price cache on startup
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
app.include_router(alerts.router)
app.include_router(trips.router)
app.include_router(bookings.router)
app.include_router(payments.router)
app.include_router(users.router)
app.include_router(language.router)
app.include_router(verification.router)
app.include_router(messages.router)
app.include_router(reviews.router)
app.include_router(newsletter.router)
app.include_router(phone.router)
app.include_router(webhooks.router)


templates = Jinja2Templates(directory="templates")


# ── SEO ───────────────────────────────────────────────────────────────────────

@app.get("/robots.txt", include_in_schema=False)
def robots():
    return FileResponse("static/robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    base = get_settings().base_url.rstrip("/")
    db: Session = SessionLocal()
    try:
        # All active upcoming trips
        trips = (
            db.query(models.Trip)
            .filter(
                models.Trip.status == models.TripStatus.active,
                models.Trip.departure_datetime >= datetime.utcnow(),
            )
            .order_by(models.Trip.departure_datetime)
            .all()
        )
    finally:
        db.close()

    static_urls = [
        ("", "daily",  "1.0"),
        ("/trips",  "hourly", "0.9"),
        ("/terms",  "monthly","0.3"),
        ("/privacy","monthly","0.3"),
    ]

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']

    for path, changefreq, priority in static_urls:
        lines.append(f"""  <url>
    <loc>{base}{path}</loc>
    <changefreq>{changefreq}</changefreq>
    <priority>{priority}</priority>
  </url>""")

    for trip in trips:
        mod = trip.departure_datetime.strftime("%Y-%m-%d")
        lines.append(f"""  <url>
    <loc>{base}/trips/{trip.id}</loc>
    <lastmod>{mod}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.8</priority>
  </url>""")

    lines.append("</urlset>")
    return Response("\n".join(lines), media_type="application/xml")


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
            .options(
                joinedload(models.Trip.driver).joinedload(models.User.reviews_received),
                joinedload(models.Trip.driver).selectinload(models.User.trips),
            )
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


@app.exception_handler(429)
async def rate_limit_handler(request: Request, exc):
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return templates.TemplateResponse(
            "errors/429.html",
            {"request": request, "current_user": None, "retry_after": None},
            status_code=429,
        )
    return Response(
        content=getattr(exc, "detail", "Too many requests."),
        status_code=429,
    )
