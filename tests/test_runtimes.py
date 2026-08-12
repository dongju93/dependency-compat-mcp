"""The shipped runtime release snapshot and the adapter that reads it.

03's operating rule for this table is blunt: "등록된 runtime의 표를 일부 버전만 넣은
fixture 상태로 납품하지 않는다." These tests therefore assert against the *committed*
``runtime_releases.json`` rather than a fixture — completeness is the property under test,
and a fixture would make it untestable.

The snapshot was generated on 2026-08-12 by ``scripts/build_runtime_releases.py``. The
lower bounds below are deliberately far under the real counts so that regenerating the
snapshot never breaks the suite, while a truncated or fixture-sized table still fails.

No test in this file touches the network.
"""

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dependency_compat_mcp.adapters.runtimes import (
    RuntimeReleaseAdapter,
    RuntimeReleaseFound,
    RuntimeReleaseMissing,
)
from dependency_compat_mcp.curated.loader import (
    RUNTIME_RELEASES_PATH,
    PackLoadError,
    RuntimeReleaseTable,
    load_runtime_releases,
    parse_exact_version,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.targets import (
    NodeRuntimeTarget,
    NpmTarget,
    PyPITarget,
    PythonRuntimeTarget,
    parse_target,
)

# A complete CPython snapshot has hundreds of releases and Node.js more; a fixture-sized
# table would have tens. These floors separate the two without pinning today's numbers.
MIN_PYTHON_RELEASES = 300
MIN_NODE_RELEASES = 500


@pytest.fixture(scope="module")
def table() -> RuntimeReleaseTable:
    return load_runtime_releases()


@pytest.fixture(scope="module")
def adapter(table: RuntimeReleaseTable) -> RuntimeReleaseAdapter:
    return RuntimeReleaseAdapter(table=table)


def _python(version: str) -> PythonRuntimeTarget:
    target = parse_target("runtime", "python", version)
    assert isinstance(target, PythonRuntimeTarget)
    return target


def _node(version: str) -> NodeRuntimeTarget:
    target = parse_target("runtime", "node", version)
    assert isinstance(target, NodeRuntimeTarget)
    return target


# --------------------------------------------------------------------------------------
# The shipped snapshot
# --------------------------------------------------------------------------------------


def test_shipped_table_loads(table: RuntimeReleaseTable) -> None:
    assert table.pack_version == "2026.08.1"


def test_snapshot_is_complete_not_a_fixture(table: RuntimeReleaseTable) -> None:
    assert len(table.python) >= MIN_PYTHON_RELEASES
    assert len(table.node) >= MIN_NODE_RELEASES


@pytest.mark.parametrize(
    ("runtime", "version", "released_at"),
    [
        ("python", "3.13.0", date(2024, 10, 7)),
        ("python", "3.12.0", date(2023, 10, 2)),
        ("node", "22.17.0", date(2025, 6, 24)),
        ("node", "20.0.0", date(2023, 4, 17)),
    ],
)
def test_well_known_releases_are_present_with_their_date(
    table: RuntimeReleaseTable, runtime: str, version: str, released_at: date
) -> None:
    target = _python(version) if runtime == "python" else _node(version)
    release = table.lookup(target)
    assert release is not None
    assert release.released_at == released_at


def test_every_version_round_trips_through_its_parser(
    table: RuntimeReleaseTable,
) -> None:
    """The table cannot hold a spelling the tool boundary would reject."""
    for release in table.python:
        assert str(parse_exact_version("pep440", str(release.version))) == str(
            release.version
        )
    for release in table.node:
        assert str(parse_exact_version("semver", str(release.version))) == str(
            release.version
        )


def test_pre_releases_are_included(table: RuntimeReleaseTable) -> None:
    """A caller may legitimately ask about `3.14.0rc1`; the table must know it."""
    assert table.lookup(_python("3.14.0rc1")) is not None


def test_eol_is_consistent_within_a_release_line(table: RuntimeReleaseTable) -> None:
    """`eol_at` is the end of life of the *line*, so 3.13.x must all agree."""
    python_lines: defaultdict[tuple[int, ...], set[date | None]] = defaultdict(set)
    for release in table.python:
        line = tuple(str(release.version).split(".")[:2])
        python_lines[line].add(release.eol_at)  # type: ignore[arg-type]
    assert {
        line: values for line, values in python_lines.items() if len(values) > 1
    } == {}

    node_lines: defaultdict[str, set[date | None]] = defaultdict(set)
    for release in table.node:
        major = str(release.version).split(".")[0]
        # nodejs/Release schedules the 0.x era per minor line, everything else per major.
        key = ".".join(str(release.version).split(".")[:2]) if major == "0" else major
        node_lines[key].add(release.eol_at)
    assert {
        line: values for line, values in node_lines.items() if len(values) > 1
    } == {}


def test_known_eol_dates_match_upstream(table: RuntimeReleaseTable) -> None:
    """Spot-check published EOLs that have already passed, so they can never move."""
    python_39 = table.lookup(_python("3.9.0"))
    assert python_39 is not None
    assert python_39.eol_at == date(2025, 10, 31)

    node_18 = table.lookup(_node("18.0.0"))
    assert node_18 is not None
    assert node_18.eol_at == date(2025, 4, 30)


def test_eol_is_recorded_verbatim_even_when_it_precedes_a_release(
    table: RuntimeReleaseTable,
) -> None:
    """Upstream is copied, not corrected.

    Python 2.7 was declared end-of-life on 2020-01-01 but 2.7.18 shipped that April, so
    "eol_at >= released_at" is simply not an upstream invariant. Recording the announced
    date anyway is the point: 03's staleness check must rest on what upstream published,
    and "fixing" the pair here would be the estimation the table forbids.
    """
    late = table.lookup(_python("2.7.18"))
    assert late is not None
    assert late.eol_at == date(2020, 1, 1)
    assert late.released_at == date(2020, 4, 20)


def test_snapshot_records_when_it_was_generated() -> None:
    """`generated_at` is what tells a reviewer how old the committed data is."""
    document = json.loads(RUNTIME_RELEASES_PATH.read_text(encoding="utf-8"))
    assert date.fromisoformat(document["generated_at"]) == date(2026, 8, 12)


# --------------------------------------------------------------------------------------
# Table validation
# --------------------------------------------------------------------------------------


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "runtime_releases.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_missing_runtime_is_a_start_up_failure(tmp_path: Path) -> None:
    """03 forbids delivering a registered runtime without its table."""
    path = _write(
        tmp_path,
        {
            "pack_version": "test",
            "runtimes": {
                "python": [{"version": "3.13.0", "released_at": "2024-10-07"}]
            },
        },
    )
    with pytest.raises(PackLoadError, match="missing release table"):
        load_runtime_releases(path)


def test_unknown_runtime_is_a_start_up_failure(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "pack_version": "test",
            "runtimes": {
                "python": [{"version": "3.13.0", "released_at": "2024-10-07"}],
                "node": [{"version": "22.17.0", "released_at": "2025-06-24"}],
                "deno": [{"version": "2.0.0", "released_at": "2024-10-09"}],
            },
        },
    )
    with pytest.raises(PackLoadError, match="unknown runtime"):
        load_runtime_releases(path)


def test_empty_runtime_table_is_a_start_up_failure(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "pack_version": "test",
            "runtimes": {
                "python": [{"version": "3.13.0", "released_at": "2024-10-07"}],
                "node": [],
            },
        },
    )
    with pytest.raises(PackLoadError, match="node release table is empty"):
        load_runtime_releases(path)


def test_non_canonical_version_is_a_start_up_failure(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "pack_version": "test",
            "runtimes": {
                "python": [{"version": "3.13.0", "released_at": "2024-10-07"}],
                "node": [{"version": "v22.17.0", "released_at": "2025-06-24"}],
            },
        },
    )
    with pytest.raises(PackLoadError, match="canonical SemVer form"):
        load_runtime_releases(path)


def test_duplicate_version_is_a_start_up_failure(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "pack_version": "test",
            "runtimes": {
                "python": [
                    {"version": "3.13.0", "released_at": "2024-10-07"},
                    {"version": "3.13.0", "released_at": "2024-10-08"},
                ],
                "node": [{"version": "22.17.0", "released_at": "2025-06-24"}],
            },
        },
    )
    with pytest.raises(PackLoadError, match="more than once"):
        load_runtime_releases(path)


# --------------------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------------------


def test_source_id_is_split_by_runtime(adapter: RuntimeReleaseAdapter) -> None:
    assert adapter.source_id_for(_python("3.13.0")) == "python_release_table"
    assert adapter.source_id_for(_node("22.17.0")) == "node_release_table"


def test_source_id_for_a_package_is_a_server_defect(
    adapter: RuntimeReleaseAdapter,
) -> None:
    """Inventing a source id would put a never-opened source in `sources_checked`."""
    package = parse_target("pypi", "example-framework", "5.2")
    assert isinstance(package, PyPITarget)
    with pytest.raises(InvariantViolation, match="no runtime release table"):
        adapter.source_id_for(package)


def test_fetch_release_returns_utc_datetimes(adapter: RuntimeReleaseAdapter) -> None:
    """`ReleaseFacts` compares aware datetimes; day-precision dates are lifted here."""
    found = adapter.fetch_release(_node("22.17.0"))
    assert isinstance(found, RuntimeReleaseFound)
    assert found.released_at == datetime(2025, 6, 24, tzinfo=UTC)
    assert found.eol_at == datetime(2027, 4, 30, tzinfo=UTC)


def test_fetch_release_keeps_a_null_eol_null(adapter: RuntimeReleaseAdapter) -> None:
    """An unpublished EOL is never estimated, so the staleness check cannot misfire."""
    found = adapter.fetch_release(_python("3.13.0"))
    assert isinstance(found, RuntimeReleaseFound)
    assert found.eol_at is None


def test_unknown_version_is_not_found(adapter: RuntimeReleaseAdapter) -> None:
    missing = adapter.fetch_release(_python("3.99.0"))
    assert isinstance(missing, RuntimeReleaseMissing)
    assert missing.reason == "version_not_in_table"


def test_a_package_is_not_a_runtime(adapter: RuntimeReleaseAdapter) -> None:
    """Kept apart from `version_not_in_table`: they mean different things to a caller."""
    package = parse_target("npm", "example-toolkit", "4.1.0")
    assert isinstance(package, NpmTarget)
    missing = adapter.fetch_release(package)
    assert isinstance(missing, RuntimeReleaseMissing)
    assert missing.reason == "not_a_runtime"


def test_source_check_mirrors_the_lookup(adapter: RuntimeReleaseAdapter) -> None:
    target = _python("3.13.0")
    check = adapter.source_check(target, adapter.fetch_release(target))
    assert check.source == "python_release_table"
    assert check.outcome == "ok"

    absent = _python("3.99.0")
    missing_check = adapter.source_check(absent, adapter.fetch_release(absent))
    assert missing_check.outcome == "not_found"
    assert missing_check.detail == "version_not_in_snapshot"


def test_source_check_for_a_package_is_a_server_defect(
    adapter: RuntimeReleaseAdapter,
) -> None:
    package = parse_target("pypi", "example-framework", "5.2")
    with pytest.raises(InvariantViolation):
        adapter.source_check(package, adapter.fetch_release(package))
