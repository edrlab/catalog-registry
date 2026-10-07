# Catalog Registry

[![tests](https://github.com/ronibhakta1/catalog-registry/actions/workflows/ci-test.yaml/badge.svg)](https://github.com/ronibhakta1/catalog-registry/actions/workflows/ci-test.yaml) [![lint](https://github.com/ronibhakta1/catalog-registry/actions/workflows/ci-lint.yaml/badge.svg)](https://github.com/ronibhakta1/catalog-registry/actions/workflows/ci-lint.yaml) [![schemas](https://github.com/ronibhakta1/catalog-registry/actions/workflows/ci-schema.yaml/badge.svg)](https://github.com/ronibhakta1/catalog-registry/actions/workflows/ci-schema.yaml) [![audit](https://github.com/ronibhakta1/catalog-registry/actions/workflows/ci-audit.yaml/badge.svg)](https://github.com/ronibhakta1/catalog-registry/actions/workflows/ci-audit.yaml)
[![python](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)

This project is part of EDRLab's OPDS Interoperability Task Force.

## Demo 
Live: [registry.thoriumreader.com](https://registry.thoriumreader.com)

The Catalog Registry will contain:

- public libraries
- academic libraries
- school libraries
- specialized libraries
- public domain publications
- and open access publications

This Catalog Registry will be pre-loaded in Thorium Reader (all platforms) and dedicated reading devices, providing discoverability for libraries and their patrons on a wide variety of platforms.

## User-facing features

This project will serve a registry using both OPDS 2.0 and HTML with the following feature-set:

- List of recommended catalogs (language specific)
- Full-text search (`GET /search`, see [`docs/search.md`](docs/search.md))
- Responses compressed to what each reader can decode: zstd, brotli, gzip, or plain (see [`docs/performance.md`](docs/performance.md))
- Geo-based search

## Running it

Requires [uv](https://docs.astral.sh/uv/) and Docker. Python 3.13 is fetched automatically.

```
make            # list every target, grouped
make setup      # uv sync, then .env from .env.example
make up         # database, migrations, API on http://localhost:8000
make seed       # the recommended catalogs for testing locally
make seed-libraries  # the library data set, not recommended (needs data/libraries.json)
make down       # stop, keep the data
make clean      # stop, drop the volume
```

`make up` migrates but does not seed. A registry you have just started is empty, so nothing
you delete comes back on the next start. After `make seed`:

```
$ curl -s localhost:8000/ | jq '.metadata'
{ "title": "Recommended Catalogs", "numberOfItems": 4 }
```

If port 5432 is already taken, run `make up DB_PORT=55432` and match it in `.env`. `make up`
tells you when this happens.

To try it by hand, open <http://localhost:8000/dev> after `make seed` and `make seed-libraries`. It
is a page for the feed and search as different reader apps would see them, with a search box that
updates as you type. It only exists when `REGISTRY_ENVIRONMENT=local`, so it is not in a deployed
service.

To check a running or deployed registry end to end (health, the feed, compression, and every
documented search, pass or fail):

```
make live-check
make live-check URL=https://registry.thoriumreader.com
```

| Endpoint | |
|---|---|
| `GET /` | The top-level feed, `application/opds-catalog+json`, ranked by `Accept-Language` |
| `GET /search?query=&page=` | Search, 50 per page, same media type. An empty query returns an empty feed. See [`docs/search.md`](docs/search.md) and [`docs/performance.md`](docs/performance.md) |
| `GET /catalogs/{id}` | One catalog. 404 problem+json if unknown, 422 if the uuid is malformed |
| `GET /health/live` | Liveness. Does not touch the database |
| `GET /health/ready` | Readiness. 503 when the database is unreachable |

## Documentation

| | |
|---|---|
| [`docs/development.md`](docs/development.md) | Setup, the daily loop, configuration, migrations, the seed, testing, benchmarks, the Cloud SQL sandbox, and troubleshooting |
| [`docs/search.md`](docs/search.md) | What search does, the query syntax, ordering, paging, the analyzer, reloading reference data, grants and operating notes |
| [`docs/performance.md`](docs/performance.md) | Why one search is one database round trip, where it runs, how to measure it, what compression each client gets, and what old devices can connect to |
| [`docs/search-test-cases.md`](docs/search-test-cases.md) | The searches used to check search: what each should return, what it returns today, a score, and how to run them (`make search-score`, `make live-check`) |
| [`docs/importing.md`](docs/importing.md) | `make add`, which imports a catalog from its live OPDS feed. The flags, how links are mapped, when it refuses |
| [`docs/schemas.md`](docs/schemas.md) | The `schema/` directory. What is contract, what is vendored, what is generated, and why Python cannot use these schemas as published |

`make` on its own lists every target. The listing is generated from the Makefile, so a target
appears as soon as its line carries a `## description`.

## Where the data comes from

Postgres, with thirteen tables (eight for the catalogs, five for search), four migrations and six native enums.

| | |
|---|---|
| `data/recommended.json` | The seed source. `make seed` upserts it, idempotently. Removing an entry does not unrecommend its row, see `docs/development.md` |
| `demo/` | Example output, and the contract-test corpus. Hadrien's, like the seed file |
| `data/libraries.json` | Library catalogs. `make seed-libraries` upserts them **not recommended**: active and readable at `/catalogs/{id}`, absent from the top-level feed |
| `data/dev-sample.json` | Invented data for trying ranking out by hand, loaded by `make seed-sample`. No test reads it |

## Language ranking

A catalog that declares `supportedLanguages` is returned only when the request's
`Accept-Language` matches one of them. Catalogs scoped to no language come first, because they
serve every reader. The matches follow, in the client's own order of preference, and then by
how specifically the tags agree.

Matching is a whole-subtag prefix in either direction. `fr-FR` finds a `fr` catalog, `fr` finds
a `fr-BE` one, and `fr-BE` does not match `fr-CA`.

The module docstring of `src/registry/domain/language.py` explains why neither RFC 4647
procedure works on its own. Filtering only widens, Lookup only narrows.
