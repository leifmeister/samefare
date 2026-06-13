"""
Pre-launch preflight:
  1. Report stub-ID (beta) payments
  2. Unverify all non-admin, non-deleted users
     (email_verified, id_verification, license_verification, didit sessions)
"""
from app.database import SessionLocal
from app import models

db = SessionLocal()

# ── 1. Stub payments ──────────────────────────────────────────────────────────
stubs = db.query(models.Payment).filter(
    (models.Payment.rapyd_customer_id == "beta-customer") |
    (models.Payment.rapyd_payment_method_id == "beta-pm")
).all()
print(f"Stub-ID payments: {len(stubs)}")
for p in stubs:
    b = p.booking
    print(f"  booking {b.id} | booking_status={b.status.value} | payment_status={p.status.value} | passenger={b.passenger.email}")

# ── 2. Unverify non-admin users ───────────────────────────────────────────────
users = db.query(models.User).filter(
    models.User.is_admin == False,
    models.User.deleted_at == None,
).all()

print(f"\nUnverifying {len(users)} non-admin user(s):")
for u in users:
    print(f"  {u.email}")
    u.email_verified              = False
    u.id_verification             = models.VerificationStatus.unverified
    u.license_verification        = models.VerificationStatus.unverified
    u.id_rejection_reason         = None
    u.license_rejection_reason    = None
    u.didit_identity_session_id   = None
    u.didit_licence_session_id    = None
    u.licence_expiry              = None
    u.licence_expiry_warned_at    = None

db.commit()
print("\nDone.")
db.close()
