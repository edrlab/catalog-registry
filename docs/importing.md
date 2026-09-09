# `make add` importing a catalog from its feed

One command imports one catalog from a live OPDS 2.0 endpoint, straight into Postgres.

```
make add ARGS="https://example.org/opds --kind public"
```

```
created: Example Library
```

Run it again and it prints `updated:`, the import is idempotent.

> `make seed` is the other way in, importing `data/recommended.json`. Both paths share the same
> validation and the same upsert. See [`development.md`](development.md).

---

## What it takes from the feed, and what you supply

The feed answers *what this catalog is called and where its parts live*. It cannot answer *what
kind of institution this is*, no OPDS document expresses that, so the rest is flags.

| | Source |
|---|---|
| `title` | the feed's `metadata.title` |
| `links` | the feed's `links` |
| `kind` | **`--kind`, the only required flag** |
| everything else | optional flags, below |

```
--kind              open | public | academic | school | specialized   (repeatable, required)
--color             gray | red | yellow | blue | green | purple | orange | pink
--description       free text
--language          BCP-47, repeatable        --country      ISO 3166-1 alpha-2
--subdivision       ISO 3166-2, repeatable    --city         free text
--coverage          global | country | subdivisions | local
--publication-type  ebook | audiobook | comic | newspaper | magazine | journal | article
--dry-run           print the document, write nothing
```

Two behaviours worth knowing before you fill these in:

- **`--language` changes who sees the catalog.** A catalog declaring no language appears for
  *every* `Accept-Language`. Declaring `en` hides it from a reader asking for French.
- **Omitting `--coverage` stores NULL, not `global`.**, the registry does not assert
  worldwide reach on a catalog's behalf.

## What it does with the links

The URL you typed becomes the `catalog` link. The rest come from the feed, mapped onto the
registry's eight rels:

| In the feed | Stored as |
|---|---|
| *the URL you typed* | `catalog` |
| `shelf`, or `http://opds-spec.org/shelf` | `shelf` |
| `search` | `search`, `templated` preserved |
| `profile`, `icon`, `alternate`, `authenticate` | unchanged |
| anything else, `self` and paging rels included | dropped |

**Your URL becomes `catalog`, never the feed's own `self`.** That link is the identity later
imports match on, so trusting a remote `self` would let a rename on their side create a second
row instead of updating the first.

`http://opds-spec.org/shelf` is the single alias accepted. OPDS 1.2 §6.1 defines the
relation that way and gives it no short form. There is no generic prefix rule, so a rel that
the specifications define but this registry does not store is dropped like any other, and
silently. That covers `http://opds-spec.org/subscriptions`, `facet` and `crawlable`. Use
`--dry-run` to see exactly which links an import would keep.

## Re-running replaces the whole record

Not a patch. Child collections are deleted and rebuilt, and every omitted flag reverts to its
default:

```
make add ARGS="<url> --kind public --color purple"   # colour purple
make add ARGS="<url> --kind public"                  # colour is gray again
```

Correct for an import - the feed plus your flags are the whole truth for that catalog - and
wrong for editing one field. Partial edits are the back office's job (v1.0).

## Two things it does not set

`status` is always `active` and `recommended` is always `true`. There is no flag for either, so
anything you add is in the public feed immediately. Marking a catalog recommended, or not, is
Phase 3.

## When it refuses

| Situation | Message |
|---|---|
| not `http`/`https` | `... is not an http(s) URL` |
| unreachable, or times out after 12s | `could not fetch ..., <reason>` |
| over 2 MB | `... returned more than 2097152 bytes` |
| not JSON, or not a JSON object | `... did not return JSON` |
| no `metadata.title` | `... has no metadata.title, so there is nothing to name it` |
| no `catalog` or `shelf` link | `... needs a catalog or shelf link` |
| a field the schema rejects | the validation error, with its path |

Nothing is written unless the whole document validates.

## Where it writes

Directly to Postgres, using `REGISTRY_DATABASE_URL`. It is a database client, like `psql`, it
never calls the API, which is why `make logs` stays silent during an import, and why it needs
`make up` running only for the database, not the service.

It is also as privileged as the DSN it holds. With the Cloud SQL Auth Proxy up, that DSN can be
production.
