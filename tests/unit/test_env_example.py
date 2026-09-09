"""`.env.example` is generated, and this is what keeps the committed copy honest.

A test rather than a CI-only `git diff --exit-code`: a stale file then fails during
`make test` too, which is where it gets noticed.
"""

from pathlib import Path

import pytest
from pydantic.fields import FieldInfo
from write_env_example import (
    SECRET_PLACEHOLDER,
    render_env_example,
    render_value,
)

pytestmark = pytest.mark.unit

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def test_the_committed_file_matches_the_settings_model() -> None:
    assert ENV_EXAMPLE.read_text(encoding="utf-8") == render_env_example(), (
        "`.env.example` is stale. Run `make env`"
    )


def test_every_setting_appears_with_the_env_prefix() -> None:
    content = render_env_example()

    assert "REGISTRY_SEED_FILE=data/recommended.json" in content
    assert "REGISTRY_BASE_URL=http://localhost:8000" in content


def test_a_field_description_is_rendered_as_a_comment() -> None:
    assert "# Origin used to build the feed's absolute self link." in render_env_example()


@pytest.mark.parametrize(
    "name", ["admin_client_secret", "db_password", "api_key", "auth_token", "test_credentials"]
)
def test_a_secret_named_field_never_writes_its_default(name: str) -> None:
    """A secret with a default is already a mistake; committing its value is a disclosure."""
    field = FieldInfo(default="hunter2")

    assert render_value(name, field) == SECRET_PLACEHOLDER


def test_a_required_field_renders_empty() -> None:
    assert render_value("base_url", FieldInfo()) == ""
