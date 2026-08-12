# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "node-semver>=0.9.1",
#     "packaging>=26.3",
# ]
# ///
"""Regenerate ``curated/runtime_releases.json`` from official upstream release data.

Run with::

    uv run scripts/build_runtime_releases.py

This script is committed so the snapshot is reproducible: a reviewer can re-run it and
diff the result instead of trusting a hand-edited table. It is *not* imported by the
server - 03 requires the runtime table to be static committed data, so the only network
access in this repository lives here, outside the request path.

Four upstream sources, all first-party:

===============================  ====================================================
CPython exact releases + dates   ``https://www.python.org/api/v2/downloads/release/``
CPython per-line EOL             ``https://peps.python.org/api/release-cycle.json``
Node.js exact releases + dates   ``https://nodejs.org/dist/index.json``
Node.js per-line EOL             ``https://raw.githubusercontent.com/nodejs/Release/main/schedule.json``
===============================  ====================================================

The CPython EOL source deserves a note. 03 asks for python.org's own machine-readable
release-cycle data rather than a third-party aggregator such as endoflife.date. That file
used to live at ``python/devguide/include/release-cycle.json``; the devguide's own
generator (``_tools/generate_release_cycle.py``) now reads it from
``https://peps.python.org/api/release-cycle.json``, which is the same data served from a
python.org host. This script follows the devguide.

Two rules the script never bends:

* **No estimated dates.** ``end_of_life`` is published as ``YYYY-MM`` for lines that have
  not reached EOL yet. A month is not a date, and turning it into one would invent a fact,
  so those lines get ``eol_at: null``. 03's staleness check does not fire on a null or a
  future EOL, so nothing is lost by being honest here.
* **No silent normalisation.** An upstream version that does not round-trip through its
  ecosystem's canonical spelling is dropped and counted, never rewritten.
"""

import json
import sys
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Final

import nodesemver
from packaging.version import InvalidVersion, Version

PYTHON_RELEASES_URL: Final = (
    "https://www.python.org/api/v2/downloads/release/?is_published=true"
)
PYTHON_RELEASE_CYCLE_URL: Final = "https://peps.python.org/api/release-cycle.json"
NODE_RELEASES_URL: Final = "https://nodejs.org/dist/index.json"
NODE_SCHEDULE_URL: Final = (
    "https://raw.githubusercontent.com/nodejs/Release/main/schedule.json"
)

# Bumped together with the curated pack: `pack_version` rides along on every piece of
# evidence and is part of the cache key, so the two files must not drift apart.
PACK_VERSION: Final = "2026.08.1"

OUTPUT_PATH: Final = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "dependency_compat_mcp"
    / "curated"
    / "runtime_releases.json"
)

_USER_AGENT: Final = "dependency-compat-mcp runtime-release-snapshot"
_TIMEOUT_SECONDS: Final = 60
_ISO_DATE_LENGTH: Final = len("YYYY-MM-DD")


def fetch_json(url: str) -> Any:
    """GET ``url`` and decode it as JSON."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def published_date(raw: str | None) -> str | None:
    """Return an upstream date only when it is a full ``YYYY-MM-DD``.

    Month-precision values (``2029-10``) are returned as ``None`` rather than padded to a
    day: padding would publish a date upstream never announced.
    """
    if not raw:
        return None
    candidate = raw[:_ISO_DATE_LENGTH]
    if len(candidate) != _ISO_DATE_LENGTH:
        return None
    try:
        datetime.strptime(candidate, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return candidate


# --------------------------------------------------------------------------------------
# CPython
# --------------------------------------------------------------------------------------


def _pep440_key(row: Mapping[str, Any]) -> Version:
    return Version(str(row["version"]))


def _semver_cmp(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    return int(
        nodesemver.compare(str(left["version"]), str(right["version"]), loose=False)
    )


def python_line_eol(cycle: Mapping[str, Mapping[str, Any]]) -> dict[str, str | None]:
    """Map ``"3.13"`` -> published EOL date, or ``None`` when only a month is known."""
    return {
        line: published_date(entry.get("end_of_life")) for line, entry in cycle.items()
    }


def python_releases(
    payload: Sequence[Mapping[str, Any]], line_eol: Mapping[str, str | None]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert the python.org download index into table rows.

    The endpoint also lists "Python install manager" builds, which are a separate product
    and not CPython releases; they fail the ``Python <version>`` name shape and are
    dropped with everything else that does not round-trip.
    """
    rows: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    for release in payload:
        name = str(release.get("name", ""))
        prefix, separator, raw_version = name.partition(" ")
        if prefix != "Python" or not separator or " " in raw_version:
            dropped.append(name)
            continue
        try:
            parsed = Version(raw_version)
        except InvalidVersion:
            dropped.append(name)
            continue
        # The spine only admits canonical PEP 440 spellings, so a version this table
        # would have to rewrite is a version the server could never be asked about.
        if str(parsed) != raw_version:
            dropped.append(name)
            continue
        released_at = published_date(release.get("release_date"))
        if released_at is None:
            dropped.append(name)
            continue
        if raw_version in rows:
            # python.org has listed the same release twice before. Keeping the first
            # occurrence makes the output independent of upstream ordering.
            dropped.append(f"{name} (duplicate)")
            continue
        line = ".".join(str(part) for part in parsed.release[:2])
        rows[raw_version] = {
            "version": raw_version,
            "released_at": released_at,
            "eol_at": line_eol.get(line),
        }
    ordered = sorted(rows.values(), key=_pep440_key)
    return ordered, dropped


# --------------------------------------------------------------------------------------
# Node.js
# --------------------------------------------------------------------------------------


def node_line_eol(schedule: Mapping[str, Mapping[str, Any]]) -> dict[str, str | None]:
    """Map a schedule key (``"v22"``, ``"v0.12"``) to its published end date."""
    return {line: published_date(entry.get("end")) for line, entry in schedule.items()}


def _node_line_key(version: nodesemver.SemVer) -> str:
    # nodejs/Release keys the 0.x era by major.minor, because those lines were released
    # and retired independently; everything from 4.x on is keyed by major alone.
    if version.major == 0:
        return f"v0.{version.minor}"
    return f"v{version.major}"


def node_releases(
    payload: Sequence[Mapping[str, Any]], line_eol: Mapping[str, str | None]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert nodejs.org's dist index into table rows."""
    rows: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    for release in payload:
        raw = str(release.get("version", ""))
        # nodejs.org writes `v22.17.0`; the canonical npm SemVer spelling drops the `v`.
        candidate = raw[1:] if raw.startswith("v") else raw
        parsed = nodesemver.valid(candidate, loose=False)
        if parsed is None or str(parsed) != candidate:
            dropped.append(raw)
            continue
        released_at = published_date(release.get("date"))
        if released_at is None:
            dropped.append(raw)
            continue
        if candidate in rows:
            dropped.append(f"{raw} (duplicate)")
            continue
        rows[candidate] = {
            "version": candidate,
            "released_at": released_at,
            "eol_at": line_eol.get(_node_line_key(parsed)),
        }
    ordered = sorted(rows.values(), key=cmp_to_key(_semver_cmp))
    return ordered, dropped


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def _report(
    label: str, rows: Sequence[Mapping[str, Any]], dropped: Iterable[str]
) -> None:
    dropped_list = list(dropped)
    without_eol = sum(1 for row in rows if row["eol_at"] is None)
    print(
        f"{label}: {len(rows)} releases, {without_eol} without a published EOL date, "
        f"{len(dropped_list)} upstream entries dropped",
        file=sys.stderr,
    )
    for name in dropped_list:
        print(f"  dropped: {name}", file=sys.stderr)


def main() -> int:
    """Fetch, convert and write the snapshot; return a process exit code."""
    python_line = python_line_eol(fetch_json(PYTHON_RELEASE_CYCLE_URL))
    python_rows, python_dropped = python_releases(
        fetch_json(PYTHON_RELEASES_URL), python_line
    )
    node_line = node_line_eol(fetch_json(NODE_SCHEDULE_URL))
    node_rows, node_dropped = node_releases(fetch_json(NODE_RELEASES_URL), node_line)

    if not python_rows or not node_rows:
        print("refusing to write an empty runtime table", file=sys.stderr)
        return 1

    document = {
        "generated_at": datetime.now(UTC).date().isoformat(),
        "pack_version": PACK_VERSION,
        "runtimes": {"python": python_rows, "node": node_rows},
    }
    # `indent=2` + trailing newline keeps the committed diff reviewable line by line,
    # which is the whole point of shipping this as data rather than as a fetch.
    OUTPUT_PATH.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    _report("python", python_rows, python_dropped)
    _report("node", node_rows, node_dropped)
    print(f"wrote {OUTPUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
