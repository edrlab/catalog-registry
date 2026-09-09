# The `schema/` directory

One directory, **three kinds of file with three different owners**. Confusing them is how a
schema gets edited by the wrong person or a generated file gets hand-patched, so the
distinction is worth holding on to.

- [The three kinds](#the-three-kinds)
- [The `$ref` graph](#the-ref-graph)
- [`schema/` — the contract](#schema--the-contract)
- [`schema/vendor/` — third-party copies](#schemavendor--third-party-copies)
- [`schema/generated/` — derived](#schemagenerated--derived)
- [Why Python cannot use these schemas directly](#why-python-cannot-use-these-schemas-directly)
- [How a validator is built](#how-a-validator-is-built)
- [Where each schema is used](#where-each-schema-is-used)
- [What to do when a schema changes](#what-to-do-when-a-schema-changes)
- [Gotchas](#gotchas)

---

## The three kinds

```
schema/
├── catalog.schema.json                          ← CONTRACT.  Hadrien owns. Never edit here.
├── feed.schema.json                             ← CONTRACT.  Hadrien owns. Never edit here.
│
├── vendor/                                      ← THIRD-PARTY. Fetched. Never edit.
│   ├── README.md                                    provenance: where and when
│   ├── link.schema.json                             readium.org
│   ├── extensions/epub/properties.schema.json       readium.org
│   ├── extensions/encryption/properties.schema.json readium.org
│   ├── opds-properties.schema.json                  specs.opds.io
│   └── acquisition-object.schema.json               specs.opds.io
│
└── generated/                                   ← DERIVED. Committed, but produced by a script.
    └── seed-input.schema.json                       `make seed-schema`
```

| | Who owns it | Edit it? | Refresh how |
|---|---|---|---|
| `schema/*.json` | **Hadrien**, upstream | Only upstream, never in a fork | `git merge upstream/main` |
| `schema/vendor/` | Readium and the OPDS spec | No | Re-fetch and review the diff |
| `schema/generated/` | This repository, mechanically | **No** — edits are overwritten | `make seed-schema` |

All three are committed. All three ship inside the runtime image, because the seed validates
against them at runtime — see the `COPY --chown=app:app schema/ ./schema/` line in
`docker/Dockerfile`.

---

## The `$ref` graph

This is why `vendor/` has five files rather than one. Following the references from the two
contract schemas reaches all of them:

```
catalog.schema.json ──┐
                      ├──► readium.org …/link.schema.json          (vendor/link.schema.json)
feed.schema.json ─────┤          │
       │              │          ├──► extensions/epub/properties.schema.json
       │              │          ├──► extensions/encryption/properties.schema.json
       │              │          ├──► specs.opds.io …/properties.schema.json
       │              │          │            └──► acquisition-object.schema.json
       │              │          └──► link.schema.json  (recursive — `alternate`, `children`)
       │
       └──► catalog.schema.json  (relative ref: the feed embeds catalogs)

generated/seed-input.schema.json ──► readium.org …/link.schema.json
```

The set is **transitively closed**: no file under `vendor/` references anything that is not
also present. That was verified by walking the refs after each fetch, and it is what makes
offline validation possible.

---

## `schema/` — the contract

`catalog.schema.json` and `feed.schema.json` are the published contract, authored by Hadrien
and served from `edrlab.github.io`. They are the reason this project can test its output
mechanically instead of by eye — his stated purpose for them on 2026-08-18.

They are also **the reason several things in this codebase exist**:

- `additionalProperties: false` on `metadata` is what makes R3 (no internal field ever reaches
  a response) an executable test rather than a code-review promise.
- The enum vocabularies in `catalog.schema.json` are the single source for
  `src/registry/domain/enums.py`, which is generated rather than transcribed.
- `feed.schema.json` requires at least one link and one of them to be `self`, which is why the
  feed renderer synthesises one.

**Do not edit them in this repository.** Changes come from upstream:

```
git fetch upstream && git merge upstream/main
make enums seed-schema     # regenerate what derives from them
make test                  # the contract tests will tell you what broke
```

That is the intended workflow: a schema change surfaces as a failing build with a readable
diff, not as a client bug report weeks later.

---

## `schema/vendor/` — third-party copies

### Why it exists

Both contract schemas reference the Readium link schema **by absolute URL**:

```json
"items": { "$ref": "https://readium.org/webpub-manifest/schema/link.schema.json" }
```

`jsonschema` resolves an unknown `$ref` **over the network**. That means every validation —
every contract test, every seed run, every CI job — would make an HTTP request to readium.org,
and would fail when readium.org is slow, unreachable, or has changed the file. None of which
is a defect in this repository.

A test suite that goes red because someone else's web server is having a bad afternoon is not
a test suite.

### How the files were created

Fetched with `curl`, then the refs in each downloaded file were walked to find the next layer,
repeating until nothing new appeared:

```bash
mkdir -p schema/vendor/extensions/epub schema/vendor/extensions/encryption
cd schema/vendor

curl -fsSL https://readium.org/webpub-manifest/schema/link.schema.json -o link.schema.json
curl -fsSL https://readium.org/webpub-manifest/schema/extensions/epub/properties.schema.json \
     -o extensions/epub/properties.schema.json
curl -fsSL https://readium.org/webpub-manifest/schema/extensions/encryption/properties.schema.json \
     -o extensions/encryption/properties.schema.json
curl -fsSL https://specs.opds.io/schema/properties.schema.json -o opds-properties.schema.json
curl -fsSL https://specs.opds.io/schema/acquisition-object.schema.json \
     -o acquisition-object.schema.json

grep -rn '\$ref' .        # confirm nothing points outside this directory
```

`schema/vendor/README.md` records the source URL of every file and the date they were fetched.
Keep it accurate; it is the only provenance there is.

### Refreshing them

Re-run the fetches and **read the diff**. An upstream change is information, not automatically
a change to adopt. The plan calls for a weekly, deliberately **non-blocking**
CI job that fetches and diffs — an upstream edit should not fail a build that has nothing to do
with it. That job is not written yet.

---

## `schema/generated/` — derived

One file today: `seed-input.schema.json`, produced by `scripts/generate_seed_schema.py`
(`make seed-schema`).

### Why it exists

`data/recommended.json` — the seed source — **fails the published `feed.schema.json`**, and
that is correct rather than a bug. It has no feed-level `links`, and no catalog in it has a
`self` link. Both are required by the published schema, because the published schema describes
*output*.

A `self` link points at the registry. At the time Hadrien authored the seed file, that registry
did not exist. Requiring one would be asking him to invent a URL.

So input and output are different documents that happen to look alike, and conflating them was
the mistake (ADR-030). The seed validates against a **relaxed** variant:

| Relaxation | Why |
|---|---|
| feed-level `links` not required | Nothing to link to yet |
| the feed's "must contain a `self` link" constraint dropped | Same |
| the same constraint dropped from every catalog | `self` is synthesised at render time |

Everything else still applies — enums, the BCP-47 pattern, `minItems` on `kind`,
`additionalProperties: false`.

`tests/integration/test_seed.py::test_the_seed_input_is_rejected_by_the_published_schema` is
the tripwire: it asserts the seed file **fails** the published schema. If it ever starts
passing, the published schema has loosened and ADR-030 is worth revisiting.

### Why it is generated rather than written

ADR-030 is explicit: derived at build time, not hand-written, **so it cannot drift** when
Hadrien edits the originals. A hand-maintained second copy of a schema is a copy that silently
disagrees with the first six months later.

The generator deep-copies `feed.schema.json`, removes exactly those three constraints, and
inlines a relaxed copy of `catalog.schema.json` in place of the `$ref` — inlined rather than
referenced because the relaxation applies to *this* copy only; the published
`catalog.schema.json` must keep requiring `self`.

CI regenerates it and runs `git diff --exit-code`, so a stale committed copy fails the build.

---

## Why Python cannot use these schemas directly

The single most surprising thing in this directory, and worth knowing before touching any of
it: **the schemas are correct and Python is the odd one out.**

JSON Schema draft-07 §6.3.3 says `pattern` is an ECMA-262 regular expression, and the BCP-47
patterns in `catalog.schema.json` and Readium's `link.schema.json` use ECMA named groups —
`(?<language>...)`. Python's `re` spells that `(?P<name>...)` and raises `PatternError` on the
ECMA form, so any Python validation of a catalog declaring `supportedLanguages` crashes.

Two further traps sit behind it: a patched validator class does not survive a `$ref` into
another document, and `Draft7Validator.check_schema` rejects these patterns via its own format
checker.

`src/registry/core/schema_validation.py` handles all three, and its module docstring is the
authoritative explanation — it sits next to the code that would break if the reasoning were
forgotten. Read it before changing anything in that module.

The one thing to remember here: **schemas are loaded through `load_schema_document`, never
`json.load`.** The translation happens on load.

---

## How a validator is built

`build_schema_registry()` registers every schema under `schema/` keyed on its **`$id`** — the
identifier a `$ref` resolves to, which is not always the filename.
`build_schema_validator(name)` returns a `Draft7Validator` for `schema/<name>` with that
registry attached. Both are `lru_cache`d, so each file is read once per process.

```python
from registry.core.schema_validation import build_schema_validator

errors = list(build_schema_validator("feed.schema.json").iter_errors(document))
```

`referencing.Registry` is the mechanism; `RefResolver` is deprecated in `jsonschema` 4.18+ and
is not used.

---

## Where each schema is used

| Schema | Used at | For |
|---|---|---|
| `catalog.schema.json` | `scripts/generate_enums.py:73` | Generating `domain/enums.py` — the vocabularies come from the contract, never transcribed |
| | `scripts/generate_seed_schema.py:45` | The relaxed copy inlined into the seed-input schema |
| | `tests/contract/test_schema_conformance.py:41` | Every catalog in a live response validates |
| | `tests/contract/test_schema_conformance.py:64` | The `demo/` fixtures validate |
| | `scripts/validate_fixtures.py:20` | Same check, as a CI job |
| `feed.schema.json` | `tests/contract/test_schema_conformance.py:33,53` | The live feed response validates, seeded and empty |
| | `scripts/generate_seed_schema.py:44` | The base the relaxed schema is derived from |
| | `tests/integration/test_seed.py:70` | Asserts the seed file **fails** it — the tripwire for ADR-030 |
| | `scripts/validate_fixtures.py:21-22` | `demo/index.json` and `demo/search.json` validate |
| `generated/seed-input.schema.json` | `src/registry/cli/seed.py:63` | **Runtime.** Every seed run validates its input first |
| | `scripts/validate_fixtures.py:16` | `data/recommended.json` validates, as a CI job |
| `vendor/*` | Never referenced by name | Resolved automatically through `$ref` by `build_schema_registry()` |
| all of them | `scripts/validate_schemas.py:19` | Every file is valid JSON and a valid draft-07 schema |

That last one exists because a **trailing comma** in `catalog.schema.json` once sat unnoticed
until someone read it by eye. It is a one-line CI job that makes that impossible to repeat, and
it matters because Hadrien edits these files directly.

`jsonschema` is a **runtime** dependency, not a dev one, precisely because of
`src/registry/cli/seed.py:63` — validating input before persisting it is production behaviour.

---

## What to do when a schema changes

**Hadrien edits `catalog.schema.json` or `feed.schema.json`:**

```
git fetch upstream && git merge upstream/main
make enums          # domain/enums.py may change
make seed-schema    # the relaxed schema may change
make schema-check   # the schemas and every fixture still validate
make test           # the contract tests say what broke
git add -A && git commit
```

Commit the source change and the regenerated files **together**. CI runs both generators and
`git diff --exit-code`, so a half-done update fails the build.

**Readium or OPDS change a vendored schema:** re-fetch, read the diff, decide whether to adopt
it, and update the date in `schema/vendor/README.md`.

**You need a new derived schema:** add a generator under `scripts/`, a `make` target, a CI
diff step, and write the output under `schema/generated/`. Do not hand-write it.

---

## Language tags: permissive in, lowercase out

The `supportedLanguages` pattern is the standard BCP-47 regex and accepts **any case** —
`EN`, `fr-BE` and `zh-Hant-TW` all validate. That is deliberate and stays that way: RFC 5646
§2.1.1 declares tags case-insensitive, and its *recommended* casing is mixed (language
lowercase, script Title, region UPPERCASE). A lowercase-only pattern would reject tags that
are not merely valid but canonical.

Everything the registry stores and emits is nonetheless lowercase, guaranteed in three places:

| Where | What |
|---|---|
| ingest | `normalise_language_tag` folds every tag (R2) |
| database | `ck_catalog_languages_language_tag_lowercase` rejects anything else |
| render | the response is built from that column, so it cannot drift |

The repository's own fixtures — `demo/`, `data/recommended.json` — are written lowercase so a
diff never turns on casing. That is a convention, not a rule the schema enforces:
`tests/integration/test_seed.py` asserts both separately, one test for the fixtures and one
that feeds uppercase input through ingest on purpose.

---

## Gotchas

**Never edit anything under `generated/`.** The next `make seed-schema` overwrites it and CI
fails on the diff.

**Never edit anything under `vendor/`.** It is someone else's file; local edits are invisible
divergence from a published spec.

**Editing `schema/*.json` in this fork** puts you at odds with upstream. Hadrien owns them.

**`$ref` resolution is offline by design.** If you add a schema that references a new external
URL, vendor it too, or the whole suite starts making network calls again — quietly, and only
failing sometimes.

**A new `$id` must be unique.** Two files with the same `$id` mean one silently wins.

**`.dockerignore` excludes `*.md`,** so `schema/vendor/README.md` is not in the image. The JSON
is; the provenance note is not, which is fine — nothing reads it at runtime.
