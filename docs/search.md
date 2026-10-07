# Search

`GET /search?query=…&page=N` finds catalogs by title, place name or city, in the languages the
place speaks. Speed, compression
and what clients send: [`performance.md`](performance.md).

```
curl 'http://localhost:8000/search?query=wallis'      # Médiathèque Valais (German name of Valais)
curl 'http://localhost:8000/search?query=bruxels'     # the Belgian catalogs, despite the typo
curl 'http://localhost:8000/search?query="numérique de paris"'
```

The answer is an OPDS feed titled "Search results", validated against `schema/feed.schema.json`.
The top-level feed advertises it with a templated `search` link, `/search{?query}`.

## What a query can do

| You type | What happens |
|---|---|
| `standard ebooks` | Any word. More words matched, or matched in a more important field, rank higher |
| `"numérique de paris"` | A phrase: the words together, in order |
| `bibliotheque -paris` | `-` leaves out catalogs containing that word (or `-"a phrase"`) |
| `Suisse`, `Schweiz`, `Svizzera` | Place names work in English and the official languages of the country |
| `PARIS`, `parís`, `Suiße` | Capitals and accents never matter. Folding is done in the database |
| `bruxels` | A typo is caught by trigram similarity, listed after the exact matches |
| `paris OR valais` | `OR` is accepted and ignored: every search is already any-word |

An empty query, spaces or only left-out words give an **empty feed** and touch no database.
Punctuation alone (`!!!`) also gives an empty feed, but it does run the search statement: the parser
only splits, and PostgreSQL discards chunks with no word in them. Input is cut at 256 characters and
16 chunks. Not searchable: supported languages (a filter is planned) and descriptions.

## Order and paging

1. Word matches first, scored by field: title 1.0, country names 0.4, subdivisions and city 0.2.
2. Then catalogs found only through trigrams, by similarity.
3. Ties: newest first, then id. The order is fixed, so pages never overlap or skip.

50 results per page, `page` from 1 to 1000 (422 outside that). `numberOfItems` is the total number of
matches, also on a page past the end. Links: `self`, `search`, `first`, and `previous` / `next`
only when there is one. Changing the weights is one constant, `RANK_WEIGHTS`, with no reindex.

## Typos

A word matches a stored word when enough of their three-letter pieces are shared. The app asks for
**0.5** (`WORD_SIMILARITY_THRESHOLD`); PostgreSQL's own default is 0.6. Measured on the seed data:

| One mistake | at 0.6 | **at 0.5** | at 0.4 |
|---|---|---|---|
| a letter dropped | 79% | **96%** | 99% |
| a letter added | 75% | **96%** | 100% |
| a letter replaced | 61% | **90%** | 97% |
| two neighbours swapped | 24% | **66%** | 83% |
| unrelated words that return something (of 40) | 0 | **1** | 3 |

Over the 90 scored searches: 0.6 scores 0.815, **0.5 scores 0.877**, 0.4 scores 0.863. Long words
survive one mistake; short words are forgiven less, and swapped letters in a short word (`pairs`) are
often missed. Re-measure with real data before moving the threshold:
`tests/integration/test_search_fuzzy.py` and `make search-score`.

## Test cases and score

The searches we use to check search, with the answer we want and the answer we get, are on one
page: [`search-test-cases.md`](search-test-cases.md). It is generated from `tests/search_cases.py`,
and the same data runs as exact-result tests and as a score from 0 to 1:

```
make search-score                                  # score the running server
make search-score ARGS="--save before.json"        # tweak search, then:
make search-score ARGS="--baseline before.json"    # better, worse or unchanged, per search
```

A search scores 1.0 when it returns the expected catalogs, in order, and nothing else. The list
includes 29 realistic typos next to the hand-picked searches, so a change to typo handling shows.

## Live check

The same searches run as a pass or fail against any running or deployed registry, read-only:

```
make live-check                                           # http://localhost:8000
make live-check URL=https://registry.thoriumreader.com ARGS="--deployed"   # after a deploy
make live-check ARGS="-v --max-ms 150"                    # list every check, fail if slow
```

`--deployed` also requires what a public service must have: the console page answers, and
`/dev/fetch` (which makes the server request a URL a visitor names) does not exist.

It first checks the plumbing (health, the feed and its `search` link, the compression each kind of
client should get, the security headers, the error responses), then sends every search of the
test-case page and requires exactly the titles listed there, in the same order: presence and
position, per search. The score against the ideal answers is printed too. Exit code 0 when
everything passed, 1 when something failed, 2 when the server does not answer. The searches describe
the twelve seed catalogs, so a registry with other data will fail them.

## Reference data

Official languages and place names come from CLDR 48.2, subdivision types from ISO 3166-2
(pycountry 26.2.16), loaded by migration `2eb49a8f4664` as literals: `alembic upgrade head` is the
only step. To review a CLDR or pycountry bump, `make reference-data` writes the CSVs to
`.cache/reference-data/out/` (`PY=1` also prints the literals). Then add a **new** data migration
ending with `SELECT public.refresh_catalog_search(array_agg(id)) FROM catalogs;`. A migration that
adds a subdivision adds its names too. Never edit a merged migration.

## What is indexed

`catalog_search` holds one row per **active** catalog, built by the database (never by the
application) from the title (label A), the country names (B), and the subdivision names and city
(C). Place names are English plus the official languages of the country. Triggers rebuild the row in
the same transaction as any change to the catalog, so `make seed`, `make add`, the back office and
hand-written SQL all keep it current. The analyzer is `public.registry_simple`: default parser, then
`unaccent`, then `simple`. No stemming and no stop words, so short common words such as "de" match
anything containing them, ranked below the real match.

## Operating it

- **Extensions.** The migration creates `unaccent` and `pg_trgm`. On Cloud SQL the migration user
  needs `cloudsqlsuperuser`.
- **Grants.** If the app connects as a different user from the one that migrates, grant it
  `INSERT, UPDATE, DELETE` on `catalog_search` and `SELECT` on `country_languages`, `country_names`,
  `subdivision_names` and `country_subdivision_types`, or catalog saves fail.
- **Its own connections.** Search runs on a separate pool whose connections are read-only, stop a
  statement after 1 second (the answer is `503`, a problem document) and carry the typo threshold.
  Nothing of that touches the feed or the importers.
- **After a bulk import** run `VACUUM ANALYZE catalog_search;`.
- **Rebuild**, if it is ever suspect: `SELECT public.refresh_catalog_search(array_agg(id)) FROM
  catalogs;`. It is derived data. `refresh_catalog_search` locks the catalog row, so two
  transactions changing one catalog's subdivisions both end up in its row.

## Known limits

- "Belgio" and "Vallonia" (Italian, not loaded for Belgium) still find the Belgian catalogs, as
  trigram matches.
- A quoted phrase also lists fuzzy extras after the exact match: the fuzzy half ignores quotes.
- BM25 ranking arrives with `pg_textsearch` once it is generally available on Cloud SQL.
