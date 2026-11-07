"""The benchmark writes published, recommended rows, so it runs against a local database only."""

import pytest
from benchmark_feed import assert_database_is_local

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://registry:registry@localhost:5432/registry",
        "postgresql+asyncpg://registry:registry@127.0.0.1/registry",
        "postgresql+asyncpg://registry:registry@[::1]/registry",
        "postgresql+asyncpg://registry:registry@db:5432/registry",
    ],
)
def test_a_local_database_is_accepted(url: str) -> None:
    assert_database_is_local(url)


def test_a_remote_database_is_refused() -> None:
    with pytest.raises(SystemExit, match=r"10\.20\.0\.3"):
        assert_database_is_local("postgresql+asyncpg://registry:secret@10.20.0.3/registry")
