"""The runtime release adapter: what it reads, and what it refuses to conclude.

The property this file used to test - "the committed table is complete" - no longer exists,
because there is no committed table. What replaces it is the property that made the table
worth removing: **a release the repository has never heard of is recognised as soon as its
publisher lists it**, and the harder one that comes with fetching at request time -
*a source that could not be read is never reported as a source that answered.*

Every failure mode of a fetched document is exercised here: a 404 on the index, a transport
failure, a body that is not the documented shape, an empty document, a timeout, and
cancellation. None of them may produce :class:`RuntimeReleaseAbsent`, because that value is
what the decision procedure turns into the factual claim ``release_not_found``.

Tests are synchronous and drive the coroutines with :func:`asyncio.run`, as the rest of this
suite does; there is no async plugin, so an ``async def`` test would never actually run.

No test in this file touches the network.
"""

import asyncio
from collections.abc import Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from dependency_compat_mcp.adapters.runtimes import (
    NODE_RELEASE_INDEX_URL,
    NODE_RELEASE_SCHEDULE_URL,
    PYTHON_RELEASE_CYCLE_URL,
    PYTHON_RELEASE_INDEX_URL,
    RuntimeIndex,
    RuntimeIndexLookup,
    RuntimeLifecycle,
    RuntimeLifecycleLookup,
    RuntimeReleaseAbsent,
    RuntimeReleaseAdapter,
    RuntimeReleaseFound,
    RuntimeReleaseUnavailable,
    RuntimeSourceFailed,
    index_check,
    index_source_id,
    lifecycle_check,
    lifecycle_source_id,
    select_eol,
    select_release,
)
from dependency_compat_mcp.domain.claims import (
    EolNotApplicable,
    EolPublished,
    EolUnavailable,
    EolUnpublished,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.targets import (
    NodeRuntimeTarget,
    NpmTarget,
    PyPITarget,
    PythonRuntimeTarget,
    Target,
    parse_target,
)
from dependency_compat_mcp.infra.http import ALLOWED_HOSTS, HttpResult
from tests.conftest import (
    FakeFetcher,
    node_release_index,
    node_release_schedule,
    python_release_cycle,
    python_release_index,
)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def _python(version: str) -> PythonRuntimeTarget:
    target = parse_target("runtime", "python", version)
    assert isinstance(target, PythonRuntimeTarget)
    return target


def _node(version: str) -> NodeRuntimeTarget:
    target = parse_target("runtime", "node", version)
    assert isinstance(target, NodeRuntimeTarget)
    return target


def _pypi(name: str, version: str) -> PyPITarget:
    target = parse_target("pypi", name, version)
    assert isinstance(target, PyPITarget)
    return target


def _npm(name: str, version: str) -> NpmTarget:
    target = parse_target("npm", name, version)
    assert isinstance(target, NpmTarget)
    return target


def _adapter(
    payloads: Mapping[str, object] | None = None,
    failures: Mapping[str, str] | None = None,
) -> RuntimeReleaseAdapter:
    return RuntimeReleaseAdapter(
        fetcher=FakeFetcher(
            payloads=dict(payloads or {}), failures=dict(failures or {})
        )
    )


def _index(
    target: Target,
    payloads: Mapping[str, object] | None = None,
    failures: Mapping[str, str] | None = None,
) -> RuntimeIndexLookup:
    return _run(_adapter(payloads, failures).fetch_index(target))


def _lifecycle(
    target: Target,
    payloads: Mapping[str, object] | None = None,
    failures: Mapping[str, str] | None = None,
) -> RuntimeLifecycleLookup:
    return _run(_adapter(payloads, failures).fetch_lifecycle(target))


# --------------------------------------------------------------------------------------
# The request path reaches only allow-listed official hosts
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        PYTHON_RELEASE_INDEX_URL,
        PYTHON_RELEASE_CYCLE_URL,
        NODE_RELEASE_INDEX_URL,
        NODE_RELEASE_SCHEDULE_URL,
    ],
)
def test_every_runtime_source_is_https_on_an_allow_listed_host(url: str) -> None:
    scheme, _, rest = url.partition("://")
    assert scheme == "https"
    assert rest.split("/", 1)[0] in ALLOWED_HOSTS


def test_the_python_index_url_keeps_its_trailing_slash() -> None:
    """python.org answers the slash-less form with a 301; the hop buys nothing."""
    assert PYTHON_RELEASE_INDEX_URL.endswith("/downloads/release/")


# --------------------------------------------------------------------------------------
# Normal responses
# --------------------------------------------------------------------------------------


def test_a_release_the_repository_never_shipped_is_recognised() -> None:
    """The property the committed snapshot could not have: no repository edit is needed.

    ``3.99.0`` is not a version any fixture in this suite knows about; the only thing that
    makes it exist is that the official index lists it.
    """
    target = _python("3.99.0")
    payloads = {
        PYTHON_RELEASE_INDEX_URL: python_release_index({"3.99.0": "2031-10-07"})
    }

    assert select_release(_index(target, payloads), target) == RuntimeReleaseFound(
        version=target.version, released_at=datetime(2031, 10, 7, tzinfo=UTC)
    )


def test_a_node_release_is_read_without_the_upstream_v_prefix() -> None:
    target = _node("24.1.0")
    payloads = {NODE_RELEASE_INDEX_URL: node_release_index({"24.1.0": "2026-05-06"})}

    found = select_release(_index(target, payloads), target)

    assert isinstance(found, RuntimeReleaseFound)
    assert str(found.version) == "24.1.0"
    assert found.released_at == datetime(2026, 5, 6, tzinfo=UTC)


def test_a_version_missing_from_a_readable_index_is_absent_not_failed() -> None:
    target = _python("3.13.99")
    payloads = {
        PYTHON_RELEASE_INDEX_URL: python_release_index({"3.13.0": "2024-10-07"})
    }

    found = select_release(_index(target, payloads), target)

    assert found == RuntimeReleaseAbsent(target=target)
    check = index_check(target, found, role="declared_about")
    assert (check.outcome, check.required) == ("not_found", True)


# --------------------------------------------------------------------------------------
# Parsing rules the generator used to enforce, now enforced per request
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(
            {"name": "Python install manager 1.0", "release_date": "2025-05-01"},
            id="separate_product",
        ),
        pytest.param(
            {"name": "Python 3.13.0RC1", "release_date": "2024-10-07"},
            id="not_canonical_pep440",
        ),
        pytest.param(
            {"name": "Python latest", "release_date": "2024-10-07"},
            id="not_a_version",
        ),
        pytest.param(
            {"name": "Python 03.13.0", "release_date": "2024-10-07"},
            id="zero_padded",
        ),
        pytest.param({"name": "Python", "release_date": "2024-10-07"}, id="no_version"),
        pytest.param(
            {"name": "Python 3.12.1", "release_date": "2023-12"},
            id="month_precision_release_date",
        ),
        pytest.param(
            {
                "name": "Python 3.15.0",
                "is_published": False,
                "release_date": "2026-10-01",
            },
            id="unpublished",
        ),
    ],
)
def test_an_unusable_index_row_is_dropped_never_rewritten(row: dict[str, Any]) -> None:
    """No silent normalisation: a spelling the server would reject as input is dropped."""
    keeper = {
        "name": "Python 3.13.0",
        "is_published": True,
        "release_date": "2024-10-07",
    }
    index = _index(_python("3.13.0"), {PYTHON_RELEASE_INDEX_URL: [row, keeper]})

    assert isinstance(index, RuntimeIndex)
    assert set(index.released_at) == {"3.13.0"}


def test_the_first_listing_of_a_duplicated_release_wins() -> None:
    """python.org has listed the same release twice; ordering must not decide the date."""
    payload = [
        {"name": "Python 3.13.0", "is_published": True, "release_date": "2024-10-07"},
        {"name": "Python 3.13.0", "is_published": True, "release_date": "2020-01-01"},
    ]
    index = _index(_python("3.13.0"), {PYTHON_RELEASE_INDEX_URL: payload})

    assert isinstance(index, RuntimeIndex)
    assert index.released_at["3.13.0"] == datetime(2024, 10, 7, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Malformed, empty and unreachable documents
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"runtimes": {}}, id="object_instead_of_array"),
        pytest.param("not json at all", id="string"),
        pytest.param([], id="empty_array"),
        pytest.param([{"name": "Python 3.13.0"}], id="no_usable_row"),
    ],
)
def test_a_malformed_or_empty_index_is_a_failure_never_an_empty_index(
    payload: object,
) -> None:
    """An empty index would say every version does not exist. That is a claim, not a gap."""
    target = _python("3.13.0")

    index = _index(target, {PYTHON_RELEASE_INDEX_URL: payload})

    assert index == RuntimeSourceFailed(detail="invalid_document")
    assert select_release(index, target) == RuntimeReleaseUnavailable(
        target=target, detail="invalid_document"
    )


def test_a_transport_failure_on_the_index_is_a_required_failed_lookup() -> None:
    target = _python("3.13.0")

    found = select_release(
        _index(target, failures={PYTHON_RELEASE_INDEX_URL: "transport_error"}), target
    )

    assert found == RuntimeReleaseUnavailable(target=target, detail="transport_error")
    check = index_check(target, found, role="declared_about")
    assert (check.outcome, check.required, check.detail) == (
        "failed",
        True,
        "transport_error",
    )


def test_a_404_on_an_official_index_is_a_failure_not_an_empty_runtime() -> None:
    """The document moving is a failure to consult it, not proof of anything."""
    adapter = RuntimeReleaseAdapter(fetcher=FakeFetcher(serve_runtime_documents=False))

    assert _run(adapter.fetch_index(_node("22.17.0"))) == RuntimeSourceFailed(
        detail="not_found"
    )


def test_an_index_lookup_that_outlives_its_budget_is_cancelled() -> None:
    """The per-attempt timeout lives in the fetcher; the adapter must not swallow it."""

    class _SlowFetcher:
        async def get_json(self, url: str) -> HttpResult:
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    adapter = RuntimeReleaseAdapter(fetcher=_SlowFetcher())

    async def _drive() -> None:
        async with asyncio.timeout(0.01):
            await adapter.fetch_index(_python("3.13.0"))

    with pytest.raises(TimeoutError):
        _run(_drive())


def test_cancellation_propagates_and_is_never_a_lookup_outcome() -> None:
    """The caller withdrawing the question is not the source failing to answer it."""

    class _HangingFetcher:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def get_json(self, url: str) -> HttpResult:
            self.started.set()
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    async def _drive() -> None:
        fetcher = _HangingFetcher()
        adapter = RuntimeReleaseAdapter(fetcher=fetcher)
        task = asyncio.ensure_future(adapter.fetch_index(_python("3.13.0")))
        await fetcher.started.wait()
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        _run(_drive())


# --------------------------------------------------------------------------------------
# Lifecycle: four states, and only one of them may be a failure
# --------------------------------------------------------------------------------------


def test_a_published_day_precision_end_of_life_is_read() -> None:
    target = _python("3.8.19")
    payloads = {PYTHON_RELEASE_CYCLE_URL: python_release_cycle({"3.8": "2024-10-07"})}

    assert select_eol(_lifecycle(target, payloads), target) == EolPublished(
        at=datetime(2024, 10, 7, tzinfo=UTC)
    )


def test_a_month_precision_end_of_life_is_unpublished_never_padded() -> None:
    """Padding ``2029-10`` to a day would manufacture the fact the staleness rule needs."""
    target = _python("3.13.1")
    payloads = {PYTHON_RELEASE_CYCLE_URL: python_release_cycle({"3.13": "2029-10"})}

    assert select_eol(_lifecycle(target, payloads), target) == EolUnpublished()


def test_a_line_the_schedule_does_not_cover_is_unpublished() -> None:
    target = _python("3.99.0")
    payloads = {PYTHON_RELEASE_CYCLE_URL: python_release_cycle({"3.13": "2029-10"})}

    assert select_eol(_lifecycle(target, payloads), target) == EolUnpublished()


def test_a_failed_schedule_is_unavailable_and_never_unpublished() -> None:
    """The distinction the whole four-way status exists for."""
    target = _python("3.13.0")

    lifecycle = _lifecycle(target, failures={PYTHON_RELEASE_CYCLE_URL: "timeout"})
    eol = select_eol(lifecycle, target)

    assert eol == EolUnavailable(detail="timeout")
    assert eol != EolUnpublished()
    check = lifecycle_check(target, lifecycle, role="declared_about")
    assert (check.outcome, check.required) == ("failed", False)


def test_a_malformed_schedule_is_unavailable() -> None:
    target = _node("22.17.0")

    lifecycle = _lifecycle(target, {NODE_RELEASE_SCHEDULE_URL: ["not", "an", "object"]})

    assert lifecycle == RuntimeSourceFailed(detail="invalid_document")
    assert select_eol(lifecycle, target) == EolUnavailable(detail="invalid_document")


def test_a_registry_package_has_no_lifecycle_at_all() -> None:
    assert select_eol(None, _pypi("django", "5.2")) == EolNotApplicable()


@pytest.mark.parametrize(
    ("version", "line"),
    [("22.17.0", "v22"), ("20.0.0", "v20"), ("0.12.18", "v0.12")],
)
def test_node_release_lines_are_keyed_the_way_the_schedule_keys_them(
    version: str, line: str
) -> None:
    """nodejs/Release keys the 0.x era by major.minor and everything later by major."""
    target = _node(version)
    payloads = {NODE_RELEASE_SCHEDULE_URL: node_release_schedule({line: "2020-01-01"})}

    assert select_eol(_lifecycle(target, payloads), target) == EolPublished(
        at=datetime(2020, 1, 1, tzinfo=UTC)
    )


def test_a_readable_schedule_is_an_optional_ok_lookup() -> None:
    target = _node("22.17.0")

    lifecycle = _lifecycle(target)

    assert isinstance(lifecycle, RuntimeLifecycle)
    check = lifecycle_check(target, lifecycle, role="declared_about")
    assert (check.outcome, check.required) == ("ok", False)


# --------------------------------------------------------------------------------------
# Source identity
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "index_id", "lifecycle_id"),
    [
        (_python("3.13.0"), "python_release_index", "python_release_cycle"),
        (_node("22.17.0"), "node_release_index", "node_release_schedule"),
    ],
)
def test_each_runtime_reports_its_two_sources_separately(
    target: Target, index_id: str, lifecycle_id: str
) -> None:
    """Two documents, two rows: merging them would hide which one failed."""
    assert index_source_id(target) == index_id
    assert lifecycle_source_id(target) == lifecycle_id


@pytest.mark.parametrize("target", [_pypi("django", "5.2"), _npm("react", "19.1.1")])
def test_a_registry_target_has_no_runtime_source(target: Target) -> None:
    """Answering plausibly would put a source in the response that was never consulted."""
    with pytest.raises(InvariantViolation):
        index_source_id(target)
    with pytest.raises(InvariantViolation):
        lifecycle_source_id(target)
