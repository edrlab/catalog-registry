"""The feed check's vocabulary and its rules about OPDS documents. Pure. Stdlib only.

What lives here: the feed check's stages and finding codes (one table, so a code, its stage, its
severity and its fallback sentence cannot drift apart), and what a feed or a web page means to the
registry: which links are kept and under which rel, and what the metadata gives a catalog. The
network and the order of the stages are `services/feed_check_service.py`'s business.
"""

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from registry.domain.catalog_document import (
    MAX_DESCRIPTION_LENGTH,
    MAX_HREF_BYTES,
    MAX_LINK_TITLE_LENGTH,
    MAX_MEDIA_TYPE_LENGTH,
    MAX_NAME_LENGTH,
)
from registry.domain.enums import LinkRel
from registry.domain.language import normalise_language_tag


class Stage(StrEnum):
    ADDRESS = "address"
    FETCH = "fetch"
    FEED = "feed"
    LINKS = "links"


STAGE_LABELS: Mapping[Stage, str] = {
    Stage.ADDRESS: "Address",
    Stage.FETCH: "Fetch",
    Stage.FEED: "OPDS 2.0 feed",
    Stage.LINKS: "Links",
}


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class FindingKind:
    stage: Stage | None
    severity: Severity
    #: `str.format` template; values are quoted and shortened by `build_finding`.
    template: str


#: Every finding code. The UI keys its own wording on the code; this sentence is the
#: fallback. `stage` None means "whichever stage was running".
FINDINGS: Mapping[str, FindingKind] = {
    "url-invalid": FindingKind(Stage.ADDRESS, Severity.ERROR, "{detail}"),
    "url-unsafe": FindingKind(Stage.ADDRESS, Severity.ERROR, "{detail}"),
    "host-unresolvable": FindingKind(
        Stage.FETCH, Severity.ERROR, "The host name could not be found."
    ),
    "duplicate-catalog": FindingKind(
        Stage.ADDRESS, Severity.ERROR, "This feed is already in the registry as {name}."
    ),
    "fetch-failed": FindingKind(Stage.FETCH, Severity.ERROR, "{detail}"),
    "fetch-timed-out": FindingKind(Stage.FETCH, Severity.ERROR, "{detail}"),
    "fetch-status": FindingKind(Stage.FETCH, Severity.ERROR, "The server answered {status}."),
    "fetch-too-large": FindingKind(Stage.FETCH, Severity.ERROR, "{detail}"),
    "redirect-unsafe": FindingKind(
        Stage.FETCH, Severity.ERROR, "A redirect leads to an address that is refused: {detail}"
    ),
    "redirect-loop": FindingKind(Stage.FETCH, Severity.ERROR, "{detail}"),
    "redirects-permanently": FindingKind(
        Stage.FETCH,
        Severity.INFO,
        "The address has moved permanently to {target}; that address will be saved.",
    ),
    "redirects-temporarily": FindingKind(
        Stage.FETCH,
        Severity.INFO,
        "The address redirects to {target} for now; the address you entered will be saved.",
    ),
    "https-downgrade": FindingKind(
        Stage.FETCH, Severity.WARNING, "A redirect leaves https for http: {target}."
    ),
    "content-type-unexpected": FindingKind(
        Stage.FETCH,
        Severity.WARNING,
        "The feed is served as {served}; OPDS 2.0 feeds use `application/opds+json`.",
    ),
    "not-json": FindingKind(
        Stage.FEED, Severity.ERROR, "The answer is not JSON, so it is not an OPDS 2.0 feed."
    ),
    "html-page": FindingKind(
        Stage.FEED,
        Severity.INFO,
        "This is a web page, not an OPDS feed. It is added as a link to the website.",
    ),
    "atom-feed": FindingKind(
        Stage.FEED,
        Severity.ERROR,
        "This is an OPDS 1 (Atom) feed. Only OPDS 2.0 (JSON) feeds can be added.",
    ),
    "not-opds2": FindingKind(Stage.FEED, Severity.ERROR, "This is not an OPDS 2.0 feed: {detail}"),
    "title-missing": FindingKind(Stage.FEED, Severity.ERROR, "The feed has no `metadata.title`."),
    "self-missing": FindingKind(Stage.FEED, Severity.WARNING, "The feed has no `self` link."),
    "no-collections": FindingKind(
        Stage.FEED,
        Severity.WARNING,
        "The feed has no `navigation`, `publications` or `groups`.",
    ),
    "feed-needs-sign-in": FindingKind(
        Stage.FEED,
        Severity.INFO,
        "The feed needs a sign-in. Its name comes from its authentication document.",
    ),
    "link-dead": FindingKind(
        Stage.LINKS, Severity.WARNING, "The {rel} link did not work: {detail}"
    ),
    "link-needs-sign-in": FindingKind(
        Stage.LINKS, Severity.INFO, "The {rel} link needs a sign-in ({status})."
    ),
    "link-type-mismatch": FindingKind(
        Stage.LINKS,
        Severity.WARNING,
        "The {rel} link says {declared} but the server sends {served}.",
    ),
    "icon-not-image": FindingKind(
        Stage.LINKS, Severity.WARNING, "The icon link does not serve an image ({served})."
    ),
    "authenticate-wrong-type": FindingKind(
        Stage.LINKS,
        Severity.WARNING,
        "The authentication document is served as {served}, "
        "not `application/opds-authentication+json`.",
    ),
    "links-capped": FindingKind(
        Stage.LINKS,
        Severity.INFO,
        "Only the first {cap} links were checked; {skipped} more were not.",
    ),
    "check-timed-out": FindingKind(
        None, Severity.WARNING, "The check stopped after {seconds} seconds; the rest was not run."
    ),
}

#: A value quoted in a sentence is cut to this length: nothing a server sent is echoed at length.
MAX_QUOTED = 200


@dataclass(frozen=True, slots=True)
class Finding:
    stage: Stage
    severity: Severity
    code: str
    message: str
    rel: str | None = None
    url: str | None = None
    pointer: str | None = None


def _quote(value: Any) -> str:
    text = str(value)
    return text if len(text) <= MAX_QUOTED else text[: MAX_QUOTED - 1] + "…"


def build_finding(
    code: str,
    *,
    stage: Stage | None = None,
    rel: str | None = None,
    url: str | None = None,
    pointer: str | None = None,
    **values: Any,
) -> Finding:
    """The finding *code*, its sentence filled with *values* (each shortened). Raises `KeyError`
    for a code not in `FINDINGS`, so a typo fails a test rather than reaching the UI."""
    kind = FINDINGS[code]
    resolved = kind.stage or stage
    assert resolved is not None, f"{code} needs the running stage"
    quoted = {name: _quote(value) for name, value in values.items()}
    if rel is not None:
        quoted.setdefault("rel", rel)
    return Finding(
        stage=resolved,
        severity=kind.severity,
        code=code,
        message=kind.template.format(**quoted),
        rel=rel,
        url=None if url is None else _quote(url),
        pointer=pointer,
    )


# --- reading a feed ------------------------------------------------------------------------------

OPDS_MEDIA_TYPE = "application/opds+json"
AUTHENTICATION_MEDIA_TYPE = "application/opds-authentication+json"
#: OPDS Authentication 1.0 §2: the link a feed (or a 401) gives to its authentication document.
AUTHENTICATION_REL = "http://opds-spec.org/auth/document"
#: Remote rels the registry stores under its own name.
REL_ALIASES: Mapping[str, LinkRel] = {
    "http://opds-spec.org/shelf": LinkRel.SHELF,
    AUTHENTICATION_REL: LinkRel.AUTHENTICATE,
}
#: Never taken from a feed: `self` is the registry's own URL, `catalog` is the address checked.
NEVER_IMPORTED = frozenset({LinkRel.SELF, LinkRel.CATALOG})
#: A JSON document deeper than this is refused before Python's recursion limit is near.
MAX_JSON_DEPTH = 64


def normalise_remote_rel(rel: Any) -> LinkRel | None:
    """One OPDS rel → the registry's vocabulary, or `None` for one we do not store.

    A Readium link's `rel` is a string *or* an array (`link.schema.json`); the first recognised
    value wins. Anything outside `LinkRel` and `REL_ALIASES` is `None`: a feed cannot widen the
    registry's eight rels by publishing others.
    """
    candidates = rel if isinstance(rel, list) else [rel]
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        if (aliased := REL_ALIASES.get(candidate)) is not None:
            return aliased
        try:
            resolved = LinkRel(candidate)
        except ValueError:
            continue
        if resolved not in NEVER_IMPORTED:
            return resolved
    return None


def parse_json_document(body: bytes) -> Any:
    """*body* as JSON, refusing nesting deeper than `MAX_JSON_DEPTH` before parsing it.

    Raises `ValueError` (with a sentence) for anything that is not reasonable JSON.
    """
    depth = deepest = 0
    in_string = escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # noqa: PLR2004 - backslash
                escaped = True
            elif byte == 0x22:  # noqa: PLR2004 - quote
                in_string = False
        elif byte == 0x22:  # noqa: PLR2004
            in_string = True
        elif byte in b"[{":
            depth += 1
            deepest = max(deepest, depth)
            if deepest > MAX_JSON_DEPTH:
                raise ValueError(f"The JSON is nested more than {MAX_JSON_DEPTH} levels deep.")
        elif byte in b"]}":
            depth -= 1
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise ValueError("The answer is not valid JSON.") from None


_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def parse_markup_kind(body: bytes, media_type: str) -> str | None:
    """`"atom-feed"`, `"html-page"`, or None when *body* is neither (by type or first bytes)."""
    head = body[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if "atom" in media_type or (head.startswith((b"<?xml", b"<feed")) and b"<feed" in head):
        return "atom-feed"
    if media_type in _HTML_TYPES or head.startswith((b"<!doctype", b"<html", b"<head", b"<body")):
        return "html-page"
    return None


@dataclass(frozen=True, slots=True)
class FeedLink:
    rel: str
    href: str
    type: str | None = None
    title: str | None = None
    templated: bool = False
    #: True answered, False dead, None not checked.
    active: bool | None = None


@dataclass(frozen=True, slots=True)
class FeedFields:
    """What a feed gives a catalog: the check report's `feed`."""

    #: None when the source has none (a web page without `<title>`): the editor types one.
    name: str | None
    description: str | None = None
    languages: tuple[str, ...] = ()
    links: tuple[FeedLink, ...] = field(default=())


def _text(value: Any, limit: int) -> str | None:
    """*value* on one line, cut to *limit*: the admin API's limit for the field it fills, so a
    suggested value always saves."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.replace("\x00", "").split())
    return cleaned[:limit] or None


def _languages(value: Any) -> tuple[str, ...]:
    tags = value if isinstance(value, list) else [value]
    seen = dict.fromkeys(
        normalise_language_tag(tag)
        for tag in tags
        if isinstance(tag, str) and re.fullmatch(r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*", tag.strip())
    )
    return tuple(seen)


def build_feed_links(links: Any, base_url: str) -> list[FeedLink]:
    """The links of a remote document the registry keeps, hrefs made absolute against *base_url*.

    Untrusted input: entries that are not objects, have no string href, resolve to anything but
    http(s), are longer than the registry can store, or carry a rel the registry does not store
    are skipped. Repeats (rel + href) collapse.
    """
    kept: dict[tuple[str, str], FeedLink] = {}
    for link in links if isinstance(links, list) else ():
        if not isinstance(link, dict) or not isinstance(link.get("href"), str):
            continue
        rel = normalise_remote_rel(link.get("rel"))
        href = urljoin(base_url, link["href"].strip())
        if (
            rel is None
            or urlsplit(href).scheme not in {"http", "https"}
            or len(href.encode()) > MAX_HREF_BYTES
        ):
            continue
        kept.setdefault(
            (rel.value, href),
            FeedLink(
                rel=rel.value,
                href=href,
                type=_text(link.get("type"), MAX_MEDIA_TYPE_LENGTH),
                title=_text(link.get("title"), MAX_LINK_TITLE_LENGTH),
                templated=bool(link.get("templated")) and rel is LinkRel.SEARCH,
            ),
        )
    return list(kept.values())


def build_feed_fields(document: Mapping[str, Any], *, base_url: str, address: str) -> FeedFields:
    """A catalog's feed fields from an OPDS 2.0 feed. *address* becomes the `catalog` link."""
    metadata = document.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    catalog = FeedLink(rel=LinkRel.CATALOG.value, href=address, type=OPDS_MEDIA_TYPE, active=True)
    return FeedFields(
        name=_text(metadata.get("title"), MAX_NAME_LENGTH),
        description=_text(metadata.get("description"), MAX_DESCRIPTION_LENGTH),
        languages=_languages(metadata.get("language")),
        links=(catalog, *build_feed_links(document.get("links"), base_url)),
    )


def build_signed_in_fields(
    authentication: Mapping[str, Any], *, address: str, authentication_url: str
) -> FeedFields:
    """A catalog's feed fields from the OPDS Authentication document of a feed behind sign-in: its
    name, the feed address and the `authenticate` link (both answered, so both active)."""
    return FeedFields(
        name=_text(authentication.get("title"), MAX_NAME_LENGTH),
        description=_text(authentication.get("description"), MAX_DESCRIPTION_LENGTH),
        links=(
            FeedLink(rel=LinkRel.CATALOG.value, href=address, type=OPDS_MEDIA_TYPE, active=True),
            FeedLink(
                rel=LinkRel.AUTHENTICATE.value,
                href=authentication_url,
                type=AUTHENTICATION_MEDIA_TYPE,
                active=True,
            ),
        ),
    )


def is_authentication_document(document: Any) -> bool:
    """OPDS Authentication 1.0 §2: an object with `title` and an `authentication` list."""
    return (
        isinstance(document, dict)
        and isinstance(document.get("title"), str)
        and isinstance(document.get("authentication"), list)
    )


_COLLECTIONS = ("navigation", "publications", "groups")


def _rels(link: Mapping[str, Any]) -> list[Any]:
    rel = link.get("rel")
    return rel if isinstance(rel, list) else [rel]


def build_feed_shape_findings(document: Any) -> list[Finding]:
    """What makes *document* (parsed JSON) more or less than an OPDS 2.0 feed (OPDS 2.0 §2)."""
    if not isinstance(document, dict):
        return [build_finding("not-opds2", detail="the top level is not a JSON object.")]
    metadata = document.get("metadata")
    links = document.get("links")
    if not isinstance(metadata, dict):
        return [build_finding("not-opds2", detail="it has no `metadata` object.")]
    findings = []
    if not _text(metadata.get("title"), MAX_NAME_LENGTH):
        findings.append(build_finding("title-missing", pointer="/metadata/title"))
    if not isinstance(links, list):
        findings.append(
            build_finding("not-opds2", detail="it has no `links` array.", pointer="/links")
        )
    elif not any(isinstance(link, dict) and "self" in _rels(link) for link in links):
        findings.append(build_finding("self-missing", pointer="/links"))
    if not any(isinstance(document.get(key), list) for key in _COLLECTIONS):
        findings.append(build_finding("no-collections"))
    return findings


def build_link_header_targets(header: str, rel: str, base_url: str) -> list[str]:
    """The targets of `Link` header entries with *rel* (RFC 8288 §3), made absolute."""
    targets = []
    for match in re.finditer(r"<([^>]*)>((?:\s*;\s*[^;,]+)*)", header):
        params = match.group(2).lower()
        if re.search(r'rel\s*=\s*"?[^";,]*' + re.escape(rel.lower()), params):
            targets.append(urljoin(base_url, match.group(1).strip()))
    return targets


def build_template_probe_url(href: str) -> str | None:
    """*href* with every RFC 6570 expression expanded with its variables undefined, which yields
    the empty string (§2.3): `…/search{?query}` → `…/search`. None if the braces do not balance."""
    expanded = re.sub(r"\{[^{}]*\}", "", href)
    return None if "{" in expanded or "}" in expanded else expanded


def build_severity_counts(findings: Iterable[Finding]) -> dict[Severity, int]:
    counts = dict.fromkeys(Severity, 0)
    for finding in findings:
        counts[finding.severity] += 1
    return counts


# --- reading a web page --------------------------------------------------------------------------

#: Only the start of a page is read: the head is all the registry wants.
HTML_HEAD_BYTES = 262_144
_APPLE_ICON_RELS = frozenset({"apple-touch-icon", "apple-touch-icon-precomposed"})
#: Where browsers look when a page names no icon; tried after the page's own, in this order.
WELL_KNOWN_ICON_PATHS = ("/apple-touch-icon.png", "/favicon.ico")
#: At most this many icon addresses are tried for one page.
MAX_ICON_CANDIDATES = 4


def _icon_area(sizes: str) -> float:
    """The largest icon a `<link sizes>` value declares (HTML Standard, the link element's `sizes`
    attribute: `any`, or `WxH` tokens). `any` is a scalable icon, so the largest; none is 0."""
    largest = 0.0
    for token in sizes.lower().split():
        if token == "any":
            return math.inf
        width, _, height = token.partition("x")
        if width.isdigit() and height.isdigit():
            largest = max(largest, int(width) * int(height))
    return largest


class _HeadReader(HTMLParser):
    """Collects `<html lang>`, `<title>`, `<meta name="description">` and the icon links. The
    stdlib parser only tokenises: nothing is fetched, run or rendered."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang: str | None = None
        self.title: list[str] = []
        self.description: str | None = None
        #: (is an Apple touch icon, declared area, href), in document order.
        self.icons: list[tuple[bool, float, str]] = []
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:  # the rest of a chunk can run past the head
            return
        values = {name.lower(): value or "" for name, value in attrs}
        if tag == "html":
            self.lang = values.get("lang") or None
        elif tag == "title" and not self.title:
            self._in_title = True
        elif tag == "meta" and values.get("name", "").lower() == "description":
            self.description = self.description or values.get("content")
        elif tag == "link" and values.get("href"):
            # `rel` is a set of tokens: "shortcut icon" and "icon shortcut" are both an icon.
            rels = set(values.get("rel", "").lower().split())
            apple = bool(rels & _APPLE_ICON_RELS)
            if apple or "icon" in rels:
                self.icons.append((apple, _icon_area(values.get("sizes", "")), values["href"]))
        elif tag == "body":
            self.done = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)


def _read_head(body: bytes) -> _HeadReader:
    reader = _HeadReader()
    text = body[:HTML_HEAD_BYTES].decode("utf-8", errors="replace")
    for start in range(0, len(text), 8192):
        reader.feed(text[start : start + 8192])
        if reader.done:
            break
    return reader


def build_page_fields(body: bytes, *, address: str) -> FeedFields:
    """A catalog's fields from a web page's head: `<title>`, the meta description, `<html lang>`
    and the page as the `catalog` link (`text/html`). Its icon is looked for when the links are
    checked (`build_icon_candidates`), because only an icon that answers is worth suggesting."""
    reader = _read_head(body)
    return FeedFields(
        name=_text("".join(reader.title), MAX_NAME_LENGTH),
        description=_text(reader.description, MAX_DESCRIPTION_LENGTH),
        languages=_languages(reader.lang),
        links=(FeedLink(rel=LinkRel.CATALOG.value, href=address, type="text/html", active=True),),
    )


def build_icon_candidates(body: bytes, *, base_url: str) -> list[str]:
    """Where a web page's icon may be, best first: its Apple touch icons, then its other icons by
    largest declared size, then the well-known `/apple-touch-icon.png` and `/favicon.ico` on its
    origin. Absolute, http(s) only, no repeats, at most `MAX_ICON_CANDIDATES`."""
    ranked = sorted(_read_head(body).icons, key=lambda icon: (not icon[0], -icon[1]))
    parts = urlsplit(base_url)
    hrefs = [urljoin(base_url, href.strip()) for _, _, href in ranked] + [
        f"{parts.scheme}://{parts.netloc}{path}" for path in WELL_KNOWN_ICON_PATHS
    ]
    kept = [
        href
        for href in dict.fromkeys(hrefs)
        if urlsplit(href).scheme in {"http", "https"} and len(href.encode()) <= MAX_HREF_BYTES
    ]
    return kept[:MAX_ICON_CANDIDATES]
