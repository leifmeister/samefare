"""
One-off validator for the Blikk Payment Channel payout integration.

Sends ONE real payout to a creditor IBAN and polls until a terminal status,
printing the raw Blikk request/response at each step. Use a tiny amount and a
bank account you control. Does NOT touch the database or the payout ledger —
this is a pure API check so you can validate the integration before enabling
PAYOUT_ENABLED for real driver payouts.

Run with the production Blikk credentials injected (recommended):

    railway run --service samefare \
        venv/bin/python scripts/test_blikk_payout.py \
        --amount 100 --name "Your Name" --ssn 0000000000 --iban IS00...

or locally with BLIKK_CHANNEL_API_KEY + BLIKK_SCA_KENNITALA in the environment.
"""
import argparse
import json
import sys
import time


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate a single Blikk Payment Channel payout.")
    ap.add_argument("--amount", type=int, default=100, help="ISK amount (integer, full ISK). Default 100.")
    ap.add_argument("--name", required=True, help="Creditor (driver) full name.")
    ap.add_argument("--ssn", required=True, help="Creditor kennitala (10 digits).")
    ap.add_argument("--iban", required=True, help="Creditor Icelandic IBAN (IS + 24 chars).")
    ap.add_argument("--reference", default="SameFare payout validation")
    ap.add_argument("--poll-seconds", type=int, default=120, help="Max seconds to poll for a terminal status.")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = ap.parse_args()

    from app.config import get_settings
    from app import blikk
    from app.blikk import BlikkError

    s = get_settings()
    print("── Blikk Payment Channel payout validation ──────────────────────────")
    print("base URL  :", blikk._CHANNEL_BASE)
    print("api key   :", (s.blikk_channel_api_key[:8] + "…") if s.blikk_channel_api_key else "(NOT SET)")
    print("scaUserSsn:", s.blikk_sca_kennitala or "(NOT SET)")
    print(f"payout    : {args.amount} ISK  →  {args.name}  /  {args.iban}")
    print("─────────────────────────────────────────────────────────────────────")

    if not s.blikk_channel_api_key:
        print("ABORT: BLIKK_CHANNEL_API_KEY not set."); sys.exit(1)
    if not s.blikk_sca_kennitala:
        print("ABORT: BLIKK_SCA_KENNITALA not set."); sys.exit(1)

    if not args.yes:
        ans = input(f"\nThis sends a REAL {args.amount} ISK bank transfer. Type 'yes' to proceed: ")
        if ans.strip().lower() != "yes":
            print("Cancelled."); sys.exit(0)

    print("\n→ POST /payment …")
    try:
        result = blikk.create_channel_payout(
            amount        = args.amount,
            creditor_ssn  = args.ssn,
            creditor_name = args.name,
            creditor_iban = args.iban,
            sca_user_ssn  = s.blikk_sca_kennitala,
            reference     = args.reference,
        )
    except BlikkError as exc:
        print("✗ BlikkError on submit:", exc)
        print("  status_code:", exc.status_code)
        print("  body       :", exc.body)
        sys.exit(2)

    print("response:\n" + _dump(result))
    payment_id = result.get("id")
    if not payment_id:
        print("✗ No payment id returned — cannot poll."); sys.exit(2)

    if result.get("status") == "SCA_REQUIRED" or result.get("redirectUrl"):
        print("\n⚠  SCA REQUIRED — the account holder must approve this payout before it settles.")
        print("   redirectUrl:", result.get("redirectUrl"))
        print("   (If this is the normal flow, fully-automatic payouts are NOT possible —")
        print("    each payout/batch needs manual SCA approval. Flag this before go-live.)")

    print(f"\n→ polling GET /payment/{payment_id} until terminal (≤{args.poll_seconds}s)…")
    deadline = time.time() + args.poll_seconds
    while time.time() < deadline:
        time.sleep(5)
        try:
            payment = blikk.get_channel_payment(payment_id)
        except BlikkError as exc:
            print("  poll error:", exc)
            continue
        status = payment.get("status")
        print("  status:", status)
        if blikk.channel_payment_is_terminal(payment):
            print("\nTERMINAL STATE:", status)
            print(_dump(payment))
            if blikk.channel_payment_succeeded(payment):
                print("\nRESULT: ✅ SUCCESS — the Blikk payout integration works end-to-end.")
            else:
                print(f"\nRESULT: ❌ {status} — inspect the body above and adjust the request shape.")
            return

    print("\n⏱  Still non-terminal after the timeout — likely PENDING (settling) or SCA_REQUIRED.")
    print("    Re-run GET /payment/" + str(payment_id) + " later, or check the Blikk dashboard.")


if __name__ == "__main__":
    main()
