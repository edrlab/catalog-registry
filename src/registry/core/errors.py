"""The one exception hierarchy.

Layers below ``api`` never raise ``HTTPException``: a service that does cannot be reused
from the CLI, a worker, or a test. ``api/exception_handlers.py`` maps these to responses.

Only the errors something actually raises live here.
"""


class RegistryError(Exception):
    """Base for every error this application raises deliberately."""

    status_code = 500
    title = "Internal Server Error"
    #: RFC 9457 `type`. `about:blank` says the status code is all there is to say.
    problem_type = "about:blank"


class NotFoundError(RegistryError):
    status_code = 404
    title = "Not Found"


class ValidationError(RegistryError):
    status_code = 422
    title = "Unprocessable Entity"


class ReadTimeoutError(RegistryError):
    """A read outran its statement timeout (ADR-058, ADR-062). The caller can simply try again."""

    status_code = 503
    title = "Service Unavailable"


class AuthenticationError(RegistryError):
    """The caller did not prove who they are: no or wrong key, no or invalid token."""

    status_code = 401
    title = "Unauthorized"
    problem_type = "https://registry.thoriumreader.com/problems/unauthorized"


class PermissionDeniedError(RegistryError):
    """The caller is known and not allowed: the wrong email domain, a deactivated actor, a role
    without the permission."""

    status_code = 403
    title = "Forbidden"
    problem_type = "https://registry.thoriumreader.com/problems/forbidden"


class AdminUnavailableError(RegistryError):
    """The admin API cannot answer safely: it is not configured, or the signing keys
    could not be fetched. It fails closed rather than letting anything through."""

    status_code = 503
    title = "Service Unavailable"
    problem_type = "https://registry.thoriumreader.com/problems/unavailable"
