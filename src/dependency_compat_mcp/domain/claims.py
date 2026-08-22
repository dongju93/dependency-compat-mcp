"""Claims, evidence and release facts: the normalised input of the decision procedure.

Every value here is produced by an adapter that has already finished parsing raw JSON, so
the decision procedure in :mod:`dependency_compat_mcp.domain.evaluate` never sees a
``dict``, an HTTP response, or an unvalidated string.

Three splits in this module exist to make a wrong response unrepresentable rather than
merely unlikely:

* :class:`InstallationGate` / :class:`CompatibilityStatement` / :class:`Corroboration` are
  the three evidence tiers of 03. A tier-C value has no ``stance`` field at all, so it
  cannot be made to argue for ``unsupported``.
* :class:`VersionConstraintEvidence` / :class:`NarrativeEvidence` keep "carries no
  constraint" distinct from "carries an empty constraint".
* :class:`EolPublished` / :class:`EolUnpublished` / :class:`EolNotApplicable` /
  :class:`EolUnavailable` keep the four reasons an end-of-life date can be absent apart.
  A bare ``datetime | None`` would let "upstream has announced no date", "this counterpart
  has no support lifecycle at all" and "the lifecycle source could not be read" collapse
  into one value, and the last of those must never be able to pass for the first two.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, assert_never

from packaging.markers import InvalidMarker, Marker

from dependency_compat_mcp.domain.targets import (
    Target,
    TargetId,
    VersionScheme,
    name_of,
    namespace_of,
    version_of,
)
from dependency_compat_mcp.domain.versions import BoundedVersion

__all__ = [
    "SOURCE_IDS",
    "Claim",
    "CompatibilityStatement",
    "Corroboration",
    "EolNotApplicable",
    "EolPublished",
    "EolStatus",
    "EolUnavailable",
    "EolUnpublished",
    "Evidence",
    "EvidenceId",
    "Fetched",
    "InstallationGate",
    "LookupRole",
    "MarkerCondition",
    "MarkerDecidability",
    "NarrativeEvidence",
    "ReleaseFacts",
    "SourceCheck",
    "SourceId",
    "SourceType",
    "Tier",
    "VersionConstraintEvidence",
    "YankedInfo",
    "analyse_marker",
    "evidence_sort_key",
]

type EvidenceId = str
type Tier = Literal["A", "B", "C"]
type SourceType = Literal[
    "registry_metadata",
    "registry_classifier",
    "official_release_index",
    "official_release_cycle",
]
type SourceId = Literal[
    "pypi_json",
    "npm_registry",
    "python_release_index",
    "python_release_cycle",
    "node_release_index",
    "node_release_schedule",
]
type LookupOutcome = Literal["ok", "not_found", "failed", "skipped"]
# Which side of the resolved relation a lookup was made for. `get_compatibility_context`
# has one target and reads its declarations, so that target is `declaring` there too.
type LookupRole = Literal["declaring", "declared_about"]

_ROLE_ORDER: Final[dict[str, int]] = {"declaring": 0, "declared_about": 1}

SOURCE_IDS: Final[tuple[SourceId, ...]] = (
    "pypi_json",
    "npm_registry",
    "python_release_index",
    "python_release_cycle",
    "node_release_index",
    "node_release_schedule",
)

_TIER_ORDER: Final[dict[str, int]] = {"A": 0, "B": 1, "C": 2}


# --------------------------------------------------------------------------------------
# Environment markers
# --------------------------------------------------------------------------------------

type MarkerDecidability = Literal[
    "always_true", "always_false", "environment_dependent", "extra_guarded"
]

# Every environment variable PEP 508 defines. The list is closed, which is what makes the
# soundness argument below work.
_MARKER_VARIABLES: Final[frozenset[str]] = frozenset(
    {
        "os_name",
        "sys_platform",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_version",
        "python_full_version",
        "implementation_name",
        "implementation_version",
        "extra",
    }
)

# String literals are stripped before looking for variable names, so a marker comparing
# against the *word* "extra" (`sys_platform == "extra"`) is not mistaken for an extra guard.
_STRING_LITERAL_RE: Final = re.compile(r"'[^']*'|\"[^\"]*\"")
_EXTRA_TOKEN_RE: Final = re.compile(r"\bextra\b")
_VARIABLE_RE: Final = re.compile(
    r"\b(" + "|".join(sorted(_MARKER_VARIABLES, key=len, reverse=True)) + r")\b"
)


@dataclass(frozen=True, slots=True)
class MarkerCondition:
    """A PEP 508 marker, how far it can be decided, and what it depends on.

    ``variables`` is carried rather than re-derived downstream: the response tells the
    caller *which* environment facts would settle the marker, and re-parsing the
    expression at the contract boundary would let the two answers drift apart.
    """

    expression: str
    decidability: MarkerDecidability
    variables: tuple[str, ...] = ()


def analyse_marker(expression: str) -> MarkerCondition:
    """Classify a marker as decided, undecidable, or guarded by an extra.

    A marker is called decided **only when it names no environment variable at all**. That
    is deliberately conservative, and the conservatism is the point.

    An earlier version sampled a grid of representative environments and called a marker
    decided when every sample agreed. That is unsound: the variables have open domains, so
    a marker naming a value outside the sample - ``python_version >= "3.15"``,
    ``sys_platform == "freebsd"``, ``platform_machine == "riscv64"`` - looked like a
    contradiction and its claim was dropped. Dropping a conditional dependency that really
    would apply makes the verdict *stronger* than the evidence, which is the one direction
    this server must never fail in. It would also have decayed on a schedule, as each new
    Python release moved past the sampled ceiling.

    The cost is that near-tautologies such as ``python_version >= "3.0"`` now come back
    ``environment_dependent`` and their claims become ``indeterminate``. 03 already treats
    that as the correct answer: the server is never given the environment that would settle
    a marker, and guessing wrong is invisible to the caller.
    """
    literal_free = _STRING_LITERAL_RE.sub("", expression)
    variables = tuple(sorted(set(_VARIABLE_RE.findall(literal_free))))
    if _EXTRA_TOKEN_RE.search(literal_free):
        # An extra-guarded dependency is not part of a default install, so it is never
        # treated as present.
        return MarkerCondition(
            expression=expression, decidability="extra_guarded", variables=variables
        )
    try:
        marker = Marker(expression)
    except InvalidMarker:
        return MarkerCondition(
            expression=expression,
            decidability="environment_dependent",
            variables=variables,
        )

    if variables:
        return MarkerCondition(
            expression=expression,
            decidability="environment_dependent",
            variables=variables,
        )

    # No variables left: the expression is constant, so one evaluation settles it.
    try:
        decided = bool(marker.evaluate({}))
    except Exception:
        return MarkerCondition(
            expression=expression, decidability="environment_dependent"
        )
    return MarkerCondition(
        expression=expression,
        decidability="always_true" if decided else "always_false",
    )


# --------------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fetched:
    """Freshness of a source: when this process retrieved it.

    Every piece of evidence this server can produce is fetched at request time, so there
    is exactly one notion of freshness. Evidence carries this type directly rather than a
    one-member union: a second, non-fetched provenance would be a second claim about when
    a fact was last confirmed, and there is no such source any more.
    """

    retrieved_at: datetime


# --------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VersionConstraintEvidence:
    """A source that carries a machine-readable range."""

    id: EvidenceId
    tier: Tier
    source_type: SourceType
    title: str
    url: str
    substantiates: str
    expression: str
    scheme: VersionScheme
    provenance: Fetched


@dataclass(frozen=True, slots=True)
class NarrativeEvidence:
    """A source that carries no range - a release note, for instance."""

    id: EvidenceId
    tier: Tier
    source_type: SourceType
    title: str
    url: str
    substantiates: str
    provenance: Fetched


type Evidence = VersionConstraintEvidence | NarrativeEvidence


def evidence_id(evidence: Evidence) -> EvidenceId:
    return evidence.id


def evidence_sort_key(evidence: Evidence) -> tuple[int, str, str, str]:
    """Stable order for the response catalogue: ``(tier, source_type, url, id)``.

    Determinism is a tested property (03), and it is also what makes the SDK's cache
    hints meaningful: the same facts must serialise to the same bytes.
    """
    return (
        _TIER_ORDER[evidence.tier],
        evidence.source_type,
        evidence.url,
        evidence.id,
    )


# --------------------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallationGate:
    """Tier A: a constraint an installer or resolver actually enforces."""

    declared_about: TargetId
    expression: str
    scheme: VersionScheme
    bounded_above: bool
    lower_bound: BoundedVersion | None
    condition: MarkerCondition | None
    evidence_id: EvidenceId


@dataclass(frozen=True, slots=True)
class CompatibilityStatement:
    """Tier B: a publisher's explicit statement about what it supports."""

    declared_about: TargetId
    stance: Literal["supports", "excludes"]
    expression: str
    scheme: VersionScheme
    evidence_id: EvidenceId


@dataclass(frozen=True, slots=True)
class Corroboration:
    """Tier C: an enumerated positive signal. Has no stance, so it can only corroborate."""

    declared_about: TargetId
    enumerated_versions: frozenset[str]
    evidence_id: EvidenceId


type Claim = InstallationGate | CompatibilityStatement | Corroboration


def claim_tier(claim: Claim) -> Tier:
    match claim:
        case InstallationGate():
            return "A"
        case CompatibilityStatement():
            return "B"
        case Corroboration():
            return "C"
        case _:
            assert_never(claim)


def claim_evidence_id(claim: Claim) -> EvidenceId:
    match claim:
        case InstallationGate(evidence_id=identifier):
            return identifier
        case CompatibilityStatement(evidence_id=identifier):
            return identifier
        case Corroboration(evidence_id=identifier):
            return identifier
        case _:
            assert_never(claim)


# --------------------------------------------------------------------------------------
# Release facts and lookup records
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class YankedInfo:
    """A withdrawn release. A distribution state, not a compatibility judgement."""

    reason: str | None


@dataclass(frozen=True, slots=True)
class EolPublished:
    """Upstream has announced a day-precision end-of-life date for this release line."""

    at: datetime


@dataclass(frozen=True, slots=True)
class EolUnpublished:
    """The lifecycle source answered, and announces no end-of-life date for this line.

    Upstream publishes month-precision dates (``2031-10``) for lines that have not reached
    end of life yet. A month is not a date, and padding one to a day would manufacture the
    very fact 03's staleness rule is supposed to rest on, so it is reported as unpublished.
    """


@dataclass(frozen=True, slots=True)
class EolNotApplicable:
    """The counterpart has no support lifecycle: a registry package, not a runtime."""


@dataclass(frozen=True, slots=True)
class EolUnavailable:
    """The lifecycle source could not be read, so nothing about end of life is known.

    Distinct from :class:`EolUnpublished` on purpose. "Upstream announced no date" is a
    fact the staleness rule may safely pass over; "the server could not ask" is not, and
    letting the two share a representation is what would allow a confident verdict to be
    built on a hidden failure.
    """

    detail: str


type EolStatus = EolPublished | EolUnpublished | EolNotApplicable | EolUnavailable


@dataclass(frozen=True, slots=True)
class ReleaseFacts:
    """Temporal facts the gate-shape rules of 03 step 5 need.

    ``declared_about_released_at`` powers the open-ceiling rule, and
    ``declared_about_eol`` its mirror image on the floor. The release instants are ``None``
    for relations where the question does not arise (package-to-package, for instance),
    which is why step 5 short-circuits to ``supported`` there; the end-of-life side says
    the same thing with :class:`EolNotApplicable` rather than another ``None``.
    """

    declaring_released_at: datetime | None
    declared_about_released_at: datetime | None
    declared_about_eol: EolStatus
    declaring_yanked: YankedInfo | None
    declared_about_yanked: YankedInfo | None


@dataclass(frozen=True, slots=True)
class SourceCheck:
    """One lookup: what the server opened, *for which target*, and what came back.

    Passed *into* the decision procedure so that ``lookup_failed`` is derived from an
    explicit value rather than from a caught exception, and so the ``sources_checked``
    the caller reads is the very value the verdict was computed from.

    ``target`` and ``role`` are part of the identity of a check rather than decoration.
    Both sides of a ``pypi x pypi`` comparison read ``pypi_json``; without the target,
    one side's success and the other's failure collapse into a single unreadable row and
    the caller cannot audit which release was actually confirmed to exist.
    """

    source: SourceId
    target: Target
    role: LookupRole
    outcome: LookupOutcome
    required: bool = True
    detail: str | None = None


def source_check_sort_key(check: SourceCheck) -> tuple[int, str, str, str, int]:
    """Total order over the fields that identify a check, so output is byte-stable.

    Keyed on the check's identity rather than its outcome: two rows differing only in
    outcome are two different lookups and must not be able to swap places between runs.
    """
    return (
        _ROLE_ORDER[check.role],
        namespace_of(check.target),
        str(name_of(check.target)),
        str(version_of(check.target)),
        SOURCE_IDS.index(check.source),
    )
