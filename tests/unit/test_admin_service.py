"""`apply_country_subdivision_types`: the service checks the role itself, not only the route."""

from typing import Any

import pytest

from registry.core.errors import PermissionDeniedError
from registry.domain.admin_models import AdminActor
from registry.domain.enums import ActorRole
from registry.services.admin_service import (
    apply_admin_icon_cleanup,
    apply_country_subdivision_types,
)


async def test_an_editor_cannot_change_the_types_a_country_offers() -> None:
    editor = AdminActor(
        id="e", email="e@edrlab.org", name=None, role=ActorRole.EDITOR, is_active=True
    )
    # Has no methods: reading or writing the country before the role check fails differently.
    writer: Any = object()

    with pytest.raises(PermissionDeniedError):
        await apply_country_subdivision_types(writer, editor, "BE", ["province"])


class StoredIdentity:
    """An `ActorStore` whose person already logged in once with *issuer* / *subject*."""

    def __init__(self, issuer: str | None, subject: str | None) -> None:
        self.issuer, self.subject = issuer, subject

    async def persist_actor_login(self, **_: Any) -> AdminActor:
        return AdminActor(
            id="a1",
            email="ada@edrlab.org",
            name=None,
            role=ActorRole.ADMIN,
            is_active=True,
            identity_issuer=self.issuer,
            identity_subject=self.subject,
        )


@pytest.mark.parametrize(
    ("stored", "given", "allowed"),
    [
        ((None, None), ("https://idp", "sub-1"), True),  # first login records the account
        (("dev", None), ("https://idp", "sub-1"), True),  # a dev login first: no subject yet
        (("https://idp", "sub-1"), ("https://idp", "sub-1"), True),  # the same account again
        (("https://idp", "sub-1"), ("https://idp", "sub-2"), False),  # a reissued mailbox
        (("https://idp", "sub-1"), ("https://other", "sub-1"), False),  # another provider
        (("https://idp", "sub-1"), ("https://idp", None), True),  # no subject to compare
    ],
)
async def test_a_login_from_another_account_with_the_same_email_is_refused(
    stored: tuple[str | None, str | None], given: tuple[str, str | None], allowed: bool
) -> None:
    from registry.services.admin_identity import AdminIdentity  # noqa: PLC0415
    from registry.services.admin_service import resolve_admin_actor  # noqa: PLC0415

    identity = AdminIdentity(email="ada@edrlab.org", name=None, issuer=given[0], subject=given[1])
    store: Any = StoredIdentity(*stored)

    if allowed:
        assert (await resolve_admin_actor(store, identity)).email == "ada@edrlab.org"
    else:
        with pytest.raises(PermissionDeniedError):
            await resolve_admin_actor(store, identity)


STORED_ICON = "https://icons.example.org/c-icon.png?v=a-1"


class CatalogUnderLock:
    """A writer holding one catalog, recording the lock before any read of it."""

    def __init__(self, links: list[dict[str, str]] | None) -> None:
        self.links = links
        self.calls: list[str] = []

    async def lock_admin_catalog(self, catalog_id: str) -> object | None:
        self.calls.append("lock")
        return None if self.links is None else object()

    async def fetch_catalog_document(self, catalog_id: str) -> dict[str, Any]:
        self.calls.append("read")
        return {"metadata": {}, "links": self.links}


@pytest.mark.parametrize(
    ("links", "deleted"),
    [
        ([{"rel": "icon", "href": STORED_ICON}], False),
        ([{"rel": "icon", "href": "https://library.example.org/icon.png"}], True),
        ([], True),
        (None, True),
    ],
    ids=["stored-again-since", "a-library-link", "no-icon", "catalog-deleted"],
)
async def test_a_stored_icon_file_is_deleted_only_when_the_locked_catalog_no_longer_uses_it(
    links: list[dict[str, str]] | None, deleted: bool
) -> None:
    writer = CatalogUnderLock(links)
    removed: list[str] = []

    async def remove(catalog_id: str) -> None:
        writer.calls.append("delete")
        removed.append(catalog_id)

    await apply_admin_icon_cleanup(
        writer,  # type: ignore[arg-type]
        "c",
        is_stored=lambda href: href.startswith("https://icons.example.org/"),
        remove_icon=remove,
    )

    assert removed == (["c"] if deleted else [])
    assert writer.calls[0] == "lock"
