"""The dependency direction of 03 is a rule, so it is checked, not just documented.

``server -> contracts -> domain <- adapters``. 03 says to enforce this with an import
linter in CI. Adding an import linter would mean adding a dependency, which 01 forbids, so
the same rule is enforced here by reading the import statements directly - the check is
mechanical either way, and this version runs in the existing test job.

The rule matters for one reason above all: it is the structural half of "no model call on
the request path". If ``domain`` cannot import ``adapters`` or ``infra``, and neither of
those imports a model client, then no amount of future editing inside ``domain`` can
introduce one without the import first showing up here.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "dependency_compat_mcp"
PACKAGE = "dependency_compat_mcp"

# What each layer may import from inside this package. `contracts` is allowed to see
# `domain` because its whole job is to project domain values onto the public schema.
ALLOWED: dict[str, frozenset[str]] = {
    "domain": frozenset({"domain"}),
    "adapters": frozenset({"domain", "infra", "adapters"}),
    # `infra` may reach inward for the shared failure vocabulary (`InvariantViolation`);
    # what matters is that nothing reaches outward *from* `domain`.
    "infra": frozenset({"infra", "domain"}),
    "contracts": frozenset({"domain", "contracts"}),
}

# Third-party packages a layer may not touch. `domain` must stay free of transport,
# network and MCP concerns so the decision procedure is testable without any of them.
FORBIDDEN_EXTERNAL: dict[str, frozenset[str]] = {
    "domain": frozenset({"mcp", "httpx2", "httpx", "starlette", "anyio", "pydantic"}),
    "infra": frozenset({"mcp"}),
    "adapters": frozenset({"mcp", "starlette"}),
}


def _python_files() -> Iterator[Path]:
    yield from sorted(PACKAGE_ROOT.rglob("*.py"))


def _layer_of(path: Path) -> str | None:
    relative = path.relative_to(PACKAGE_ROOT)
    return relative.parts[0] if len(relative.parts) > 1 else None


def _imported_modules(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                for alias in names:
                    yield alias.name
            case ast.ImportFrom(module=module, level=0) if module:
                yield module
            case ast.ImportFrom(level=level) if level > 0:  # pragma: no cover
                pytest.fail(f"{path}: relative imports are not used in this package")


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: p.name)
def test_layer_only_imports_permitted_layers(path: Path) -> None:
    layer = _layer_of(path)
    if layer is None or layer not in ALLOWED:
        return
    allowed = ALLOWED[layer]
    for module in _imported_modules(path):
        if not module.startswith(f"{PACKAGE}."):
            continue
        target_layer = module.split(".")[1]
        if target_layer in ALLOWED and target_layer not in allowed:
            pytest.fail(
                f"{path.relative_to(PACKAGE_ROOT)} imports {module}: "
                f"layer {layer!r} may only import {sorted(allowed)}"
            )


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: p.name)
def test_layer_avoids_forbidden_external_packages(path: Path) -> None:
    layer = _layer_of(path)
    if layer is None or layer not in FORBIDDEN_EXTERNAL:
        return
    forbidden = FORBIDDEN_EXTERNAL[layer]
    for module in _imported_modules(path):
        root = module.split(".")[0]
        if root in forbidden:
            pytest.fail(
                f"{path.relative_to(PACKAGE_ROOT)} imports {module}: "
                f"layer {layer!r} must not depend on {root!r}"
            )


def test_no_model_client_anywhere_on_the_request_path() -> None:
    """03's strongest guarantee: nothing in this package talks to a language model.

    A structural check rather than a promise. If someone adds an SDK, the import appears
    here first, and the reason the whole evidence model is trustworthy stops holding.
    """
    banned = {
        "anthropic",
        "openai",
        "google",
        "cohere",
        "mistralai",
        "ollama",
        "langchain",
    }
    offenders: list[str] = []
    for path in _python_files():
        for module in _imported_modules(path):
            if module.split(".")[0] in banned:
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {module}")
    assert offenders == [], f"model client imports found: {offenders}"
