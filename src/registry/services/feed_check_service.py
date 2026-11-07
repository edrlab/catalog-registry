"""The feed check: one request, four stages, a report.

`address` → `fetch` → `feed` → `links`. A stage that finds an error stops the ones after it; `links`
never blocks. Everything runs inside one budget, so the report always comes back, with whatever did
not finish marked `skipped`. The network is a `FeedFetcher`; this module never imports an HTTP
client, so its tests drive it with a fake.
"""

import asyncio
import dataclasses
import itertools
import time
import uuid
from collections.abc import Awaitable, Callable  # noqa: TC003
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

from registry.core.errors import CatalogNotFoundError, PermissionDeniedError
from registry.domain.admin_models import AdminActor
from registry.domain.catalog_document import build_catalog_id
from registry.domain.enums import LinkRel
from registry.domain.feed_check import (
    AUTHENTICATION_MEDIA_TYPE,
    AUTHENTICATION_REL,
    OPDS_MEDIA_TYPE,
    STAGE_LABELS,
    FeedFields,
    FeedLink,
    Finding,
    Severity,
    Stage,
    build_feed_fields,
    build_feed_shape_findings,
    build_finding,
    build_link_header_targets,
    build_page_fields,
    build_severity_counts,
    build_signed_in_fields,
    build_template_probe_url,
    is_authentication_document,
    parse_json_document,
    parse_markup_kind,
)
from registry.domain.fetching import FetchError, FetchResult
from registry.domain.permissions import build_permissions
from registry.domain.url_policy import validate_outbound_url
from registry.repositories.protocols import AdminReader, FeedFetcher
from registry.schemas.admin import (
    FeedCheckDuplicate,
    FeedCheckFields,
    FeedCheckFinding,
    FeedCheckLink,
    FeedCheckResponse,
    FeedCheckStage,
    FeedCheckSummary,
    FeedLinksChange,
    FeedValueChange,
)

FEED_ACCEPT: Final = "application/opds+json, application/json;q=0.9, text/html;q=0.5, */*;q=0.1"
PROBE_ACCEPT: Final = "*/*"
FEED_MAX_BYTES: Final = 2_000_000
PROBE_MAX_BYTES: Final = 65_536
AUTHENTICATION_MAX_BYTES: Final = 262_144
#: Library servers are often slow: 4 to 6 s for a home page is common (measured 2026-11-05).
CHECK_BUDGET_SECONDS: Final = 18.0
FEED_BUDGET_SECONDS: Final = 12.0
LINK_BUDGET_SECONDS: Final = 4.0
#: The links stage ends this much before the whole check, so it can keep what finished.
REPORT_MARGIN_SECONDS: Final = 0.25
MAX_CHECKED_LINKS: Final = 20
LINK_CONCURRENCY: Final = 4
PER_HOST_CONCURRENCY: Final = 2
PERMANENT_REDIRECTS: Final = frozenset({301, 308})
SIGN_IN_STATUSES: Final = frozenset({401, 403})
#: A 401/403 on these means "members only", which is how they are meant to work.
SIGN_IN_RELS: Final = frozenset({LinkRel.SHELF.value, LinkRel.AUTHENTICATE.value, "profile"})


def _ok(status: int) -> bool:
    return 200 <= status < 300  # noqa: PLR2004


@dataclasses.dataclass
class _Run:
    """What one check has found so far. Mutable on purpose: a timeout keeps whatever is here."""

    url: str
    findings: list[Finding] = dataclasses.field(default_factory=list)
    states: dict[Stage, tuple[str, int]] = dataclasses.field(default_factory=dict)
    result: FetchResult | None = None
    address: str = ""
    format: str | None = None
    fields: FeedFields | None = None
    duplicate: tuple[str, str] | None = None
    #: A 401/403 that led to an OPDS Authentication document: (document, its URL).
    sign_in: tuple[dict[str, Any], str] | None = None

    def add(self, *findings: Finding) -> None:
        self.findings.extend(findings)

    def blocked_in(self, stage: Stage) -> bool:
        return any(f.stage is stage and f.severity is Severity.ERROR for f in self.findings)

    @property
    def blocked(self) -> bool:
        return any(finding.severity is Severity.ERROR for finding in self.findings)


async def resolve_feed_check(
    fetcher: FeedFetcher,
    reader: AdminReader,
    actor: AdminActor,
    url: str,
    *,
    catalog_id: str | None = None,
    budget: float = CHECK_BUDGET_SECONDS,
) -> FeedCheckResponse:
    """Check *url* and say what a catalog would get from it. With *catalog_id*, also what changed
    against that catalog (raises `CatalogNotFoundError` if it does not exist).

    Checking makes the registry request a URL, so it needs the permission to add a catalog, or to
    edit the one being re-checked; a viewer cannot use it to probe addresses."""
    permission = "catalog.edit" if catalog_id else "catalog.create"
    if permission not in build_permissions(actor.role):
        raise PermissionDeniedError(f"Your role cannot do this ({permission}).")
    stored = None
    if catalog_id:
        # One spelling from here on: the duplicate check compares it with the stored id.
        catalog_id = _normalise_checked_id(catalog_id)
        stored = await _fetch_stored_catalog(reader, catalog_id)
    started = time.monotonic()
    run = _Run(url=url, address=url)
    stages: list[tuple[Stage, Callable[[], Awaitable[None]]]] = [
        (Stage.ADDRESS, lambda: _check_address(run, reader, catalog_id)),
        (Stage.FETCH, lambda: _check_fetch(run, fetcher, reader, catalog_id)),
        (Stage.FEED, lambda: _check_feed(run)),
        (Stage.LINKS, lambda: _check_links(run, fetcher, started + budget)),
    ]
    current = Stage.ADDRESS
    try:
        async with asyncio.timeout(budget):
            for current, check in stages:
                stage_started = time.monotonic()
                await check()
                run.states[current] = (_stage_state(run, current), _ms(stage_started))
                if run.blocked:
                    break
    except TimeoutError:
        run.add(build_finding("check-timed-out", stage=current, seconds=f"{budget:g}"))
        if run.fields is None:
            # No feed was read, so there is nothing a catalog could get: that blocks.
            run.add(
                build_finding(
                    "fetch-timed-out",
                    detail=f"No complete answer within {budget:g} seconds.",
                    url=run.url,
                )
            )
        run.states[current] = ("failed" if run.blocked_in(current) else "warned", _ms(started))
    return _build_response(run, stored, started)


# --- stages --------------------------------------------------------------------------------------


async def _check_address(run: _Run, reader: AdminReader, catalog_id: str | None) -> None:
    try:
        validate_outbound_url(run.url)
    except FetchError as refused:
        run.add(build_finding(refused.code, detail=refused.detail, url=run.url))
        return
    await _check_duplicate(run, reader, run.url, catalog_id)


async def _check_duplicate(
    run: _Run, reader: AdminReader, href: str, catalog_id: str | None
) -> None:
    owner = await reader.fetch_admin_catalog_by_href(href, str(build_catalog_id(href)))
    if owner is not None and owner[0] != catalog_id:
        run.duplicate = owner
        run.add(build_finding("duplicate-catalog", stage=Stage.ADDRESS, name=owner[1], url=href))


async def _check_fetch(
    run: _Run, fetcher: FeedFetcher, reader: AdminReader, catalog_id: str | None
) -> None:
    try:
        # Cut rather than refused at the cap: a web page's head is all that is read, and a page
        # can be large. A feed cut at the cap is refused in the `feed` stage.
        result = await fetcher.fetch(
            run.url,
            accept=FEED_ACCEPT,
            max_bytes=FEED_MAX_BYTES,
            deadline=FEED_BUDGET_SECONDS,
            truncate=True,
        )
    except FetchError as failed:
        run.add(build_finding(failed.code, stage=Stage.FETCH, detail=failed.detail, url=run.url))
        return
    run.result = result
    if result.redirects:
        chain = [hop.url for hop in result.redirects] + [result.final_url]
        for before, after in itertools.pairwise(chain):
            if before.startswith("https:") and after.startswith("http:"):
                run.add(build_finding("https-downgrade", target=after, url=after))
        if all(hop.status in PERMANENT_REDIRECTS for hop in result.redirects):
            run.address = result.final_url
            run.add(
                build_finding(
                    "redirects-permanently", target=result.final_url, url=result.final_url
                )
            )
            await _check_duplicate(run, reader, run.address, catalog_id)
        else:
            run.add(
                build_finding(
                    "redirects-temporarily", target=result.final_url, url=result.final_url
                )
            )
    if result.status in SIGN_IN_STATUSES:
        run.sign_in = await _find_authentication_document(fetcher, result)
    if not _ok(result.status) and run.sign_in is None:
        run.add(build_finding("fetch-status", status=result.status, url=result.final_url))


async def _check_feed(run: _Run) -> None:
    result = run.result
    assert result is not None
    if run.sign_in is not None:
        document, document_url = run.sign_in
        run.format = "opds2"
        run.fields = build_signed_in_fields(
            document, address=run.address, authentication_url=document_url
        )
        run.add(build_finding("feed-needs-sign-in", url=document_url))
        return
    markup = parse_markup_kind(result.body, result.media_type)
    if markup == "atom-feed":
        run.add(build_finding("atom-feed"))
        return
    if markup == "html-page":
        run.format = "html"
        run.fields = build_page_fields(result.body, base_url=result.final_url, address=run.address)
        run.add(build_finding("html-page"))
        return
    if result.truncated:
        run.add(
            build_finding(
                "fetch-too-large",
                stage=Stage.FETCH,
                detail=f"The feed is larger than {FEED_MAX_BYTES} bytes.",
            )
        )
        return
    try:
        document = parse_json_document(result.body)
    except ValueError:
        run.add(build_finding("not-json"))
        return
    shape = build_feed_shape_findings(document)
    run.add(*shape)
    if any(finding.severity is Severity.ERROR for finding in shape):
        return
    run.format = "opds2"
    run.fields = build_feed_fields(document, base_url=result.final_url, address=run.address)
    if result.media_type != OPDS_MEDIA_TYPE:
        run.add(build_finding("content-type-unexpected", served=result.media_type or "no type"))


async def _find_authentication_document(
    fetcher: FeedFetcher, result: FetchResult
) -> tuple[dict[str, Any], str] | None:
    """The OPDS Authentication document a 401/403 leads to: in its body, or through a `Link` header
    (OPDS Authentication 1.0 §2). None when there is none: then the status is `fetch-status`."""
    try:
        document = parse_json_document(result.body)
    except ValueError:
        document = None
    if is_authentication_document(document):
        assert isinstance(document, dict)
        return document, result.final_url
    targets = build_link_header_targets(
        result.headers.get("link", ""), AUTHENTICATION_REL, result.final_url
    )
    if not targets:
        return None
    try:
        linked = await fetcher.fetch(
            targets[0],
            accept=f"{AUTHENTICATION_MEDIA_TYPE}, application/json;q=0.9",
            max_bytes=AUTHENTICATION_MAX_BYTES,
            deadline=LINK_BUDGET_SECONDS,
        )
        document = parse_json_document(linked.body) if _ok(linked.status) else None
    except (FetchError, ValueError):
        return None
    if not is_authentication_document(document):
        return None
    assert isinstance(document, dict)
    return document, targets[0]


async def _check_links(run: _Run, fetcher: FeedFetcher, ends_at: float) -> None:
    fields = run.fields
    assert fields is not None
    links = list(fields.links)
    pending = [index for index, link in enumerate(links) if link.active is None]
    checked, skipped = pending[:MAX_CHECKED_LINKS], pending[MAX_CHECKED_LINKS:]
    if skipped:
        run.add(build_finding("links-capped", cap=MAX_CHECKED_LINKS, skipped=len(skipped)))
    every = asyncio.Semaphore(LINK_CONCURRENCY)
    per_host: dict[str, asyncio.Semaphore] = {}

    async def probe(index: int) -> None:
        link = links[index]
        host = urlsplit(link.href).hostname or ""
        async with every, per_host.setdefault(host, asyncio.Semaphore(PER_HOST_CONCURRENCY)):
            active, findings = await _probe_link(fetcher, link)
        links[index] = dataclasses.replace(link, active=active)
        run.add(*findings)

    # Stop just before the whole check's deadline: when both fire together the outer one wins and
    # the links that already answered would be lost with the rest.
    remaining = ends_at - time.monotonic() - REPORT_MARGIN_SECONDS
    try:
        async with asyncio.timeout(max(0.0, remaining)):
            await asyncio.gather(*(probe(index) for index in checked))
    except TimeoutError:
        run.add(
            build_finding("check-timed-out", stage=Stage.LINKS, seconds=f"{CHECK_BUDGET_SECONDS:g}")
        )
    run.fields = dataclasses.replace(fields, links=tuple(links))


async def _probe_link(fetcher: FeedFetcher, link: FeedLink) -> tuple[bool, list[Finding]]:
    """Whether *link* answers, and what is worth saying about it. HEAD first; a server that does not
    answer HEAD properly gets one small GET."""
    target = build_template_probe_url(link.href) if link.templated else link.href
    if target is None:
        return False, [_dead(link, "the template's braces do not match.")]
    try:
        result = await fetcher.fetch(
            target,
            method="HEAD",
            accept=PROBE_ACCEPT,
            max_bytes=PROBE_MAX_BYTES,
            deadline=LINK_BUDGET_SECONDS,
            truncate=True,
        )
        if not _ok(result.status) and result.status not in SIGN_IN_STATUSES:
            result = await fetcher.fetch(
                target,
                accept=PROBE_ACCEPT,
                max_bytes=PROBE_MAX_BYTES,
                deadline=LINK_BUDGET_SECONDS,
                truncate=True,
            )
    except FetchError as failed:
        return False, [_dead(link, failed.detail)]
    if result.status in SIGN_IN_STATUSES:
        if link.rel in SIGN_IN_RELS:
            return True, [
                build_finding(
                    "link-needs-sign-in", rel=link.rel, url=link.href, status=result.status
                )
            ]
        return False, [_dead(link, f"the server answered {result.status}.")]
    if not _ok(result.status):
        return False, [_dead(link, f"the server answered {result.status}.")]
    return True, _build_type_findings(link, result.media_type)


def _build_type_findings(link: FeedLink, served: str) -> list[Finding]:
    if not served:
        return []
    if link.rel == LinkRel.ICON.value:
        if not served.startswith("image/"):
            return [build_finding("icon-not-image", rel=link.rel, url=link.href, served=served)]
        return []
    if link.rel == LinkRel.AUTHENTICATE.value and served != AUTHENTICATION_MEDIA_TYPE:
        return [
            build_finding("authenticate-wrong-type", rel=link.rel, url=link.href, served=served)
        ]
    declared = (link.type or "").split(";", 1)[0].strip().lower()
    if declared and declared != served:
        return [
            build_finding(
                "link-type-mismatch", rel=link.rel, url=link.href, declared=declared, served=served
            )
        ]
    return []


def _dead(link: FeedLink, detail: str) -> Finding:
    return build_finding("link-dead", rel=link.rel, url=link.href, detail=detail)


# --- the report ----------------------------------------------------------------------------------


def _ms(since: float) -> int:
    return round((time.monotonic() - since) * 1000)


def _stage_state(run: _Run, stage: Stage) -> str:
    severities = {finding.severity for finding in run.findings if finding.stage is stage}
    if Severity.ERROR in severities:
        return "failed"
    return "warned" if Severity.WARNING in severities else "passed"


def _normalise_checked_id(catalog_id: str) -> str:
    """*catalog_id* in the database's spelling (lowercase, hyphenated)."""
    try:
        return str(uuid.UUID(catalog_id))
    except ValueError:
        raise CatalogNotFoundError(f"No catalog with id {catalog_id}.") from None


async def _fetch_stored_catalog(reader: AdminReader, catalog_id: str) -> FeedFields:
    detail = await reader.fetch_admin_catalog(catalog_id)
    if detail is None:
        raise CatalogNotFoundError(f"No catalog with id {catalog_id}.")
    return FeedFields(
        name=detail.row.name,
        description=detail.description,
        languages=tuple(detail.row.languages),
        links=tuple(
            FeedLink(
                rel=link.rel,
                href=link.href,
                type=link.media_type,
                title=link.title,
                templated=link.templated,
                active=True,
            )
            for link in detail.links
            if link.rel != LinkRel.SELF.value
        ),
    )


def _build_changes(stored: FeedFields, feed: FeedFields) -> list[FeedValueChange | FeedLinksChange]:
    changes: list[FeedValueChange | FeedLinksChange] = [
        FeedValueChange(field=field, catalog=before, feed=after)
        for field, before, after in (
            ("name", stored.name, feed.name),
            ("description", stored.description, feed.description),
            ("languages", sorted(stored.languages), sorted(feed.languages)),
        )
        if before != after
    ]
    before = {(link.rel, link.href): link for link in stored.links}
    after = {(link.rel, link.href): link for link in feed.links}
    added = [_build_link(link) for key, link in after.items() if key not in before]
    removed = [_build_link(link) for key, link in before.items() if key not in after]
    if added or removed:
        changes.append(FeedLinksChange(field="links", added=added, removed=removed))
    return changes


def _build_link(link: FeedLink) -> FeedCheckLink:
    return FeedCheckLink(
        rel=link.rel,
        href=link.href,
        type=link.type,
        title=link.title,
        templated=link.templated,
        active=link.active,
    )


def _build_response(run: _Run, stored: FeedFields | None, started: float) -> FeedCheckResponse:
    counts = build_severity_counts(run.findings)
    blocked = counts[Severity.ERROR] > 0
    fields = None if blocked else run.fields
    return FeedCheckResponse(
        url=run.url,
        final_url=run.result.final_url if run.result else None,
        checked_at=datetime.now(UTC),
        duration_ms=_ms(started),
        verdict="blocked" if blocked else "warnings" if counts[Severity.WARNING] else "ok",
        format=None if blocked else run.format,
        stages=[
            FeedCheckStage(
                key=stage.value,
                label=STAGE_LABELS[stage],
                state=run.states.get(stage, ("skipped", 0))[0],
                duration_ms=run.states.get(stage, ("skipped", 0))[1],
            )
            for stage in Stage
        ],
        findings=[
            FeedCheckFinding(
                stage=finding.stage.value,
                severity=finding.severity.value,
                code=finding.code,
                message=finding.message,
                rel=finding.rel,
                url=finding.url,
                pointer=finding.pointer,
            )
            for finding in run.findings
        ],
        summary=FeedCheckSummary(
            errors=counts[Severity.ERROR],
            warnings=counts[Severity.WARNING],
            info=counts[Severity.INFO],
        ),
        feed=None
        if fields is None
        else FeedCheckFields(
            name=fields.name,
            description=fields.description,
            languages=list(fields.languages),
            links=[_build_link(link) for link in fields.links],
        ),
        duplicate=FeedCheckDuplicate(id=run.duplicate[0], name=run.duplicate[1])
        if run.duplicate
        else None,
        changes=_build_changes(stored, fields) if stored and fields else None,
    )
