"""Who may call the admin API, as FastAPI dependencies.

The order is fixed: key, then identity, then the person (by lowercased email), then the
permission. Every `/admin` route depends on
`require_permission(...)`, and `tests/unit/test_admin_routes_are_guarded.py` fails if one does not.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Final

from fastapi import Depends, Request

from registry.api.admin_log import ACTOR_STATE_KEY
from registry.core.errors import AuthenticationError, PermissionDeniedError
from registry.domain.admin_models import AdminActor
from registry.domain.permissions import build_permissions
from registry.services.admin_identity import (
    AdminIdentity,
    build_identity,
    verify_admin_token,
    verify_service_key,
)
from registry.services.admin_service import resolve_admin_actor

HEADER_SERVICE_KEY: Final = "x-registry-service-key"
HEADER_ADMIN_TOKEN: Final = "cf-access-jwt-assertion"
HEADER_DEV_IDENTITY: Final = "x-registry-dev-identity"

#: Set on the dependency `require_permission` returns, so a test can find every guarded route.
PERMISSION_ATTRIBUTE: Final = "required_permission"


async def authenticate_admin_caller(request: Request) -> AdminIdentity:
    """Prove the call is allowed, and who is acting.

    A refusal is logged once by the error handler (`api/admin_log.py`); the key is never logged.
    """
    settings = request.app.state.settings
    verify_service_key(request.headers.get(HEADER_SERVICE_KEY), settings.service_key_values)

    dev_email = request.headers.get(HEADER_DEV_IDENTITY)
    # Never alongside a configured issuer: an unverified email must not outrank a verified token.
    if dev_email and settings.admin_dev_identity and not settings.admin_token_issuer:
        return build_identity(dev_email, allowed_domains=None, issuer="dev")

    token = request.headers.get(HEADER_ADMIN_TOKEN)
    if not token:
        raise AuthenticationError("Missing token.")
    resolve_key = request.app.state.resolve_admin_key
    issuer, audience = settings.admin_token_issuer, settings.admin_token_audience
    if resolve_key is None or not (issuer and audience):
        # Only reachable locally (Settings refuses this configuration when deployed): a token
        # that cannot be checked is not a valid one, and it is not an outage.
        raise AuthenticationError("The token is not valid.")
    return await verify_admin_token(
        token,
        resolve_key=resolve_key,
        issuer=issuer,
        audience=audience,
        allowed_domains=settings.allowed_email_domains,
    )


async def authenticate_admin_actor(
    request: Request, identity: Annotated[AdminIdentity, Depends(authenticate_admin_caller)]
) -> AdminActor:
    async with request.app.state.open_actor_store() as store:
        actor = await resolve_admin_actor(store, identity)
    # For the request's log line (`log_admin_request`): who did it, by id, never by email.
    setattr(request.state, ACTOR_STATE_KEY, actor.id)
    return actor


def require_permission(permission: str) -> Callable[..., Awaitable[AdminActor]]:
    """A dependency that authenticates the caller and checks their role grants *permission*."""

    async def check(
        actor: Annotated[AdminActor, Depends(authenticate_admin_actor)],
    ) -> AdminActor:
        if permission not in build_permissions(actor.role):
            raise PermissionDeniedError(f"Your role cannot do this ({permission}).")
        return actor

    setattr(check, PERMISSION_ATTRIBUTE, permission)
    return check
