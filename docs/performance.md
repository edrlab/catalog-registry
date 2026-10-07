# Performance

Hadrien's target for search: **under 100 ms warm from Europe, 150 ms acceptable.** This page is
what makes that true, and how to check it.

## One read, one round trip

The time a read takes is mostly how many times the app sends something to the database and waits
for the answer (a round trip), not how much work the database does (about 3 ms). Search used to
make 14 round trips, a single catalog 9, and the feed 9 (19 at a thousand catalogs, because every
collection was loaded separately): a connection ping, BEGIN, the settings, the query, a load, five
relationship loads, COMMIT. Every public read now makes **one**: a single statement returns the
page and everything under it (kinds, languages and subdivisions as arrays, links as one JSON
array). The rows become the same objects the renderer already uses, so the output is identical.

Measured on 7 October 2026 against the code from before this work, same database, through a proxy
that delivers every chunk a fixed time after it arrived, so chunks sent back to back overlap as on
a real network. Median of 40 warm requests, no compression. "Round trip" is the time to the
database and back.

Production size (12 catalogs), before / now:

| Round trip to the database | Search | Feed | One catalog |
|---|---|---|---|
| 0 ms (same machine) | 11.6 / **2.0** ms | 6.8 / **3.0** ms | 10.2 / **2.6** ms |
| 10 ms (a region away) | 199 / **19** ms | 133 / **19** ms | 132 / **19** ms |
| 20 ms | 355 / **31** ms | 235 / **31** ms | 232 / **29** ms |
| Round trips per request | 14 / **1** | 9 / **1** | 9 / **1** |

A thousand catalogs (1,012; search is a page of 50), before / one statement but still ORM objects /
now. Over 20 ms the number of round trips dominates; on one machine it is the Python:

| | Search | Feed | One catalog |
|---|---|---|---|
| Same machine | 30 / 9.0 / **5.1** ms | 120 / 117 / **39.5** ms | 10.7 / 3.1 / **1.7** ms |
| 20 ms round trip | 373 / 39.5 / **36.9** ms | 634 / 142 / **67.5** ms | 232 / 29.3 / **30.0** ms |
| Round trips per request | 14 / 1 / **1** | 19 / 1 / **1** | 9 / 1 / **1** |

**What is left is Python.** Going from 19 round trips to 1 took the thousand-catalog feed from 634
to 142 ms at 20 ms, but it still took 117 ms on one machine. A profiler showed why: building one
SQLAlchemy object per catalog and per kind, language and link, each with change tracking nobody uses
on a read, was nine tenths of the Python time. The reads now build plain slotted values
(`CatalogView`, with `NamedTuple` rows) with the same attribute names, so the renderer is
unchanged. That took the feed to 39.5 ms on one machine and 67.5 ms at 20 ms.

What remains in a 1,000 catalog feed, by profile: the database round trip, validating and
serialising the response model (about 20%, kept: it is the whitelist, R3), compression and JSON.

Tests count the round trips on the wire (they would have caught the 14): a run of bytes the client
sends, then the server's answer, is one, however the network cuts it into reads. Still no N+1 (R4):
there is no per-row query at all. This is the first claim to re-check after any change to the read
path: `tests/integration/test_search_query_count.py` and `test_read_one_statement.py`.

## Three connection pools

| Pool | Used by | Settings on the connection |
|---|---|---|
| search | `/search` | read-only, stops a statement after 1 s, typo threshold 0.5 |
| read | `/`, `/catalogs/{id}` | read-only, stops a statement after 10 s |
| main | the importers and `/health/ready` | none (can write) |

The limits are sent once when a connection opens, not on every request, so a read costs one round trip.
A read that runs past its limit answers `503` as a problem document. A connection the database dropped
while it sat idle is retried once. With up to 5 instances, that is at most about 225 connections.

## Where it runs, and what it costs

Cloud Run `thorium-catalog-registry` is in **europe-west1** (Belgium); Cloud SQL
`development-sandbox-db` is in **europe-west9** (Paris). Measured on the live service, the app to
database trip is about **2 to 4 ms per round trip**, so one is cheap and fourteen were not.
Moving Cloud Run to europe-west9 would cut it further; it is not needed for the reads any more.

**Min instances.** The service keeps 1 instance warm (service-level scaling, min 1, max 5; request-based
billing, so the CPU is limited between requests). Without it the first request after a quiet period waits
for a new instance: seconds.

**Distance from the client is not ours to fix.** From a far country even `/health/live`, which
touches no database, takes several hundred milliseconds. Caching at the edge would help; it is
deferred (ADR-009).

## Measure it

- **The Cloud Run metrics tab** (Observability) already splits it: `request_latencies` is the time
  inside the container, `e2e_latencies` adds Google's network in front. A client's own total minus
  `e2e_latencies` is its distance. Nothing extra is sent with each response for this.
- **`make bench`** times `/` and `/search` on a local server. Local means no network between app and
  database, so it shows the application's own work; read it with the table above.
- **`make search-score ARGS="--url https://…"`** runs the search cases against a deployed instance.
- From Paris, ask for `curl -w '%{time_connect} %{time_appconnect} %{time_starttransfer}\n' -o
  /dev/null -s https://registry.thoriumreader.com/search?query=wallis` a few times.

## Compression

The client says what it can decode in `Accept-Encoding`; the server picks. Measured on real clients:

| Client | Sends | Gets |
|---|---|---|
| Recent browsers | `gzip, deflate, br, zstd` | **zstd** (level 6) |
| Thorium Reader (`node-fetch`) | `gzip, deflate, br` | **brotli** (level 4) |
| Apple `URLSession`, Node, OkHttp, Android, Python `requests` | `gzip` (and `deflate`) | **gzip** (level 6) |
| KOReader, Python `urllib` | `identity` | the body as it is |
| `curl` with no flags | nothing | the body as it is |

gzip is the only coding every client accepts, so it is always the fallback. zstd is used only when a
client names it (a `*` never gets it): it compresses several times faster than brotli 4 for about 2% more
bytes on the real feed (level 6: 967 B against 948 B for a 4.2 KB page), so it costs nothing and
helps a large response. A client that refuses `identity` and every coding we offer (`identity;q=0`, or `*;q=0`) gets
`406 Not Acceptable` as a problem document, as RFC 9110 §12.5.3 allows. Responses under 1 KB are not
compressed. `Vary: Accept-Encoding` is always set. Code: `src/registry/api/compression.py`, ADR-061.

## Who is asking

Each request writes one JSON line to the log, whichever way it was answered (a normal response, a
HEAD, a 406 or an unexpected 500): `path`, `status`, `accept_encoding`, the `encoding`
chosen and the `user_agent`. No address, no query string, no other header. In Cloud Logging:

```
resource.type="cloud_run_revision" jsonPayload.message="client"
```

Count by `jsonPayload.user_agent` and `jsonPayload.accept_encoding` for a week after rollout to
see which readers and devices really call the registry.

## Connections and old devices

Checked on the live service with `testssl.sh` and `openssl`:

- **Use `registry.thoriumreader.com`, never the `*.a.run.app` address**, for anything a device uses.
  Only the custom name still accepts TLS 1.0 and 1.1; the raw address refuses them. That is
  Google's setting, not ours, and can change.
- Keys are RSA 2048 with SHA-256. SNI is required: a client that sends no host name gets no
  certificate (very old Android, Windows XP, Java 6).
- The chain is leaf, Google `WR3`, then `GTS Root R1` **cross-signed by the old GlobalSign Root
  CA**. A device that only trusts that 1998 root connects today. That root and the cross-signature
  **expire on 2028-01-28**; after that such devices cannot connect, whatever we do.
- Not tested, because it needs a device: what a given Kindle, Kobo, PocketBook or Boox supports.
  One old device opening `https://registry.thoriumreader.com/` is a five minute check.
