"""`AdminCatalogWriter`: which database failures become a duplicate, and which stay errors."""

import uuid
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from registry.repositories.admin_repository import AdminCatalogWriter

DOCUMENT = {
    "metadata": {"title": "T", "kind": ["public"]},
    "links": [{"rel": "catalog", "href": "https://example.org/opds.json"}],
}


class FailingSession:
    """Accepts the new catalog, then fails the flush with *constraint* violated."""

    def __init__(self, constraint: str) -> None:
        self.constraint = constraint

    def add(self, _: object) -> None:
        pass

    async def flush(self) -> None:
        orig = Exception(f'violates check constraint "{self.constraint}"')
        raise IntegrityError("INSERT INTO catalogs ...", {}, orig)


async def test_a_violated_constraint_that_is_not_a_duplicate_stays_an_integrity_error() -> None:
    """Only the catalog id and the link identity mean "this catalog exists already"; any other
    constraint is a defect, and calling it a duplicate would hide it."""
    session: Any = FailingSession("ck_catalogs_published_when_published")

    with pytest.raises(IntegrityError):
        await AdminCatalogWriter(session).persist_new_catalog(DOCUMENT, uuid.uuid4(), publish=False)
