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
* :class:`Fetched` / :class:`Curated` keep the two incompatible notions of freshness -
  when the server looked, and when a human last checked the source - in separate types.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Literal, assert_never

from packaging.markers import InvalidMarker, Marker

from dependency_compat_mcp.domain.targets import TargetId, VersionScheme
from dependency_compat_mcp.domain.versions import BoundedVersion

__all__ = [
    "SOURCE_IDS",
    "Claim",
    "CompatibilityStatement",
    "Corroboration",
    "Curated",
    "Evidence",
    "EvidenceId",
    "Fetched",
    "InstallationGate",
    "MarkerCondition",
    "MarkerDecidability",
    "NarrativeEvidence",
    "Provenance",
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
    "official_support_policy",
    "official_release_note",
]
type SourceId = Literal[
    "pypi_json",
    "npm_registry",
    "curated_pack",
    "python_release_table",
    "node_release_table",
]
type LookupOutcome = Literal["ok", "not_found", "failed", "skipped"]

SOURCE_IDS: Final[tuple[SourceId, ...]] = (
    "pypi_json",
    "npm_registry",
    "curated_pack",
    "python_release_table",
    "node_release_table",
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
    """A PEP 508 marker plus how far it can be decided without an environment."""

    expression: str
    decidability: MarkerDecidability


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
    if _EXTRA_TOKEN_RE.search(literal_free):
        # An extra-guarded dependency is not part of a default install, so it is never
        # treated as present.
        return MarkerCondition(expression=expression, decidability="extra_guarded")
    try:
        marker = Marker(expression)
    except InvalidMarker:
        return MarkerCondition(
            expression=expression, decidability="environment_dependent"
        )

    if _VARIABLE_RE.search(literal_free):
        return MarkerCondition(
            expression=expression, decidability="environment_dependent"
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
    """Freshness of a looked-up source: when this process retrieved it."""

    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class Curated:
    """Freshness of a committed source: when a human last checked it against the original."""

    reviewed_at: date
    pack_version: str


type Provenance = Fetched | Curated


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
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class NarrativeEvidence:
    """A source that carries no range - a release note, for instance."""

    id: EvidenceId
    tier: Tier
    source_type: SourceType
    title: str
    url: str
    substantiates: str
    provenance: Provenance


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
class ReleaseFacts:
    """Temporal facts the gate-shape rules of 03 step 5 need.

    ``declared_about_released_at`` powers the open-ceiling rule, and
    ``declared_about_eol_at`` its mirror image on the floor. Both are ``None`` for
    relations where the question does not arise (package-to-package, for instance), which
    is why step 5 short-circuits to ``supported`` there.
    """

    declaring_released_at: datetime | None
    declared_about_released_at: datetime | None
    declared_about_eol_at: datetime | None
    declaring_yanked: YankedInfo | None
    declared_about_yanked: YankedInfo | None


@dataclass(frozen=True, slots=True)
class SourceCheck:
    """What the server opened and what came back.

    Passed *into* the decision procedure so that ``lookup_failed`` is derived from an
    explicit value rather than from a caught exception, and so the ``sources_checked``
    the caller reads is the very value the verdict was computed from.
    """

    source: SourceId
    outcome: LookupOutcome
    required: bool = True
    detail: str | None = None


def source_check_sort_key(check: SourceCheck) -> tuple[int, str]:
    return (SOURCE_IDS.index(check.source), check.outcome)
