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
| `bruxels` | A typo is forgiven, listed after the exact matches (see Typos) |
| `lib` | The last word also matches as the start of a word: Library, Librivox |
| `paris OR valais` | `OR` is accepted and ignored: every search is already any-word |

An empty query, spaces or only left-out words give an **empty feed** and touch no database.
Punctuation alone (`!!!`) also gives an empty feed, but it does run the search statement: the parser
only splits, and the database discards chunks with no word in them. A word the index splits
(`ile-de-france`) is searched as the phrase "ile de france". Input is cut at 256 characters and
16 chunks. Not searchable: supported languages (a filter is planned) and descriptions.

## Order and paging

1. Matches on a word as typed, by BM25 score (TIN, PlanetScale's search index): rarer words and
   shorter titles count more, and a field's boost multiplies it: title ×5, city ×2, country and
   subdivision names ×1. Every typed word is scored, even one found in most catalogs.
2. Then catalogs found only through a typo or the last word's prefix: TIN scores those 0.
3. Ties: newest first, then id. The order is fixed, so pages never overlap or skip.

50 results per page, and at most 100 results in all: `page` is 1 or 2 (422 outside that), so there is
no paging through hundreds or thousands of catalogs. `numberOfItems` is the number of matches
**capped at 100**, also on a page past the end. A search with more matches than that is narrowed with
a longer query or `restrict`, not paged. Links: `self`, `search`, `first`, and `previous` / `next`
only when there is one. Changing a boost is one constant (`TITLE_BOOST`, `CITY_BOOST`), with no
reindex.

## Typos

A word of 6 letters or more may be 2 edits away (a letter dropped, added or replaced each count
one, two neighbours swapped count two), a word of 4 or 5 letters 1 edit, a shorter word none. The
first letter must be right. Measured on the seed data, one mistake per word:

| One mistake | trigrams at 0.5 (before) | **TIN** |
|---|---|---|
| a letter dropped | 96% | **87%** |
| a letter added | 96% | **89%** |
| a letter replaced | 90% | **87%** |
| two neighbours swapped | 66% | **81%** |
| unrelated words that return something (of 40) | 1 | **0** |

Every miss is a mistake in the first letter, or two letters swapped in a word of 4 or 5 letters
(`pairs`). Over the 90 scored searches the score went from 0.877 to **0.950**. Re-measure before
changing the allowance: `tests/integration/test_search_fuzzy.py` and `make search-score`.

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

`catalog_search` holds one row per **published** catalog, built by the database (never by the
application): the title, the city and the place names (country and subdivision names, English plus
the official languages of the country), each through `public.fold()` and each with its own TIN
index. Place names are indexed without BM25 length normalisation, so a country with many languages
is not penalised. The row also carries the three `restrict` flags. Triggers rebuild it in the same
transaction as any change to the catalog, its subdivisions, links, kinds or publication types, so
`make seed`, `make add`, the back office and hand-written SQL all keep it current. No stemming and
no stop words, so short common words such as "de" match anything containing them, ranked below the
real match.

The query is built in the database too (`registry_tinql`), so typed and stored text are folded by
the same function. TIN folds `è` and `Ö` itself but keeps `ß`, `æ`, `ø` and `ł`; `fold()` handles
those.

## Operating it

- **Extensions.** The migrations create `unaccent`, `pg_trgm` (unused since TIN, dropped later) and
  `tin`. `tin` exists on PlanetScale; locally and in CI the database image is `planetscale/lead`,
  TIN's open-source stand-in, which gives the same results but scans every row, so it says nothing
  about speed.
- **Grants.** If the app connects as a different user from the one that migrates, grant it
  `INSERT, UPDATE, DELETE` on `catalog_search` and `SELECT` on `country_languages`, `country_names`,
  `subdivision_names` and `country_subdivision_types`, or catalog saves fail.
- **Its own connections.** Search runs on a separate pool whose connections are read-only and stop a
  statement after 1 second (the answer is `503`, a problem document). Nothing of that touches the
  feed or the importers.
- **After a bulk import** run `VACUUM ANALYZE catalog_search;`.
- **Rebuild**, if it is ever suspect: `SELECT public.refresh_catalog_search(array_agg(id)) FROM
  catalogs;`. It is derived data. `refresh_catalog_search` locks the catalog row, so two
  transactions changing one catalog's subdivisions both end up in its row.

## Restricting

`GET /search?query=…&restrict=opds,no-open,no-audiobook` removes catalogs that an app or device
cannot use: `opds` keeps catalogs with a `catalog` link of type `application/opds+json`, `no-open`
drops those with `open` among their kinds, `no-audiobook` drops those whose publication types are
audiobook and nothing else (mixed catalogs and catalogs with no types stay). The total, the pages
and every page link follow the restriction, and the ranking is untouched. Each is a column test on
`catalog_search` in the same single statement; the top-level feed applies the same three rules in
Python (`registry/domain/restrictions.py`). At a million catalogs a restriction that removes most
hits will cost more; that is not measured yet.

## Known limits

- A mistake in the first letter of a word is not forgiven (`xutenberg`).
- A typo or prefix match scores 0, so among those the newest catalog comes first, not the closest.
- "Belgio" (Italian, not loaded for Belgium) still finds the Belgian catalogs, as a typo of Belgie.
- `French Guiana` also lists the other French catalogs: `french` is two letters from `france`.
- `numberOfItems` counts at most 100 matches; beyond that the exact number is not computed.
