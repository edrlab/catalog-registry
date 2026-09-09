"""Validating documents against the repository's JSON Schemas, from Python.

Two things stand between `jsonschema` and the schemas in `schema/`, and both are properties
of the schemas being *correct*, not broken:

1. **`pattern` is ECMA-262**, per JSON Schema draft-07 §6.3.3. ECMA spells a named group
   ``(?<name>...)``; Python's ``re`` spells the same thing ``(?P<name>...)`` and raises
   ``PatternError`` on the ECMA form. The BCP-47 tag pattern in ``catalog.schema.json`` and
   the language pattern in Readium's ``link.schema.json`` both use named groups, so every
   pattern is translated before it reaches ``re``.
2. **`$ref` points at readium.org.** Resolving it over the network makes validation fail when
   readium.org is slow or unreachable — not a defect in this repository. The copies under
   ``schema/vendor/`` are registered locally instead (`conventions/tooling.md`).

This lives in the package rather than in `scripts/` because the seed path validates incoming
documents before persisting, which is production behaviour — `jsonschema` is a runtime
dependency for that reason.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schema"

#: ``(?<`` opens a named group in ECMA-262. ``(?<=`` and ``(?<!`` are lookbehind, which is
#: spelled identically in both dialects and must not be rewritten.
_ECMA_NAMED_GROUP = re.compile(r"\(\?<(?![=!])")


def to_python_pattern(pattern: str) -> str:
    """Translate an ECMA-262 regular expression to the Python ``re`` dialect. Idempotent."""
    return _ECMA_NAMED_GROUP.sub("(?P<", pattern)


def _translate_patterns(node: Any) -> Any:
    """Rewrite every ``pattern`` value in a schema document, at any depth.

    Translating on load rather than patching the validator's ``pattern`` keyword is
    deliberate: `jsonschema` re-selects a validator class from the ``$schema`` of each
    referenced document, so a patched class does not survive a ``$ref`` into
    ``link.schema.json``. Rewriting the document does.
    """
    if isinstance(node, dict):
        return {
            key: to_python_pattern(value)
            if key == "pattern" and isinstance(value, str)
            else _translate_patterns(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_translate_patterns(item) for item in node]
    return node


def load_json_document(path: Path) -> Any:
    """Load an instance document. No pattern translation — patterns only occur in schemas."""
    return json.loads(path.read_text(encoding="utf-8"))


def load_schema_document(path: Path) -> Any:
    """Load a schema with its ECMA-262 patterns translated to the Python ``re`` dialect."""
    return _translate_patterns(json.loads(path.read_text(encoding="utf-8")))


#: The draft-07 metaschema, with no format checker attached.
#:
#: ``Draft7Validator.check_schema`` attaches one by default, and its ``format: "regex"``
#: assertion compiles the value with Python's ``re`` — which rejects the ECMA-262 patterns
#: these schemas correctly contain. Everything else the metaschema asserts still applies.
_METASCHEMA_VALIDATOR = Draft7Validator(Draft7Validator.META_SCHEMA)


def check_schema_is_valid(document: Any) -> None:
    """Raise if *document* is not a valid draft-07 schema."""
    _METASCHEMA_VALIDATOR.validate(document)


@lru_cache(maxsize=1)
def build_schema_registry() -> Registry:
    """Every local and vendored schema, registered under the ``$id`` its refs resolve to.

    Readium's ``link.schema.json`` refers to its extensions by a path relative to its own
    ``$id``, so those resolve to a readium.org URL even though the file sits under
    ``schema/vendor/``. Each vendored file carries that ``$id``, so one key is enough.
    """
    resources = [
        (document["$id"], Resource.from_contents(document, default_specification=DRAFT7))
        for path in sorted(SCHEMA_DIR.rglob("*.json"))
        if isinstance(document := load_schema_document(path), dict) and "$id" in document
    ]
    return Registry().with_resources(resources)


@lru_cache(maxsize=8)
def build_schema_validator(schema_name: str) -> Draft7Validator:
    """A validator for ``schema/<schema_name>``, with refs resolved locally."""
    schema = load_schema_document(SCHEMA_DIR / schema_name)
    return Draft7Validator(schema, registry=build_schema_registry())
