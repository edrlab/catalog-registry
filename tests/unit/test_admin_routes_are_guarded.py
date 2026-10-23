"""Every `/admin` route must pass through `require_permission`: a new route that forgets it would
be reachable by anyone holding a key."""

import pytest
from fastapi.routing import APIRoute

from registry.api.admin_dependencies import PERMISSION_ATTRIBUTE
from registry.api.routes import admin
from registry.core.config import Settings
from registry.main import create_app

pytestmark = pytest.mark.unit


def test_every_admin_route_requires_a_permission() -> None:
    admin_routes = [route for route in admin.router.routes if isinstance(route, APIRoute)]

    def guards(dependant: object) -> list[str]:
        found = [
            getattr(sub.call, PERMISSION_ATTRIBUTE)
            for sub in getattr(dependant, "dependencies", [])
            if hasattr(sub.call, PERMISSION_ATTRIBUTE)
        ]
        for sub in getattr(dependant, "dependencies", []):
            found += guards(sub)
        return found

    assert admin_routes, "no /admin routes found: the scan is broken"
    unguarded = [route.path for route in admin_routes if not guards(route.dependant)]
    assert not unguarded, f"/admin routes without require_permission: {unguarded}"


def test_the_admin_router_is_mounted_on_the_app() -> None:
    """The scan above reads the router itself, so also prove the app serves what it scanned."""
    app = create_app(Settings(database_url="postgresql+asyncpg://u:p@localhost/db", _env_file=None))
    served = {path for path in app.openapi()["paths"] if path.startswith("/admin")}

    assert served == {route.path for route in admin.router.routes if isinstance(route, APIRoute)}
