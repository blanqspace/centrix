"""Environment-backed settings for Centrix."""
from __future__ import annotations

from functools import lru_cache
from typing import Dict

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Application configuration sourced from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env_mode: str = "DEV"
    app_brand: str = "Centrix"

    ipc_db: str = "runtime/ctl.db"
    order_approval_ttl_sec: int = 300
    confirm_strict: bool = True

    slack_enabled: bool = False
    slack_simulation: bool = True
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    slack_signing_secret: str | None = None
    slack_channel_control: str | None = None
    slack_channel_logs: str | None = None
    slack_channel_alerts: str | None = None
    slack_channel_orders: str | None = None
    slack_role_map: Dict[str, str] = Field(default_factory=dict)

    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    dashboard_auth_token: str | None = None

    ibkr_enabled: bool = False
    tws_host: str = "127.0.0.1"
    tws_port: int = 4002
    ibkr_client_id: int = 7


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Return a cached instance of application settings."""

    return AppSettings()


settings = get_settings()
