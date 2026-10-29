"""Who is calling the admin API, from a signed token.

A key alone cannot impersonate a person: the token is verified here as well.

RS256 only, against the issuer's published keys; `iss`, `aud` and `exp` are required (RFC 7519
§4.1), with 60 seconds of clock skew. The key is looked up by the token's `kid` (RFC 7517).
"""

import asyncio
import hmac
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jwt

from registry.core.errors import AdminUnavailableError, AuthenticationError, PermissionDeniedError

CLOCK_SKEW_SECONDS = 60
KEY_FETCH_RETRY_SECONDS = 15.0

#: Finds the public key that signed a token. Injected so tests sign with a key of their own.
SigningKeyResolver = Callable[[str], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    email: str
    name: str | None
    issuer: str
    subject: str | None


def build_jwks_key_resolver(jwks_url: str) -> SigningKeyResolver:
    """Fetch the issuer's keys over HTTPS, cached by `PyJWKClient`.

    `PyJWKClient` blocks on the network, so it runs in a thread. A key set that cannot be fetched
    is 503, not 401: the caller did nothing wrong.
    """
    client = jwt.PyJWKClient(jwks_url, timeout=5)
    failed_at: float | None = None

    async def resolve(token: str) -> Any:
        nonlocal failed_at
        # After a failed fetch, answer at once for a while: the client holds a lock during the
        # fetch, so every request would otherwise wait out its own 5 s timeout, one after another.
        if failed_at is not None and time.monotonic() - failed_at < KEY_FETCH_RETRY_SECONDS:
            raise AdminUnavailableError("The signing keys could not be fetched.")
        try:
            key = (await asyncio.to_thread(client.get_signing_key_from_jwt, token)).key
        except (jwt.PyJWKClientConnectionError, ValueError) as error:
            # ValueError: the key set is not JSON (a proxy's error page). Not the caller's fault.
            failed_at = time.monotonic()
            raise AdminUnavailableError("The signing keys could not be fetched.") from error
        except jwt.PyJWTError as error:
            raise AuthenticationError("The token is not valid.") from error
        failed_at = None
        return key

    return resolve


def verify_service_key(presented: str | None, accepted: tuple[str, ...]) -> None:
    """Constant-time comparison against every accepted key, so a rotation can overlap."""
    if not accepted:
        raise AdminUnavailableError("The admin API is not configured.")
    candidate = (presented or "").encode()
    matched = False
    for key in accepted:
        # No short circuit: the time taken must not reveal which key matched.
        matched |= hmac.compare_digest(candidate, key.encode())
    if not matched:
        raise AuthenticationError("Missing or invalid service key.")


async def verify_admin_token(
    token: str | None,
    *,
    resolve_key: SigningKeyResolver,
    issuer: str,
    audience: str,
    allowed_domains: tuple[str, ...],
) -> AdminIdentity:
    if not token:
        raise AuthenticationError("Missing token.")
    key = await resolve_key(token)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as error:
        raise AuthenticationError("The token is not valid.") from error

    email = claims.get("email")
    if not isinstance(email, str) or not email:
        raise AuthenticationError("The token carries no email.")
    return build_identity(
        email,
        allowed_domains=allowed_domains,
        name=claims.get("name"),
        issuer=issuer,
        subject=claims.get("sub"),
    )


def build_identity(
    email: str,
    *,
    allowed_domains: tuple[str, ...] | None,
    name: str | None = None,
    issuer: str,
    subject: str | None = None,
) -> AdminIdentity:
    """Lowercase the email (the actor key) and enforce the domain allow-list.

    *allowed_domains* is None for the local dev identity, which is not tied to a domain.
    """
    normalised = email.lower()
    local, _, domain = normalised.partition("@")
    if (
        normalised.count("@") != 1
        or not local
        or not domain
        or any(c.isspace() for c in normalised)
    ):
        raise AuthenticationError("The identity is not an email address.")
    if allowed_domains is not None and domain not in allowed_domains:
        raise PermissionDeniedError("This account can't use the admin API.")
    return AdminIdentity(
        email=normalised,
        name=name if isinstance(name, str) and name else None,
        issuer=issuer,
        subject=subject if isinstance(subject, str) else None,
    )
