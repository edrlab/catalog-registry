"""Settings. The only place the environment is read.

Field descriptions are not decoration: `scripts/write_env_example.py` renders them as the
comments in `.env.example`, so this class is the single source of truth for what a local
developer has to set.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: What `.env.example` ships for the service key. Fine on a laptop, refused anywhere deployed.
PLACEHOLDER_SERVICE_KEY = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="REGISTRY_",
        extra="forbid",  # a typo'd variable fails at boot, not silently six weeks later
    )

    # str, not PostgresDsn: Cloud Run's Cloud SQL unix-socket form
    # (postgresql+asyncpg://user:pass@/db?host=/cloudsql/...) has an empty
    # host, which PostgresDsn's stricter URL parser rejects outright even
    # though asyncpg/SQLAlchemy accept it fine.
    database_url: str = Field(
        description="Async Postgres DSN. Required, so no deployment falls back to a local default.",
    )
    seed_file: Path = Field(
        default=Path("data/recommended.json"),
        description="Feed-shaped document the seed command imports.",
    )
    environment: Literal["local", "test", "staging", "production"] = Field(
        default="production",
        description="Where this runs. A missing value means production, the strictest.",
    )
    dev_fetch_token: SecretStr | None = Field(
        default=None,
        description=(
            "Shared secret for the dev console's /dev/fetch route outside local. Unset means the "
            "route does not exist there. Set it from a secret store, never in a file."
        ),
    )
    base_url: str = Field(
        default="http://localhost:8000",
        description="Origin used to build the feed's absolute self link.",
    )
    service_keys: SecretStr | None = Field(
        default=None,
        description="Keys accepted for admin requests, comma separated. Unset disables them.",
    )
    admin_token_issuer: str | None = Field(
        default=None, description="Token issuer (`iss`) for admin requests."
    )
    admin_jwks_url: str | None = Field(
        default=None, description="Where the issuer's signing keys are published."
    )
    admin_token_audience: str | None = Field(
        default=None, description="Token audience for admin requests."
    )
    admin_allowed_email_domains: str = Field(
        default="edrlab.org",
        description="Allowed email domains for admin requests, comma separated.",
    )
    admin_dev_identity: bool = Field(
        default=False,
        description="Local development only.",
    )

    @model_validator(mode="after")
    def _admin_configuration_fails_closed(self) -> "Settings":
        deployed = self.environment not in ("local", "test")
        if self.admin_dev_identity and self.environment != "local":
            raise ValueError("REGISTRY_ADMIN_DEV_IDENTITY is only allowed when environment=local")
        if self.admin_dev_identity and self.admin_token_issuer:
            # A dev identity is an unverified claim; it must not coexist with verified tokens.
            raise ValueError(
                "REGISTRY_ADMIN_DEV_IDENTITY cannot be on while REGISTRY_ADMIN_TOKEN_ISSUER is set"
            )
        if deployed and self.service_key_values:
            if PLACEHOLDER_SERVICE_KEY in self.service_key_values:
                raise ValueError("REGISTRY_SERVICE_KEYS still holds a placeholder value")
            if not (self.admin_token_issuer and self.admin_token_audience and self.admin_jwks_url):
                raise ValueError(
                    "REGISTRY_ADMIN_TOKEN_ISSUER, REGISTRY_ADMIN_TOKEN_AUDIENCE and "
                    "REGISTRY_ADMIN_JWKS_URL are required once REGISTRY_SERVICE_KEYS is set "
                    "outside local"
                )
            if not self.admin_jwks_url.startswith("https://"):
                raise ValueError("REGISTRY_ADMIN_JWKS_URL must be https outside local")
        return self

    @property
    def service_key_values(self) -> tuple[str, ...]:
        raw = self.service_keys.get_secret_value() if self.service_keys else ""
        return tuple(key.strip() for key in raw.split(",") if key.strip())

    @property
    def allowed_email_domains(self) -> tuple[str, ...]:
        parts = (
            domain.strip().lower().lstrip("@")
            for domain in self.admin_allowed_email_domains.split(",")
        )
        return tuple(domain for domain in parts if domain)
