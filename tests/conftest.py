"""Shared wiring for the end-to-end tests.

The whole server is exercised against an in-memory fetcher. That is not only about speed:
03 requires the same facts to produce the same bytes, and a test that reached the real
registries could not tell a regression from an upstream edit.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from dependency_compat_mcp.adapters.npm import NpmAdapter
from dependency_compat_mcp.adapters.pypi import PyPIAdapter
from dependency_compat_mcp.adapters.runtimes import RuntimeReleaseAdapter
from dependency_compat_mcp.curated.loader import (
    CuratedPack,
    load_curated_pack,
    load_runtime_releases,
)
from dependency_compat_mcp.infra.http import (
    DEFAULT_REQUEST_BUDGET,
    HttpFailed,
    HttpNotFound,
    HttpOk,
    HttpResult,
)
from dependency_compat_mcp.service import CompatibilityService

FIXED_NOW: Final = datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC)


@dataclass
class FakeFetcher:
    """A :class:`JsonFetcher` backed by a dict of canned responses.

    An unknown URL is a 404 rather than an error, because that is what the registries do
    for a release that does not exist, and telling the two apart is the point of
    ``release_not_found`` vs ``lookup_failed``.
    """

    payloads: Mapping[str, object] = field(default_factory=dict)
    failures: Mapping[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    now: datetime = FIXED_NOW

    async def get_json(self, url: str) -> HttpResult:
        self.calls.append(url)
        if url in self.failures:
            return HttpFailed(url=url, detail=self.failures[url])
        if url in self.payloads:
            return HttpOk(url=url, payload=self.payloads[url], retrieved_at=self.now)
        return HttpNotFound(url=url)

    async def aclose(self) -> None:
        return None


def pypi_release(
    name: str,
    version: str,
    *,
    requires_python: str | None = None,
    requires_dist: list[str] | None = None,
    classifiers: list[str] | None = None,
    uploaded: str = "2024-04-08T00:00:00.000000Z",
    yanked: bool = False,
    yanked_reason: str | None = None,
) -> dict[str, Any]:
    """The shape of ``https://pypi.org/pypi/{name}/{version}/json`` we actually read."""
    return {
        "info": {
            "name": name,
            "version": version,
            "requires_python": requires_python,
            "requires_dist": requires_dist,
            "classifiers": classifiers or [],
            "yanked": yanked,
            "yanked_reason": yanked_reason,
        },
        "urls": [
            {
                "upload_time_iso_8601": uploaded,
                "yanked": yanked,
                "yanked_reason": yanked_reason,
            }
        ],
    }


def npm_packument(
    name: str,
    version: str,
    *,
    engines: dict[str, str] | None = None,
    dependencies: dict[str, str] | None = None,
    peer_dependencies: dict[str, str] | None = None,
    released: str = "2025-06-01T00:00:00.000Z",
) -> dict[str, Any]:
    """The shape of ``https://registry.npmjs.org/{name}`` we actually read."""
    entry: dict[str, Any] = {"name": name, "version": version}
    if engines is not None:
        entry["engines"] = engines
    if dependencies is not None:
        entry["dependencies"] = dependencies
    if peer_dependencies is not None:
        entry["peerDependencies"] = peer_dependencies
    return {
        "name": name,
        "versions": {version: entry},
        "time": {version: released},
    }


def pypi_url(name: str, version: str) -> str:
    return f"https://pypi.org/pypi/{name}/{version}/json"


def npm_url(name: str) -> str:
    return f"https://registry.npmjs.org/{name.replace('@', '@').replace('/', '%2F')}"


def build_service(
    fetcher: FakeFetcher,
    *,
    pack: CuratedPack | None = None,
    request_budget_seconds: float = DEFAULT_REQUEST_BUDGET,
) -> CompatibilityService:
    return CompatibilityService(
        pypi=PyPIAdapter(fetcher=fetcher),
        npm=NpmAdapter(fetcher=fetcher),
        runtimes=RuntimeReleaseAdapter(table=load_runtime_releases()),
        pack=pack if pack is not None else load_curated_pack(),
        request_budget_seconds=request_budget_seconds,
    )


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def shipped_pack() -> CuratedPack:
    """The pack that actually ships. It is empty on purpose - see the pack README."""
    return load_curated_pack()


@pytest.fixture
def curated_fixture_pack() -> CuratedPack:
    """A real curated pack with statements and changes, for the paths the shipped pack cannot reach."""
    return load_curated_pack(Path(__file__).parent / "fixtures" / "packs" / "valid")
