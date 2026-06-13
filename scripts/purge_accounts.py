"""
HARD-PURGE every account except a small keep-list, plus ALL data belonging to
the purged accounts (trips, bookings, payments, reviews, messages, ride alerts,
payout items/payouts, ledger entries, reports).

This is irreversible. It does NOT use the in-app soft-delete/anonymise flow —
rows are physically removed. Use only for wiping test data before launch.

Dry-run by default: prints the keep-list and the kill-list and exits.
Pass --apply to actually delete (single transaction; rolls back on any error).

    python scripts/purge_accounts.py            # preview only
    python scripts/purge_accounts.py --apply     # really delete

Keep-list is matched on User.full_name (exact). If any keeper name is missing
the script ABORTS — better to fix the spelling than silently nuke a keeper.
"""
import sys

from app.database import SessionLocal
from app import models

KEEP_NAMES = [
    "Páll Arnar Guðmundsson",
    "Benedikt Bjarnason",
    "Leifur Thorsteinsson",
    "Felix Jung",
]

APPLY = "--apply" in sys.argv

db = SessionLocal()


def ids(rows):
    return [r[0] for r in rows]


# ── Resolve keep-list ─────────────────────────────────────────────────────────
keep_users = db.query(models.User).filter(models.User.full_name.in_(KEEP_NAMES)).all()
found_names = {u.full_name for u in keep_users}
missing = [n for n in KEEP_NAMES if n not in found_names]

print("KEEP (matched by full_name):")
for u in keep_users:
    admin = " [admin]" if u.is_admin else ""
    print(f"  ✓ id={u.id:<5} {u.full_name!r:<32} {u.email}{admin}")

if missing:
    print("\n✗ ABORT — these keep-list names matched no account:")
    for n in missing:
        print(f"    {n!r}")
    print("  Fix the spelling (User.full_name is matched exactly) and re-run.")
    db.close()
    sys.exit(1)

keep_ids = [u.id for u in keep_users]

# ── Resolve doomed sets (compute every id-set explicitly so this is correct ────
#    regardless of DB engine / whether FK cascade is enforced) ─────────────────
doomed_users = db.query(models.User).filter(~models.User.id.in_(keep_ids)).all()
doomed_user_ids = [u.id for u in doomed_users]

if not doomed_user_ids:
    print("\nNothing to delete — every account is on the keep-list.")
    db.close()
    sys.exit(0)

doomed_trip_ids = ids(
    db.query(models.Trip.id).filter(models.Trip.driver_id.in_(doomed_user_ids)).all()
)
doomed_booking_ids = ids(
    db.query(models.Booking.id).filter(
        models.Booking.passenger_id.in_(doomed_user_ids)
        | models.Booking.trip_id.in_(doomed_trip_ids)
    ).all()
)
doomed_payment_ids = ids(
    db.query(models.Payment.id).filter(
        models.Payment.booking_id.in_(doomed_booking_ids)
    ).all()
)
doomed_payout_item_ids = ids(
    db.query(models.PayoutItem.id).filter(
        models.PayoutItem.driver_id.in_(doomed_user_ids)
        | models.PayoutItem.payment_id.in_(doomed_payment_ids)
        | models.PayoutItem.booking_id.in_(doomed_booking_ids)
    ).all()
)
doomed_driver_payout_ids = ids(
    db.query(models.DriverPayout.id).filter(
        models.DriverPayout.driver_id.in_(doomed_user_ids)
    ).all()
)

print(f"\nKILL — {len(doomed_user_ids)} account(s) and all their data:")
for u in doomed_users:
    admin = " ⚠️ ADMIN" if u.is_admin else ""
    print(f"  ✗ id={u.id:<5} {u.full_name!r:<32} {u.email}{admin}")

print("\nCascaded rows to be removed:")
print(f"  trips ............ {len(doomed_trip_ids)}")
print(f"  bookings ......... {len(doomed_booking_ids)}")
print(f"  payments ......... {len(doomed_payment_ids)}")
print(f"  payout_items ..... {len(doomed_payout_item_ids)}")
print(f"  driver_payouts ... {len(doomed_driver_payout_ids)}")

admins_doomed = [u for u in doomed_users if u.is_admin]
if admins_doomed:
    print(f"\n⚠️  {len(admins_doomed)} ADMIN account(s) are in the kill-list (see above).")

if not APPLY:
    print("\nDry run. No changes made. Re-run with --apply to delete.")
    db.close()
    sys.exit(0)


# ── Delete bottom-up (children → parents) in one transaction ──────────────────
def bulk_delete(model, *conds):
    q = db.query(model).filter(*conds)
    n = q.delete(synchronize_session=False)
    return n


try:
    # 1. Ledger entries (permanent records — but this is a test-data purge)
    n = bulk_delete(
        models.PayoutLedgerEntry,
        models.PayoutLedgerEntry.driver_id.in_(doomed_user_ids)
        | models.PayoutLedgerEntry.booking_id.in_(doomed_booking_ids)
        | models.PayoutLedgerEntry.payment_id.in_(doomed_payment_ids)
        | models.PayoutLedgerEntry.payout_item_id.in_(doomed_payout_item_ids)
        | models.PayoutLedgerEntry.driver_payout_id.in_(doomed_driver_payout_ids),
    )
    print(f"  deleted payout_ledger ... {n}")

    # 2. Payout items (RESTRICT on payment/booking/driver) — also any item that
    #    belongs to a doomed driver_payout.
    n = bulk_delete(
        models.PayoutItem,
        models.PayoutItem.id.in_(doomed_payout_item_ids)
        | models.PayoutItem.driver_payout_id.in_(doomed_driver_payout_ids),
    )
    print(f"  deleted payout_items .... {n}")

    # 3. Driver payouts (RESTRICT on driver)
    n = bulk_delete(models.DriverPayout, models.DriverPayout.id.in_(doomed_driver_payout_ids))
    print(f"  deleted driver_payouts .. {n}")

    # 4. Reviews (by/about doomed users, or on doomed trips/bookings)
    n = bulk_delete(
        models.Review,
        models.Review.reviewer_id.in_(doomed_user_ids)
        | models.Review.reviewee_id.in_(doomed_user_ids)
        | models.Review.booking_id.in_(doomed_booking_ids)
        | models.Review.trip_id.in_(doomed_trip_ids),
    )
    print(f"  deleted reviews ......... {n}")

    # 5. Messages (sent by doomed, or on doomed bookings)
    n = bulk_delete(
        models.Message,
        models.Message.sender_id.in_(doomed_user_ids)
        | models.Message.booking_id.in_(doomed_booking_ids),
    )
    print(f"  deleted messages ........ {n}")

    # 6. User reports (reporter/reported doomed, or on doomed bookings)
    n = bulk_delete(
        models.UserReport,
        models.UserReport.reporter_id.in_(doomed_user_ids)
        | models.UserReport.reported_id.in_(doomed_user_ids)
        | models.UserReport.booking_id.in_(doomed_booking_ids),
    )
    print(f"  deleted user_reports .... {n}")

    # 7. Payments
    n = bulk_delete(models.Payment, models.Payment.id.in_(doomed_payment_ids))
    print(f"  deleted payments ........ {n}")

    # 8. Bookings
    n = bulk_delete(models.Booking, models.Booking.id.in_(doomed_booking_ids))
    print(f"  deleted bookings ........ {n}")

    # 9. Ride alerts
    n = bulk_delete(models.RideAlert, models.RideAlert.user_id.in_(doomed_user_ids))
    print(f"  deleted ride_alerts ..... {n}")

    # 10. Trips
    n = bulk_delete(models.Trip, models.Trip.id.in_(doomed_trip_ids))
    print(f"  deleted trips ........... {n}")

    # 11. City suggestions — keep the suggestion, drop the doomed author link.
    n = db.query(models.CitySuggestion).filter(
        models.CitySuggestion.suggested_by_id.in_(doomed_user_ids)
    ).update({models.CitySuggestion.suggested_by_id: None}, synchronize_session=False)
    print(f"  nulled city_suggestions . {n}")

    # 12. Finally the users themselves
    n = bulk_delete(models.User, models.User.id.in_(doomed_user_ids))
    print(f"  deleted users ........... {n}")

    db.commit()
    print("\n✅ Done — committed.")
except Exception as e:
    db.rollback()
    print(f"\n✗ ERROR — rolled back, nothing deleted: {e!r}")
    raise
finally:
    db.close()
