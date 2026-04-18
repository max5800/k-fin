"""Application configuration via environment variables."""

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Comdirect
    comdirect_client_id: str = ""
    comdirect_client_secret: str = ""
    comdirect_username: str = ""
    comdirect_pin: str = ""
    comdirect_tan_method: str = "pushTAN"

    # Sync
    sync_interval_minutes: int = 60
    sync_initial_days: int = 90
    sync_dedup_enabled: bool = True
    account_transaction_limit: int = 500
    account_transaction_min_booking_date: str | None = None
    depot_transaction_limit: int = 100
    depot_transaction_min_booking_date: str | None = None

    # Agent / LLM (M7)
    anthropic_api_key: str = ""

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    api_token: str = ""
    worker_url: str = "http://comdirect-worker:8001"

    # Bootstrap login (DEV ONLY — issues the static API_TOKEN after a fixed
    # credential check). Hard-disabled when app_env == "production".
    bootstrap_login_enabled: bool = False
    bootstrap_email: str = ""
    bootstrap_password: str = ""

    # CORS — comma-separated origins, e.g. "http://localhost:3000,http://localhost:5173"
    cors_origins: str = ""

    # Database / normalization
    database_url: str = ""
    own_ibans: str = ""

    # Reports
    reports_dir: str = "/data/reports"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _normalize_settings(self):
        # CloudNativePG's *-app secret publishes a postgresql:// URI.
        # Swap the scheme so SQLAlchemy picks the psycopg v3 driver.
        if self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
        return self

    def get_own_ibans(self) -> list[str]:
        """Parse comma-separated OWN_IBANS into a list."""
        return [s.strip() for s in self.own_ibans.split(",") if s.strip()]

    def get_cors_origins(self) -> list[str]:
        """Parse comma-separated CORS_ORIGINS into a list."""
        return [s.strip() for s in self.cors_origins.split(",") if s.strip()]


settings = Settings()
