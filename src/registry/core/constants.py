"""Constants shared across layers. No logic, no imports from other layers."""

from typing import Final

#: the media type of every OPDS response this service emits.
OPDS_CATALOG_MEDIA_TYPE: Final = "application/opds-catalog+json"

#: The media type `demo/search.json` uses for the search feed and its links. The top-level
#: feed's own links keep `OPDS_CATALOG_MEDIA_TYPE`.
OPDS_JSON_MEDIA_TYPE: Final = "application/opds+json"

#: RFC 9457, the media type of every error response.
PROBLEM_JSON_MEDIA_TYPE: Final = "application/problem+json"

HEADER_REQUEST_ID: Final = "X-Request-Id"

#: Sent on every request the registry makes to a library's server: who is asking, and
#: where to read why. Honest rather than imitating a reader app.
OUTBOUND_USER_AGENT = "ThoriumCatalogRegistry/1.0 (+https://registry.thoriumreader.com)"
