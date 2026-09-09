# Development guide

Everything needed to get this repository running, change it, and diagnose it when it
misbehaves. `README.md` is the short version; this is the long one.

- [Prerequisites](#prerequisites)
- [First run](#first-run)
- [The daily loop](#the-daily-loop)
- [Three ways to run the service](#three-ways-to-run-the-service)
- [Configuration](#configuration)
- [The database](#the-database)
- [Migrations](#migrations)
- [The seed](#the-seed)
- [Generated files](#generated-files)
- [Testing](#testing)
- [The Cloud SQL sandbox](#the-cloud-sql-sandbox)
- [Troubleshooting](#troubleshooting)
- [Open questions that affect the code](#open-questions-that-affect-the-code)

---

## Prerequisites

| | |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Package and Python manager. Fetches Python 3.13 itself; you do not need it installed |
| Docker | Postgres, the image build, and the test containers |

Nothing else. No system Postgres, no `pyenv`, no virtualenv to activate — `uv run` handles it.

`make` on its own lists every target, grouped. The listing is generated from the Makefile, so
it is never out of date.

---

## First run

```
git clone <this repo>
cd catalog-registry
make setup
make up
```

`make setup` installs dependencies and copies `.env.example` to `.env` if you have no `.env`.

`make up` does three things in order, and each depends on the last:

1. builds the image and starts Postgres and the API under Docker Compose
2. checks the database container is actually reachable on the compose network
3. runs `alembic upgrade head` — eight tables, seven migrations, 249 countries

It does **not** seed. `make up` is run many times a day, and a command you run that often
must not keep reinstating rows you deleted on purpose. A fresh database serves an empty feed:

```
$ curl -s localhost:8000/ | jq '.metadata'
{ "title": "Recommended Catalogs", "numberOfItems": 0 }
```

Fill it either way — the recommended catalogs, or one live feed at a time:

```
make seed
make add ARGS="https://example.org/opds --kind public"
```

**If port 5432 is already taken** — common, another project's database — `make up` stops and
tells you, rather than half-starting. Pick another port and match it in `.env`:

```
make up DB_PORT=55432
```
```
REGISTRY_DATABASE_URL=postgresql+asyncpg://registry:registry@localhost:55432/registry
```

The `.env` line matters only for `make run` and `make test`, which connect from your machine.
The containers talk to each other over the compose network and ignore it.

---

## The daily loop

```
make run        # local uvicorn, hot reload, against the compose database
make test       # full suite
make lint       # ruff check, ruff format --check, mypy --strict
make fmt        # apply ruff fixes and formatting
```

`make up` can stay running the whole time; `make run` just talks to its database.

Before pushing, `make lint && make test` is what CI runs, minus the schema jobs
(`make schema-check`).

---

## Three ways to run the service

| | `make run` | `make up` | `make docker-run` |
|---|---|---|---|
| What | local uvicorn process | compose stack | the runtime image |
| Image stage | none | `development` | `runtime` |
| Source | your working tree | bind-mounted, hot reload | **baked into the image** |
| Port | 8000 | 8000 | 8001 |
| Stop with | Ctrl-C | `make down` | `make stop` |

`make docker-run` exists for one reason: `make up` runs the **development** stage with your
source mounted, so it cannot catch *"works locally, missing from the image"*. That bug is
real — the Dockerfile once shipped `demo/` but not `data/`, and only running the runtime
image found it. It joins the compose network and shares the database, so both can run at
once and you can compare them.

It needs the compose stack up. To point it elsewhere:

```
make docker-run IMAGE_DSN=postgresql+asyncpg://user:pass@host:5432/db
```

---

## Configuration

All settings are read in exactly one place, `registry.core.config.Settings`, and are prefixed
`REGISTRY_`.

| Variable | Default | Notes |
|---|---|---|
| `REGISTRY_DATABASE_URL` | **required** | No default, so no deployment silently falls back to a development database |
| `REGISTRY_SEED_FILE` | `data/recommended.json` | What `make seed` imports |
| `REGISTRY_ENVIRONMENT` | `local` | `local\|test\|staging\|production` |
| `REGISTRY_BASE_URL` | `http://localhost:8000` | Used to build absolute `self` links |

Two rules that will bite you otherwise:

**Only `REGISTRY_*` keys may live in `.env`.** `Settings` uses `extra="forbid"` and reads
every key in the file, so an unprefixed one fails the service at boot. Compose variables like
`DB_PORT` go in the shell or on the `make` command line, never in `.env`.

**A typo fails at boot, loudly.** That is deliberate — the alternative is a variable that
silently does nothing for six weeks. Renaming a setting has the same effect, which is how a
stale `.env` gets caught.

`.env.example` is **generated** from the `Settings` class (see
[Generated files](#generated-files)); do not edit it by hand.

---

## The database

**Local Postgres is the development database. The Cloud SQL sandbox is not.**

Local development and CI use containers; production uses Cloud SQL. A shared cloud
development database means two
developers running migrations against the same schema and clobbering each other, destructive
migration testing becomes frightening, and test runs become slow and order-dependent. A
container per developer and a fresh one per CI run removes all of it.

Eight tables:

```
catalogs ├── catalog_kinds ├── catalog_publication_types
         ├── catalog_languages ├── catalog_subdivisions → subdivisions → countries
         └── links
```

Six native Postgres enums, generated from `schema/catalog.schema.json`. `make psql` opens a
shell on the development database.

Constraints worth knowing, because they will reject your data rather than quietly accept it:

- `language_tag` must be lowercase, `country_code` and `subdivision_code` uppercase
- an `active` catalog must have a `published_at`
- `templated` is only allowed on a `search` link
- `(catalog_id, rel, href)` is unique across links
- `coverage` is **nullable with no default** (ADR-032) — `NULL` means *not declared*,
  `global` means *worldwide*, and collapsing them loses information

---

## Migrations

```
make migrate                  # alembic upgrade head
make revision m="add x"       # autogenerate a draft
```

Rules, from R6:

- **Autogenerate produces a draft.** A human reads and edits it before it is committed.
  Autogenerate does not detect triggers, enum value changes, or index renames.
- **A merged migration is never edited.** Forward-only.
- `downgrade()` is implemented, or raises with a reason.
- One logical change per migration; data migrations separate from schema migrations.

To confirm the models and migrations still agree, autogenerate and check it produces nothing:

```
make revision m="drift check"
# then read the file — it should be empty — and delete it
```

---

## The seed

`data/recommended.json` is the seed source (ADR-029). Four catalogs, and **presence in the
file is the `recommended` flag**. `demo/` is example *output* and the contract-test corpus;
`archive/` is out of scope for v0.

```
make seed        # idempotent; run it as often as you like
```

To import a catalog from its live feed instead of the file, use `make add` —
see [`importing.md`](importing.md). `make seed-sample` loads `data/dev-sample.json`, invented
catalogs covering the regional-English cases a browser sends, for trying ranking out by hand.

### Input and output are deliberately different schemas

The seed file has no feed-level `links` and no `self` link on any catalog, so the published
`feed.schema.json` rejects every record — a `self` link would have to name a registry that did
not exist when the file was authored. Input therefore validates against
`schema/generated/seed-input.schema.json`, derived from the published schemas; `self` links are
synthesised at render time; output validates against the published schemas in the contract
tests. That is ADR-030, and [`schemas.md`](schemas.md) has the detail.

### Identity across runs

Catalogs are matched on their `catalog` rel href — the library's own feed URL, which is
externally owned and stable. `self` hrefs all point at `edrlab.github.io` and change at
cutover, which would silently duplicate every catalog. A catalog with only a `shelf` link
falls back to that; one with neither is rejected rather than inserted under an identity that
cannot be matched again. This is **Q1**, still formally open.

---

## Generated files

Three files are generated and committed. Each is diffed by CI, so a stale copy fails the build
instead of drifting quietly.

| File | From | Regenerate |
|---|---|---|
| `.env.example` | `Settings` | `make env` |
| `src/registry/domain/enums.py` | `schema/catalog.schema.json` | `make enums` |
| `schema/generated/seed-input.schema.json` | the published schemas | `make seed-schema` |

`.env.example` also has a test asserting the committed copy matches, so a stale one fails
`make test` locally too, not only in CI.

Add a field to `Settings`, or Hadrien edits a schema → run the matching target, commit both
the source and the generated file in the same change.

---

## Testing

```
make test
```

100 tests, five layers:

| Layer | Database | Proves |
|---|---|---|
| `tests/unit/` | none | Pure logic — language matching, link ordering, the feed service against a fake |
| `tests/integration/` | real | Queries, constraints, migrations, the seed |
| `tests/contract/` | real | Output validates against the repo's own JSON Schemas |
| `tests/e2e/` | real | The HTTP surface end to end |
| `tests/architecture/` | none | Layer boundaries are not violated |

**No mocked database.** A mocked session proves the code calls the methods the mock expects,
which is a tautology; it cannot catch a constraint violation, an enum mismatch, a broken
migration, or an N+1.

**Where the database comes from.** With `TEST_DATABASE_URL` set, the suite uses it; without,
testcontainers starts a fresh Postgres. CI sets it, because GitHub Actions already provides
the container and health-check wiring.

**The suite refuses to run against anything that is not a local throwaway database**, because
it applies migrations and writes rows. The Cloud SQL Auth Proxy publishes a remote instance on
`localhost`, so the guard also checks the database name looks like a test database.
`ALLOW_REMOTE_TEST_DATABASE=1` overrides it, deliberately.

Isolation is by **rollback, not truncation**: each test runs in one outer transaction that is
rolled back. Sessions join it with `join_transaction_mode="create_savepoint"`, so application
code calling `commit()` releases a savepoint and the outer rollback still undoes everything.

---

## Measuring the feed

```
make bench                 # 10, 100, 1000 recommended catalogs
make bench N="10 5000"     # any sizes
```

It inserts synthetic catalogs titled `ZZ Bench …`, issues 120 real HTTP requests per size with
a different `Accept-Language` each time, and deletes its rows in a `finally` block. **Point it
at a throwaway database** — it writes.

It asserts nothing and cannot fail the build. Q20 records that no latency target has ever been
stated, and an invented threshold produces flaky builds and no information
(`conventions/testing.md` §9). It exists to answer "what happens at n" and to give a change a
measured before.

Latency is linear in the number of recommended catalogs, roughly 0.1 ms each, with no knee.
The query count stays flat at 6 until ~500 and then steps as `selectinload` chunks its `IN`
list — batching, not N+1. What the decomposition shows, and the order to optimise in when
there is a reason to, is ADR-035.

---

## The Cloud SQL sandbox

A third thing: not the dev loop, not production. Somewhere to verify the connection path and
extension availability early, and to let other people try the service.

```
cloud-sql-proxy --port 5433 PROJECT:REGION:INSTANCE

REGISTRY_DATABASE_URL=postgresql+asyncpg://USER:PASS@localhost:5433/DB \
    uv run make check-db
```

`make check-db` reports the server version, whether `gen_random_uuid()` works, and whether
`pgcrypto`, `pg_trgm` and `postgis` are available. **`pg_trgm` is the one that can stall a
phase** — enabling an extension on Cloud SQL is an administrative action someone else
performs, so if it comes back unavailable, ask now rather than at the start of v0.2. Non-zero
exit if `pgcrypto` or `pg_trgm` are missing; `postgis` only reports, since it is not needed
until v1.2.

Do not put sandbox credentials in `.env`. They are real credentials for a shared server, not
the throwaway `registry:registry`. Deployed environments read secrets from Google Secret
Manager.

---

## Troubleshooting

### `make up` says port 5432 is already in use

Another project's database has it. Use a different host port and match it in `.env`:

```
make up DB_PORT=55432
```

### `Name or service not known` from alembic, or `db` will not resolve

The database container is running but attached to no network. This happens when a port
collision makes Docker fail *while* wiring the container up: the container is left created but
detached, and its healthcheck still passes, because `pg_isready` runs inside the container and
never touches the network. Compose reports success and the failure surfaces later as an
unresolvable hostname.

`make up` now detects this and recreates the container itself. To confirm what happened:

```
docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' catalog-registry-db-1
```

Empty output means no network. `make down && make up` repairs it.

### `extra_forbidden` at boot, naming a key you did not add

Your `.env` has a key `Settings` does not know — usually a setting that was renamed, or a
compose variable like `DB_PORT` that does not belong there. Regenerate and re-copy:

```
make env
cp .env.example .env      # then re-apply your DSN if you changed the port
```

### `.env.example is stale` from `make test`

You added or renamed a `Settings` field. Run `make env` and commit the result.

### The API serves old code after a change

`make up` always rebuilds, so this should not happen. If it does, `make clean && make up`
drops the volume and rebuilds from scratch.

### `make docker-run` says the compose database is not running

It needs `make up` first, or an explicit `IMAGE_DSN=`.

### Postgres 18 refuses to start, mentioning `pg_upgrade`

The volume is mounted at the pre-18 path. `compose.override.yaml` mounts
`/var/lib/postgresql`, not `/var/lib/postgresql/data`. If you have an old volume from before
that fix, `make clean` drops it.

---

## Open questions that affect the code

These are not the coding agent's to close (`architecture/open-questions.md`). Each is marked
in the code where it bites.

| Q | Question | Where it shows up |
|---|---|---|
| **Q1** | Upsert conflict target | `cli/seed.py` — currently the `catalog` rel href, with `shelf` as fallback |
| **Q9** | GCP region | Deployment only |
| **Q22** | Which milestone integrator filtering lands in | Nothing in v0 changes either way |
| **Q26** | Some seed catalogs link to `text/html`, not OPDS | Hadrien's own eReader filter would drop them. Possibly missing links rather than intent |

`architecture/open-questions.md` in the knowledge base is the full list, with owners.
