from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "SameFare"
    secret_key: str = "dev-secret-key-change-in-production"
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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
