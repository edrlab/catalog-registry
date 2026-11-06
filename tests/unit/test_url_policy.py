"""`validate_outbound_url`: what the registry refuses to request before any lookup."""

import ipaddress

import pytest

from registry.domain.fetching import FetchError
from registry.domain.url_policy import is_public_address, validate_outbound_url


@pytest.mark.parametrize(
    ("url", "host", "port"),
    [
        ("https://example.org/opds", "example.org", 443),
        ("http://example.org/opds", "example.org", 80),
        ("HTTPS://Example.ORG/Feed", "example.org", 443),
        ("https://example.org:8443/opds", "example.org", 8443),
        ("https://example.org:1024/", "example.org", 1024),
        ("https://bücher.de/opds", "xn--bcher-kva.de", 443),
        ("https://123.example.org/", "123.example.org", 443),
        ("https://8.8.8.8/opds", "8.8.8.8", 443),
        ("https://[2001:4860:4860::8888]/", "2001:4860:4860::8888", 443),
        ("https://example.org./opds", "example.org", 443),
    ],
)
def test_a_public_web_address_is_allowed(url: str, host: str, port: int) -> None:
    allowed = validate_outbound_url(url)

    assert (allowed.host, allowed.port) == (host, port)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/feed",
        "file:///etc/passwd",
        "gopher://example.org/",
        "javascript:alert(1)",
        "data:application/json,{}",
        "https://user@example.org/",
        "https://user:secret@example.org/",
        "https://example.org@169.254.169.254/",
        "https://example.org:22/",
        "https://example.org:25/",
        "https://example.org:0/",
        "https://example.org:1023/",
        "http://127.0.0.1/",
        "http://127.0.0.1:8000/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://255.255.255.255/",
        "http://224.0.0.1/",
        "http://[::1]/",
        "http://[::]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f.0.0.1/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://017700000001/",
    ],
)
def test_an_unsafe_address_is_refused_before_any_lookup(url: str) -> None:
    with pytest.raises(FetchError) as refused:
        validate_outbound_url(url)

    assert refused.value.code == "url-unsafe"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.org/opds",
        "https://",
        "https:///path",
        "https://exa mple.org/",
        "https://example.org/a b",
        "https://example.org/\x00",
        "https://example.org/\n",
        " https://example.org/",
        "https://example.org:99999/",
        "https://example.org:abc/",
        "https://[::1/",
        "https://example.org/" + "a" * 2048,
    ],
)
def test_a_malformed_address_is_invalid(url: str) -> None:
    with pytest.raises(FetchError) as refused:
        validate_outbound_url(url)

    assert refused.value.code in {"url-invalid", "url-unsafe"}
    assert refused.value.detail


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2001:4860:4860::8888", True),
        ("127.0.0.1", False),
        ("::ffff:8.8.8.8", True),
        ("::ffff:10.0.0.1", False),
        ("64:ff9b::808:808", True),
        ("64:ff9b::a00:1", False),
        ("2002:0a00:0001::", False),
        ("ff02::1", False),
    ],
)
def test_embedded_ipv4_addresses_are_looked_through(address: str, public: bool) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is public
