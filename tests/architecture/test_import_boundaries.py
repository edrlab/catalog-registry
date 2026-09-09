"""Layer boundaries, enforced (`conventions/testing.md` §6).

It passes trivially today, which is the point: it starts passing before there is anything to
violate. Layering rules that are only written down get broken within weeks, always for a
good local reason.
"""

import ast
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
