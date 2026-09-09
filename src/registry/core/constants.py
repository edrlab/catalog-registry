"""Constants shared across layers. No logic, no imports from other layers."""

from typing import Final

#: the media type of every OPDS response this service emits.
OPDS_CATALOG_MEDIA_TYPE: Final = "application/opds-catalog+json"

#: RFC 9457, the media type of every error response.
PROBLEM_JSON_MEDIA_TYPE: Final = "application/problem+json"

HEADER_REQUEST_ID: Final = "X-Request-Id"
