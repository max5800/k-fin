"""Application configuration via environment variables."""
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

    # App
    app_env: str = "development"
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
