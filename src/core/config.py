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

    # Firefly III
    firefly_base_url: str = "http://localhost:8080"
    firefly_access_token: str = ""

    # Sync
    sync_interval_minutes: int = 60
    sync_initial_days: int = 90
    sync_dedup_enabled: bool = True
    account_transaction_limit: int = 500
    account_transaction_min_booking_date: str | None = None
    depot_transaction_limit: int = 100
    depot_transaction_min_booking_date: str | None = None

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    api_token: str = ""

    # Database / normalization
    database_url: str = ""
    own_ibans: list[str] = []

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _normalize_database_url(self):
        # CloudNativePG's *-app secret publishes a postgresql:// URI.
        # Swap the scheme so SQLAlchemy picks the psycopg v3 driver.
        if self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
        return self


settings = Settings()
