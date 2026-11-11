"""`resolve_feed_check`: the stages, the verdict and the report, with the network faked.

The fetcher is a `FeedFetcher` fake that answers by URL, so each case says exactly what a server
sent. The real client's protections are tested on their own (`test_safe_client.py`).
"""

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from registry.core.errors import CatalogNotFoundError, PermissionDeniedError
from registry.domain.admin_models import AdminActor
from registry.domain.enums import ActorRole
from registry.domain.fetching import FetchError, FetchResult, Hop
from registry.services import feed_check_service
from registry.services.feed_check_service import MAX_CHECKED_LINKS, resolve_feed_check

FEED = "https://feeds.example.org/opds"
AUTH = "https://feeds.example.org/auth.json"
EDITOR = AdminActor(id="a", email="e@edrlab.org", name=None, role=ActorRole.EDITOR, is_active=True)
VIEWER = AdminActor(id="v", email="v@edrlab.org", name=None, role=ActorRole.VIEWER, is_active=True)

Answer = FetchResult | FetchError


def answer(
    url: str,
    body: Any = b"",
    *,
    status: int = 200,
    media_type: str = "application/opds+json",
    final_url: str | None = None,
    redirects: tuple[Hop, ...] = (),
    headers: dict[str, str] | None = None,
) -> FetchResult:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return FetchResult(
        url=url,
        final_url=final_url or url,
        status=status,
        headers={"content-type": media_type, **(headers or {})},
        body=raw,
        redirects=redirects,
    )


def opds(*links: dict[str, Any], title: str = "A Library") -> dict[str, Any]:
    return {
        "metadata": {"title": title, "language": "fr"},
        "links": [{"rel": "self", "href": FEED}, *links],
        "navigation": [],
    }


class FakeFetcher:
    def __init__(self, answers: dict[str, Answer], *, delay: float = 0.0) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []
        self.delay = delay

    async def fetch(self, url: str, *, method: str = "GET", **_: Any) -> FetchResult:
        self.calls.append((method, url))
        if self.delay:
            await asyncio.sleep(self.delay)
        found = self.answers.get(url) or FetchError("fetch-failed", "The connection failed.")
        if isinstance(found, FetchError):
            raise found
        return found


def any_reader(owners: dict[str, tuple[str, str]] | None = None) -> Any:
    """A `FakeReader` typed loosely: it implements only what the check reads of `AdminReader`."""
    return FakeReader(owners)


class FakeReader:
    """*owners* maps an href, or the id derived from one, to the catalog (id, name) holding it;
    *stored* is the one catalog `fetch_admin_catalog` finds, under its database id."""

    def __init__(
        self,
        owners: dict[str, tuple[str, str]] | None = None,
        stored: str | None = None,
        stored_links: list[Any] | None = None,
    ) -> None:
        self.owners = owners or {}
        self.stored = stored
        self.stored_links = stored_links or []
        self.read_ids: list[str] = []

    async def fetch_admin_catalog_by_href(
        self, href: str, derived_id: str
    ) -> tuple[str, str] | None:
        return self.owners.get(href) or self.owners.get(derived_id)

    async def fetch_admin_catalog(self, catalog_id: str) -> Any:
        self.read_ids.append(catalog_id)
        if catalog_id != self.stored:
            return None
        return SimpleNamespace(
            row=SimpleNamespace(name="Mine", languages=["fr"]),
            description=None,
            links=self.stored_links,
        )


async def check(
    answers: dict[str, Answer],
    url: str = FEED,
    *,
    reader: Any = None,
    actor: AdminActor = EDITOR,
    **options: Any,
) -> Any:
    return await resolve_feed_check(
        FakeFetcher(answers),
        reader or any_reader(),
        actor,
        url,
        **options,
    )


def codes(report: Any) -> list[str]:
    return [finding.code for finding in report.findings]


def states(report: Any) -> list[str]:
    return [stage.state for stage in report.stages]


async def test_a_good_feed_passes_every_stage() -> None:
    report = await check(
        {
            FEED: answer(
                FEED, opds({"rel": "search", "href": "/search{?query}", "templated": True})
            ),
            "https://feeds.example.org/search": answer("https://feeds.example.org/search"),
        }
    )

    assert report.verdict == "ok"
    assert report.format == "opds2"
    assert [stage.key for stage in report.stages] == ["address", "fetch", "feed", "links"]
    assert states(report) == ["passed"] * 4
    assert report.feed.name == "A Library"
    assert report.feed.languages == ["fr"]
    assert [(link.rel, link.active) for link in report.feed.links] == [
        ("catalog", True),
        ("search", True),
    ]
    assert report.summary.errors == report.summary.warnings == 0


async def test_an_unsafe_address_is_blocked_without_any_request() -> None:
    fetcher = FakeFetcher({})

    report = await resolve_feed_check(
        fetcher,
        any_reader(),
        EDITOR,
        "http://169.254.169.254/",
    )

    assert report.verdict == "blocked"
    assert codes(report) == ["url-unsafe"]
    assert states(report) == ["failed", "skipped", "skipped", "skipped"]
    assert report.feed is None
    assert fetcher.calls == []


async def test_a_registered_feed_is_a_duplicate() -> None:
    report = await check({}, reader=FakeReader({FEED: ("id-1", "Old Library")}))

    assert report.verdict == "blocked"
    assert codes(report) == ["duplicate-catalog"]
    assert report.duplicate.id == "id-1"
    assert report.duplicate.name == "Old Library"


async def test_re_checking_the_same_catalog_is_not_a_duplicate() -> None:
    reader = FakeReader({FEED: ("00000000-0000-0000-0000-000000000001", "Mine")})

    with pytest.raises(CatalogNotFoundError):
        await check({FEED: answer(FEED, opds())}, reader=reader, catalog_id="not-a-uuid")


async def test_re_checking_with_an_uppercase_id_is_not_a_duplicate_of_itself() -> None:
    mine = "0f8fad5b-d9cb-469f-a165-70867728950e"
    reader = FakeReader({FEED: (mine, "Mine")}, stored=mine)

    report = await check({FEED: answer(FEED, opds())}, reader=reader, catalog_id=mine.upper())

    assert reader.read_ids == [mine]
    assert report.duplicate is None
    assert "duplicate-catalog" not in codes(report)


async def test_re_checking_reports_no_link_change_when_the_links_are_the_same() -> None:
    mine = "0f8fad5b-d9cb-469f-a165-70867728950e"
    same = SimpleNamespace(rel="catalog", href=FEED, media_type=None, title=None, templated=False)
    reader = FakeReader({FEED: (mine, "Mine")}, stored=mine, stored_links=[same])

    report = await check({FEED: answer(FEED, opds())}, reader=reader, catalog_id=mine)

    assert [change.field for change in report.changes] == ["name"]


async def test_an_address_a_catalog_was_created_from_is_a_duplicate() -> None:
    """The catalog moved to another address, but its id is still the one derived from this one."""
    derived = str(uuid.uuid5(uuid.NAMESPACE_URL, FEED))
    reader = FakeReader({derived: (derived, "Moved Library")})

    report = await check({}, reader=reader)

    assert report.verdict == "blocked"
    assert report.duplicate.id == derived


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (FetchError("fetch-timed-out", "No answer."), "fetch-timed-out"),
        (FetchError("fetch-too-large", "Too large."), "fetch-too-large"),
        (FetchError("redirect-unsafe", "Private."), "redirect-unsafe"),
        (FetchError("host-unresolvable", "No such host."), "host-unresolvable"),
    ],
)
async def test_a_failed_fetch_blocks(failure: FetchError, code: str) -> None:
    report = await check({FEED: failure})

    assert report.verdict == "blocked"
    assert code in codes(report)
    assert report.stages[2].state == "skipped"


@pytest.mark.parametrize("status", [404, 410, 500, 503])
async def test_an_error_status_blocks(status: int) -> None:
    report = await check({FEED: answer(FEED, b"{}", status=status)})

    assert codes(report) == ["fetch-status"]
    assert str(status) in report.findings[0].message


async def test_an_atom_feed_is_blocked() -> None:
    atom = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>'

    report = await check({FEED: answer(FEED, atom, media_type="application/atom+xml")})

    assert report.verdict == "blocked"
    assert codes(report) == ["atom-feed"]


async def test_a_web_page_is_accepted_with_its_head_and_icon() -> None:
    page = (
        b'<html lang="fr"><head><title>Ma bib</title>'
        b'<link rel="icon" href="/favicon.ico"></head><body>'
    )
    icon = "https://feeds.example.org/favicon.ico"

    report = await check(
        {
            FEED: answer(FEED, page, media_type="text/html"),
            icon: answer(icon, media_type="image/x-icon"),
        }
    )

    assert report.verdict == "ok"
    assert report.format == "html"
    assert codes(report) == ["html-page"]
    assert report.feed.name == "Ma bib"
    assert [(link.rel, link.type, link.active) for link in report.feed.links] == [
        ("catalog", "text/html", True),
        ("icon", "image/x-icon", True),
    ]


SITE = "https://www.example.org/"


def html(*links: str) -> FetchResult:
    return answer(SITE, f"<head>{''.join(links)}</head>".encode(), media_type="text/html")


def icon_links(report: Any) -> list[tuple[str, str | None]]:
    return [(link.href, link.type) for link in report.feed.links if link.rel == "icon"]


async def test_a_feed_without_an_icon_gets_its_websites_best_icon_that_answers() -> None:
    feed = opds({"rel": "alternate", "href": SITE, "type": "text/html; charset=utf-8"})
    page = html(
        '<link rel="icon" href="/small.png">',
        '<link rel="apple-touch-icon" href="/gone.png">',
    )

    report = await check(
        {
            FEED: answer(FEED, feed),
            SITE: page,
            f"{SITE}small.png": answer(f"{SITE}small.png", media_type="image/png"),
        }
    )

    assert report.verdict == "ok"  # the Apple touch icon that failed is not reported
    assert icon_links(report) == [(f"{SITE}small.png", "image/png")]


async def test_a_page_with_no_icon_that_answers_suggests_none_and_says_nothing() -> None:
    page = b'<html><head><title>Bib</title><link rel="icon" href="/gone.ico"></head>'

    report = await check({FEED: answer(FEED, page, media_type="text/html")})

    assert report.verdict == "ok"
    assert codes(report) == ["html-page"]
    assert icon_links(report) == []


async def test_a_well_known_icon_is_used_when_the_page_names_none() -> None:
    touch = "https://feeds.example.org/apple-touch-icon.png"
    page = b"<html><head><title>Bib</title></head>"

    report = await check(
        {
            FEED: answer(FEED, page, media_type="text/html"),
            touch: answer(touch, media_type="text/html"),  # a soft 404: not an image
            "https://feeds.example.org/favicon.ico": answer(touch, media_type="image/x-icon"),
        }
    )

    assert icon_links(report) == [("https://feeds.example.org/favicon.ico", "image/x-icon")]


async def test_a_slow_icon_search_gives_up_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_check_service, "ICON_BUDGET_SECONDS", 0.08)
    favicon = "https://feeds.example.org/favicon.ico"
    page = b"<html><head><title>Bib</title></head>"
    fetcher = FakeFetcher(
        {
            FEED: answer(FEED, page, media_type="text/html"),
            favicon: answer(favicon, media_type="image/x-icon"),
        },
        delay=0.05,
    )

    report = await resolve_feed_check(fetcher, any_reader(), EDITOR, FEED)

    # The first candidate fails at 0.05 s; the favicon would answer at 0.10 s, past the limit.
    assert report.verdict == "ok"
    assert codes(report) == ["html-page"]
    assert icon_links(report) == []


async def test_a_feed_with_its_own_icon_never_reads_its_website() -> None:
    icon = "https://feeds.example.org/icon.png"
    fetcher = FakeFetcher(
        {
            FEED: answer(
                FEED,
                opds(
                    {"rel": "icon", "href": icon},
                    {"rel": "alternate", "href": SITE, "type": "text/html"},
                ),
            ),
            icon: answer(icon, media_type="image/png"),
            SITE: answer(SITE, media_type="text/html"),
        }
    )

    report = await resolve_feed_check(fetcher, any_reader(), EDITOR, FEED, website=SITE)

    assert ("GET", SITE) not in fetcher.calls
    assert icon_links(report) == [(icon, None)]


async def test_a_given_website_becomes_the_alternate_link_and_gives_the_icon() -> None:
    favicon = f"{SITE}favicon.ico"

    report = await check(
        {
            FEED: answer(FEED, opds()),
            SITE: html(),
            favicon: answer(favicon, media_type="image/vnd.microsoft.icon"),
        },
        website=SITE,
    )

    assert report.verdict == "ok"
    assert [(link.rel, link.href, link.type, link.active) for link in report.feed.links] == [
        ("catalog", FEED, "application/opds+json", True),
        ("alternate", SITE, "text/html", True),
        ("icon", favicon, "image/vnd.microsoft.icon", True),
    ]


async def test_a_given_website_the_feed_already_links_is_not_repeated() -> None:
    feed = opds({"rel": "alternate", "href": SITE, "type": "text/html"})

    report = await check({FEED: answer(FEED, feed), SITE: html()}, website=SITE.rstrip("/"))

    assert [link.rel for link in report.feed.links].count("alternate") == 1


async def test_a_dead_website_warns_and_blocks_nothing() -> None:
    report = await check({FEED: answer(FEED, opds())}, website=SITE)

    assert report.verdict == "warnings"
    assert [(f.code, f.rel) for f in report.findings] == [("link-dead", "alternate")]
    assert icon_links(report) == []


async def test_a_refused_website_blocks_before_any_request() -> None:
    fetcher = FakeFetcher({})

    report = await resolve_feed_check(
        fetcher, any_reader(), EDITOR, FEED, website="http://127.0.0.1/"
    )

    assert report.verdict == "blocked"
    assert [(f.code, f.rel, f.url) for f in report.findings] == [
        ("url-unsafe", "alternate", "http://127.0.0.1/")
    ]
    assert fetcher.calls == []


async def test_a_web_page_ignores_the_website() -> None:
    page = b"<html><head><title>Bib</title></head>"

    report = await check({FEED: answer(FEED, page, media_type="text/html")}, website=SITE)

    assert [link.rel for link in report.feed.links] == ["catalog"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"not json", "not-json"),
        (b"[]", "not-opds2"),
        (b'{"metadata": {}, "links": []}', "title-missing"),
    ],
)
async def test_what_is_not_an_opds2_feed_is_blocked(body: bytes, code: str) -> None:
    report = await check({FEED: answer(FEED, body)})

    assert report.verdict == "blocked"
    assert code in codes(report)


async def test_a_feed_served_as_plain_json_warns() -> None:
    report = await check({FEED: answer(FEED, opds(), media_type="application/json")})

    assert report.verdict == "warnings"
    assert codes(report) == ["content-type-unexpected"]
    assert report.stages[1].state == "passed"


async def test_a_permanent_redirect_saves_the_new_address() -> None:
    moved = "https://new.example.org/opds"
    hops = (Hop(FEED, 301), Hop("https://mid.example.org/opds", 308))

    report = await check({FEED: answer(FEED, opds(), final_url=moved, redirects=hops)})

    assert report.final_url == moved
    assert "redirects-permanently" in codes(report)
    assert report.feed.links[0].href == moved


async def test_a_temporary_redirect_keeps_the_typed_address() -> None:
    hops = (Hop(FEED, 301), Hop("https://mid.example.org/opds", 302))

    report = await check(
        {FEED: answer(FEED, opds(), final_url="https://cdn.example.org/opds", redirects=hops)}
    )

    assert "redirects-temporarily" in codes(report)
    assert report.feed.links[0].href == FEED


async def test_a_permanent_move_onto_a_registered_feed_is_a_duplicate() -> None:
    moved = "https://new.example.org/opds"
    reader = FakeReader({moved: ("id-2", "Moved Library")})

    report = await check(
        {FEED: answer(FEED, opds(), final_url=moved, redirects=(Hop(FEED, 301),))}, reader=reader
    )

    assert report.verdict == "blocked"
    assert "duplicate-catalog" in codes(report)


async def test_leaving_https_for_http_warns() -> None:
    plain = "http://feeds.example.org/opds"

    report = await check({FEED: answer(FEED, opds(), final_url=plain, redirects=(Hop(FEED, 302),))})

    assert "https-downgrade" in codes(report)


async def test_a_feed_behind_sign_in_is_accepted_from_its_authentication_document() -> None:
    document = {"id": "urn:x", "title": "Members", "authentication": [{"type": "x"}]}

    report = await check(
        {
            FEED: answer(
                FEED, document, status=401, media_type="application/opds-authentication+json"
            )
        }
    )

    assert report.verdict == "ok"
    assert codes(report) == ["feed-needs-sign-in"]
    assert report.feed.name == "Members"
    assert [(link.rel, link.href, link.active) for link in report.feed.links] == [
        ("catalog", FEED, True),
        ("authenticate", FEED, True),
    ]


async def test_a_sign_in_document_named_by_a_link_header_is_followed() -> None:
    auth = "https://feeds.example.org/auth.json"
    document = {"id": "urn:x", "title": "Members", "authentication": []}

    report = await check(
        {
            FEED: answer(
                FEED,
                b"",
                status=401,
                headers={"link": f'<{auth}>; rel="http://opds-spec.org/auth/document"'},
            ),
            auth: answer(auth, document, media_type="application/opds-authentication+json"),
        }
    )

    assert report.verdict == "ok"
    assert report.feed.links[1].href == auth


@pytest.mark.parametrize(
    "target",
    [
        FetchError("fetch-failed", "The connection failed."),
        answer(AUTH, b"not json", media_type="application/json"),
        answer(AUTH, {"id": "urn:x"}, status=404),
        answer(AUTH, {"title": "Not a sign-in document"}, media_type="application/json"),
    ],
)
async def test_a_link_header_that_leads_to_no_sign_in_document_leaves_a_failed_fetch(
    target: Answer,
) -> None:
    header = {"link": f'<{AUTH}>; rel="http://opds-spec.org/auth/document"'}

    report = await check({FEED: answer(FEED, b"", status=401, headers=header), AUTH: target})

    assert codes(report) == ["fetch-status"]


async def test_a_401_without_an_authentication_document_is_a_failed_fetch() -> None:
    report = await check({FEED: answer(FEED, b"denied", status=401, media_type="text/plain")})

    assert report.verdict == "blocked"
    assert codes(report) == ["fetch-status"]


async def test_dead_links_warn_and_come_back_inactive() -> None:
    shelf = "https://feeds.example.org/shelf"
    icon = "https://feeds.example.org/icon.png"
    alternate = "https://www.example.org/"

    report = await check(
        {
            FEED: answer(
                FEED,
                opds(
                    {"rel": "shelf", "href": shelf},
                    {"rel": "icon", "href": icon},
                    {"rel": "alternate", "href": alternate, "type": "text/html"},
                ),
            ),
            shelf: answer(shelf, status=401),
            icon: answer(icon, media_type="text/html"),
            alternate: FetchError("fetch-failed", "The connection failed."),
        }
    )

    assert report.verdict == "warnings"
    assert report.stages[3].state == "warned"
    active = {link.rel: link.active for link in report.feed.links}
    assert active == {"catalog": True, "shelf": True, "icon": True, "alternate": False}
    by_code = {finding.code: finding for finding in report.findings}
    assert by_code["link-needs-sign-in"].rel == "shelf"
    assert by_code["icon-not-image"].url == icon
    assert by_code["link-dead"].rel == "alternate"
    assert by_code["link-dead"].url == alternate


async def test_a_template_with_unbalanced_braces_is_dead_without_a_request() -> None:
    fetcher = FakeFetcher(
        {
            FEED: answer(
                FEED,
                opds({"rel": "search", "href": "/search{?query", "templated": True}),
            )
        }
    )

    report = await resolve_feed_check(fetcher, any_reader(), EDITOR, FEED)

    assert [(f.code, f.rel) for f in report.findings] == [("link-dead", "search")]
    assert fetcher.calls == [("GET", FEED)]


async def test_a_sign_in_status_on_an_ordinary_link_means_it_is_dead() -> None:
    """Only a shelf, an authentication or a profile link is meant to be members-only."""
    alternate = "https://www.example.org/"

    report = await check(
        {
            FEED: answer(FEED, opds({"rel": "alternate", "href": alternate})),
            alternate: answer(alternate, status=403),
        }
    )

    assert [(f.code, f.rel) for f in report.findings] == [("link-dead", "alternate")]


@pytest.mark.parametrize(
    ("link", "served", "code"),
    [
        (
            {"rel": "http://opds-spec.org/auth/document"},
            "application/json",
            "authenticate-wrong-type",
        ),
        (
            {"rel": "alternate", "type": "text/html; charset=utf-8"},
            "application/pdf",
            "link-type-mismatch",
        ),
    ],
)
async def test_a_link_served_as_another_type_than_expected_warns(
    link: dict[str, str], served: str, code: str
) -> None:
    href = "https://feeds.example.org/other"

    report = await check(
        {
            FEED: answer(FEED, opds({**link, "href": href})),
            href: answer(href, media_type=served),
        }
    )

    assert codes(report) == [code]


async def test_a_link_that_fails_head_gets_one_get() -> None:
    search = "https://feeds.example.org/search"
    fetcher = FakeFetcher(
        {
            FEED: answer(FEED, opds({"rel": "search", "href": search})),
            search: answer(search, status=405),
        }
    )

    await resolve_feed_check(fetcher, any_reader(), EDITOR, FEED)

    assert fetcher.calls[-2:] == [("HEAD", search), ("GET", search)]


async def test_links_over_the_cap_are_not_checked() -> None:
    links = [{"rel": "alternate", "href": f"https://x.example/{n}"} for n in range(25)]
    answers: dict[str, Answer] = {FEED: answer(FEED, opds(*links))}
    answers |= {link["href"]: answer(link["href"]) for link in links}

    report = await check(answers)

    assert "links-capped" in codes(report)
    states_by_href = [link.active for link in report.feed.links[1:]]
    assert states_by_href.count(True) == MAX_CHECKED_LINKS
    assert states_by_href.count(None) == 5


async def test_the_budget_ends_the_check_with_a_report() -> None:
    links = [{"rel": "alternate", "href": f"https://x.example/{n}"} for n in range(3)]
    answers: dict[str, Answer] = {FEED: answer(FEED, opds(*links))}
    answers |= {link["href"]: answer(link["href"]) for link in links}

    report = await resolve_feed_check(
        FakeFetcher(answers, delay=0.05),
        any_reader(),
        EDITOR,
        FEED,
        budget=0.08,
    )

    assert "check-timed-out" in codes(report)
    assert report.verdict in {"warnings", "blocked"}


async def test_a_viewer_cannot_check() -> None:
    with pytest.raises(PermissionDeniedError):
        await check({}, actor=VIEWER)


async def test_a_large_web_page_is_read_from_its_head() -> None:
    page = b"<html><head><title>Big</title></head><body>"
    cut = FetchResult(
        url=FEED,
        final_url=FEED,
        status=200,
        headers={"content-type": "text/html"},
        body=page,
        truncated=True,
    )

    report = await check({FEED: cut})

    assert report.verdict == "ok"
    assert report.feed.name == "Big"


async def test_a_feed_cut_at_the_cap_is_too_large() -> None:
    cut = FetchResult(
        url=FEED,
        final_url=FEED,
        status=200,
        headers={"content-type": "application/opds+json"},
        body=b'{"metadata": {"title": "x"',
        truncated=True,
    )

    report = await check({FEED: cut})

    assert report.verdict == "blocked"
    assert codes(report) == ["fetch-too-large"]


async def test_links_that_answered_before_the_budget_ran_out_keep_their_result() -> None:
    """A timeout keeps what finished: one slow link must not make every link look unchecked."""
    fast = "https://fast.example.org/"
    slow = "https://slow.example.org/"

    class SlowLinkFetcher(FakeFetcher):
        async def fetch(self, url: str, *, method: str = "GET", **options: Any) -> FetchResult:
            if url == slow:
                await asyncio.sleep(10)
            return await super().fetch(url, method=method, **options)

    fetcher = SlowLinkFetcher(
        {
            FEED: answer(
                FEED, opds({"rel": "alternate", "href": fast}, {"rel": "alternate", "href": slow})
            ),
            fast: answer(fast),
        }
    )

    report = await resolve_feed_check(fetcher, any_reader(), EDITOR, FEED, budget=0.6)

    assert report.feed is not None
    active = {link.href: link.active for link in report.feed.links}
    assert active[fast] is True
    assert active[slow] is None
    assert "check-timed-out" in codes(report)


async def test_a_check_that_runs_out_of_time_before_the_feed_is_read_is_blocked() -> None:
    """No feed was read, so there is nothing to save: the verdict is blocked, not a warning."""
    fetcher = FakeFetcher({FEED: answer(FEED, opds())}, delay=5)

    report = await resolve_feed_check(fetcher, any_reader(), EDITOR, FEED, budget=0.2)

    assert report.verdict == "blocked"
    assert report.feed is None
    assert {"check-timed-out", "fetch-timed-out"} <= set(codes(report))
    assert [stage.state for stage in report.stages][1:] == ["failed", "skipped", "skipped"]
