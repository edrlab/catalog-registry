# Performance

Hadrien's target for search: **under 100 ms warm from Europe, 150 ms acceptable.** This page is
what makes that true, and how to check it.

## One read, one database message

The time a read takes is mostly *how many times the app waits for the database*, not how much work
the database does (about 3 ms). Search used to send 14 messages and the feed and a single catalog 9
each: a connection ping, BEGIN, the settings, the query, a load, five relationship loads, COMMIT.
Every public read now sends **one**: a single statement returns the page and everything under it
(kinds, languages and subdivisions as arrays, links as one JSON array). The rows become the same
`Catalog` objects the renderer already uses, so the output is identical.

Median, real app, warm, through a proxy that adds the delay to the database
(it adds a little of its own, so read the ratios):

| Round trip to the database | Search before | Search now | Feed before | Feed now | One catalog before | One catalog now |
|---|---|---|---|---|---|---|
| 0 ms (same machine) | 22 ms | **15 ms** | 17 ms | **5 ms** | 15 ms | **4 ms** |
| 10 ms (a region away) | 227 ms | **36 ms** | 143 ms | **20 ms** | 134 ms | **19 ms** |

Search: 1,012 catalogs, a page of 50. Feed and catalog: 12 catalogs, the size of production. A
feed of 1,000 catalogs goes from 19 messages to 1 (487 ms to 191 ms at 10 ms) but stays near
120 ms on one machine: that part is Python rendering the page.

Tests count the messages on the wire (they would have caught the 14). Still no N+1 (R4): there is
no per-row query at all. Decisions: ADR-060, ADR-062, ADR-058 (amended).

## Where it runs, and what it costs

Cloud Run `thorium-catalog-registry` is in **europe-west1** (Belgium); Cloud SQL
`development-sandbox-db` is in **europe-west9** (Paris). Measured on the live service, the app to
database trip is about **2 to 4 ms per message**, so one message is cheap and fourteen were not.
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
| Thorium Reader (`node-fetch`), browsers | `gzip, deflate, br` | **brotli** (level 4) |
| Apple `URLSession`, Node, OkHttp, Android, Python `requests` | `gzip` (and `deflate`) | **gzip** (level 6) |
| KOReader, Python `urllib` | `identity` | the body as it is |
| `curl` with no flags | nothing | the body as it is |

gzip is the only coding every client accepts, so it is always the fallback. zstd is not offered: only
recent browsers send it, and it gains nothing at these sizes. Responses under 1 KB are not
compressed. `Vary: Accept-Encoding` is always set. Code: `src/registry/api/compression.py`, ADR-061.

## Who is asking

Each request writes one JSON line to the log: `path`, `status`, `accept_encoding`, the `encoding`
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
