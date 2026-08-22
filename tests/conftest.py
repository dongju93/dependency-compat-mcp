"""Shared wiring for the end-to-end tests.

The whole server is exercised against an in-memory fetcher. That is not only about speed:
03 requires the same facts to produce the same bytes, and a test that reached the real
registries - or the real python.org and nodejs.org - could not tell a regression from an
upstream edit.

Every source is now fetched, runtimes included, so the fake has to answer four more URLs
than it used to. :data:`RUNTIME_DOCUMENTS` holds a small, fixed stand-in for each of them
and :class:`FakeFetcher` falls back to it. That fallback is what the committed runtime
snapshot used to be: a test that does not care which Python releases exist gets a
believable set, and a test that *does* care overrides the URL - with a payload of its own,
or by naming it in ``failures``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import pytest

from dependency_compat_mcp.adapters.npm import NpmAdapter
from dependency_compat_mcp.adapters.pypi import PyPIAdapter
from dependency_compat_mcp.adapters.runtimes import (
    NODE_RELEASE_INDEX_URL,
    NODE_RELEASE_SCHEDULE_URL,
    PYTHON_RELEASE_CYCLE_URL,
    PYTHON_RELEASE_INDEX_URL,
    RuntimeReleaseAdapter,
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


def python_release_index(releases: Mapping[str, str]) -> list[dict[str, Any]]:
    """The shape of ``https://www.python.org/api/v2/downloads/release/`` we read."""
    return [
        {
            "name": f"Python {version}",
            "is_published": True,
            "release_date": f"{released}T00:00:00Z",
        }
        for version, released in releases.items()
    ]


def python_release_cycle(lines: Mapping[str, str | None]) -> dict[str, Any]:
    """The shape of ``https://peps.python.org/api/release-cycle.json`` we read."""
    return {line: {"end_of_life": eol} for line, eol in lines.items()}


def node_release_index(releases: Mapping[str, str]) -> list[dict[str, Any]]:
    """The shape of ``https://nodejs.org/dist/index.json`` we read."""
    return [
        {"version": f"v{version}", "date": released}
        for version, released in releases.items()
    ]


def node_release_schedule(lines: Mapping[str, str | None]) -> dict[str, Any]:
    """The shape of ``nodejs/Release``'s ``schedule.json`` we read."""
    return {line: {"end": end} for line, end in lines.items()}


# A believable stand-in for the four official documents. Deliberately small: every version
# a test names has to be visible here, so "why is this release_not_found" is answerable by
# reading one table. `3.11` and `3.14` carry month-precision end-of-life dates exactly as
# upstream publishes them for lines that have not reached it yet.
RUNTIME_DOCUMENTS: Final[Mapping[str, object]] = {
    PYTHON_RELEASE_INDEX_URL: python_release_index(
        {
            "3.8.0": "2019-10-14",
            "3.11.0": "2022-10-24",
            "3.12.0": "2023-10-02",
            "3.13.0": "2024-10-07",
            "3.13.1": "2024-12-03",
            "3.14.0": "2025-10-07",
        }
    ),
    PYTHON_RELEASE_CYCLE_URL: python_release_cycle(
        {
            "3.8": "2024-10-07",
            "3.11": "2027-10",
            "3.12": "2028-10",
            "3.13": "2029-10",
            "3.14": "2030-10",
        }
    ),
    NODE_RELEASE_INDEX_URL: node_release_index(
        {"18.0.0": "2022-04-19", "20.0.0": "2023-04-17", "22.17.0": "2025-06-24"}
    ),
    NODE_RELEASE_SCHEDULE_URL: node_release_schedule(
        {"v18": "2025-04-30", "v20": "2026-04-30", "v22": "2027-04-30"}
    ),
}


@dataclass
class FakeFetcher:
    """A :class:`JsonFetcher` backed by a dict of canned responses.

    Resolution order is ``failures`` -> ``payloads`` -> :data:`RUNTIME_DOCUMENTS` -> 404.
    An unknown URL is a 404 rather than an error, because that is what the registries do
    for a release that does not exist, and telling the two apart is the point of
    ``release_not_found`` vs ``lookup_failed``.
    """

    payloads: Mapping[str, object] = field(default_factory=dict)
    failures: Mapping[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    now: datetime = FIXED_NOW
    serve_runtime_documents: bool = True

    async def get_json(self, url: str) -> HttpResult:
        self.calls.append(url)
        if url in self.failures:
            return HttpFailed(url=url, detail=self.failures[url])
        if url in self.payloads:
            return HttpOk(url=url, payload=self.payloads[url], retrieved_at=self.now)
        if self.serve_runtime_documents and url in RUNTIME_DOCUMENTS:
            return HttpOk(
                url=url, payload=RUNTIME_DOCUMENTS[url], retrieved_at=self.now
            )
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
    request_budget_seconds: float = DEFAULT_REQUEST_BUDGET,
) -> CompatibilityService:
    return CompatibilityService(
        pypi=PyPIAdapter(fetcher=fetcher),
        npm=NpmAdapter(fetcher=fetcher),
        runtimes=RuntimeReleaseAdapter(fetcher=fetcher),
        request_budget_seconds=request_budget_seconds,
    )


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()
