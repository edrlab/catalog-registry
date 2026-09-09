"""The response class every OPDS endpoint uses.

A plain `JSONResponse` returned from a handler bypasses `response_model` entirely. FastAPI
passes a `Response` through untouched, so the endpoints return dicts and name this as their
`response_class` instead. That keeps the OPDS media type *and* gets the response model into
the OpenAPI document.
"""

from fastapi.responses import JSONResponse

from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE


class OPDSResponse(JSONResponse):
    media_type = OPDS_CATALOG_MEDIA_TYPE
