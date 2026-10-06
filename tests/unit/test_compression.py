"""Which coding the client gets, worked out from its `Accept-Encoding` alone (ADR-061)."""

import pytest

from registry.api.compression import choose_encoding, parse_accept_encoding

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("header", "parsed"),
    [
        (None, {}),
        ("", {}),
        ("gzip", {"gzip": 1.0}),
        ("gzip, deflate, br", {"gzip": 1.0, "deflate": 1.0, "br": 1.0}),
        ("GZIP", {"gzip": 1.0}),
        ("gzip;q=0.5, br", {"gzip": 0.5, "br": 1.0}),
        ("gzip ; q = 0.8 ,br ;q=0", {"gzip": 0.8, "br": 0.0}),
        ("br;q=abc, gzip", {"gzip": 1.0}),
        ("gzip;q=7", {"gzip": 1.0}),
        ("gzip;q=-1", {"gzip": 0.0}),
        (",, ,gzip,", {"gzip": 1.0}),
        ("*;q=0.1", {"*": 0.1}),
    ],
)
def test_the_header_is_read_leniently_but_never_guessed_at(
    header: str | None, parsed: dict[str, float]
) -> None:
    assert parse_accept_encoding(header) == parsed


@pytest.mark.parametrize(
    ("header", "chosen"),
    [
        # What the real clients send (measured, docs/search.md).
        ("gzip, deflate, br", "br"),  # Thorium Reader (node-fetch), browsers
        ("gzip, deflate", "gzip"),  # URLSession, Node fetch, requests, httpx, OkHttp
        ("gzip", "gzip"),  # Android HttpURLConnection
        ("gzip, deflate, br, zstd", "br"),  # recent browsers: zstd is not offered
        ("deflate, gzip", "gzip"),  # curl --compressed
        ("gzip;q=1.0,deflate;q=0.6,identity;q=0.3", "gzip"),  # Ruby Net::HTTP
        ("identity", None),  # KOReader, Python urllib: the body as it is
        (None, None),  # curl with no flags
        ("", None),
        # What the specification says.
        ("br", "br"),
        ("gzip;q=1.0, br;q=0.5", "gzip"),  # the client's preference outranks ours
        ("gzip;q=0.5, br;q=0.5", "br"),  # a tie goes to the server's order
        ("br;q=0, gzip", "gzip"),  # q=0 refuses a coding
        ("br;q=0, gzip;q=0", None),
        ("*", "br"),  # anything goes: our first choice
        ("*;q=0", None),
        ("*;q=0.1, gzip", "gzip"),  # named beats the wildcard
        ("*, br;q=0", "gzip"),
        ("deflate, zstd, compress", None),  # only codings we do not offer
        ("x-gzip", None),
    ],
)
def test_the_coding_is_the_clients_best_choice_among_what_we_offer(
    header: str | None, chosen: str | None
) -> None:
    assert choose_encoding(header) == chosen
