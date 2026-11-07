"""Which URLs the registry may request. Pure: no DNS, no network.

The address a name resolves to is checked when connecting (`public_backend`), because only that
check cannot be raced. This module refuses what is wrong before any lookup: the scheme, credentials
in the URL, odd ports, and address literals that are not public or not written canonically.
"""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from registry.domain.fetching import FetchError

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}
#: Well-known ports other than the web's are services, not feeds (SSH, SMTP, databases' defaults
#: are above 1024 but still need a public address, which `public_backend` enforces).
MIN_HIGH_PORT = 1024
MAX_URL_LENGTH = 2048

#: IPv6 prefixes that carry an IPv4 address the check must look through.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
#: RFC 4291 §2.4: global unicast. Everything outside it (the deprecated IPv4-compatible `::a.b.c.d`,
#: site-local `fec0::/10`, unassigned space) is refused even where `is_global` would say otherwise.
_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")
#: The forms `inet_aton` reads as IPv4 that `ipaddress` does not: `2130706433`, `0177.0.0.1`,
#: `0x7f.1`. A resolver would turn them into an address the canonical check never saw.
_LEGACY_IPV4 = re.compile(r"(?:0x[0-9a-f]*|\d+)(?:\.(?:0x[0-9a-f]*|\d+)){0,3}", re.IGNORECASE)
_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")


@dataclass(frozen=True, slots=True)
class OutboundUrl:
    url: str
    scheme: str
    #: Lowercased; IDNA (punycode) for international names; no brackets for IPv6.
    host: str
    port: int


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable, looking through IPv6 forms that embed an IPv4 address."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is None and address in _NAT64:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_address(embedded)
        return address in _GLOBAL_UNICAST and address.is_global
    return address.is_global and not address.is_multicast


def _unsafe(url: str, detail: str) -> FetchError:
    return FetchError("url-unsafe", detail, url=url)


def _invalid(url: str, detail: str) -> FetchError:
    return FetchError("url-invalid", detail, url=url)


def validate_outbound_url(url: str) -> OutboundUrl:
    """*url* if the registry may request it. Raises `FetchError` (`url-invalid` or `url-unsafe`)."""
    if not url or len(url) > MAX_URL_LENGTH:
        raise _invalid(url, f"The address must be 1 to {MAX_URL_LENGTH} characters.")
    if _CONTROL_OR_SPACE.search(url):
        raise _invalid(url, "The address contains spaces or control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _invalid(url, "The address is not a valid URL.") from None
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise _unsafe(url, "Only http and https addresses can be checked.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _unsafe(url, "Addresses with a user name or password are not accepted.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise _invalid(url, "The address has no host name.")
    port = port if port is not None else DEFAULT_PORTS[scheme]
    if port not in DEFAULT_PORTS.values() and not MIN_HIGH_PORT <= port <= 65535:  # noqa: PLR2004
        raise _unsafe(url, f"Port {port} is not allowed; use 80, 443 or a port above 1023.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not is_public_address(address):
            raise _unsafe(url, "The address points to a private or reserved network.")
    elif _LEGACY_IPV4.fullmatch(host):
        raise _unsafe(url, "Numeric addresses must be written as a plain IPv4 or IPv6 address.")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise _invalid(url, "The host name is not valid.") from None
    return OutboundUrl(url=url, scheme=scheme, host=host, port=port)
