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
- [Measuring the feed](#measuring-the-feed)

---

## Prerequisites

| | |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Package and Python manager. Fetches Python 3.13 itself; you do not need it installed |
| Docker | Postgres, the image build, and the test containers |

Nothing else. No system Postgres, no `pyenv`, no virtualenv to activate. `uv run` handles it.

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
3. runs `alembic upgrade head`. Eight tables, two migrations, 249 countries

It does **not** seed. `make up` is run many times a day, and a command you run that often
must not keep reinstating rows you deleted on purpose. A fresh database serves an empty feed:

```
$ curl -s localhost:8000/ | jq '.metadata'
{ "title": "Recommended Catalogs", "numberOfItems": 0 }
```

Fill it either way, the recommended catalogs, or one live feed at a time:

```
make seed
make add ARGS="https://example.org/opds --kind public"
```

**If port 5432 is already taken**. Common, another project's database. `make up` stops and
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
real, the Dockerfile once shipped `demo/` but not `data/`, and only running the runtime
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

**A typo fails at boot, loudly.** That is deliberate, the alternative is a variable that
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

- `country` and `subdivisions` must be **uppercase** (`BE`, `BE-WAL`). Lowercase is rejected on
  import with a message naming the value, not silently corrected, so fix it in the source file
- `language_tag` must be lowercase. Unlike the ISO codes it *is* normalised on import, because
  BCP-47 treats case as insignificant and `Accept-Language` arrives lowercase
- an `active` catalog must have a `published_at`
- `templated` is only allowed on a `search` link
- `(catalog_id, rel, href)` is unique across links
- `coverage` is **nullable with no default**. `NULL` means *not declared*,
  `global` means *worldwide*, and collapsing them loses information
- `id` is the catalog's only UUID. `metadata.identifier` is rendered from it
  (`urn:uuid:{id}`) and never stored separately. It is **derived, not random**: the seed uses a
  supplied `metadata.identifier` when there is one and otherwise
  `uuid5(NAMESPACE_URL, identity href)`, so the same catalog has the same id in Docker, in CI
  and on Cloud Run

---

## Migrations

```
make migrate                  # alembic upgrade head
make revision m="add x"       # autogenerate a draft
```

Rules:

- **Autogenerate produces a draft.** A human reads and edits it before it is committed.
  Autogenerate does not detect triggers, enum value changes, or index renames.
- **A merged migration is never edited.** Forward-only.
- `downgrade()` is implemented, or raises with a reason.
- One logical change per migration; data migrations separate from schema migrations.

`0001_initial_schema` is a squash of what were eight separate migrations, done once, before
anything was deployed — no environment held applied revision history to protect. That is the
only case where rewriting merged migrations is safe; it does not happen again once something
is deployed.

To confirm the models and migrations still agree, autogenerate and check it produces nothing:

```
make revision m="drift check"
# then read the file, it should be empty, and delete it
```

---

## The seed

`data/recommended.json` is the seed source — presence in the file **is** the `recommended`
flag. `demo/` is example output + contract-test corpus.

```
make seed          # idempotent
make add ARGS="https://example.org/opds --kind public"   # import a live feed instead
make seed-sample    # data/dev-sample.json — regional-English test cases
```

**Removing a catalog from the file does not unrecommend it.** `make add` rows have no
provenance to reconcile against, so unrecommending is manual until that's fixed
(`tests/integration/test_seed.py` asserts this).

**Input/output are different schemas**: seed file has no `self`/feed-level `links` (none
exist yet when it's authored), so it validates against `schema/generated/seed-input.schema.json`,
not the published `feed.schema.json`. `self` links are synthesised at render time. Detail:
[`schemas.md`](schemas.md).

**Identity**: a document that carries `metadata.identifier` is matched on the id derived from
it; everything else is matched on its `catalog`/`shelf` link href. No browsable link = rejected.
So a catalog in `data/recommended.json` survives a feed-URL change as one row, which is what
those identifiers are for. One without an identifier (`data/libraries.json`) is inserted as a
second row instead, and the fix there is to add an identifier or wipe and re-seed. See
[`importing.md`](importing.md).

**The id is derived from the document, not generated by Postgres.** A `metadata.identifier` in
the source file *is* `catalogs.id` with the `urn:uuid:` prefix stripped, so
`data/recommended.json`'s hand-assigned values are the ones clients see rendered back. A
document without the field (`data/libraries.json` has none) gets
`uuid5(NAMESPACE_URL, identity href)`. Either way the id is reproducible: it is the same in
every environment and it survives a wipe and re-seed, which is what makes `/catalogs/{id}`
usable for a catalog that no feed lists.

**Rows written before this rule keep the id they were given.** An upsert does not rewrite a
primary key, so a database seeded earlier still holds random ids, and adding an identifier to a
catalog already stored does not move it onto that id. Only a wipe and re-seed makes the two
agree. Nothing in the deploy does that: merging to `main` builds and deploys the image and runs
no migration, so a schema or data reset is always a deliberate manual step.

**Two data sets, and only one of them is recommended:**

```
make seed             # data/recommended.json  → recommended, in the top-level feed
make seed-libraries   # data/libraries.json    → active, NOT recommended, feed omits them
```

`data/libraries.json` arrives with upstream PR edrlab/catalog-registry#13. Until it merges,
`make seed-libraries` reports `data/libraries.json is not a file. Nothing to seed.` and writes
nothing. The registry works without it, and `make seed` is unaffected.

`--no-recommended` is the only difference; both go through the same validation and upsert.
`status` and `recommended` are separate columns, so a library catalog is a real published
catalog reachable at `/catalogs/{id}` while staying out of `GET /`.

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
| `tests/unit/` | none | Pure logic, language matching, link ordering, the feed service against a fake |
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
at a throwaway database**, it writes.

It asserts nothing and cannot fail the build. No latency target has ever been
stated, and an invented threshold produces flaky builds and no information
 It exists to answer "what happens at n" and to give a change a
measured before.

Latency is linear in the number of recommended catalogs, roughly 0.1 ms each, with no knee.
The query count stays flat at 6 until ~500 and then steps as `selectinload` chunks its `IN`
list. Batching, not N+1. What the decomposition shows, and the order to optimise in when
there is a reason to, is recorded in the plan.

