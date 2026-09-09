IMAGE   := catalog-registry
NETWORK := catalog-registry_default

# PORT is the local process and the compose API. IMAGE_PORT is `make docker-run`, given its
# own default so the runtime image can sit alongside a running `make up` instead of
# colliding with it on 8000.
PORT       ?= 8000
IMAGE_PORT ?= 8001

# The runtime image talks to the compose database over the compose network, where the host
# is `db`. A DSN naming `localhost` would resolve to the container itself.
IMAGE_DSN  ?= postgresql+asyncpg://registry:registry@db:5432/registry

# Host port for the compose database. Exported so `make up DB_PORT=55432` reaches compose,
# which reads it from the environment.
DB_PORT ?= 5432
export DB_PORT

.DEFAULT_GOAL := help

.PHONY: help setup env enums seed-schema up down clean run seed add seed-sample migrate revision psql \
        test lint fmt bench check-db schema-check docker-build docker-run logs stop

# Self-documenting: a target appears here when its line carries a `## ` description, and
# `##@ ` starts a section. Nothing to keep in step, add a target with `## what it does`
# and it shows up.
help:  ## List the available targets
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ {printf "\n%s\n", substr($$0, 5)} \
		/^[a-zA-Z_-]+:.*##/ {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  up, psql and logs run inside Docker. run, seed, add, migrate and revision"
	@echo "  are host processes reaching the database through its published port, so"
	@echo "  REGISTRY_DATABASE_URL in .env must name the same port as DB_PORT."
	@echo
	@echo "  Ports: PORT ($(PORT)) for 'make run', IMAGE_PORT ($(IMAGE_PORT)) for"
	@echo "  'make docker-run', DB_PORT ($(DB_PORT)) for the database."

##@ Getting started

setup:  ## Install dependencies and create .env if it is missing
	uv sync
	@test -f .env || cp .env.example .env

# Database, migrations, API. Deliberately *not* the seed: `make up` is run constantly, and a
# command you run constantly must not keep reinstating rows you removed on purpose. Run
# `make seed` when you want Hadrien's four back.
up:  ## Start db + API in Docker and migrate (no seed)
	@# A busy host port makes Docker fail *while* wiring the container up, leaving it created
	@# but attached to no network. Its healthcheck still passes, because pg_isready runs
	@# inside the container and never touches the network, so compose reports success and
	@# the failure surfaces minutes later as an unresolvable hostname. Refuse early instead.
	@if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:$(DB_PORT) -sTCP:LISTEN >/dev/null 2>&1 \
	    && [ -z "$$(docker compose ps -q db 2>/dev/null)" ]; then \
		echo "Port $(DB_PORT) is already in use by something that is not this project."; \
		echo "  Use another port:  make up DB_PORT=55432"; \
		echo "  Then match it in .env so 'make run' reaches the same database:"; \
		echo "    REGISTRY_DATABASE_URL=postgresql+asyncpg://registry:registry@localhost:55432/registry"; \
		exit 1; \
	fi
	@# --build, always: compose reuses an existing image otherwise, which means `make up`
	@# can silently serve code from a previous build.
	docker compose up -d --wait --build
	@# Trust nothing: a healthy db container can still be off the network (see above).
	@if ! docker compose exec -T api getent hosts db >/dev/null 2>&1; then \
		echo "The database container is not on the compose network, recreating it."; \
		docker compose up -d --force-recreate --wait; \
	fi
	docker compose exec -T api alembic upgrade head
	@echo "Running on http://localhost:$(PORT), empty. 'make seed' for the recommended catalogs."

down:  ## Stop the stack, keeping the database volume
	docker compose down

clean:  ## Stop the stack and drop the database volume
	docker compose down -v

##@ Development

# Local process against the compose database. Ctrl-C to stop. Not the container, see
# docker-run. Catalog JSON is read by `make seed`, not at request time, so no reload glob.
run:  ## Run the API on the host instead of in Docker (needs make up)
	uv run uvicorn registry.main:create_app --factory --reload --port $(PORT)

seed:  ## Import data/recommended.json into the database (needs make up)
	uv run python -m registry.cli seed

# The editorial fields are not in anyone's feed, so they are flags. --dry-run prints the
# document and writes nothing.
add:  ## Import one catalog from its live feed URL: make add ARGS="<url> --kind public"
	uv run python -m registry.cli add $(ARGS)

# Invented catalogs covering the regional-English cases a browser actually sends. Not
# Hadrien's data and not a fixture any test reads, it exists to be looked at by hand.
seed-sample:  ## Import data/dev-sample.json to try ranking out
	REGISTRY_SEED_FILE=data/dev-sample.json uv run python -m registry.cli seed

migrate:  ## Apply migrations (needs make up)
	uv run alembic upgrade head

# A draft. A human reads and edits it before it is committed.
revision:  ## Autogenerate a migration draft: make revision m="add x"
	uv run alembic revision --autogenerate -m "$(m)"

psql:  ## Open a psql shell on the development database
	docker compose exec db psql -U registry -d registry

##@ Checks

test:  ## Run the test suite against a throwaway Postgres container
	uv run pytest

lint:  ## ruff check, ruff format --check, mypy --strict
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src/

fmt:  ## Apply ruff fixes and formatting
	uv run ruff check --fix .
	uv run ruff format .

schema-check:  ## Validate schema/ and every fixture under demo/ and data/
	uv run python scripts/validate_schemas.py
	uv run python scripts/validate_fixtures.py demo

# Confirm a database is usable before anything depends on it, point REGISTRY_DATABASE_URL
# at Cloud SQL through the Auth Proxy and run this. See README, "The Cloud SQL sandbox".
# No latency target has ever been agreed, so this asserts nothing. It answers "what
# happens at n" on demand, and gives any future optimisation a measured before.
bench:  ## Measure GET / as the recommended set grows: make bench N="10 1000"
	uv run python scripts/benchmark_feed.py $(N)

check-db:  ## Report a database's version and extension availability
	uv run python scripts/check_database.py

##@ Generated files

# Each of these is committed and diffed by CI, so a stale copy fails the build rather than
# drifting quietly.

env:  ## Regenerate .env.example from Settings
	uv run python scripts/write_env_example.py

enums:  ## Regenerate domain/enums.py from schema/catalog.schema.json
	uv run python scripts/generate_enums.py

seed-schema:  ## Regenerate the relaxed seed-input schema
	uv run python scripts/generate_seed_schema.py

##@ Container

docker-build:  ## Build the runtime image
	docker build -f docker/Dockerfile -t $(IMAGE) .

# `make up` runs the *development* target, with your source bind-mounted. This runs the
# runtime image, the artifact that actually deploys. Against the same database, which is
# the only way to catch "works locally, missing from the image" before a deploy does.
docker-run: docker-build  ## Run the deployable image, not the dev one (needs make up)
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || { \
		echo "The compose database is not running. Start it first:"; \
		echo "    make up"; \
		echo "Or point the image at another database:"; \
		echo "    make docker-run IMAGE_DSN=postgresql+asyncpg://user:pass@host:5432/db"; \
		exit 1; }
	@docker rm -f $(IMAGE) >/dev/null 2>&1 || true
	@docker run --rm -d --name $(IMAGE) --network $(NETWORK) -p $(IMAGE_PORT):8000 \
		-e REGISTRY_DATABASE_URL="$(IMAGE_DSN)" $(IMAGE) >/dev/null
	@echo "$(IMAGE) on http://localhost:$(IMAGE_PORT). 'make logs' to follow, 'make stop' to stop"

# Two things can be running, and which one you meant is not worth having to remember:
# `make docker-run` starts a standalone container named $(IMAGE), while `make up` starts
# compose, whose API container is $(IMAGE)-api-1. Follow whichever is actually up.
logs:  ## Follow the logs of whichever API is running
	@if [ -n "$$(docker ps -q -f name=^$(IMAGE)$$)" ]; then \
		docker logs -f $(IMAGE); \
	elif [ -n "$$(docker compose ps -q api 2>/dev/null)" ]; then \
		docker compose logs -f api; \
	else \
		echo "Nothing is running. Start one:"; \
		echo "    make up          the development stack"; \
		echo "    make docker-run  the runtime image"; \
		exit 1; \
	fi

# Only the standalone container: `make run` is a foreground process stopped with Ctrl-C, and
# the compose stack is stopped with `make down`.
stop:  ## Stop the runtime container started by docker-run
	@if docker stop $(IMAGE) >/dev/null 2>&1; then \
		echo "stopped $(IMAGE)"; \
	elif [ -n "$$(docker compose ps -q api 2>/dev/null)" ]; then \
		echo "$(IMAGE) is not running, but the compose stack is, stop it with 'make down'."; \
	else \
		echo "$(IMAGE) is not running."; \
	fi
