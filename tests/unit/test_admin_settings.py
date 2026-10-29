"""Misconfigurations that must stop the server at boot rather than open the admin API."""

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from registry.core.config import PLACEHOLDER_SERVICE_KEY, Settings

pytestmark = pytest.mark.unit

DSN = "postgresql+asyncpg://u:p@localhost/db"
#: Deployed keys need 32+ characters.
KEY = "k" * 32
NEXT_KEY = "n" * 32


def build(**overrides: Any) -> Settings:
    return Settings(database_url=DSN, _env_file=None, **overrides)


def test_the_dev_identity_is_allowed_locally() -> None:
    assert build(environment="local", admin_dev_identity=True).admin_dev_identity


@pytest.mark.parametrize("environment", ["test", "staging", "production"])
def test_the_dev_identity_refuses_to_start_anywhere_else(environment: str) -> None:
    with pytest.raises(ValidationError, match="ADMIN_DEV_IDENTITY"):
        build(environment=environment, admin_dev_identity=True)


def test_the_dev_identity_cannot_coexist_with_a_token_issuer() -> None:
    with pytest.raises(ValidationError, match="ADMIN_TOKEN_ISSUER"):
        build(
            environment="local",
            admin_dev_identity=True,
            admin_token_issuer="https://issuer.example.test",
        )


def test_a_deployed_keys_url_must_be_https() -> None:
    with pytest.raises(ValidationError, match="must be https"):
        build(
            environment="production",
            service_keys=SecretStr(KEY),
            admin_token_issuer="https://issuer.example.test",
            admin_token_audience="aud",
            admin_jwks_url="http://issuer.example.test/keys",
        )


def test_a_deployed_service_key_needs_the_token_issuer_audience_and_keys_url() -> None:
    with pytest.raises(ValidationError, match="ADMIN_TOKEN_ISSUER"):
        build(environment="production", service_keys=SecretStr(KEY))


def test_a_deployed_service_key_may_not_be_the_placeholder() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        build(
            environment="production",
            service_keys=SecretStr(f"real,{PLACEHOLDER_SERVICE_KEY}"),
            admin_token_issuer="https://issuer.example.test",
            admin_jwks_url="https://issuer.example.test/keys",
            admin_token_audience="aud",
        )


def test_a_fully_configured_deployment_starts() -> None:
    settings = build(
        environment="production",
        service_keys=SecretStr(f"{KEY}, {NEXT_KEY}"),
        admin_token_issuer="https://issuer.example.test",
        admin_jwks_url="https://issuer.example.test/keys",
        admin_token_audience="aud",
    )

    assert settings.service_key_values == (KEY, NEXT_KEY)


def test_a_deployment_with_no_admin_configuration_still_starts() -> None:
    """Existing deployments must not break: the admin routes then answer 503."""
    assert build(environment="production").service_key_values == ()


@pytest.mark.parametrize("blank", ["", " ", ",", " , "])
def test_a_blank_key_list_configures_no_key(blank: str) -> None:
    assert build(service_keys=SecretStr(blank)).service_key_values == ()


def test_the_allowed_domains_are_split_and_lowercased() -> None:
    assert build(
        admin_allowed_email_domains=" EDRLab.org, @example.com "
    ).allowed_email_domains == (
        "edrlab.org",
        "example.com",
    )


def test_a_missing_environment_is_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGISTRY_ENVIRONMENT", raising=False)

    assert build().environment == "production"
