"""Alembic environment, async template.

``compare_type`` and ``compare_server_default`` both default to ``False``; with them off
autogenerate silently misses type and default changes, producing a migration that looks
complete and is not.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from registry.core.config import Settings
from registry.db.base import Base
from registry.db.models import *  # noqa: F403 — registers every model on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Escaped, because Alembic stores this in a ConfigParser and `%` there means interpolation.
# A password containing a percent-encoded character, `p%40ss` for `p@ss`, otherwise fails
# every Alembic command with "invalid interpolation syntax" before it reaches the database.
config.set_main_option("sqlalchemy.url", str(Settings().database_url).replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy."
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
