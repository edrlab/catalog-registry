"""The wire-name ↔ column-name table, and the casing rules. Single source of truth.

Casing is applied here and nowhere else. ISO 3166-1 alpha-2 is uppercase on the wire and in
the database; BCP-47 tags are lowercase in the database (enforced by a check constraint) and
lowercase on the wire.
"""

from types import MappingProxyType

#: wire name → column name. Only fields that reach a client appear here; that is what makes
#: `catalog_renderer` a whitelist rather than a filter.
WIRE_TO_COLUMN = MappingProxyType(
    {
        "title": "title",
        "kind": "kinds",
        "description": "description",
        "color": "color",
        "supportedLanguages": "languages",
        "publicationTypes": "publication_types",
        "country": "country_code",
        "subdivisions": "subdivisions",
        "city": "city",
        "coverage": "coverage",
    }
)

COLUMN_TO_WIRE = MappingProxyType({column: wire for wire, column in WIRE_TO_COLUMN.items()})


def to_wire_name(column_name: str) -> str:
    return COLUMN_TO_WIRE[column_name]


def to_column_name(wire_name: str) -> str:
    return WIRE_TO_COLUMN[wire_name]
