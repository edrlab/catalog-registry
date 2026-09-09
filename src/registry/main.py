"""Application factory, lifespan, and wiring. No business logic lives here.

This is the composition root: the one place that knows both the `CatalogReader` protocol the
service depends on and the SQLAlchemy repository that satisfies it. Routes reach it through
`app.state`, which is what keeps `api` free of any import from `repositories`.

A factory rather than a module-level ``app``: tests need to build an app with different
settings, and a module-level instance makes that impossible without import tricks.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI

from registry.api.exception_handlers import register_exception_handlers
from registry.api.middleware import RequestIdMiddleware
from registry.api.routes import catalogs, feed, health
from registry.core.config import Settings
from registry.db.session import (
    build_session_factory,
    check_database_connection,
    create_database_engine,
)
from registry.repositories.catalog_repository import CatalogRepository


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(resolved)
        app.state.settings = resolved
        app.state.engine = engine
        app.state.session_factory = build_session_factory(engine)
        app.state.check_database_connection = partial(check_database_connection, engine)

        @asynccontextmanager
        async def open_catalog_reader() -> AsyncIterator[CatalogRepository]:
            """One session per request, closed when the request ends."""
            async with app.state.session_factory() as session:
                yield CatalogRepository(session)

        app.state.open_catalog_reader = open_catalog_reader
        yield
        await engine.dispose()

    app = FastAPI(title="OPDS Catalog Registry", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(feed.router)
    app.include_router(catalogs.router)
    app.include_router(health.router)
    return app
