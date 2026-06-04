from functools import lru_cache
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-secret-key-change-in-production"


class Settings(BaseSettings):
    app_name: str = "SameFare"
    secret_key: str = _DEV_SECRET
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Railway injects DATABASE_URL automatically from the Postgres plugin
    database_url: str = "postgresql://postgres:password@localhost:5432/samferd"

    # Beta mode — bypasses payment and auto-approves verifications
    beta_mode: bool = False

    # Email via Resend (https://resend.com)
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    email_from:     str = Field(default="SameFare <samefare@samefare.com>", alias="EMAIL_FROM")
    base_url:       str = Field(default="https://samefare.com", alias="BASE_URL")

    # SMS via Twilio (https://twilio.com)
    # TWILIO_SENDER_ID — alphanumeric sender name shown to recipient (max 11 chars,
    # letters/digits, must start with a letter).  Falls back to TWILIO_FROM_NUMBER
    # when blank.  Alphanumeric IDs are one-way only (recipients cannot reply).
    twilio_account_sid:  str = Field(default="", alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token:   str = Field(default="", alias="TWILIO_AUTH_TOKEN")
    twilio_from_number:  str = Field(default="", alias="TWILIO_FROM_NUMBER")
    twilio_sender_id:    str = Field(default="Samefare", alias="TWILIO_SENDER_ID")

    # Rapyd payment processing (https://rapyd.net)
    rapyd_access_key: str  = Field(default="", alias="RAPYD_ACCESS_KEY")
    rapyd_secret_key: str  = Field(default="", alias="RAPYD_SECRET_KEY")
    rapyd_sandbox:    bool = Field(default=True, alias="RAPYD_SANDBOX")

    # Payout rails — set True only after Blikk / Stripe Connect credentials are wired up.
    # While False the ledger runs in full (items are created and advanced) but the
    # background task that submits outbound transfers is skipped so no money moves.
    payout_enabled: bool = Field(default=False, alias="PAYOUT_ENABLED")

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

    @property
    def secure_cookies(self) -> bool:
        """Set the Secure flag on auth cookies when serving over HTTPS."""
        return self.base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
