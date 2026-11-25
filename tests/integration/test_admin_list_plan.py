"""The admin list cuts its page with a top-N sort in memory, whatever the number of matches.

Numbering the whole filtered set before cutting the page sorted every catalog, and with many
catalogs and the default `work_mem` that sort went to disk. Here 5,000 catalogs and a tiny
`work_mem` make any full sort spill, so the plan shows which one the statement does.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.repositories.admin_repository import _LISTS

pytestmark = pytest.mark.integration

CATALOGS = 5000


async def test_the_name_list_is_a_top_n_heapsort_that_never_spills(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO catalogs (title, status, published_at) "
            "SELECT 'Plan probe ' || md5(n::text), 'published', now() "
            "FROM generate_series(1, :n) AS n"
        ),
        {"n": CATALOGS},
    )
    await db_session.execute(text("ANALYZE catalogs"))
    await db_session.execute(text("SET LOCAL work_mem = '64kB'"))
    plan = "\n".join(
        (
            await db_session.execute(
                text(f"EXPLAIN (ANALYZE, COSTS OFF) {_LISTS['name'].text}"),
                {
                    "statuses": ["published"],
                    "kinds": None,
                    "coverages": None,
                    "publication_types": None,
                    "countries": None,
                    "languages": None,
                    "recommended": None,
                    "words": None,
                    "limit": 100,
                    "offset": 0,
                },
            )
        ).scalars()
    )

    assert "top-N heapsort" in plan, plan
    assert "external" not in plan, plan
