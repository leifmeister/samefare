from functools import lru_cache
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-secret-key-change-in-production"


class Settings(BaseSettings):
    app_name: str = "SameFare"
    secret_key: str = _DEV_SECRET
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 30  # 30 days (sliding — refreshed on activity)

    # Railway injects DATABASE_URL automatically from the Postgres plugin
    database_url: str = "postgresql://postgres:password@localhost:5432/samferd"

    # Beta mode — bypasses payment and auto-approves verifications
    beta_mode: bool = False

    # Email via Resend (https://resend.com)
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    email_from:     str = Field(default="SameFare <samefare@samefare.com>", alias="EMAIL_FROM")
    base_url:       str = Field(default="https://samefare.com", alias="BASE_URL")

    # Meta (Facebook) Pixel — empty by default so nothing tracks until set.
    # Loaded only after the visitor accepts analytics cookies (see base.html).
    meta_pixel_id:  str = Field(default="", alias="META_PIXEL_ID")

    # SMS via Twilio (https://twilio.com)
    # TWILIO_SENDER_ID — alphanumeric sender name shown to recipient (max 11 chars,
    # letters/digits, must start with a letter).  Falls back to TWILIO_FROM_NUMBER
    # when blank.  Alphanumeric IDs are one-way only (recipients cannot reply).
    twilio_account_sid:  str = Field(default="", alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token:   str = Field(default="", alias="TWILIO_AUTH_TOKEN")
    twilio_from_number:  str = Field(default="", alias="TWILIO_FROM_NUMBER")
    twilio_sender_id:    str = Field(default="Samefare", alias="TWILIO_SENDER_ID")
    # Comma-separated country dial-code prefixes that use the alphanumeric sender ID
    # instead of the US from-number. Kept deliberately narrow: only countries we have
    # EVIDENCE about. Iceland (+354) is proven to work on alpha (23 verified users);
    # Czech (+420) is proven to FAIL on the US number (Twilio 21612, 0 verified) so it
    # needs alpha. Every other country stays on the US number — its status quo — until
    # we have a reason to change it, so we never move a working country onto an
    # untested sender (that's what put Poland at risk). Add a prefix via the
    # SMS_ALPHA_COUNTRIES env var (no deploy) once a country is confirmed. The sender
    # also falls back to the number automatically if alpha is rejected (see _send).
    sms_alpha_countries: str = Field(default="+354,+420", alias="SMS_ALPHA_COUNTRIES")
    # Operator phone for critical operational alerts (e.g. failed driver payouts).
    # Override with ADMIN_ALERT_PHONE; set blank to disable SMS alerts.
    admin_alert_phone:   str = Field(default="+3546257175", alias="ADMIN_ALERT_PHONE")

    # Rapyd payment processing (https://rapyd.net)
    rapyd_access_key: str  = Field(default="", alias="RAPYD_ACCESS_KEY")
    rapyd_secret_key: str  = Field(default="", alias="RAPYD_SECRET_KEY")
    rapyd_sandbox:    bool = Field(default=True, alias="RAPYD_SANDBOX")

    # Payout rail — set True only after Blikk credentials are wired up (Iceland-only; Blikk is the only rail).
    # While False the ledger runs in full (items are created and advanced) but the
    # background task that submits outbound transfers is skipped so no money moves.
    payout_enabled: bool = Field(default=False, alias="PAYOUT_ENABLED")
    # Driver payouts are submitted once a day in a single batch run (rather than
    # trickling every 10 min) so the approver can clear them all in one session.
    # Hour of day (UTC, 0–23) at/after which the daily run fires. Default 18:00.
    payout_run_hour: int = Field(default=18, alias="PAYOUT_RUN_HOUR")
    # A Blikk SCA approval link is only valid briefly. If a submitted payout sits
    # awaiting approval longer than this (minutes), the link has expired and SCA
    # was never completed — the reconcile task times it out (marks it failed and
    # alerts) instead of polling forever. No money moved, so it is safe to fail.
    payout_approval_timeout_min: int = Field(default=180, alias="PAYOUT_APPROVAL_TIMEOUT_MIN")
    # Day-before trip reminders are only sent during this local-time evening window
    # (Iceland = UTC), so a reminder never wakes anyone in the middle of the night.
    # The job reminds every trip departing the next calendar day; start/end are the
    # hours of day (0–23, end exclusive) during which it is allowed to send.
    reminder_hour_start: int = Field(default=18, alias="REMINDER_HOUR_START")
    reminder_hour_end:   int = Field(default=22, alias="REMINDER_HOUR_END")

    # Blikk P2P bank transfers (https://blikk.tech)
    # blikk_api_key         — from Blikk partner dashboard (Api-Key header)
    # blikk_platform_phone  — Samefare's own Blikk phone number (receives service fees)
    # blikk_payments        — accept Blikk as a passenger payment method (default off;
    #                         Blikk is used for driver payouts regardless of this flag)
    blikk_api_key:           str  = Field(default="", alias="BLIKK_API_KEY")
    blikk_platform_phone:    str  = Field(default="+3546257175", alias="BLIKK_PLATFORM_PHONE")
    blikk_payments:          bool = Field(default=False, alias="BLIKK_PAYMENTS")
    # Payment Channel API key — separate from P2P key; tied to SameFare's bank
    # account in Blikk's system. Obtain from Blikk when setting up the channel.
    blikk_channel_api_key:   str  = Field(default="", alias="BLIKK_CHANNEL_API_KEY")
    # Kennitala of the person with transfer authority on SameFare's Blikk payment
    # account (i.e. the merchant account holder). Used as scaUserSsn on every
    # Payment Channel payout — this person authenticates the transfer.
    # Set this to the kennitala of whoever owns/manages the merchants.blikk.tech account.
    blikk_sca_kennitala: str  = Field(default="", alias="BLIKK_SCA_KENNITALA")
    # Payment Channel API base URL. Sandbox and production use different hosts;
    # set BLIKK_CHANNEL_BASE_URL to the production endpoint for live payouts.
    blikk_channel_base_url: str = Field(
        default="https://api.blikk.tech/paymentchannel", alias="BLIKK_CHANNEL_BASE_URL")

    # Didit KYC/AML (https://didit.me)
    # didit_api_key             — from Business Console → API & Webhooks
    # didit_webhook_secret      — HMAC secret for X-Signature-V2 verification
    # didit_workflow_id_identity — workflow for passport / national ID (identity only)
    # didit_workflow_id_licence  — workflow for driver's licence (identity + driving)
    didit_api_key:              str = Field(default="", alias="DIDIT_API_KEY")
    didit_webhook_secret:       str = Field(default="", alias="DIDIT_WEBHOOK_SECRET")
    didit_workflow_id_identity: str = Field(default="", alias="DIDIT_WORKFLOW_ID_IDENTITY")
    didit_workflow_id_licence:  str = Field(default="", alias="DIDIT_WORKFLOW_ID_LICENCE")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    @model_validator(mode="after")
    def _check_production_secret(self) -> "Settings":
        if self.base_url.startswith("https://") and self.secret_key == _DEV_SECRET:
            raise ValueError(
                "SECRET_KEY must be set to a strong random value in production. "
                "Generate one with: "
                "python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return self

    @model_validator(mode="after")
    def _check_rapyd_environment(self) -> "Settings":
        # A production deploy with Rapyd keys must NOT point at the sandbox URL:
        # production keys only authenticate against api.rapyd.net, so talking to
        # sandboxapi.rapyd.net silently 401s every payment. RAPYD_SANDBOX defaults
        # to True, so an unset/stale value on a prod box is the failure mode this
        # guard catches — fail loudly at boot instead of at every checkout.
        if (
            self.base_url.startswith("https://")
            and self.rapyd_access_key
            and self.rapyd_secret_key
            and self.rapyd_sandbox
        ):
            raise ValueError(
                "RAPYD_SANDBOX is true on a production deployment with Rapyd keys "
                "configured. Production keys only work against api.rapyd.net — set "
                "RAPYD_SANDBOX=false (or unset the keys for a sandbox/dev box)."
            )
        return self

    @property
    def secure_cookies(self) -> bool:
        """Set the Secure flag on auth cookies when serving over HTTPS."""
        return self.base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
