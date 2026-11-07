"""Checks on one catalog document, shared by the seed, `add` and the admin API. Pure. Stdlib only.

A catalog document is the feed-shaped input the seed reads: `{metadata: {...}, links: [...]}`.
The JSON Schema (`schema/generated/seed-input.schema.json`) checks shapes; these check what a
schema cannot say, each of which would otherwise reach Postgres and fail on a constraint whose
name says nothing to the person who typed the value.

Each problem carries its path inside the document, so the admin API can name the field and the
seed can still print one sentence.
"""

import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from registry.domain.enums import LinkRel
from registry.domain.language import normalise_language_tag
from registry.domain.links import IDENTITY_RELS


@dataclass(frozen=True, slots=True)
class DocumentProblem:
    #: Where in the document, e.g. `("links", 0, "href")`.
    path: tuple[str | int, ...]
    message: str


#: ISO 3166-1 alpha-2, and ISO 3166-2 as `CC-` plus one to three characters. The same shapes as
#: `schema/catalog.schema.json`, matched with `fullmatch` because the schema's `^...$` is applied
#: with `re.search`, and Python's `$` also matches before a trailing newline: `"BE\n"` passes it.
ISO_CODE_SHAPES = {
    "country": re.compile(r"[A-Z]{2}"),
    "subdivisions": re.compile(r"[A-Z]{2}-[A-Z0-9]{1,3}"),
}


#: Text limits, one set for the admin API's bodies and the values a feed check suggests, so a
#: suggestion always saves.
MAX_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2000
MAX_MEDIA_TYPE_LENGTH = 255
MAX_LINK_TITLE_LENGTH = 300
MAX_CITY_LENGTH = 100
#: In UTF-8 bytes: a btree index entry holds at most 2704 bytes, and an href is indexed.
MAX_HREF_BYTES = 2048


#: An absolute web URL. Anything else (`javascript:`, a relative path) would reach every reader.
_WEB_URL = re.compile(r"https?://[^\s/]", re.IGNORECASE)


def link_rels(link: dict[str, Any]) -> list[LinkRel]:
    """The rels on *link* that this registry stores, in the order they were written.

    A Readium link's ``rel`` is a string *or* an array (`link.schema.json`), and it may name
    relations the registry has no column for. `start` is a real OPDS rel and not one of ours;
    it is skipped rather than raising, exactly as the `add` path skips it. A link left with no
    known rel contributes nothing, which is what `next` and `self` already do.
    """
    rel = link.get("rel")
    written = rel if isinstance(rel, list) else [rel]
    return [LinkRel(value) for value in written if value in set(LinkRel)]


def build_identity_href(document: dict[str, Any]) -> str | None:
    """The href of the first `catalog` link, else of the first `shelf` link: what the catalog is
    recognised by across seeds. None when it has neither."""
    for rel in IDENTITY_RELS:
        for link in document["links"]:
            if rel in link_rels(link):
                return str(link["href"])
    return None


def build_catalog_id(identity_href: str) -> uuid.UUID:
    """`uuid5(NAMESPACE_URL, href)`, a name-based UUID (RFC 9562 §5.5): the same catalog gets the
    same id in every environment, whoever creates it."""
    return uuid.uuid5(uuid.NAMESPACE_URL, identity_href)


def build_iso_code_problems(metadata: dict[str, Any]) -> list[DocumentProblem]:
    """A `country` or `subdivisions` entry that is not a well-formed uppercase ISO code.

    Uppercase is enforced, never folded: a lowercase code is an authoring mistake its author
    should see. The whole shape is checked, not just the case, so `"BE\\n"` and `"Belgium"` are
    refused as malformed rather than "corrected".
    """
    candidates: list[tuple[tuple[str | int, ...], str, Any]] = []
    if (country := metadata.get("country")) is not None:
        candidates.append((("metadata", "country"), "country", country))
    candidates.extend(
        (("metadata", "subdivisions", index), "subdivisions", code)
        for index, code in enumerate(metadata.get("subdivisions") or ())
    )
    problems = []
    for path, field, value in candidates:
        shape = ISO_CODE_SHAPES[field]
        if isinstance(value, str) and shape.fullmatch(value):
            continue
        if isinstance(value, str) and shape.fullmatch(value.upper()):
            problems.append(DocumentProblem(path, f"{field} {value!r} should be {value.upper()!r}"))
        else:
            problems.append(
                DocumentProblem(path, f"{field} {value!r} is not a well-formed ISO code")
            )
    return problems


def _build_repeat_problems(
    values: Iterable[Any], path: tuple[str, ...], name: str
) -> list[DocumentProblem]:
    seen: set[Any] = set()
    problems = []
    for index, value in enumerate(values):
        if value in seen:
            problems.append(DocumentProblem((*path, index), f"{name} {value!r} is listed twice"))
        seen.add(value)
    return problems


def build_link_problems(links: list[dict[str, Any]]) -> list[DocumentProblem]:
    """What `links` and `uq_links_identity_href` would refuse, named by link.

    - every href an absolute http or https URL of at most `MAX_HREF_BYTES` in UTF-8 (the index
      on `links.href`);
    - at least one `catalog` or `shelf` link, the catalog's identity;
    - `templated` only on `search` (`templated_only_search`);
    - one link per rel and href (`uq_links_catalog_rel_href`);
    - a `catalog` and a `shelf` link never share an href (`uq_links_identity_href`).
    """
    problems = []
    seen: set[tuple[LinkRel, Any]] = set()
    identity_hrefs: dict[str, LinkRel] = {}
    has_identity = False
    for index, link in enumerate(links):
        rels = link_rels(link)
        href = link.get("href")
        if not (isinstance(href, str) and _WEB_URL.match(href)):
            problems.append(
                DocumentProblem(("links", index, "href"), "Must be an http or https URL")
            )
        elif len(href.encode()) > MAX_HREF_BYTES:
            problems.append(DocumentProblem(("links", index, "href"), "The address is too long."))
        if link.get("templated") and any(rel is not LinkRel.SEARCH for rel in rels):
            problems.append(
                DocumentProblem(
                    ("links", index, "templated"), "Only a search link can be templated"
                )
            )
        for rel in rels:
            if (rel, href) in seen:
                problems.append(
                    DocumentProblem(
                        ("links", index, "href"), f"A {rel} link to {href} is listed twice"
                    )
                )
            seen.add((rel, href))
            if rel in IDENTITY_RELS:
                has_identity = True
                other = identity_hrefs.setdefault(str(href), rel)
                if other is not rel:
                    problems.append(
                        DocumentProblem(
                            ("links", index, "href"),
                            f"The {rel} link and the {other} link cannot share the href {href}",
                        )
                    )
    if not has_identity:
        problems.append(
            DocumentProblem(("links",), "At least one catalog or shelf link is required")
        )
    return problems


#: Besides letters and combining marks, a city name may hold these: "Saint-Étienne", "L'Aquila",
#: "St. Gallen". Digits, parentheses, slashes and other symbols are refused.
CITY_PUNCTUATION = frozenset(" -'\u2019.")  # \u2019 is the typographic apostrophe


def build_city_problems(metadata: dict[str, Any]) -> list[DocumentProblem]:
    """A city must read as a place name: letters (any script) and combining marks, with spaces,
    hyphens, apostrophes and periods between them; it starts with a letter and has no double
    spaces. One rule for the seed, `make add` and the admin API."""
    city = metadata.get("city")
    if city is None:
        return []
    path = ("metadata", "city")
    if not isinstance(city, str) or not city.strip():
        return [DocumentProblem(path, "The city cannot be blank.")]
    if len(city) > MAX_CITY_LENGTH:
        return [DocumentProblem(path, f"Use at most {MAX_CITY_LENGTH} characters.")]
    if city != city.strip():
        return [DocumentProblem(path, "Remove the spaces at the start or end.")]
    letters_only = all(
        unicodedata.category(char)[0] in "LM" or char in CITY_PUNCTUATION for char in city
    )
    if not letters_only or unicodedata.category(city[0])[0] != "L" or "  " in city:
        return [DocumentProblem(path, "Use letters only: no numbers or symbols.")]
    return []


def build_consistency_problems(document: dict[str, Any]) -> list[DocumentProblem]:
    """Repeats, the city and link rules: what the schema cannot see and the database would
    refuse or should not hold."""
    metadata = document["metadata"]
    return [
        *build_city_problems(metadata),
        *_build_repeat_problems(metadata.get("kind") or (), ("metadata", "kind"), "kind"),
        *_build_repeat_problems(
            metadata.get("publicationTypes") or (),
            ("metadata", "publicationTypes"),
            "publication type",
        ),
        *_build_repeat_problems(
            (normalise_language_tag(tag) for tag in metadata.get("supportedLanguages") or ()),
            ("metadata", "supportedLanguages"),
            "language",
        ),
        *_build_repeat_problems(
            metadata.get("subdivisions") or (), ("metadata", "subdivisions"), "subdivision"
        ),
        *build_link_problems(document["links"]),
    ]


def build_document_problems(document: dict[str, Any]) -> list[DocumentProblem]:
    """Everything in one catalog document that the schema cannot catch and the database would
    refuse. Run on a document that passed the schema."""
    return [*build_iso_code_problems(document["metadata"]), *build_consistency_problems(document)]
