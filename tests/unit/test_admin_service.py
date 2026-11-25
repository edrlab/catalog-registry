"""`apply_country_subdivision_types`: the service checks the role itself, not only the route."""

from typing import Any

import pytest

from registry.core.errors import PermissionDeniedError
from registry.domain.admin_models import AdminActor
from registry.domain.enums import ActorRole
from registry.services.admin_service import apply_country_subdivision_types


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
