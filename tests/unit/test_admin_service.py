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
