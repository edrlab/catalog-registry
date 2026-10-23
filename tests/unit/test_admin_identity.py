"""Key and token verification (RFC 7519 claims, RS256), with a key pair of our own."""

import base64
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from registry.core.errors import AdminUnavailableError, AuthenticationError, PermissionDeniedError
from registry.services.admin_identity import (
    AdminIdentity,
    build_identity,
    verify_admin_token,
    verify_service_key,
)

pytestmark = pytest.mark.unit

ISSUER = "https://issuer.example.test"
AUD = "aud-tag"
DOMAINS = ("edrlab.org",)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def resolve_test_key(_token: str) -> Any:
    return KEY.public_key()


def sign(key: Any = KEY, algorithm: str = "RS256", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": [AUD],
        "exp": now + 300,
        "iat": now,
        "sub": "access-subject",
        "email": "Roni@EDRLab.org",
    }
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm=algorithm)


async def verify(token: str | None) -> AdminIdentity:
    return await verify_admin_token(
        token,
        resolve_key=resolve_test_key,
        issuer=ISSUER,
        audience=AUD,
        allowed_domains=DOMAINS,
    )


# --- service key -------------------------------------------------------------------------------


def test_the_service_key_is_accepted() -> None:
    verify_service_key("secret", ("secret",))


def test_either_of_two_keys_is_accepted_during_a_rotation() -> None:
    verify_service_key("next", ("current", "next"))


@pytest.mark.parametrize("presented", [None, "", "wrong", "secre", "secret "])
def test_a_missing_or_wrong_service_key_is_a_401(presented: str | None) -> None:
    with pytest.raises(AuthenticationError):
        verify_service_key(presented, ("secret",))


def test_no_configured_key_fails_closed_with_a_503() -> None:
    with pytest.raises(AdminUnavailableError):
        verify_service_key("anything", ())


# --- access token ------------------------------------------------------------------------------


async def test_a_valid_token_yields_the_lowercased_email() -> None:
    identity = await verify(sign(name="Roni"))

    assert identity.email == "roni@edrlab.org"
    assert identity.name == "Roni"
    assert identity.issuer == ISSUER
    assert identity.subject == "access-subject"


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "not-a-jwt",
        sign(exp=int(time.time()) - 3600),  # expired beyond the 60 s skew
        sign(aud=["someone-else"]),
        sign(iss="https://other.example.test"),
        sign(key=OTHER_KEY),  # signed by a key that is not the team's
        sign(email=None),
        sign(exp=None),
    ],
    ids=[
        "none",
        "empty",
        "garbage",
        "expired",
        "wrong-aud",
        "wrong-iss",
        "wrong-key",
        "no-email",
        "no-exp",
    ],
)
async def test_a_bad_token_is_a_401(token: str | None) -> None:
    with pytest.raises(AuthenticationError):
        await verify(token)


async def test_a_token_expired_within_the_clock_skew_is_accepted() -> None:
    assert (await verify(sign(exp=int(time.time()) - 30))).email == "roni@edrlab.org"


async def test_an_unsigned_token_is_refused() -> None:
    header = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0"  # {"alg":"none","typ":"JWT"}
    body = (
        base64.urlsafe_b64encode(
            b'{"iss":"%s","aud":"%s","exp":9999999999,"email":"a@edrlab.org"}'
            % (ISSUER.encode(), AUD.encode())
        )
        .rstrip(b"=")
        .decode()
    )

    with pytest.raises(AuthenticationError):
        await verify(f"{header}.{body}.")


async def test_a_symmetric_algorithm_is_refused() -> None:
    """HS256 signed with the public key as the secret is the classic algorithm confusion."""
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUD, "exp": int(time.time()) + 300, "email": "a@edrlab.org"},
        "shared-secret-shared-secret-1234",
        algorithm="HS256",
    )

    with pytest.raises(AuthenticationError):
        await verify(token)


async def test_an_email_outside_the_allowed_domains_is_a_403() -> None:
    with pytest.raises(PermissionDeniedError):
        await verify(sign(email="someone@gmail.com"))


async def test_a_look_alike_domain_is_refused() -> None:
    with pytest.raises(PermissionDeniedError):
        await verify(sign(email="x@edrlab.org.evil.example"))
    with pytest.raises(PermissionDeniedError):
        await verify(sign(email="x@notedrlab.org"))


def test_the_dev_identity_skips_the_domain_check_but_is_lowercased() -> None:
    assert build_identity("Dev@Example.com", allowed_domains=None, issuer="dev").email == (
        "dev@example.com"
    )


@pytest.mark.parametrize("email", ["nobody", "@edrlab.org", "roni@", ""])
def test_an_identity_that_is_not_an_email_address_is_a_401(email: str) -> None:
    with pytest.raises(AuthenticationError):
        build_identity(email, allowed_domains=None, issuer="dev")
