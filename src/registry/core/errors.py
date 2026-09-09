"""The one exception hierarchy.

Layers below ``api`` never raise ``HTTPException``: a service that does cannot be reused
from the CLI, a worker, or a test. ``api/exception_handlers.py`` maps these to responses.

Only the errors something actually raises live here.
"""


class RegistryError(Exception):
    """Base for every error this application raises deliberately."""

    status_code = 500
    title = "Internal Server Error"


class NotFoundError(RegistryError):
    status_code = 404
    title = "Not Found"


class ValidationError(RegistryError):
    status_code = 422
    title = "Unprocessable Entity"
