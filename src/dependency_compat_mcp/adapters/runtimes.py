"""Runtime release facts for ``runtime:python`` and ``runtime:node``, read at request time.

This adapter has the same job as the registry adapters - turn an official source into
domain values - and it now does it the same way: over the network, through
:mod:`dependency_compat_mcp.infra.http`, under the same host allowlist, redirect
re-validation, body ceiling and per-attempt timeout.

It replaces a committed snapshot. The snapshot was wrong for this product in one specific
way: a Python or Node release published after the last regeneration did not exist as far as
the server was concerned, and the response said ``release_not_found`` - a factual claim -
rather than admitting the repository was simply out of date. Nothing in the repository
should have to be edited for the server to know that CPython 3.14.2 shipped.

**Two sources per runtime, and they answer different questions.**

============  ===========================================================================
existence     ``https://www.python.org/api/v2/downloads/release/`` /
              ``https://nodejs.org/dist/index.json``
lifecycle     ``https://peps.python.org/api/release-cycle.json`` /
              ``.../nodejs/Release/main/schedule.json``
============  ===========================================================================

The split matters because their failure modes must not be shared. The existence source is
*required*: without it the server cannot say whether the release asked about exists, and
guessing either way is exactly the mistake 03 step 0 forbids. The lifecycle source is
*optional*: it only ever narrows a satisfied open-ended gate, so a response can still be
decided without it - as long as the failure is reported rather than read as "no end-of-life
date was announced". That is why :func:`select_eol` answers a four-way
:data:`~dependency_compat_mcp.domain.claims.EolStatus` and never a bare ``datetime | None``.

**Documents are fetched whole and selected from purely.** ``fetch_index`` and
``fetch_lifecycle`` return a parsed document; ``select_release`` and ``select_eol`` are pure
functions over it. That is what lets the application service cache one fetch and answer any
version from it, without a per-version cache entry that would multiply the same request.

Two rules the parsers never bend, inherited from the generator this module replaced:

* **No silent normalisation.** An upstream version that does not round-trip through its
  ecosystem's canonical spelling is dropped, never rewritten - ``v22.17.0`` is not a
  version this server admits from a caller, so it must not admit it from a registry either.
* **No estimated dates.** Upstream publishes ``end_of_life`` as ``YYYY-MM`` for lines that
  have not reached it yet. A month is not a date; those lines are reported as unpublished.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, assert_never

from dependency_compat_mcp.domain.claims import (
    EolNotApplicable,
    EolPublished,
    EolStatus,
    EolUnavailable,
    EolUnpublished,
    LookupRole,
    SourceCheck,
    SourceId,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.targets import (
    ExactVersion,
    NodeRuntimeTarget,
    NpmTarget,
    PyPITarget,
    PythonRuntimeTarget,
    RuntimeName,
    Target,
    parse_pep440_version,
    parse_semver_version,
    version_of,
)
from dependency_compat_mcp.domain.versions import release_tuple
from dependency_compat_mcp.infra.http import (
    HttpFailed,
    HttpNotFound,
    HttpOk,
    JsonFetcher,
    build_url,
)

__all__ = [
    "NODE_RELEASE_INDEX_URL",
    "NODE_RELEASE_SCHEDULE_URL",
    "PYTHON_RELEASE_CYCLE_URL",
    "PYTHON_RELEASE_INDEX_URL",
    "RuntimeIndex",
    "RuntimeIndexLookup",
    "RuntimeLifecycle",
    "RuntimeLifecycleLookup",
    "RuntimeReleaseAbsent",
    "RuntimeReleaseAdapter",
    "RuntimeReleaseFound",
    "RuntimeReleaseLookup",
    "RuntimeReleaseUnavailable",
    "RuntimeSourceFailed",
    "index_check",
    "index_source_id",
    "lifecycle_check",
    "lifecycle_source_id",
    "runtime_of",
    "select_eol",
    "select_release",
]

# The trailing empty segment keeps the path's trailing slash: python.org answers the
# slash-less form with a 301, and following a redirect to reach the documented URL would
# spend a hop and a round trip on nothing.
PYTHON_RELEASE_INDEX_URL: Final = build_url(
    "www.python.org", "api", "v2", "downloads", "release", ""
)
PYTHON_RELEASE_CYCLE_URL: Final = build_url(
    "peps.python.org", "api", "release-cycle.json"
)
NODE_RELEASE_INDEX_URL: Final = build_url("nodejs.org", "dist", "index.json")
NODE_RELEASE_SCHEDULE_URL: Final = build_url(
    "raw.githubusercontent.com", "nodejs", "Release", "main", "schedule.json"
)

_PYTHON_NAME_PREFIX: Final = "Python"
_ISO_DATE_LENGTH: Final = len("YYYY-MM-DD")

_INDEX_SOURCE_IDS: Final[Mapping[RuntimeName, SourceId]] = {
    "python": "python_release_index",
    "node": "node_release_index",
}
_LIFECYCLE_SOURCE_IDS: Final[Mapping[RuntimeName, SourceId]] = {
    "python": "python_release_cycle",
    "node": "node_release_schedule",
}
_INDEX_URLS: Final[Mapping[RuntimeName, str]] = {
    "python": PYTHON_RELEASE_INDEX_URL,
    "node": NODE_RELEASE_INDEX_URL,
}
_LIFECYCLE_URLS: Final[Mapping[RuntimeName, str]] = {
    "python": PYTHON_RELEASE_CYCLE_URL,
    "node": NODE_RELEASE_SCHEDULE_URL,
}


# --------------------------------------------------------------------------------------
# Lookup results
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeSourceFailed:
    """An official source could not be read. ``detail`` is a stable code, never prose.

    The codes are the ones :mod:`dependency_compat_mcp.infra.http` produces, plus
    ``invalid_document`` for a 2xx body that is not the shape the publisher documents, and
    ``not_found`` for a 404 - an official index that has moved is a failure to consult the
    source, not a statement that the runtime has no releases.
    """

    detail: str


@dataclass(frozen=True, slots=True)
class RuntimeIndex:
    """Every exact release the official index lists, mapped to its publication instant."""

    released_at: Mapping[str, datetime]


type RuntimeIndexLookup = RuntimeIndex | RuntimeSourceFailed


@dataclass(frozen=True, slots=True)
class RuntimeLifecycle:
    """Per-release-line end of life. ``None`` means the line has no published date."""

    eol_at: Mapping[str, datetime | None]


type RuntimeLifecycleLookup = RuntimeLifecycle | RuntimeSourceFailed


@dataclass(frozen=True, slots=True)
class RuntimeReleaseFound:
    """The official index lists this exact release."""

    version: ExactVersion
    released_at: datetime


@dataclass(frozen=True, slots=True)
class RuntimeReleaseAbsent:
    """The official index answered and does not list this release.

    This is the only value that may become ``release_not_found``: it rests on a source
    that was actually read.
    """

    target: Target


@dataclass(frozen=True, slots=True)
class RuntimeReleaseUnavailable:
    """The index could not be read, so whether the release exists is simply unknown."""

    target: Target
    detail: str


type RuntimeReleaseLookup = (
    RuntimeReleaseFound | RuntimeReleaseAbsent | RuntimeReleaseUnavailable
)


# --------------------------------------------------------------------------------------
# Target classification
# --------------------------------------------------------------------------------------


def runtime_of(target: Target) -> RuntimeName:
    """Which runtime ``target`` names.

    Raises :class:`InvariantViolation` for a registry target. There is no honest answer -
    a PyPI package is not covered by either release index - and returning a plausible one
    would put a source in ``sources_checked`` that was never consulted. Callers dispatch on
    the target first, so reaching this is a server defect, and 03 step 7 requires defects to
    surface as tool errors rather than hide inside an ``unknown``.
    """
    match target:
        case PythonRuntimeTarget():
            return "python"
        case NodeRuntimeTarget():
            return "node"
        case PyPITarget() | NpmTarget():
            raise InvariantViolation(
                f"{type(target).__name__} has no runtime release index; "
                "classify the target before asking for its source."
            )
        case _:
            assert_never(target)


def index_source_id(target: Target) -> SourceId:
    """The source id a release-existence lookup for ``target`` is reported under."""
    return _INDEX_SOURCE_IDS[runtime_of(target)]


def lifecycle_source_id(target: Target) -> SourceId:
    """The source id a support-lifecycle lookup for ``target`` is reported under."""
    return _LIFECYCLE_SOURCE_IDS[runtime_of(target)]


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def _as_utc(day: date) -> datetime:
    """Midnight UTC on ``day``.

    The decision procedure compares release instants, so the sources' day-precision dates
    have to become aware datetimes somewhere. Doing it here, once, keeps naive datetimes
    out of the domain entirely - a naive/aware comparison raises ``TypeError`` deep inside
    the decision procedure, which is exactly the kind of failure that must be impossible.
    """
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _published_date(raw: object) -> date | None:
    """An upstream date, but only when it is a full ``YYYY-MM-DD``.

    Month-precision values (``2031-10``) come back ``None`` rather than padded to a day:
    padding would publish a date upstream never announced, and 03's staleness rule would
    then fire on a fact nobody stated.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw[:_ISO_DATE_LENGTH]
    if len(candidate) != _ISO_DATE_LENGTH:
        return None
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        return None


def _canonical_python_version(raw: str) -> str | None:
    """``raw`` if it is already the canonical PEP 440 spelling, else ``None``."""
    try:
        parsed = parse_pep440_version(raw)
    except ValueError:
        return None
    return raw if str(parsed) == raw else None


def _canonical_node_version(raw: str) -> str | None:
    """``raw`` with nodejs.org's ``v`` prefix removed, if the rest is canonical SemVer."""
    candidate = raw[1:] if raw.startswith("v") else raw
    try:
        parsed = parse_semver_version(candidate)
    except ValueError:
        return None
    return candidate if str(parsed) == candidate else None


def _rows(payload: object) -> list[Mapping[str, object]] | None:
    if not isinstance(payload, list):
        return None
    return [row for row in payload if isinstance(row, Mapping)]


def _parse_python_index(payload: object) -> Mapping[str, datetime] | None:
    """python.org's download index -> canonical version -> publication instant.

    The endpoint also lists "Python install manager" builds, which are a separate product
    and not CPython releases; they fail the ``Python <version>`` name shape and are dropped
    along with everything else that does not round-trip.
    """
    rows = _rows(payload)
    if rows is None:
        return None
    released: dict[str, datetime] = {}
    for row in rows:
        if row.get("is_published") is False:
            continue
        name = row.get("name")
        if not isinstance(name, str):
            continue
        prefix, separator, raw_version = name.partition(" ")
        if prefix != _PYTHON_NAME_PREFIX or not separator or " " in raw_version:
            continue
        version = _canonical_python_version(raw_version)
        if version is None:
            continue
        released_at = _published_date(row.get("release_date"))
        if released_at is None:
            continue
        # python.org has listed the same release twice before. Keeping the first
        # occurrence makes the answer independent of upstream ordering.
        released.setdefault(version, _as_utc(released_at))
    return released


def _parse_node_index(payload: object) -> Mapping[str, datetime] | None:
    """nodejs.org's dist index -> canonical version -> publication instant."""
    rows = _rows(payload)
    if rows is None:
        return None
    released: dict[str, datetime] = {}
    for row in rows:
        raw = row.get("version")
        if not isinstance(raw, str):
            continue
        version = _canonical_node_version(raw)
        if version is None:
            continue
        released_at = _published_date(row.get("date"))
        if released_at is None:
            continue
        released.setdefault(version, _as_utc(released_at))
    return released


def _parse_lifecycle(
    payload: object, field: str
) -> Mapping[str, datetime | None] | None:
    """A ``{line: {..., <field>: date}}`` schedule -> line -> published end of life.

    A line present with no day-precision date maps to ``None``: upstream was consulted and
    announced nothing, which is a different fact from the line being absent entirely.
    """
    if not isinstance(payload, Mapping):
        return None
    lines: dict[str, datetime | None] = {}
    for line, entry in payload.items():
        if not isinstance(line, str) or not isinstance(entry, Mapping):
            continue
        published = _published_date(entry.get(field))
        lines[line] = None if published is None else _as_utc(published)
    return lines


def _line_key(target: Target) -> str:
    """The schedule key for ``target``'s release line, in that schedule's own spelling."""
    release = release_tuple(version_of(target))
    match runtime_of(target):
        case "python":
            return ".".join(str(part) for part in release[:2])
        case "node":
            # nodejs/Release keys the 0.x era by major.minor, because those lines were
            # released and retired independently; everything from 4.x on is keyed by major.
            major, minor = release[0], release[1]
            return f"v0.{minor}" if major == 0 else f"v{major}"
        case never:
            assert_never(never)


# --------------------------------------------------------------------------------------
# Selection (pure)
# --------------------------------------------------------------------------------------


def select_release(lookup: RuntimeIndexLookup, target: Target) -> RuntimeReleaseLookup:
    """Read one release out of a fetched index. Pure and total."""
    match lookup:
        case RuntimeSourceFailed(detail=detail):
            return RuntimeReleaseUnavailable(target=target, detail=detail)
        case RuntimeIndex(released_at=released_at):
            version = version_of(target)
            found = released_at.get(str(version))
            if found is None:
                return RuntimeReleaseAbsent(target=target)
            return RuntimeReleaseFound(version=version, released_at=found)
        case _:
            assert_never(lookup)


def select_eol(lookup: RuntimeLifecycleLookup | None, target: Target) -> EolStatus:
    """Read one release line's end of life out of a fetched schedule. Pure and total.

    ``None`` means the question does not arise for this target - a registry package has no
    support lifecycle - and is reported as :class:`EolNotApplicable` rather than as a
    missing date, so the staleness rule can tell "not asked" from "asked, nothing known".
    """
    if lookup is None:
        return EolNotApplicable()
    match lookup:
        case RuntimeSourceFailed(detail=detail):
            return EolUnavailable(detail=detail)
        case RuntimeLifecycle(eol_at=eol_at):
            line = _line_key(target)
            if line not in eol_at:
                # The schedule was read and simply does not cover this line. Upstream has
                # announced nothing about it, which is the same fact as a published-but-
                # month-precision date, not a failure to look.
                return EolUnpublished()
            published = eol_at[line]
            if published is None:
                return EolUnpublished()
            return EolPublished(at=published)
        case _:
            assert_never(lookup)


# --------------------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeReleaseAdapter:
    """Fetches and parses the official runtime release and lifecycle documents."""

    fetcher: JsonFetcher

    async def fetch_index(self, target: Target) -> RuntimeIndexLookup:
        """Fetch the release index for ``target``'s runtime.

        Cancellation propagates; every other failure is a value, so the decision procedure
        has an explicit branch for it instead of inheriting one from a caught exception.
        """
        runtime = runtime_of(target)
        payload = await self._get(_INDEX_URLS[runtime])
        match payload:
            case RuntimeSourceFailed():
                return payload
            case _:
                parsed = (
                    _parse_python_index(payload)
                    if runtime == "python"
                    else _parse_node_index(payload)
                )
        if not parsed:
            # An official release index is never empty. Reading an unparseable or empty one
            # as "this runtime has no releases" would turn a source failure into the
            # factual claim that every version asked about does not exist.
            return RuntimeSourceFailed(detail="invalid_document")
        return RuntimeIndex(released_at=parsed)

    async def fetch_lifecycle(self, target: Target) -> RuntimeLifecycleLookup:
        """Fetch the support schedule for ``target``'s runtime."""
        runtime = runtime_of(target)
        payload = await self._get(_LIFECYCLE_URLS[runtime])
        match payload:
            case RuntimeSourceFailed():
                return payload
            case _:
                parsed = _parse_lifecycle(
                    payload, "end_of_life" if runtime == "python" else "end"
                )
        if not parsed:
            return RuntimeSourceFailed(detail="invalid_document")
        return RuntimeLifecycle(eol_at=parsed)

    async def _get(self, url: str) -> object | RuntimeSourceFailed:
        result = await self.fetcher.get_json(url)
        match result:
            case HttpOk(payload=payload):
                return payload
            case HttpNotFound():
                # An official document that has moved is a failure to consult the source.
                return RuntimeSourceFailed(detail="not_found")
            case HttpFailed(detail=detail):
                return RuntimeSourceFailed(detail=detail)
            case _:
                assert_never(result)


# --------------------------------------------------------------------------------------
# Source checks
# --------------------------------------------------------------------------------------


def index_check(
    target: Target, lookup: RuntimeReleaseLookup, *, role: LookupRole
) -> SourceCheck:
    """The ``SourceCheck`` for a completed release-existence lookup.

    Derived from the value the verdict was computed from, so what the server reports it
    consulted and what it actually used cannot diverge (03 [3]). Required: without it the
    server cannot say whether the release exists.
    """
    source = index_source_id(target)
    match lookup:
        case RuntimeReleaseFound():
            return SourceCheck(source=source, target=target, role=role, outcome="ok")
        case RuntimeReleaseAbsent():
            return SourceCheck(
                source=source, target=target, role=role, outcome="not_found"
            )
        case RuntimeReleaseUnavailable(detail=detail):
            return SourceCheck(
                source=source,
                target=target,
                role=role,
                outcome="failed",
                required=True,
                detail=detail,
            )
        case _:
            assert_never(lookup)


def lifecycle_check(
    target: Target, lookup: RuntimeLifecycleLookup, *, role: LookupRole
) -> SourceCheck:
    """The ``SourceCheck`` for a completed lifecycle lookup.

    Optional: the schedule only narrows an already-satisfied open-ended gate, so a failure
    here must not turn the whole request into ``lookup_failed``. It becomes a
    ``source_unavailable`` limitation, and step 5 refuses to call the pair ``supported``
    while it cannot check the floor.
    """
    source = lifecycle_source_id(target)
    match lookup:
        case RuntimeLifecycle():
            return SourceCheck(
                source=source, target=target, role=role, outcome="ok", required=False
            )
        case RuntimeSourceFailed(detail=detail):
            return SourceCheck(
                source=source,
                target=target,
                role=role,
                outcome="failed",
                required=False,
                detail=detail,
            )
        case _:
            assert_never(lookup)
