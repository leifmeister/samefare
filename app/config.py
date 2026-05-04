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
    email_from:     str = Field(default="SameFare <noreply@samefare.com>", alias="EMAIL_FROM")
    base_url:       str = Field(default="https://samefare.com", alias="BASE_URL")

    # SMS via Twilio (https://twilio.com)
    twilio_account_sid:  str = Field(default="", alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token:   str = Field(default="", alias="TWILIO_AUTH_TOKEN")
    twilio_from_number:  str = Field(default="", alias="TWILIO_FROM_NUMBER")

    # Rapyd payment processing (https://rapyd.net)
    rapyd_access_key: str  = Field(default="", alias="RAPYD_ACCESS_KEY")
    rapyd_secret_key: str  = Field(default="", alias="RAPYD_SECRET_KEY")
    rapyd_sandbox:    bool = Field(default=True, alias="RAPYD_SANDBOX")

    # Payout rails — set True only after Blikk / Stripe Connect credentials are wired up.
    # While False the ledger runs in full (items are created and advanced) but the
    # background task that submits outbound transfers is skipped so no money moves.
    payout_enabled: bool = Field(default=False, alias="PAYOUT_ENABLED")

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
