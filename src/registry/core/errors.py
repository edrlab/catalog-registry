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


class CatalogNotFoundError(NotFoundError):
    """No catalog with this id. Its own type, because a plain 404 is also what an unknown route
    answers, and a caller must be able to tell "the catalog is gone" from "wrong address"."""

    problem_type = "https://registry.thoriumreader.com/problems/catalog-not-found"


class CountryNotFoundError(NotFoundError):
    """No active or historical country with this ISO 3166-1 alpha-2 code. Its own type for the
    same reason as `CatalogNotFoundError`."""

    problem_type = "https://registry.thoriumreader.com/problems/country-not-found"


class ConflictError(RegistryError):
    """The request is well formed but cannot apply to the catalog as it is now. Each case has its
    own `type` (admin API contract §2), so a caller can tell them apart."""

    status_code = 409
    title = "Conflict"


class VersionConflictError(ConflictError):
    """The caller saw an older version: someone changed the catalog since."""

    problem_type = "https://registry.thoriumreader.com/problems/version-conflict"


class InvalidTransitionError(ConflictError):
    """The lifecycle does not allow this action from the catalog's status."""

    problem_type = "https://registry.thoriumreader.com/problems/invalid-transition"


class NotPublishedError(ConflictError):
    """Recommendation changes only on a published catalog (W21)."""

    problem_type = "https://registry.thoriumreader.com/problems/not-published"


class NotDeletableError(ConflictError):
    """Only a draft can be deleted; anything a reader may have seen is archived instead."""

    problem_type = "https://registry.thoriumreader.com/problems/not-deletable"


class ArchivedReadOnlyError(ConflictError):
    """An archived catalog cannot be edited until it is unarchived (W20)."""

    problem_type = "https://registry.thoriumreader.com/problems/archived-read-only"


class DuplicateCatalogError(ConflictError):
    """Another catalog already has this `catalog`/`shelf` href, or the id derived from it: one
    identity href is one catalog (`uq_links_identity_href`, ADR-038)."""

    problem_type = "https://registry.thoriumreader.com/problems/duplicate-catalog"


class ValidationError(RegistryError):
    status_code = 422
    title = "Unprocessable Entity"


class FieldValidationError(ValidationError):
    """Invalid input, field by field, as the admin API's 422 `errors` (contract §2)."""

    title = "Validation failed"
    problem_type = "https://registry.thoriumreader.com/problems/validation"

    def __init__(self, errors: list[dict[str, str]]) -> None:
        plural = len(errors) != 1
        super().__init__(f"{len(errors)} fields are invalid" if plural else "1 field is invalid")
        self.errors = errors


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
