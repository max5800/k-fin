"""Application configuration via environment variables."""

import logging

from pydantic import model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


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
    # Depot transactions: Comdirect's min-bookingDate defaults to -180d (6 mo.),
    # which would truncate the trailing-12M dividend-yield KPI. Pull 2 years by
    # default. paging-count for /brokerage/v3/depots/{id}/transactions caps at
    # 500 (Comdirect: ERROR_VALIDATION_PAGING_INVALID at >500); buy-and-hold
    # depots stay well under that. The connector warn-logs if we hit the cap so
    # paging truncation is visible — full paging loop is M11 tech debt.
    depot_transaction_limit: int = 500
    depot_transaction_min_booking_date: str | None = "-730d"

    # Agent / LLM (M7)
    anthropic_api_key: str = ""
    # SearXNG meta-search instance for the categorization agent's
    # `search_web` tool. Empty ⇒ tool not registered, agent runs without
    # web lookup. Set in cluster via Helm chart (api.env.SEARXNG_URL).
    searxng_url: str = ""

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    api_token: str = ""
    worker_url: str = "http://comdirect-worker:8001"

    # JWT auth (M10a)
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 60

    # Bootstrap user seed — temporary (M10a only, removed after Vault/ESO wired up).
    # Used only by the Alembic migration to seed the initial user row.
    bootstrap_user_email: str = ""
    bootstrap_user_display_name: str = "Max"
    bootstrap_user_initial_password: str = ""

    # Bootstrap login (DEV ONLY — issues the static API_TOKEN after a fixed
    # credential check). Force-disabled when app_env == "production" by
    # `_normalize_settings`.
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

    # Dev-only DB tools (wipe transactions, seed mock dataset).
    # Default False; the dev router's destructive endpoints (/wipe, /seed)
    # 404 unless this is explicitly True. Force-disabled when
    # app_env == "production" by `_normalize_settings`. Helm sets True only
    # in `dev/values.local.yaml`; chart/values.yaml leaves it unset so prod
    # never carries the env var at all.
    dev_tools_enabled: bool = False

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
        # Belt-and-braces: dev-only flags are force-disabled in production
        # regardless of env-var input. A stray `DEV_TOOLS_ENABLED=true` or
        # `BOOTSTRAP_LOGIN_ENABLED=true` (Helm overlay typo, env-var leak,
        # copy-paste from a dev values file) must never expose the wipe/
        # seed endpoints or the static-credential login on a production
        # deployment.
        if self.app_env == "production":
            if self.dev_tools_enabled:
                logger.warning(
                    "DEV_TOOLS_ENABLED=true ignored: app_env=production"
                )
                self.dev_tools_enabled = False
            if self.bootstrap_login_enabled:
                logger.warning(
                    "BOOTSTRAP_LOGIN_ENABLED=true ignored: app_env=production"
                )
                self.bootstrap_login_enabled = False
        return self

    def get_own_ibans(self) -> list[str]:
        """Parse comma-separated OWN_IBANS into a list."""
        return [s.strip() for s in self.own_ibans.split(",") if s.strip()]

    def get_cors_origins(self) -> list[str]:
        """Parse comma-separated CORS_ORIGINS into a list."""
        return [s.strip() for s in self.cors_origins.split(",") if s.strip()]


settings = Settings()
