"""Layer boundaries, enforced

It passes trivially today, which is the point: it starts passing before there is anything to
violate. Layering rules that are only written down get broken within weeks, always for a
good local reason.
"""

import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "registry"

FORBIDDEN = {
    "registry.domain": {
        "sqlalchemy",
        "fastapi",
        "jsonschema",
        "registry.api",
        "registry.rendering",
        "registry.schemas",
        "registry.repositories",
        "registry.services",
    },
    "registry.services": {"sqlalchemy", "fastapi", "registry.api"},
    "registry.api": {"sqlalchemy", "registry.repositories"},
    "registry.rendering": {"sqlalchemy", "fastapi"},
}


def _module_name(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT.parent).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    return imported


def _violates(imported: str, forbidden: str) -> bool:
    return imported == forbidden or imported.startswith(f"{forbidden}.")


def test_no_module_imports_across_a_forbidden_boundary() -> None:
    violations: list[str] = []

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        module = _module_name(path)
        for layer, forbidden_imports in FORBIDDEN.items():
            if not (module == layer or module.startswith(f"{layer}.")):
                continue
            for imported in _imported_modules(path):
                for forbidden in forbidden_imports:
                    if _violates(imported, forbidden):
                        violations.append(f"{module} imports {imported}")

    assert not violations, "forbidden imports: " + ", ".join(sorted(violations))


def _imports_of(relative: str) -> set[str]:
    return _imported_modules(SOURCE_ROOT / relative)


def test_the_search_query_parser_imports_only_the_standard_library() -> None:
    """`domain/search_query.py` is pure (ADR-059): no framework, no database, no registry."""
    imported = _imports_of("domain/search_query.py")

    assert imported, "the parser imports nothing at all? the scan is broken"
    outside = {name for name in imported if name.split(".")[0] not in sys.stdlib_module_names}
    assert not outside, f"non-stdlib imports in domain/search_query.py: {sorted(outside)}"


def test_the_search_route_reaches_the_database_only_through_app_state() -> None:
    imported = _imports_of("api/routes/search.py")

    assert not {m for m in imported if _violates(m, "sqlalchemy")}
    assert not {m for m in imported if _violates(m, "registry.repositories")}
    assert not {m for m in imported if _violates(m, "registry.db")}
    assert not {m for m in imported if _violates(m, "asyncpg")}


@pytest.mark.parametrize(
    "relative",
    [
        "services/search_service.py",
        "rendering/search_renderer.py",
        "domain/search_query.py",
        "api/routes/search.py",
    ],
)
def test_the_search_modules_above_the_repository_import_no_sqlalchemy_or_asyncpg(
    relative: str,
) -> None:
    imported = _imports_of(relative)

    assert not {m for m in imported if _violates(m, "sqlalchemy") or _violates(m, "asyncpg")}


def test_only_the_composition_root_imports_the_search_repository() -> None:
    importers = [
        _module_name(path)
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if any(
            _violates(m, "registry.repositories.search_repository") for m in _imported_modules(path)
        )
    ]

    assert importers == ["registry.main"]


def test_the_search_service_depends_on_the_protocol_not_the_repository() -> None:
    imported = _imports_of("services/search_service.py")

    assert "registry.repositories.protocols" in imported
    assert not {m for m in imported if _violates(m, "registry.repositories.search_repository")}


def test_the_search_renderer_imports_no_database_models() -> None:
    """It renders a `SearchPage` through `render_catalog`; it never builds its own query."""
    imported = _imports_of("rendering/search_renderer.py")

    assert not {m for m in imported if _violates(m, "registry.db")}
