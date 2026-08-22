"""Determinism, which is how 03's "no model on the request path" decision is enforced.

The same facts must serialise to the same bytes, ``retrieved_at`` excepted. This is not a
performance nicety:

* 01 makes the responses cacheable, and a cache is meaningless if the same input can
  produce two different answers;
* a regression test can only pin behaviour that is reproducible;
* and a language model spliced into the verdict path would break this test on its first
  run, which is exactly the point. It is the executable form of the promise.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from dependency_compat_mcp.curated.loader import CuratedPack
from dependency_compat_mcp.domain.targets import parse_target
from tests.conftest import (
    FakeFetcher,
    build_service,
    npm_packument,
    npm_url,
    pypi_release,
    pypi_url,
)

PAYLOADS: dict[str, Any] = {
    pypi_url("django", "5.2"): pypi_release(
        "Django",
        "5.2",
        requires_python=">=3.10,<3.14",
        requires_dist=["asgiref>=3.8", 'tzdata; sys_platform == "win32"'],
        classifiers=[
            "Programming Language :: Python :: 3.12",
            "Programming Language :: Python :: 3.13",
        ],
    ),
    pypi_url("example-framework", "5.2"): pypi_release(
        "example-framework", "5.2", requires_python=">=3.10"
    ),
    # Two conditional declarations about one target, so the response carries more than one
    # decision cause. Their order is what fixes the summary sentence, which makes it the
    # newest thing in the response that could vary between runs.
    pypi_url("guarded-app", "1.0"): pypi_release(
        "guarded-app",
        "1.0",
        requires_dist=[
            'helper>=1.0; sys_platform == "win32"',
            'helper>=2.0; python_version < "3.11"',
        ],
    ),
    pypi_url("helper", "1.5"): pypi_release("helper", "1.5"),
    npm_url("react"): npm_packument(
        "react",
        "19.1.1",
        engines={"node": ">=18"},
        dependencies={"loose-envify": "^1.1.0"},
    ),
}

CASES = [
    (("pypi", "django", "5.2"), ("runtime", "python", "3.13.0")),
    (("runtime", "python", "3.13.0"), ("pypi", "django", "5.2")),
    (("pypi", "django", "5.2"), ("pypi", "asgiref", "3.8.1")),
    (("npm", "react", "19.1.1"), ("runtime", "node", "22.17.0")),
    (("runtime", "python", "3.13.0"), ("runtime", "node", "22.17.0")),
    (("pypi", "guarded-app", "1.0"), ("pypi", "helper", "1.5")),
]


def _strip_retrieved_at(node: Any) -> Any:
    """Drop the one field 03 excludes from result equality.

    When the server looked is not part of what it found, so it must not make two runs over
    the same facts compare unequal.
    """
    match node:
        case dict():
            return {
                key: _strip_retrieved_at(value)
                for key, value in node.items()
                if key != "retrieved_at"
            }
        case list():
            return [_strip_retrieved_at(item) for item in node]
        case _:
            return node


def _canonical(result: Any) -> str:
    return json.dumps(
        _strip_retrieved_at(result.model_dump(mode="json")),
        sort_keys=False,
        indent=None,
    )


@pytest.mark.parametrize(
    ("subject", "counterpart"), CASES, ids=lambda value: "-".join(value)
)
def test_two_runs_over_the_same_facts_produce_identical_bytes(
    subject: tuple[str, str, str], counterpart: tuple[str, str, str]
) -> None:
    async def run(now: datetime) -> str:
        # A fresh service each time, so nothing is carried over in the cache.
        service = build_service(FakeFetcher(payloads=PAYLOADS, now=now))
        return _canonical(
            await service.check_compatibility(
                parse_target(*subject), parse_target(*counterpart)
            )
        )

    first = asyncio.run(run(datetime(2026, 8, 12, tzinfo=UTC)))
    second = asyncio.run(run(datetime(2027, 1, 1, 12, 30, tzinfo=UTC)))
    assert first == second


def test_context_responses_are_byte_stable() -> None:
    async def run(now: datetime) -> str:
        service = build_service(FakeFetcher(payloads=PAYLOADS, now=now))
        return _canonical(
            await service.get_compatibility_context(
                parse_target("pypi", "django", "5.2")
            )
        )

    assert asyncio.run(run(datetime(2026, 8, 12, tzinfo=UTC))) == asyncio.run(
        run(datetime(2030, 3, 3, tzinfo=UTC))
    )


def test_only_retrieved_at_differs_between_runs() -> None:
    """Guard the guard: if nothing differed, stripping the field would prove nothing."""

    async def run(now: datetime) -> dict[str, Any]:
        service = build_service(FakeFetcher(payloads=PAYLOADS, now=now))
        result = await service.check_compatibility(
            parse_target("pypi", "django", "5.2"),
            parse_target("runtime", "python", "3.13.0"),
        )
        return result.model_dump(mode="json")

    first = asyncio.run(run(datetime(2026, 8, 12, tzinfo=UTC)))
    second = asyncio.run(run(datetime(2027, 1, 1, 12, 30, tzinfo=UTC)))
    assert first != second
    assert {e["provenance"]["retrieved_at"] for e in first["evidence"]} != {
        e["provenance"]["retrieved_at"] for e in second["evidence"]
    }


def test_evidence_order_does_not_depend_on_collection_order() -> None:
    """The catalogue is sorted by ``(tier, source_type, url)``, not by arrival."""

    async def ordered(payloads: dict[str, Any]) -> list[str]:
        service = build_service(FakeFetcher(payloads=payloads))
        result = await service.check_compatibility(
            parse_target("pypi", "django", "5.2"),
            parse_target("runtime", "python", "3.13.0"),
        )
        dumped = result.model_dump(mode="json")
        return [item["source_type"] for item in dumped["evidence"]]

    forward = asyncio.run(ordered(dict(PAYLOADS)))
    reversed_payloads = dict(reversed(list(PAYLOADS.items())))
    assert forward == asyncio.run(ordered(reversed_payloads))

    # Tier A before tier C, which is the documented sort key showing through.
    def tier_a_first(source_type: str) -> bool:
        return source_type != "registry_metadata"

    assert forward == sorted(forward, key=tier_a_first)


def test_a_pack_change_changes_the_answer_and_the_cache_key(
    curated_fixture_pack: CuratedPack,
) -> None:
    """`pack_version` is inside the cache key because a pack edit really is new evidence."""

    async def run(pack: CuratedPack | None) -> dict[str, Any]:
        service = build_service(FakeFetcher(payloads=PAYLOADS), pack=pack)
        result = await service.get_compatibility_context(
            parse_target("pypi", "example-framework", "5.2")
        )
        return result.model_dump(mode="json")

    without = asyncio.run(run(None))
    with_pack = asyncio.run(run(curated_fixture_pack))

    assert without["depth"] == "registry_only"
    assert with_pack["depth"] == "registry_and_curated"
    assert any(item["provenance"].get("pack_version") for item in with_pack["evidence"])
