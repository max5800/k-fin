"""MCP server configuration — URL + token for calling the Finance API."""

from pydantic_settings import BaseSettings


class McpSettings(BaseSettings):
    finance_api_url: str = "http://localhost:8000"
    finance_api_token: str = ""
    request_timeout_s: float = 30.0
    max_response_bytes: int = 100_000

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = McpSettings()
