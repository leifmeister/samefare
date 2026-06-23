#!/usr/bin/env python3
"""
Database migration script — safe to run on every deploy.
All operations use IF NOT EXISTS / DO-EXCEPTION blocks so they are idempotent.
"""
import os
import sys
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    # Fall back to config when running locally
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from app.config import get_settings
    DATABASE_URL = get_settings().database_url

# Railway injects postgres:// but psycopg2 needs postgresql://
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def migrate() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor()

    steps: list[tuple[str, str]] = []

    # ── 1. Enum types ──────────────────────────────────────────────────────────
    steps.append(("enum: verificationstatus", """
        DO $$ BEGIN
            CREATE TYPE verificationstatus AS ENUM
                ('unverified', 'pending', 'approved', 'rejected');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    # ── 2. trips ───────────────────────────────────────────────────────────────
    steps.append(("trips.pickup_address",
        "ALTER TABLE trips ADD COLUMN IF NOT EXISTS pickup_address  VARCHAR(255)"))
    steps.append(("trips.dropoff_address",
        "ALTER TABLE trips ADD COLUMN IF NOT EXISTS dropoff_address VARCHAR(255)"))
    steps.append(("trips.instant_book",
        "ALTER TABLE trips ADD COLUMN IF NOT EXISTS instant_book BOOLEAN NOT NULL DEFAULT TRUE"))

    # ── 3. users ───────────────────────────────────────────────────────────────
    steps.append(("users.phone_verified",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified    BOOLEAN NOT NULL DEFAULT FALSE"))
    steps.append(("users.phone_otp",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_otp         VARCHAR(6)"))
    steps.append(("users.phone_otp_expires",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_otp_expires TIMESTAMP"))
    steps.append(("users.reset_token",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token         VARCHAR(64)"))
    steps.append(("users.reset_token_expires",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires TIMESTAMP"))
    steps.append(("users.id_verification",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_verification      verificationstatus NOT NULL DEFAULT 'unverified'"))
    steps.append(("users.license_verification",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS license_verification verificationstatus NOT NULL DEFAULT 'unverified'"))
    steps.append(("users.id_doc_filename",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_doc_filename      VARCHAR(255)"))
    steps.append(("users.license_doc_filename",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS license_doc_filename VARCHAR(255)"))
    steps.append(("users.id_rejection_reason",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_rejection_reason      TEXT"))
    steps.append(("users.license_rejection_reason",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS license_rejection_reason TEXT"))
    steps.append(("users.default_car_make",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_car_make  VARCHAR(100)"))
    steps.append(("users.default_car_model",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_car_model VARCHAR(100)"))
    steps.append(("users.default_car_year",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_car_year  INTEGER"))
    steps.append(("users.default_car_type",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_car_type  cartype NOT NULL DEFAULT 'sedan'"))

    steps.append(("users.didit_identity_session_id",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS didit_identity_session_id VARCHAR(64)"))
    steps.append(("users.didit_licence_session_id",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS didit_licence_session_id  VARCHAR(64)"))

    # Driver accountability columns
    steps.append(("users.cancellations_90d",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS cancellations_90d      INTEGER NOT NULL DEFAULT 0"))
    steps.append(("users.late_cancellations_90d",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS late_cancellations_90d INTEGER NOT NULL DEFAULT 0"))
    steps.append(("users.no_shows_confirmed",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS no_shows_confirmed     INTEGER NOT NULL DEFAULT 0"))
    steps.append(("users.posting_suspended",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS posting_suspended      BOOLEAN NOT NULL DEFAULT FALSE"))
    # AML retention deadline — null for active accounts; set to deleted_at + 5 years on soft-delete.
    steps.append(("users.aml_retain_until",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS aml_retain_until       TIMESTAMP"))
    # Licence expiry — extracted from Didit document data at approval time.
    steps.append(("users.licence_expiry",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS licence_expiry         DATE"))
    steps.append(("users.licence_expiry_warned_at",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS licence_expiry_warned_at TIMESTAMP"))

    # ── 4. bookings ────────────────────────────────────────────────────────────
    steps.append(("bookings.service_fee",
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS service_fee INTEGER NOT NULL DEFAULT 0"))

    # ── 5. payments ────────────────────────────────────────────────────────────
    steps.append(("payments.card_last4",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS card_last4    VARCHAR(4)"))
    steps.append(("payments.card_brand",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS card_brand    VARCHAR(20)"))
    steps.append(("payments.refund_amount",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_amount INTEGER NOT NULL DEFAULT 0"))

    # ── 6. messages table (new) ────────────────────────────────────────────────
    steps.append(("table: messages", """
        CREATE TABLE IF NOT EXISTS messages (
            id         SERIAL PRIMARY KEY,
            booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
            sender_id  INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
            body       TEXT    NOT NULL,
            is_read    BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """))
    steps.append(("index: ix_messages_booking_id",
        "CREATE INDEX IF NOT EXISTS ix_messages_booking_id ON messages(booking_id)"))
    steps.append(("index: ix_messages_sender_id",
        "CREATE INDEX IF NOT EXISTS ix_messages_sender_id  ON messages(sender_id)"))

    # ── 7. avatar blobs (persist across Railway's ephemeral filesystem) ────────
    # Profile photos used to be written to static/avatars on the container disk,
    # which is wiped on every redeploy — so they vanished. Store the bytes in the
    # DB instead, in their own table so the users row stays lean.
    steps.append(("table: user_avatars", """
        CREATE TABLE IF NOT EXISTS user_avatars (
            user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            data         BYTEA       NOT NULL,
            content_type VARCHAR(40) NOT NULL DEFAULT 'image/jpeg',
            updated_at   TIMESTAMP   NOT NULL DEFAULT now()
        )
    """))
    # Old avatar_url values point at static/avatars files that no longer exist on
    # disk — clear them so the UI shows the placeholder instead of a broken image.
    # New uploads set avatar_url to the /u/<id>/avatar serve route.
    steps.append(("clear stale avatar_url",
        "UPDATE users SET avatar_url = NULL WHERE avatar_url LIKE '/static/avatars/%'"))

    # ── 8. admin verification locks ────────────────────────────────────────────
    # A manual admin approve/reject locks the verification so a later Didit
    # webhook can't silently overturn the human decision.
    steps.append(("users.id_verification_locked",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS id_verification_locked      BOOLEAN NOT NULL DEFAULT FALSE"))
    steps.append(("users.license_verification_locked",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS license_verification_locked BOOLEAN NOT NULL DEFAULT FALSE"))

    # ── 9. public ride-request board (demand visible to drivers) ───────────────
    steps.append(("ride_alerts.is_public",
        "ALTER TABLE ride_alerts ADD COLUMN IF NOT EXISTS is_public    BOOLEAN NOT NULL DEFAULT FALSE"))
    steps.append(("ride_alerts.note",
        "ALTER TABLE ride_alerts ADD COLUMN IF NOT EXISTS note         VARCHAR(120)"))
    steps.append(("ride_alerts.fulfilled_at",
        "ALTER TABLE ride_alerts ADD COLUMN IF NOT EXISTS fulfilled_at TIMESTAMP"))
    steps.append(("ix_ride_alerts_public",
        "CREATE INDEX IF NOT EXISTS ix_ride_alerts_public ON ride_alerts(is_public)"))

    # ── Run ────────────────────────────────────────────────────────────────────
    for label, sql in steps:
        cur.execute(sql)
        print(f"  ok  {label}")

    conn.commit()
    cur.close()
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
