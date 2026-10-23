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

from registry.api.compression import CompressionMiddleware, configure_client_log
from registry.api.exception_handlers import register_exception_handlers
from registry.api.middleware import PublicCorsMiddleware, ResponseHeadersMiddleware
from registry.api.routes import admin, catalogs, dev, feed, health, search
from registry.core.config import Settings
from registry.db.session import (
    build_read_engine,
    build_session_factory,
    check_database_connection,
    create_database_engine,
)
from registry.repositories.actor_repository import ActorRepository
from registry.repositories.admin_repository import AdminRepository
from registry.repositories.catalog_repository import (
    READ_CONNECTION_SETTINGS,
    CatalogRepository,
)
from registry.repositories.search_repository import (
    SEARCH_CONNECTION_SETTINGS,
    CatalogSearchRepository,
)
from registry.services.admin_identity import build_jwks_key_resolver


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(resolved)
        app.state.settings = resolved
        app.state.engine = engine
        # The feed and one catalog are reads: their own pool, read-only, with a timeout (ADR-062).
        read_engine = build_read_engine(resolved, READ_CONNECTION_SETTINGS)
        app.state.read_engine = read_engine
        app.state.session_factory = build_session_factory(read_engine)
        app.state.check_database_connection = partial(check_database_connection, engine)

        @asynccontextmanager
        async def open_catalog_reader() -> AsyncIterator[CatalogRepository]:
            """One session per request, closed when the request ends."""
            async with app.state.session_factory() as session:
                yield CatalogRepository(session)

        app.state.open_catalog_reader = open_catalog_reader

        # Search has its own pool: the timeout, read-only mode and typo threshold ride on its
        # connections, and nothing of that reaches the feed or the importers (ADR-058, ADR-060).
        search_engine = build_read_engine(resolved, SEARCH_CONNECTION_SETTINGS)
        app.state.search_engine = search_engine
        app.state.search_session_factory = build_session_factory(search_engine)

        @asynccontextmanager
        async def open_catalog_searcher() -> AsyncIterator[CatalogSearchRepository]:
            async with app.state.search_session_factory() as session:
                yield CatalogSearchRepository(session)

        app.state.open_catalog_searcher = open_catalog_searcher

        # The admin API. Its reads share the catalog read pool. A login is a write, so the actor
        # store takes the main engine.
        app.state.write_session_factory = build_session_factory(engine)

        @asynccontextmanager
        async def open_admin_reader() -> AsyncIterator[AdminRepository]:
            async with app.state.session_factory() as session:
                yield AdminRepository(session)

        @asynccontextmanager
        async def open_actor_store() -> AsyncIterator[ActorRepository]:
            async with app.state.write_session_factory() as session:
                yield ActorRepository(session)

        app.state.open_admin_reader = open_admin_reader
        app.state.open_actor_store = open_actor_store
        # None when no issuer is configured: a token then cannot be checked and is a 401.
        app.state.resolve_access_key = (
            build_jwks_key_resolver(resolved.admin_jwks_url) if resolved.admin_jwks_url else None
        )
        yield
        await search_engine.dispose()
        await read_engine.dispose()
        await engine.dispose()

    app = FastAPI(title="OPDS Catalog Registry", version="0.1.0", lifespan=lifespan)
    # Innermost, so it compresses the finished body and the others see the final headers. Chooses
    # `br`, `gzip` or nothing from the client's `Accept-Encoding` and logs who asked (ADR-061).
    configure_client_log()
    app.add_middleware(CompressionMiddleware)
    # Public data, and browser-based readers are a legitimate
    # consumer. Read methods only, no credentials: `*` with credentials is what turns a public
    # read into a session-riding write. `/admin` is excluded: it is never called from a browser.
    app.add_middleware(
        PublicCorsMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_credentials=False,
    )
    app.add_middleware(ResponseHeadersMiddleware)
    register_exception_handlers(app)
    app.include_router(feed.router)
    app.include_router(catalogs.router)
    app.include_router(search.router)
    app.include_router(health.router)
    app.include_router(admin.router)
    # The console page and its simulator read only what the registry already serves publicly, so
    # they are mounted everywhere (ADR-063).
    app.include_router(dev.router)
    if resolved.environment == "local":
        # Reading a library's own feed makes the server request a URL a visitor names, so it is
        # never mounted in a deployed environment. Imported here so no other environment loads it,
        # or the dev-only `httpx` it needs.
        from registry.api.routes import dev_fetch  # noqa: PLC0415

        app.include_router(dev_fetch.router)
    return app
