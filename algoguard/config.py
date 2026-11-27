"""Settings from the environment (or a .env file). See .env.example."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ALGOGUARD_", extra="ignore")

    SECRET_KEY: str = "change-me"              # signs HOLD confirmation tokens and derives the vault key
    ENCRYPTION_KEY: str | None = None          # optional separate seed for the key vault
    ADMIN_TOKEN: str = "change-me-admin"       # bearer token for the management endpoints

    DRY_RUN: bool = True                       # True: decide and log, never send orders to Delta
    DELTA_BASE_URL: str = "https://cdn-ind.testnet.deltaex.org"  # India testnet; production: https://api.india.delta.exchange
    DELTA_API_KEY: str | None = None
    DELTA_API_SECRET: str | None = None

    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None

    ANTHROPIC_API_KEY: str | None = None       # optional: friendlier explanations, checked against the facts
    EXPLAINER_MODEL: str = "claude-haiku-5-5"

    HOLD_SECONDS: int = 120                    # how long a HOLD can be confirmed


settings = Settings()
