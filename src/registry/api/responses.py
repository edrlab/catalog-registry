"""The response class every OPDS endpoint uses.

The endpoints return the rendered document wrapped in this class. FastAPI passes a `Response`
through untouched, so the document is not validated and dumped a second time through
`response_model`: measured at about 8 ms of 19 ms for a 1000-catalog feed. The endpoints still
name `response_model`, which is what puts the schema in the OpenAPI document, and the e2e tests
check the body equals the model's own dump, so the two cannot drift.

Each endpoint also answers HEAD (RFC 9110 §9.3.2) through a second route with
`include_in_schema=False`, so the OpenAPI document keeps one operation per path.
"""

from fastapi.responses import JSONResponse

from registry.core.constants import OPDS_CATALOG_MEDIA_TYPE


class OPDSResponse(JSONResponse):
    media_type = OPDS_CATALOG_MEDIA_TYPE
