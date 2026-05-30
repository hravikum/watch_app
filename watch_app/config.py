"""Application configuration.

All settings come from environment variables (or a .env file at the project root).
Access via `get_settings()` — it's cached so repeated calls are free.
"""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Settings loaded from environment / .env file.

    Field names are lowercase; env vars use the WATCH_APP_ prefix or the plain
    provider-standard name (e.g. ANTHROPIC_API_KEY).
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    db_url: str = Field(
        default=f"sqlite:///{PROJECT_ROOT}/data/watch_app.db",
        alias="WATCH_APP_DB_URL",
    )

    # --- LLM ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    llm_model: str = Field(
        default="claude-haiku-4-5-20251001",
        alias="WATCH_APP_LLM_MODEL",
    )

    # --- Scoring ---
    default_preset: str = Field(
        default="rising_star",
        alias="WATCH_APP_DEFAULT_PRESET",
    )

    # --- Admin ---
    # Gates the /review tag-editing page. Off by default so it's never exposed
    # on a public deployment; set WATCH_APP_ADMIN=1 locally to enable it.
    admin_mode: bool = Field(default=False, alias="WATCH_APP_ADMIN")

    # --- Paths (derived, not env-configurable) ---
    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


@lru_cache
def get_settings() -> Settings:
    """Return the cached Settings instance.

    Tests can clear the cache via get_settings.cache_clear() if they need
    to swap in different env vars.
    """
    return Settings()
