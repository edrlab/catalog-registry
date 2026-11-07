# uv 0.12.6, pinned by digest. A stage of its own because Dependabot updates FROM lines, not
# images named in COPY --from.
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1
WORKDIR /app

# uv only where something is installed; the runtime image runs the finished venv without it.
FROM base AS build
COPY --from=uv /uv /usr/local/bin/uv

# Dependencies before source: dependencies change rarely, source changes constantly, so a
# code edit rebuilds one layer instead of reinstalling the world.
FROM build AS dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

FROM build AS development
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "registry.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM base AS runtime
RUN useradd --create-home --uid 1000 app
COPY --from=dependencies --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/ ./src/
COPY --chown=app:app migrations/ ./migrations/
COPY --chown=app:app alembic.ini ./
# Runtime assets, not just repository files: the seed reads data/ and validates it against
# the generated schema under schema/. demo/ is the contract-test corpus and stays out.
COPY --chown=app:app schema/ ./schema/
COPY --chown=app:app data/ ./data/
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src"
USER app
EXPOSE 8000
# Shell form so $PORT expands: Cloud Run injects the port to listen on and a container that
# ignores it never passes its health check. Defaults to 8000 everywhere else.
CMD ["sh", "-c", "exec uvicorn registry.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
