"""Confirm a database is usable by this service before anything depends on it.

Written for the Phase 0 connectivity task: point `REGISTRY_DATABASE_URL` at Cloud SQL
(through the Auth Proxy) and run it. Enabling an extension on Cloud SQL is an administrative
action someone else performs, so discovering a missing one at the start of v0.2 costs a
round trip — this makes that discovery cheap and early.

    cloud-sql-proxy --port 5433 PROJECT:REGION:INSTANCE
    REGISTRY_DATABASE_URL=postgresql+asyncpg://USER:PASS@localhost:5433/DB \
        uv run python scripts/check_database.py
"""

import asyncio
import sys

from sqlalchemy import text

from registry.core.config import Settings
from registry.db.session import create_database_engine

#: name → why this service cares, and which version needs it.
EXTENSIONS = {
    "pgcrypto": "gen_random_uuid() — v0 (built into PostgreSQL 13+, so often not required)",
    "pg_trgm": "fuzzy search — v0.2",
    "postgis": "geo ranking — v1.2 (not needed yet; worth knowing now)",
}


async def report() -> int:
    settings = Settings()
    engine = create_database_engine(settings)
    problems = 0

    try:
        async with engine.connect() as connection:
            version = await connection.scalar(text("SELECT version()"))
            database = await connection.scalar(text("SELECT current_database()"))
            print(f"connected to {database}")
            print(f"  {version}")

            uuid_value = await connection.scalar(text("SELECT gen_random_uuid()"))
            print(f"  gen_random_uuid() -> {uuid_value}")

            rows = (
                await connection.execute(
                    text(
                        "SELECT name, default_version, installed_version "
                        "FROM pg_available_extensions WHERE name = ANY(:names)"
                    ),
                    {"names": list(EXTENSIONS)},
                )
            ).all()
            available = {name: (default, installed) for name, default, installed in rows}

            print("\nextensions:")
            for name, reason in EXTENSIONS.items():
                if name not in available:
                    state = "NOT AVAILABLE on this instance"
                    problems += name != "postgis"  # postgis is not needed until v1.2
                elif available[name][1]:
                    state = f"installed {available[name][1]}"
                else:
                    state = f"available {available[name][0]}, not installed"
                print(f"  {name:<10} {state:<40} {reason}")
    except Exception as exc:
        print(f"FAILED to connect: {exc}")
        return 1
    finally:
        await engine.dispose()

    print("\nnot checkable from SQL — confirm by hand:")
    print("  region (Q9), and that this instance is a sandbox, not production")
    return 1 if problems else 0


def main() -> int:
    return asyncio.run(report())


if __name__ == "__main__":
    sys.exit(main())
