"""Loading documents and schemas for validation with Python's `jsonschema`."""

from pathlib import Path

import jsonschema
import pytest

from registry.core.schema_validation import check_schema_is_valid, load_json_document

pytestmark = pytest.mark.unit

ECMA_NAMED_GROUP = "^(?<language>[a-z]{2,3})$"


def test_an_instance_document_is_loaded_as_written(tmp_path: Path) -> None:
    """Only schemas have their patterns translated; a document's own strings are data."""
    document = tmp_path / "document.json"
    document.write_text(f'{{"pattern": "{ECMA_NAMED_GROUP}"}}', encoding="utf-8")

    assert load_json_document(document) == {"pattern": ECMA_NAMED_GROUP}


def test_a_schema_with_an_ecma_named_group_is_a_valid_schema() -> None:
    """draft-07 patterns are ECMA-262, which Python's `re` cannot compile. The metaschema check
    must not reject a correct schema for that."""
    check_schema_is_valid({"type": "string", "pattern": ECMA_NAMED_GROUP})


def test_a_document_that_is_not_a_schema_is_refused() -> None:
    with pytest.raises(jsonschema.ValidationError):
        check_schema_is_valid({"type": 5})
