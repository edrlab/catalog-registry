# Catalog Registry

[![tests](https://github.com/edrlab/catalog-registry/actions/workflows/ci-test.yaml/badge.svg)](https://github.com/edrlab/catalog-registry/actions/workflows/ci-test.yaml) [![lint](https://github.com/edrlab/catalog-registry/actions/workflows/ci-lint.yaml/badge.svg)](https://github.com/edrlab/catalog-registry/actions/workflows/ci-lint.yaml) [![schemas](https://github.com/edrlab/catalog-registry/actions/workflows/ci-schema.yaml/badge.svg)](https://github.com/edrlab/catalog-registry/actions/workflows/ci-schema.yaml) [![audit](https://github.com/edrlab/catalog-registry/actions/workflows/ci-audit.yaml/badge.svg)](https://github.com/edrlab/catalog-registry/actions/workflows/ci-audit.yaml) [![docker build](https://github.com/edrlab/catalog-registry/actions/workflows/ci-docker-build.yaml/badge.svg)](https://github.com/edrlab/catalog-registry/actions/workflows/ci-docker-build.yaml)
[![python](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/) [![ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff) [![live](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fregistry.thoriumreader.com%2Fhealth%2Flive&query=%24.status&label=registry.thoriumreader.com&color=brightgreen)](https://registry.thoriumreader.com) [![cloud run revision](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fregistry.thoriumreader.com%2Fhealth%2Flive&query=%24.revision&label=Cloud%20Run%20revision&logo=googlecloud&logoColor=white&color=4285F4)](https://registry.thoriumreader.com/health/live)

This project is part of EDRLab's OPDS Interoperability Task Force.

## What it is

A registry of OPDS catalogs: public, academic, school and specialized libraries, plus public domain
and open access publications. It is pre-loaded in Thorium Reader (all platforms) and dedicated
reading devices, so readers can discover libraries on a wide variety of platforms.

Live: [registry.thoriumreader.com](https://registry.thoriumreader.com)

- Recommended catalogs, ranked by the reader's language
- Full-text search: title, place name or city ([`docs/search.md`](docs/search.md))
- Responses compressed to what each reader can decode: zstd, brotli, gzip or plain ([`docs/performance.md`](docs/performance.md))
- A dev console at `/dev` to see the feed and search as different reader apps would

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Docker. Python 3.13 is fetched automatically.

```
make setup      # uv sync, then .env from .env.example
make up         # database, migrations, API on http://localhost:8000
make seed       # the recommended catalogs
make down       # stop, keep the data
make clean      # stop, drop the volume
```

If port 5432 is taken, run `make up DB_PORT=55432` and
match it in `.env`.

`make up` migrates but does not seed, so a fresh registry is empty until `make seed`.

## Make targets

`make` on its own lists them with descriptions.

| | Targets |
|---|---|
| Run | `setup`, `up`, `down`, `clean`, `run` (API on the host, needs `up`), `logs`, `psql` |
| Data | `seed`, `seed-libraries` (needs `data/libraries.json`), `seed-sample`, `add ARGS="<url> --kind public"` |
| Database | `migrate`, `revision m="add x"`, `check-db` |
| Quality | `test`, `lint`, `fmt`, `schema-check` |
| Search | `search-score`, `search-cases`, `reference-data`, `bench N="10 1000"` |
| Check | `live-check URL=...` |
| Generate | `env`, `enums`, `seed-schema` |
| Docker | `docker-build`, `docker-run`, `stop` |

## Endpoints

Local: `http://localhost:8000`. Production: `https://registry.thoriumreader.com`. Use that name,
not the `*.run.app` address, for anything a reader device uses.

| Endpoint | |
|---|---|
| `GET /` | The top-level feed, `application/opds-catalog+json`, ranked by `Accept-Language` |
| `GET /search?query=&page=` | Search, 50 per page, same media type. An empty query returns an empty feed |
| `GET /catalogs/{id}` | One catalog. 404 problem+json if unknown, 422 if the uuid is malformed |
| `GET /dev` | The console. Reading a library's own feed from it works only on a local run |
| `GET /health/live` | Liveness. Does not touch the database |
| `GET /health/ready` | Readiness. 503 when the database is unreachable |

```
curl -s 'http://localhost:8000/search?query=wallis'
```

Check a running or deployed registry end to end (health, feed, compression, every documented search):

```
make live-check
make live-check URL=https://registry.thoriumreader.com ARGS="--deployed"
```

## Documentation

| | |
|---|---|
| [`docs/development.md`](docs/development.md) | Setup, the daily loop, configuration, migrations, the seed, testing, benchmarks, troubleshooting |
| [`docs/search.md`](docs/search.md) | The query syntax, ordering, paging, reference data and operating notes |
| [`docs/performance.md`](docs/performance.md) | Why a search is one database round trip, how to measure it, what compression each client gets |
| [`docs/search-test-cases.md`](docs/search-test-cases.md) | The searches used to check search and how to run them (`make search-score`) |
| [`docs/importing.md`](docs/importing.md) | `make add`: import a catalog from its live OPDS feed |
| [`docs/schemas.md`](docs/schemas.md) | The `schema/` directory: contract, vendored and generated files |

## Data

| | |
|---|---|
| `data/recommended.json` | Seed source for `make seed`, idempotent. Removing an entry does not unrecommend its row (see `docs/development.md`) |
| `data/libraries.json` | `make seed-libraries`: active and readable at `/catalogs/{id}`, **not recommended**, so absent from the top-level feed |
| `data/dev-sample.json` | Invented data for trying ranking by hand (`make seed-sample`) |
| `demo/` | Example output and the contract-test corpus |

## Language ranking

A catalog that declares `supportedLanguages` is returned only when the request's
`Accept-Language` matches one of them. Catalogs scoped to no language come first, because they
serve every reader. The matches follow, in the client's own order of preference, and then by
how specifically the tags agree.

Matching is a whole-subtag prefix in either direction. `fr-FR` finds a `fr` catalog, `fr` finds
a `fr-BE` one, and `fr-BE` does not match `fr-CA`.

The module docstring of `src/registry/domain/language.py` explains why neither RFC 4647
procedure works on its own. Filtering only widens, Lookup only narrows.
