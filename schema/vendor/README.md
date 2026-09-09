# Vendored schemas

`catalog.schema.json` and `feed.schema.json` reference the Readium link schema by absolute
URL. `jsonschema` resolves an unknown `$ref` over the network, which makes contract tests
fail when readium.org is slow or unreachable — a failure that is not a defect in this
repository. These copies are registered with a local `referencing.Registry` instead
(`conventions/tooling.md`, "The Readium `$ref`").

Fetched 2026-08-31. Transitively closed — no file here references anything not present.

| File | Source |
|---|---|
| `link.schema.json` | https://readium.org/webpub-manifest/schema/link.schema.json |
| `extensions/epub/properties.schema.json` | https://readium.org/webpub-manifest/schema/extensions/epub/properties.schema.json |
| `extensions/encryption/properties.schema.json` | https://readium.org/webpub-manifest/schema/extensions/encryption/properties.schema.json |
| `opds-properties.schema.json` | https://specs.opds.io/schema/properties.schema.json |
| `acquisition-object.schema.json` | https://specs.opds.io/schema/acquisition-object.schema.json |

Do not edit. To refresh, re-fetch and review the diff.
