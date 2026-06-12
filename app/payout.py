"""
app/payout.py — Payout ledger and outbound transfer logic.

SameFare uses Rapyd to collect passenger payments.  Once a payment is
captured (funds settled), this module pairs it to the driver's entitlement
and routes the outbound transfer through either:

  • Blikk          — Icelandic real-time account-to-account (IS IBAN required)
  • Stripe Connect — for drivers without an Icelandic bank account
                     (FX fees apply and are the driver's responsibility)

Architecture rule
-----------------
Rapyd, Blikk, and Stripe are payment rails.  The payout ledger is the
source of truth for pairing and reconciliation.  Never calculate "who
should be paid" from Booking rows on the fly; always derive it from
PayoutItem / PayoutLedgerEntry.

Payout eligibility
------------------
A PayoutItem becomes eligible when ALL of the following hold:
  1. payment.status == captured        (funds confirmed by Rapyd)
  2. booking.status in (completed, no_show)  (ride outcome is final)
  3. no existing PayoutItem for this payment_id  (unique constraint guards duplicates)

A PayoutItem advances to payout_ready when the driver has configured a
payout method (Blikk IBAN or Stripe Connect account ID).

Provider integration
--------------------
_send_blikk_payout() is live: it submits via the Blikk Payment Channel and
returns (payment_id, approval_url). Blikk payouts require the account holder
(scaUserSsn) to approve each transfer at the returned redirectUrl before money
moves, so send_driver_payout() stores the URL and SMS-alerts the approver;
reconcile_blikk_payout() confirms the batch once approved.
_send_stripe_connect_payout() is still a stub (raises NotImplementedError).
The whole pipeline stays dry until PAYOUT_ENABLED=true in the environment.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import models
from app.models import (
    DriverPayout, DriverPayoutStatus,
    LedgerEntryType,
    PayoutItem, PayoutItemStatus,
    PayoutLedgerEntry, PayoutMethod,
)

log = logging.getLogger(__name__)


# ── Ledger helper ──────────────────────────────────────────────────────────────

def write_ledger_entry(
    db: Session,
    entry_type: LedgerEntryType,
    amount: int,
    *,
    payment_id:       int | None = None,
    payout_item_id:   int | None = None,
    driver_payout_id: int | None = None,
    booking_id:       int | None = None,
    driver_id:        int | None = None,
    currency: str = "ISK",
    note: str | None = None,
) -> PayoutLedgerEntry:
    """
    Append one immutable row to the payout ledger.
    Never update or delete ledger rows — issue reversals/corrections instead.
    Does NOT commit; caller is responsible.
    """
    entry = PayoutLedgerEntry(
        entry_type       = entry_type,
        amount           = amount,
        currency         = currency,
        payment_id       = payment_id,
        payout_item_id   = payout_item_id,
        driver_payout_id = driver_payout_id,
        booking_id       = booking_id,
        driver_id        = driver_id,
        note             = note,
    )
    db.add(entry)
    return entry


# ── Payout method resolution ───────────────────────────────────────────────────

def resolve_payout_method(driver: models.User) -> PayoutMethod | None:
    """
    Determine which rail to use for a driver, or None if not yet configured.

    Preference order:
      1. Blikk  — if the driver has an Icelandic IBAN (starts with 'IS')
      2. Stripe Connect — if the driver has a Stripe account ID

    Drivers without either are held in `pending` until they complete onboarding.
    """
    if driver.blikk_account_iban and driver.blikk_account_iban.upper().startswith("IS"):
        return PayoutMethod.blikk
    if driver.stripe_account_id:
        return PayoutMethod.stripe_connect
    return None


# ── Batch idempotency key ──────────────────────────────────────────────────────

def _batch_idempotency_key(driver_id: int, item_ids: list[int]) -> str:
    """
    Deterministic provider idempotency key for a DriverPayout batch.

    Derived from committed PayoutItem IDs (already in the DB before this
    function is ever called) so the key is identical on every retry attempt.
    If the provider accepted the first call and the subsequent db.commit()
    failed, the next run generates the same key and the provider de-duplicates —
    preventing a second real money transfer.

    Format: dp-<driver_id>-<24-char SHA-256 hex prefix>
    The prefix covers 96 bits — collision probability is negligible for any
    realistic payout volume.
    """
    payload = f"{driver_id}:{','.join(str(i) for i in sorted(item_ids))}"
    digest  = hashlib.sha256(payload.encode()).hexdigest()[:24]
    return f"dp-{driver_id}-{digest}"


# ── PayoutItem creation ────────────────────────────────────────────────────────

def create_payout_item_for_payment(
    db: Session,
    payment: models.Payment,
) -> PayoutItem | None:
    """
    Create a PayoutItem pairing a captured passenger payment to the driver's
    entitlement.  Idempotent: returns the existing item if one already exists,
    returns None if the payment is not yet eligible.

    Called by _run_create_payout_items() after both:
      • payment.status transitions to `captured`, AND
      • booking.status reaches a terminal ride state (completed / no_show)

    Amounts are snapshotted from the Payment record at creation time so that
    later refunds or adjustments to the booking row don't silently alter what
    the driver is owed.
    """
    booking = payment.booking
    trip    = booking.trip
    driver  = trip.driver

    # Eligibility: payment must be captured
    if payment.status != models.PaymentStatus.captured:
        return None

    # Eligibility: ride outcome must be final
    if booking.status not in (
        models.BookingStatus.completed,
        models.BookingStatus.no_show,
    ):
        return None

    # Idempotency: return existing item if already created
    existing = db.query(PayoutItem).filter(PayoutItem.payment_id == payment.id).first()
    if existing:
        return existing

    payout_method = resolve_payout_method(driver)

    item = PayoutItem(
        payment_id      = payment.id,
        booking_id      = booking.id,
        driver_id       = driver.id,
        amount          = payment.driver_payout,
        platform_fee    = payment.platform_fee,
        passenger_total = payment.passenger_total,
        payout_method   = payout_method,
        status          = (
            PayoutItemStatus.payout_ready
            if payout_method is not None
            else PayoutItemStatus.pending
        ),
        # Deterministic idempotency key — safe to retry without duplicating
        idempotency_key = f"pi-{payment.id}",
    )
    db.add(item)
    db.flush()   # populate item.id for ledger FKs

    write_ledger_entry(
        db, LedgerEntryType.driver_payable_created,
        amount           = payment.driver_payout,
        payment_id       = payment.id,
        payout_item_id   = item.id,
        booking_id       = booking.id,
        driver_id        = driver.id,
        note             = (
            f"Booking {booking.id} captured "
            f"({trip.origin}→{trip.destination}); driver payable created"
        ),
    )
    write_ledger_entry(
        db, LedgerEntryType.platform_fee_retained,
        amount           = payment.platform_fee,
        payment_id       = payment.id,
        payout_item_id   = item.id,
        booking_id       = booking.id,
        driver_id        = driver.id,
        note             = f"Platform fee retained for booking {booking.id}",
    )
    if payout_method is not None:
        write_ledger_entry(
            db, LedgerEntryType.driver_payout_ready,
            amount           = payment.driver_payout,
            payout_item_id   = item.id,
            driver_id        = driver.id,
            note             = f"Payout method {payout_method} resolved at creation",
        )

    log.info(
        "PayoutItem %s created for payment %s (booking %s) driver %s — "
        "%s ISK via %s",
        item.id, payment.id, booking.id, driver.id,
        payment.driver_payout, payout_method or "no_method_yet",
    )
    return item


# ── PayoutItem advancement ─────────────────────────────────────────────────────

def advance_payout_item(db: Session, item: PayoutItem) -> bool:
    """
    Attempt to move a `pending` PayoutItem to `payout_ready` now that the
    driver has configured their payout method.
    Returns True if advanced, False if still waiting on driver action.
    """
    if item.status != PayoutItemStatus.pending:
        return False

    payout_method = resolve_payout_method(item.driver)
    if payout_method is None:
        return False

    item.payout_method = payout_method
    item.status        = PayoutItemStatus.payout_ready

    write_ledger_entry(
        db, LedgerEntryType.driver_payout_ready,
        amount         = item.amount,
        payout_item_id = item.id,
        driver_id      = item.driver_id,
        note           = (
            f"Driver {item.driver_id} configured {payout_method}; "
            f"PayoutItem advanced to payout_ready"
        ),
    )
    log.info(
        "PayoutItem %s advanced to payout_ready for driver %s via %s",
        item.id, item.driver_id, payout_method,
    )
    return True


def cancel_payout_item(
    db: Session,
    item: PayoutItem,
    reason: str = "booking_cancelled",
) -> None:
    """
    Cancel a PayoutItem that has not yet been sent.
    Called when a passenger is refunded after capture but before the driver
    payout has been submitted.
    Caller must db.commit() afterwards.
    """
    if item.status in (
        PayoutItemStatus.payout_sent,
        PayoutItemStatus.payout_confirmed,
    ):
        log.error(
            "cancel_payout_item: PayoutItem %s is already %s — cannot cancel; "
            "post a reversal instead",
            item.id, item.status,
        )
        return

    item.status = PayoutItemStatus.cancelled
    write_ledger_entry(
        db, LedgerEntryType.payout_item_cancelled,
        amount         = -item.amount,   # negative: credit back against driver payable
        payout_item_id = item.id,
        driver_id      = item.driver_id,
        note           = reason,
    )
    log.info("PayoutItem %s cancelled (reason: %s)", item.id, reason)


# ── Refund → payout ledger impact ─────────────────────────────────────────────

def handle_refund_payout_impact(
    db: Session,
    payment: models.Payment,
    booking_id: int,
    refund_amount: int,
) -> None:
    """
    Record the payout-ledger consequences of a Rapyd-confirmed passenger refund.

    Must be called AFTER Rapyd confirms the refund and BEFORE the payment
    status is set to refunded/partial_refund.  The caller must db.commit()
    afterwards so that the ledger entry, any PayoutItem state change, and the
    payment status transition land in a single atomic commit.

    What this function does
    -----------------------
    1. Writes a `passenger_refund_confirmed` ledger entry — the durable audit
       record that real money left Rapyd toward the passenger.

    2. Looks up the PayoutItem paired to this payment (if any):

       • No PayoutItem:
           Payment never reached `captured`+`completed`, or the background task
           hasn't run yet.  The ledger entry above is the only record needed.

       • PayoutItem in a pre-send state
         (pending / payout_ready / payout_failed / retry_ready / cancelled):
           Cancel the item via cancel_payout_item() so the driver is not paid.

       • PayoutItem in payout_sent or payout_confirmed:
           The driver has already been paid or the transfer is in-flight.
           Mark the item `reversed` and write a `driver_payout_reversed` entry
           with a negative amount.  A human operator must recover the funds.
    """
    # ── 1. Passenger refund ledger entry ───────────────────────────────────────
    write_ledger_entry(
        db, LedgerEntryType.passenger_refund_confirmed,
        amount     = -refund_amount,   # negative: money leaving the platform
        payment_id = payment.id,
        booking_id = booking_id,
        note       = (
            f"Rapyd confirmed refund of {refund_amount} ISK "
            f"to passenger for booking {booking_id}"
        ),
    )

    # ── 2. PayoutItem impact ───────────────────────────────────────────────────
    item = (
        db.query(PayoutItem)
        .filter(PayoutItem.payment_id == payment.id)
        .first()
    )
    if item is None:
        # No payout item yet — nothing further to do.
        return

    if item.status in (PayoutItemStatus.payout_sent, PayoutItemStatus.payout_confirmed):
        # Driver has already been paid or the transfer is in-flight.
        # Post a reversal so the ledger stays balanced; flag for manual recovery.
        item.status = PayoutItemStatus.reversed
        write_ledger_entry(
            db, LedgerEntryType.driver_payout_reversed,
            amount           = -item.amount,
            payout_item_id   = item.id,
            driver_id        = item.driver_id,
            payment_id       = payment.id,
            booking_id       = booking_id,
            note             = (
                f"Passenger refund confirmed for booking {booking_id}; "
                f"driver payout was already {item.status.value} — "
                f"manual fund recovery required"
            ),
        )
        log.warning(
            "PayoutItem %s REVERSED for booking %s — driver payout already %s; "
            "manual fund recovery required",
            item.id, booking_id, item.status,
        )
    else:
        # Pre-send state — cancel cleanly so the driver is never paid.
        cancel_payout_item(
            db, item,
            reason=f"passenger_refunded_booking_{booking_id}",
        )


# ── Provider integrations ──────────────────────────────────────────────────────
# Blikk (_send_blikk_payout) is live. Stripe Connect (_send_stripe_connect_payout)
# is still a stub that raises NotImplementedError until a platform account is set
# up. The surrounding batch/send/reconcile logic is fully implemented; the whole
# pipeline stays dry until PAYOUT_ENABLED=true.


class PayoutProviderError(Exception):
    """Raised when a Blikk or Stripe Connect API call fails."""


def _send_blikk_payout(batch: DriverPayout, driver: models.User) -> tuple[str, str | None]:
    """
    Submit an A2A transfer via the Blikk Payment Channel API.
    Platform's bank account is pre-configured in the channel; driver identified
    by kennitala + IBAN.

    Returns (blikk_payment_id, approval_url) on success. approval_url is the
    redirectUrl Blikk returns with SCA_REQUIRED / USER_ONBOARDING_REQUIRED — the
    account holder (scaUserSsn) must open it to approve the transfer before money
    moves. It is None only if Blikk settles without an approval step.
    Raises PayoutProviderError on failure.

    scaUserSsn is the kennitala of the person at SameFare with transfer authority
    on the Blikk payment account (set via BLIKK_SCA_KENNITALA env var).
    This is fixed per channel — the merchant account holder, not the driver.
    """
    from app import blikk as blikk_client
    from app.blikk import BlikkError
    from app.config import get_settings

    s = get_settings()

    if not driver.kennitala:
        raise PayoutProviderError(
            f"Driver {driver.id} has no kennitala — cannot send Blikk payout."
        )
    if not driver.blikk_account_iban:
        raise PayoutProviderError(
            f"Driver {driver.id} has no IBAN — cannot send Blikk payout."
        )
    if not s.blikk_sca_kennitala:
        raise PayoutProviderError(
            "BLIKK_SCA_KENNITALA is not set — required for Payment Channel payouts."
        )

    sca_ssn = s.blikk_sca_kennitala

    try:
        result = blikk_client.create_channel_payout(
            amount        = batch.amount,
            creditor_ssn  = driver.kennitala,
            creditor_name = driver.full_name,
            creditor_iban = driver.blikk_account_iban,
            sca_user_ssn  = sca_ssn,
            reference     = f"SameFare payout batch {batch.id}",
        )
    except BlikkError as exc:
        raise PayoutProviderError(f"Blikk channel payout failed: {exc}") from exc

    payment_id = result.get("id", "")
    if not payment_id:
        raise PayoutProviderError("Blikk returned no payment ID.")

    # Blikk may accept the HTTP call (200/201) but already report a terminal
    # *failure* in the body — e.g. insufficient funds rejected immediately.
    # Raise so the batch is marked failed instead of being recorded as `sent`.
    if (blikk_client.channel_payment_is_terminal(result)
            and not blikk_client.channel_payment_succeeded(result)):
        raise PayoutProviderError(
            f"Blikk rejected payout {payment_id} at submission: "
            f"status={result.get('status')!r}"
        )

    # SCA_REQUIRED / USER_ONBOARDING_REQUIRED come back with a redirectUrl the
    # account holder must open to approve. Surface it so the batch can alert the
    # approver and reconcile can finish once approved.
    return payment_id, result.get("redirectUrl")


def _send_stripe_connect_payout(batch: DriverPayout, driver: models.User) -> str:
    """
    Transfer funds to driver.stripe_account_id via Stripe Connect.
    Returns the Stripe Transfer ID on success.
    Raises PayoutProviderError on failure.

    Important: Rapyd is the incoming processor, so Stripe Connect has no
    automatic knowledge of these funds.  SameFare must maintain a funded
    Stripe platform balance and treat this as an outbound transfer, not a
    destination charge.

    TODO: implement once Stripe Connect platform account is set up.
    See: https://stripe.com/docs/connect/separate-charges-and-transfers
    """
    raise NotImplementedError(
        "Stripe Connect payout not yet implemented. "
        "Set up a Stripe Connect platform account and replace this stub."
    )


# ── DriverPayout batching ──────────────────────────────────────────────────────

def build_driver_payout_batch(
    db: Session,
    driver: models.User,
    items: list[PayoutItem],
) -> DriverPayout | None:
    """
    Persist a DriverPayout record aggregating multiple payout_ready items for
    one driver.  All items must share the same payout_method.

    Does NOT submit the payout — the caller must db.commit() this record first,
    then call send_driver_payout() in a separate step.  This two-phase approach
    ensures the batch and its stable idempotency key are durable before any
    provider API call, so a crash between submission and the status-update commit
    can be recovered by re-submitting the same stored key (provider deduplicates).

    Idempotent: if a DriverPayout with the deterministic key already exists
    (e.g. a previous run built the batch but crashed before sending), returns
    the existing record so the caller can proceed directly to submission.

    Returns None if items is empty.
    Caller must db.commit() afterwards.
    """
    if not items:
        return None

    idem_key = _batch_idempotency_key(driver.id, [i.id for i in items])

    # Idempotent: return existing batch if this key was already persisted.
    # Covers the crash-after-build-before-send scenario — the caller will
    # find the batch in `pending` state and submit it in Phase 2.
    existing = (
        db.query(DriverPayout)
        .filter(DriverPayout.idempotency_key == idem_key)
        .first()
    )
    if existing:
        log.info(
            "build_driver_payout_batch: found existing DriverPayout %s "
            "(status=%s) for driver %s — skipping re-creation",
            existing.id, existing.status, driver.id,
        )
        return existing

    payout_method = items[0].payout_method
    total         = sum(i.amount for i in items)

    batch = DriverPayout(
        driver_id       = driver.id,
        amount          = total,
        currency        = "ISK",
        payout_method   = payout_method,
        status          = DriverPayoutStatus.pending,
        idempotency_key = idem_key,
    )
    db.add(batch)
    db.flush()   # populate batch.id for FKs and ledger entries

    for item in items:
        item.driver_payout_id = batch.id
        item.status           = PayoutItemStatus.payout_sent
        write_ledger_entry(
            db, LedgerEntryType.driver_payout_batched,
            amount           = item.amount,
            payout_item_id   = item.id,
            driver_payout_id = batch.id,
            driver_id        = driver.id,
            note             = f"Batched into DriverPayout {batch.id}",
        )

    log.info(
        "DriverPayout %s created for driver %s — %s ISK across %d item(s) via %s",
        batch.id, driver.id, total, len(items), payout_method,
    )
    return batch


# ── DriverPayout submission ────────────────────────────────────────────────────

def send_driver_payout(db: Session, batch: DriverPayout) -> bool:
    """
    Submit a pending DriverPayout batch to the appropriate provider.
    Updates batch.status and writes ledger entries.
    Returns True on success, False on failure.
    Caller must db.commit() afterwards.

    On failure the batch stays in `failed` state and each constituent item
    moves to `payout_failed`.  A human operator (or future retry task) can
    create a new DriverPayout covering the same items after marking them
    `retry_ready`.
    """
    driver = batch.driver

    try:
        approval_url = None
        if batch.payout_method == PayoutMethod.blikk:
            provider_id, approval_url = _send_blikk_payout(batch, driver)
        elif batch.payout_method == PayoutMethod.stripe_connect:
            provider_id = _send_stripe_connect_payout(batch, driver)
        else:
            raise PayoutProviderError(f"Unrecognised payout method: {batch.payout_method}")

        batch.provider_payout_id = provider_id
        batch.approval_url       = approval_url
        batch.status             = DriverPayoutStatus.sent
        batch.sent_at            = datetime.utcnow()

        write_ledger_entry(
            db, LedgerEntryType.driver_payout_sent,
            amount           = batch.amount,
            driver_payout_id = batch.id,
            driver_id        = driver.id,
            note             = f"Provider ref: {provider_id}",
        )
        log.info(
            "DriverPayout %s sent via %s to driver %s (ref: %s)%s",
            batch.id, batch.payout_method, driver.id, provider_id,
            " — awaiting SCA approval" if approval_url else "",
        )
        # Blikk payouts need the account holder to approve at batch.approval_url
        # before money moves. This runs on demand from the admin /admin/payouts
        # Approve action, so the link is fresh; the approver is redirected
        # straight to it and reconcile_blikk_payout confirms once they approve.
        return True

    except (NotImplementedError, PayoutProviderError) as exc:
        _mark_payout_failed(db, batch, str(exc))
        return False


def _mark_payout_failed(db: Session, batch: DriverPayout, reason: str) -> None:
    """
    Move a DriverPayout and its items into the failed state and record it on the
    ledger. Used both when submission is rejected and when a previously `sent`
    batch is later found to have failed at the bank (e.g. insufficient funds).
    Items land in `payout_failed` for operator-driven recovery. Caller commits.
    """
    batch.status         = DriverPayoutStatus.failed
    batch.failed_at      = datetime.utcnow()
    batch.failure_reason = reason

    for item in batch.items:
        item.status = PayoutItemStatus.payout_failed

    write_ledger_entry(
        db, LedgerEntryType.driver_payout_failed,
        amount           = batch.amount,
        driver_payout_id = batch.id,
        driver_id        = batch.driver_id,
        note             = reason,
    )
    log.error(
        "DriverPayout %s FAILED for driver %s via %s: %s",
        batch.id, batch.driver_id, batch.payout_method, reason,
    )

    # Critical: a driver wasn't paid. Alert the operator by SMS (best-effort).
    try:
        from app import sms
        sms.admin_alert(
            f"SameFare ALERT: driver payout FAILED. Batch {batch.id}, "
            f"driver {batch.driver_id}, {batch.amount} ISK via {batch.payout_method}. "
            f"Reason: {reason}. Check the payout ledger."
        )
    except Exception as exc:  # never let alerting break failure handling
        log.error("admin_alert raised while reporting payout failure: %s", exc)


def confirm_driver_payout(db: Session, batch: DriverPayout) -> None:
    """
    Record provider confirmation of a sent payout (called from a webhook or
    polling task once the provider confirms settlement).
    Caller must db.commit() afterwards.
    """
    if batch.status != DriverPayoutStatus.sent:
        log.warning(
            "confirm_driver_payout called on DriverPayout %s with status %s — expected sent",
            batch.id, batch.status,
        )
        return

    batch.status       = DriverPayoutStatus.confirmed
    batch.confirmed_at = datetime.utcnow()
    batch.approval_url = None   # approved & settled — link no longer actionable

    for item in batch.items:
        if item.status == PayoutItemStatus.payout_sent:
            item.status = PayoutItemStatus.payout_confirmed

    write_ledger_entry(
        db, LedgerEntryType.driver_payout_confirmed,
        amount           = batch.amount,
        driver_payout_id = batch.id,
        driver_id        = batch.driver_id,
        note             = f"Provider confirmed payout {batch.provider_payout_id}",
    )
    log.info(
        "DriverPayout %s confirmed for driver %s — %s ISK via %s",
        batch.id, batch.driver_id, batch.amount, batch.payout_method,
    )


def reconcile_blikk_payout(db: Session, batch: DriverPayout) -> bool:
    """
    Poll Blikk for the terminal status of a `sent` Blikk payout and reconcile it.

    Blikk Payment Channel transfers settle asynchronously: create_channel_payout()
    returns once the order is accepted, but the bank can still REJECT it on
    settlement (e.g. insufficient funds in SameFare's account). Without this,
    the batch would sit in `sent` forever and the ledger would wrongly show the
    driver as paid.

        SUCCESS                       → confirm_driver_payout()
        REJECTED / ERROR / CANCELLED  → _mark_payout_failed() (items → payout_failed)
        still in flight / fetch error → leave as-is, retry next tick

    Returns True if the batch reached a terminal state on this call. Caller commits.
    """
    if batch.status != DriverPayoutStatus.sent or batch.payout_method != PayoutMethod.blikk:
        return False
    if not batch.provider_payout_id:
        log.warning("reconcile_blikk_payout: batch %s is sent but has no provider id", batch.id)
        return False

    from app import blikk as blikk_client
    from app.blikk import BlikkError

    try:
        payment = blikk_client.get_channel_payment(batch.provider_payout_id)
    except BlikkError as exc:
        # Transient (network / provider hiccup) — leave `sent`, reconcile next tick.
        log.warning(
            "reconcile_blikk_payout: could not fetch Blikk payment %s for batch %s: %s",
            batch.provider_payout_id, batch.id, exc,
        )
        return False

    if not blikk_client.channel_payment_is_terminal(payment):
        # Stale-approval guard. A Blikk SCA approval link is valid only briefly.
        # If the batch has sat awaiting approval past the timeout, the link has
        # expired and SCA was never completed — so it will never settle and we
        # must not poll forever. Time it out (fail + alert). No money moved (it
        # never reached SUCCESS), so failing is safe. A human can verify in Blikk
        # and re-issue. We deliberately do NOT auto-resubmit: if the transfer had
        # in fact settled and Blikk were merely slow to report it, a resubmit
        # would double-pay the driver.
        from app.config import get_settings
        timeout_min = get_settings().payout_approval_timeout_min
        status      = payment.get("status")
        if batch.sent_at and datetime.utcnow() - batch.sent_at > timedelta(minutes=timeout_min):
            batch.approval_url = None   # link is dead — don't show it as approvable
            _mark_payout_failed(
                db, batch,
                f"SCA approval not completed within {timeout_min} min "
                f"(Blikk status {status!r}); the approval link expired. "
                f"No money moved — verify in Blikk and re-issue if still owed."
            )
            return True
        return False  # still within the approval window — check again next tick

    if blikk_client.channel_payment_succeeded(payment):
        confirm_driver_payout(db, batch)
        return True

    _mark_payout_failed(
        db, batch,
        f"Blikk reported {payment.get('status')!r} for payment {batch.provider_payout_id}",
    )
    return True
