"""The `add` CLI path, against real Postgres.

Not covered by the transaction-per-test isolation the rest of the suite uses:
`add_catalog_from_url` owns its own engine and commits for real (that is the point — it is a
database client, not something that runs inside the API's request transaction), so this test
cleans up explicitly rather than relying on the outer rollback `db_session` gets.
"""

import pytest
from sqlalchemy import delete, select

from registry.cli.add import add_catalog_from_url, build_catalog_document
from registry.core.config import Settings
from registry.db.models.catalog import Catalog
from registry.db.session import build_session_factory, create_database_engine

pytestmark = pytest.mark.integration

URL = "https://library.example/repeat-add-opds"
FEED = {"metadata": {"title": "Repeat Add Library"}, "links": []}


async def test_re_adding_the_same_url_updates_instead_of_duplicating(
    settings: Settings, migrated_database: str
) -> None:
    """The bug this guards against: a second `make add` on the same URL inserting a duplicate.

    Identity is the `catalog` rel href, which is the URL the operator typed, so both documents
    resolve to the same row. `metadata.identifier` differs between them and is irrelevant:
    the import discards it, and the row keeps the `id` Postgres gave it on the first add.
    """
    first_document = build_catalog_document(FEED, URL, kind=["open"])
    second_document = build_catalog_document(FEED, URL, kind=["open"])
    assert first_document["metadata"]["identifier"] != second_document["metadata"]["identifier"]

    engine = create_database_engine(settings)
    try:
        created_first = await add_catalog_from_url(URL, first_document, settings)
        created_second = await add_catalog_from_url(URL, second_document, settings)

        async with build_session_factory(engine)() as session:
            rows = (
                await session.scalars(
                    select(Catalog).where(Catalog.title == FEED["metadata"]["title"])
                )
            ).all()

        assert created_first is True
        assert created_second is False
        assert len(rows) == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(Catalog).where(Catalog.title == FEED["metadata"]["title"])
            )
        await engine.dispose()
