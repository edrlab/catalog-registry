"""English names for language tags."""

import pytest

from registry.services.language_names import build_language_name

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("tag", "name"),
    [
        # Aliased in CLDR: named as themselves, not as the language they alias.
        ("tw", "Twi"),
        ("fat", "Fanti"),
        ("tl", "Tagalog"),
        ("sh", "Serbo-Croatian"),
        # Unchanged.
        ("fr", "French"),
        ("FR", "French"),
        ("ak", "Akan"),
        ("fil", "Filipino"),
        ("kha", "Khasi"),
        ("pt-br", "Portuguese (Brazil)"),
        ("fr-ca", "French (Canada)"),
        ("cak", "Kaqchikel"),
        ("qaa", "qaa"),
    ],
)
def test_each_tag_has_its_own_english_name(tag: str, name: str) -> None:
    assert build_language_name(tag) == name
