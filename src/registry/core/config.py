"""Settings. The only place the environment is read.

Field descriptions are not decoration: `scripts/write_env_example.py` renders them as the
comments in `.env.example`, so this class is the single source of truth for what a local
developer has to set.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="REGISTRY_",
        extra="forbid",  # a typo'd variable fails at boot, not silently six weeks later
    )

    database_url: PostgresDsn = Field(
        description="Async Postgres DSN. Required, so no deployment falls back to a local default.",
    )
    seed_file: Path = Field(
        default=Path("data/recommended.json"),
        description="Feed-shaped document the seed command imports.",
    )
    environment: Literal["local", "test", "staging", "production"] = "local"
    base_url: str = Field(
        default="http://localhost:8000",
        description="Origin used to build the feed's absolute self link.",
    )
