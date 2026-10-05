# Search

`GET /search?query=…&page=N` finds catalogs by title, place name or city, in the languages
the place speaks. Design and decisions: ADR-051 to ADR-059 in the knowledge base.

```
curl 'http://localhost:8000/search?query=wallis'      # Médiathèque Valais (German name of Valais)
curl 'http://localhost:8000/search?query=bruxels'     # Belgian catalogs, despite the typo
curl 'http://localhost:8000/search?query="numérique de paris"'
```

The response is an OPDS feed titled "Search results", validated against `schema/feed.schema.json`.
The top-level feed advertises it with a templated `search` link, `/search{?query}`.

## What a query can do

| You type | Effect |
|---|---|
| `standard ebooks` | Any word. Catalogs matching more words, or matching in a more important field, rank higher |
| `"numérique de paris"` | A phrase: the words must appear together, in order |
| `bibliotheque -paris` | `-` excludes catalogs containing that word (or `-"a phrase"`), from every match |
| `Suisse`, `Schweiz`, `Svizzera` | Place names work in English and in the official languages of the place's country |
| `parís`, `PARIS`, `Suiße` | Case and accents never matter. Folding is done in the database (ADR-059) |
| `bruxels` | Typos and plurals are caught by trigram similarity, listed after exact matches |
| `paris OR wallis` | `OR` is accepted and ignored: every search is already any-word |

An empty query, spaces, punctuation only, or only excluded words returns an **empty feed**, not
every catalog, and does no database work. Input is cut at 256 characters and 16 chunks.

Not searchable: supported languages ("italian" finds nothing by language; a language filter is
planned), and descriptions.

## How results are ordered

1. **Word matches first**, scored by `ts_rank` with weights per field: title 1.0, country names
   0.4, subdivision names and city 0.2 (the Notion order, open question Q3).
2. **Trigram-only matches next**, by similarity. A catalog found by both is a word match.
3. Ties: newest first, then id. The order is deterministic, so pages never overlap or skip.

Changing the weight order is one constant, `RANK_WEIGHTS` in
`src/registry/repositories/search_repository.py`, with no reindex.

## Paging

50 results per page, `page` from 1 to 1000 (422 outside that). `numberOfItems` is the total
number of matches and is still reported on a page past the end. Links: `self`, `search`,
`first`, and `previous` / `next` only when they exist.

## The analyzer

Postgres's equivalent of an Elasticsearch analyzer is a text search configuration:
`public.registry_simple` = default parser, then `unaccent`, then `simple`. No stemming and no
stop words, for titles and place names alike. The consequence, accepted for now (Q5): short
common words such as "de" match anything containing them, ranked below the real match.

## What is indexed

`catalog_search` has one row per **active** catalog, built by the database from the catalog's
title, city, country and subdivisions plus the reference names:

| Label | Field |
|---|---|
| A | title |
| B | country names: English and the country's official languages |
| C | subdivision names (English and the official languages of their country), and the city |

The row is maintained by triggers in the same transaction as the change (ADR-056). Seeding,
`make add`, the future back office and hand-written SQL all keep it current. Nothing in the
application writes to it.

## Reference data

Official languages and place names come from CLDR 48.2, subdivision types from ISO 3166-2
(pycountry 26.2.16), loaded by the migration `2eb49a8f4664` as literals, so every environment
has them after `alembic upgrade head`.

To review a CLDR or pycountry bump:

```
make reference-data          # CSVs in .cache/reference-data/out/ (gitignored)
make reference-data PY=1     # also prints the rows as Python literals
```

then add a **new** data migration carrying the difference and ending with
`SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs;`. Never edit a merged
migration. A migration that adds a subdivision must add its names too (`subdivision_names`
only holds rows for subdivisions present in `subdivisions`).

## Operating it

- **Extensions.** The migration runs `CREATE EXTENSION IF NOT EXISTS unaccent` and `pg_trgm`.
  On Cloud SQL the migration user needs the `cloudsqlsuperuser` role.
- **Grants.** Triggers run with the rights of the user making the change. If the app connects as
  a different database user from the one that migrates, grant it `INSERT, UPDATE, DELETE` on
  `catalog_search` and `SELECT` on `country_languages`, `country_names`, `subdivision_names`
  and `country_subdivision_types`, or catalog saves fail.
- **Timeout.** Searches run in a read-only transaction with a 1 second statement timeout; a
  slow one answers `503` as a problem document. The setting is transaction-local and does not
  leak to pooled connections.
- **After a bulk import** run `VACUUM ANALYZE catalog_search;` so the GIN pending lists merge
  and the planner has statistics.
- **Full rebuild**, if it is ever suspect: `SELECT public.refresh_catalog_search(array_agg(id))
  FROM catalogs;`. It is derived data and can be rebuilt at any time.
- **Concurrency.** `refresh_catalog_search` locks the catalog row (`FOR NO KEY UPDATE`) so two
  transactions changing one catalog's subdivisions both end up in its search row.

## Measured

`make bench N="1000 10000"` times `/search` over synthetic catalogs with a country and varied titles,
against a local server, so these are server times without network. Hadrien's target is under 100 ms
warm from Europe (150 ms acceptable). Measured 4 October 2026 on PostgreSQL 18, 120 requests per query:

| Catalogs | Query | p50 | p95 | Matches |
|---|---|---|---|---|
| 10,000 | title word | 18 ms | 20 ms | 830 |
| 10,000 | place name in another language | 23 ms | 27 ms | 1,429 |
| 10,000 | typo of a place name | 30 ms | 38 ms | 1,429 |
| 10,000 | phrase | 14 ms | 16 ms | 38 |
| 10,000 | no match | 5 ms | 5 ms | 0 |

The cost follows the number of matches, not the size of the table. It is run only on a throwaway
database: the script writes `ZZ Bench` rows and deletes them afterwards.

## How far typo tolerance reaches

Measured, not assumed (`tests/integration/test_search_fuzzy.py`): every word the seed is findable by,
damaged in each of the four ways a person mistypes, on the 12-catalog seed.

| One mistake | At 0.6 (PostgreSQL default) | **At 0.5 (what the app uses)** | At 0.4 |
|---|---|---|---|
| a letter dropped | 79% | **96%** | 99% |
| a letter added | 75% | **96%** | 100% |
| a letter replaced | 61% | **90%** | 97% |
| two neighbours swapped | 24% | **66%** | 83% |
| unrelated words returning something (of 40) | 0 | **1** (`library`) | 3 |

The app applies 0.5 (`WORD_SIMILARITY_THRESHOLD` in `search_repository.py`) inside each search
transaction, so the server's own setting stays at 0.6 and nothing leaks to other connections. Chosen on
6 October 2026. What it changed in the plan's table (four rows, all in the trigram tier, all after the
exact matches): `parsi` now finds the Paris library; `Liber` also lists Librivox and Ebooks libres; `Wallis`
also lists Lirtuel (Wallonia); and a quoted phrase such as `"numérique de paris"` also lists Bibliothèque
numérique Romande, because the fuzzy half ignores the quotes.

Long words (9 letters or more) survive one added letter every time; short words are forgiven less.
Loosening the threshold trades missed typos for noise, and the right value depends on how many catalogs
there are: re-measure against the real data (`make bench`, and the fuzzy tests) before moving it.

## Known behaviour

- "Belgio" and "Vallonia" (Italian, not loaded for Belgium) still find the Belgian catalogs as
  trigram matches.
- Swapped letters in a short word are found about two times in three ("parsi" now finds Paris); a
  quoted phrase still gets fuzzy extras after the exact match.
- BM25 ranking arrives with `pg_textsearch` once it is generally available on Cloud SQL (ADR-047).
