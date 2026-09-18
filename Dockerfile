FROM python:3.13-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies before source: dependencies change rarely, source changes constantly, so a
# code edit rebuilds one layer instead of reinstalling the world.
FROM base AS dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

FROM base AS development
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "registry.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

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
CMD ["sh", "-c", "exec uvicorn registry.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
