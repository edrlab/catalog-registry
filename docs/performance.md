# Performance

Hadrien's target for search: **under 100 ms warm from Europe, 150 ms acceptable.** This page is
what makes that true, and how to check it.

## One search, one database message

The time a search takes is mostly *how many times the app waits for the database*, not how much
work the database does (about 3 ms). A search used to send 14 messages: a connection ping, BEGIN,
`SET TRANSACTION READ ONLY`, the settings, the search, a load, five relationship loads, COMMIT.
It now sends **one**: a single statement returns the page and everything under it (kinds,
languages, subdivisions as arrays, links as one JSON array). The rows become the same `Catalog`
objects the renderer already uses, so the output is identical.

| Round trip to the database | Before: 14 messages | Now: 1 message |
|---|---|---|
| 0 ms (same machine) | 22 ms | **15 ms** |
| 2 ms (same region) | 77 ms | **18 ms** |
| 10 ms (a region away) | 227 ms | **36 ms** |
| 12 ms | 254 ms | **45 ms** |

Median of 100 warm searches on 1,012 catalogs, the real app, through a proxy that adds the delay
(the proxy adds a little of its own, so read the ratios). Page of 50, p95 about 10 ms above these.

A test counts the messages on the wire (it would have caught the 14). It is still no N+1 (R4):
there is no per-row query at all. Decisions: ADR-060, ADR-058 (amended).

## Where it runs, and what it costs

Cloud Run `thorium-catalog-registry` is in **europe-west1** (Belgium); Cloud SQL
`development-sandbox-db` is in **europe-west9** (Paris). Measured on the live service, the app to
database trip is about **2 to 4 ms per message**, so one message is cheap and fourteen were not.
Moving Cloud Run to europe-west9 would cut it further; it is not needed for search any more.

**Min instances.** With none kept warm, the first request after a quiet period waits for a new
instance: seconds. Set `--min-instances=1` for anything promised as "warm".

**Distance from the client is not ours to fix.** From a far country even `/health/live`, which
touches no database, takes several hundred milliseconds. Caching at the edge would help; it is
deferred (ADR-009).

## Measure it

- **`Server-Timing: app;dur=<ms>`** is on every response: the time the application took. The
  client's own total minus this is the network. Try `curl -s -D - -o /dev/null URL | grep -i
  server-timing`.
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
