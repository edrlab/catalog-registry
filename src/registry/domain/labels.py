"""Display labels for the enumerations the admin API exposes.

The keys are the wire values. Clients render what `/admin/filter-options` returns and
hardcodes none of it. Labels follow the shared data definitions.
"""

from collections.abc import Mapping
from typing import Final

STATUS_LABELS: Final[Mapping[str, str]] = {
    "draft": "Draft",
    "published": "Published",
    "unpublished": "Unpublished",
    "archived": "Archived",
}

KIND_LABELS: Final[Mapping[str, str]] = {
    "open": "Open catalogs",
    "public": "Public libraries",
    "academic": "Academic libraries",
    "school": "School libraries",
    "specialized": "Special libraries",
}

COVERAGE_LABELS: Final[Mapping[str, str]] = {
    "global": "Global",
    "country": "National",
    "subdivisions": "Subdivisions",
    "local": "Local",
}

PUBLICATION_TYPE_LABELS: Final[Mapping[str, str]] = {
    "ebook": "Ebook",
    "audiobook": "Audiobook",
    "comic": "Comic",
    "newspaper": "Newspaper",
    "magazine": "Magazine",
    "journal": "Journal",
    "article": "Article",
}

COLOR_LABELS: Final[Mapping[str, str]] = {
    "gray": "Gray",
    "red": "Red",
    "yellow": "Yellow",
    "blue": "Blue",
    "green": "Green",
    "purple": "Purple",
    "orange": "Orange",
    "pink": "Pink",
}
