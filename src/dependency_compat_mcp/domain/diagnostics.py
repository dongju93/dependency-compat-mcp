"""Closed code sets for what was observed but does not change the verdict, for what could
not be checked at all, and for what actually left the verdict open.

03 insists these be codes rather than prose: a caller has to be able to branch on them,
and they have to be computed rather than written. Keeping them apart is the point -
``notices`` are confirmed facts, ``limitations`` are unchecked scope, ``decision_causes``
are the reasons a verdict could not be reached, and mixing them into one "caveats" list
would make an honest ``unknown`` indistinguishable from hedging.

The third set is the newest and the one that carries the most weight. A ``limitation`` is
coverage the server did not have (``curated_pack_missing`` is true of almost every
response today); a :data:`DecisionCause` is the thing that *produced* this particular
``unknown``. Reading the first as if it were the second is exactly the confusion this
split removes - and it is why a cause carries its own evidence ids and, where one exists,
the verbatim marker, rather than a bare code the caller has to re-interpret.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal, assert_never

from dependency_compat_mcp.domain.claims import EvidenceId, MarkerCondition
from dependency_compat_mcp.domain.errors import InvariantViolation

__all__ = [
    "CAUSE_KINDS",
    "LIMITATION_CODES",
    "NOTICE_CODES",
    "CauseKind",
    "ClaimGuard",
    "ConditionalClaim",
    "DecisionCause",
    "GuardKind",
    "Limitation",
    "LimitationCode",
    "Notice",
    "NoticeCode",
    "UnprovenClaim",
    "UnprovenKind",
    "cause_evidence_ids",
    "cause_kind",
    "guard_of",
    "sorted_causes",
    "sorted_limitations",
    "sorted_notices",
]

# Coverage only. What actually left a verdict open is a `DecisionCause`, never a code
# here: `open_upper_bound`, `stale_lower_bound` and `tier_c_only` used to live in this set
# and were produced on the unknown path alone, so keeping them would have stated the same
# fact twice in two shapes that could drift.
#
# `marker_guarded_claim` and `extra_guarded_claim` stay, because they are *not* confined to
# the unknown path: a target can be named by both a guarded and an unguarded declaration,
# and then the verdict is decided by the unguarded one while the guarded one remains
# unverified scope. `Supported` carries no causes, so this is the only place that says so.
type LimitationCode = Literal[
    "curated_pack_missing",
    "curated_not_verified_for_version",
    "marker_guarded_claim",
    "extra_guarded_claim",
    "source_unavailable",
]

type NoticeCode = Literal[
    "subject_yanked",
    "counterpart_yanked",
    "gate_contradicts_statement",
]

LIMITATION_CODES: Final[tuple[LimitationCode, ...]] = (
    "curated_pack_missing",
    "curated_not_verified_for_version",
    "marker_guarded_claim",
    "extra_guarded_claim",
    "source_unavailable",
)

NOTICE_CODES: Final[tuple[NoticeCode, ...]] = (
    "subject_yanked",
    "counterpart_yanked",
    "gate_contradicts_statement",
)


@dataclass(frozen=True, slots=True)
class Limitation:
    """Scope the server did *not* verify."""

    code: LimitationCode


@dataclass(frozen=True, slots=True)
class Notice:
    """A confirmed fact that does not move the verdict, with the sources it rests on."""

    code: NoticeCode
    evidence_ids: tuple[EvidenceId, ...] = field(default=())


def sorted_limitations(limitations: Iterable[Limitation]) -> tuple[Limitation, ...]:
    """De-duplicate and order by the declared code order, for byte-stable output."""
    unique = {limitation.code: limitation for limitation in limitations}
    return tuple(unique[code] for code in LIMITATION_CODES if code in unique)


def sorted_notices(notices: Iterable[Notice]) -> tuple[Notice, ...]:
    """Merge notices sharing a code, union their evidence, and order by the code order."""
    merged: dict[NoticeCode, set[EvidenceId]] = {}
    for notice in notices:
        merged.setdefault(notice.code, set()).update(notice.evidence_ids)
    return tuple(
        Notice(code=code, evidence_ids=tuple(sorted(merged[code])))
        for code in NOTICE_CODES
        if code in merged
    )


# --------------------------------------------------------------------------------------
# Decision causes
# --------------------------------------------------------------------------------------

type GuardKind = Literal["environment_marker", "extra_marker"]

# Every cause kind except the conditional one, which is a separate type because it
# carries a marker. Named so the summary table can be keyed by it and be *total*: adding
# a kind without a sentence for it then fails to type-check rather than at request time.
type UnprovenKind = Literal[
    "open_upper_bound",
    "stale_lower_bound",
    "tier_c_only",
    "claim_outside_range",
    "uncomparable_claim",
]

type CauseKind = Literal["conditional_claim"] | UnprovenKind

# Priority order, most actionable to the caller first, and the tie-break that makes the
# rendered summary deterministic. A conditional claim leads because the caller can settle
# it by naming an environment or an extra; the two bound rules and the tier-C rule follow
# because they describe evidence that exists but stops short; the last two report claims
# that were read and decided nothing.
CAUSE_KINDS: Final[tuple[CauseKind, ...]] = (
    "conditional_claim",
    "open_upper_bound",
    "stale_lower_bound",
    "tier_c_only",
    "claim_outside_range",
    "uncomparable_claim",
)

_CAUSE_ORDER: Final[dict[CauseKind, int]] = {
    kind: index for index, kind in enumerate(CAUSE_KINDS)
}


@dataclass(frozen=True, slots=True)
class ClaimGuard:
    """The undecided condition on a claim, carried verbatim.

    Only the two *undecidable* markers can appear. A marker that names no environment
    variable is settled during classification and never reaches a cause, so "a conditional
    claim whose condition is already decided" has no representation here.
    """

    kind: GuardKind
    expression: str
    variables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConditionalClaim:
    """A claim that neither applies nor drops out until an environment is supplied."""

    condition: ClaimGuard
    evidence_ids: tuple[EvidenceId, ...]
    kind: Literal["conditional_claim"] = "conditional_claim"

    def __post_init__(self) -> None:
        _require_evidence(self.kind, self.evidence_ids)


@dataclass(frozen=True, slots=True)
class UnprovenClaim:
    """Evidence was found and read, but it stops short of settling the question."""

    kind: UnprovenKind
    evidence_ids: tuple[EvidenceId, ...]

    def __post_init__(self) -> None:
        _require_evidence(self.kind, self.evidence_ids)


def _require_evidence(kind: CauseKind, evidence_ids: tuple[EvidenceId, ...]) -> None:
    """A cause with no source behind it is the same defect as an unevidenced verdict.

    ``Supported`` and ``Unsupported`` already refuse it, and a cause is held to the same
    rule: the whole point of the field is to hand the caller something to go read, so
    "the evidence was insufficient, and here is nothing to look at" must not be
    constructible. Enforcing it only in the response model would have made it a
    serialisation failure instead of an impossible value.
    """
    if not evidence_ids:
        raise InvariantViolation(f"decision cause {kind!r} must cite an evidence id")


type DecisionCause = ConditionalClaim | UnprovenClaim


def cause_kind(cause: DecisionCause) -> CauseKind:
    match cause:
        case ConditionalClaim():
            return "conditional_claim"
        case UnprovenClaim(kind=kind):
            return kind
        case _:
            assert_never(cause)


def cause_evidence_ids(cause: DecisionCause) -> tuple[EvidenceId, ...]:
    match cause:
        case ConditionalClaim(evidence_ids=ids) | UnprovenClaim(evidence_ids=ids):
            return ids
        case _:
            assert_never(cause)


def guard_of(condition: MarkerCondition) -> ClaimGuard | None:
    """Project a marker onto a guard, or ``None`` when the marker is already decided."""
    match condition.decidability:
        case "environment_dependent":
            kind: GuardKind = "environment_marker"
        case "extra_guarded":
            kind = "extra_marker"
        case "always_true" | "always_false":
            return None
        case never:
            assert_never(never)
    return ClaimGuard(
        kind=kind, expression=condition.expression, variables=condition.variables
    )


def _cause_identity(cause: DecisionCause) -> tuple[CauseKind, str]:
    match cause:
        case ConditionalClaim(condition=condition):
            # Two different markers are two different reasons, so the expression is part
            # of the identity. Merging them would hide one of them from the caller.
            return ("conditional_claim", condition.expression)
        case UnprovenClaim(kind=kind):
            return (kind, "")
        case _:
            assert_never(cause)


def _with_evidence(
    cause: DecisionCause, evidence_ids: tuple[EvidenceId, ...]
) -> DecisionCause:
    match cause:
        case ConditionalClaim(condition=condition):
            return ConditionalClaim(condition=condition, evidence_ids=evidence_ids)
        case UnprovenClaim(kind=kind):
            return UnprovenClaim(kind=kind, evidence_ids=evidence_ids)
        case _:
            assert_never(cause)


def sorted_causes(causes: Iterable[DecisionCause]) -> tuple[DecisionCause, ...]:
    """Merge causes sharing an identity, union their evidence, and order by priority.

    The order is the one :mod:`dependency_compat_mcp.domain.summaries` reads to pick the
    sentence, so it is fixed here rather than at the rendering site: the same facts must
    produce the same first cause and therefore the same summary.
    """
    merged: dict[tuple[CauseKind, str], DecisionCause] = {}
    evidence: dict[tuple[CauseKind, str], list[EvidenceId]] = {}
    for cause in causes:
        identity = _cause_identity(cause)
        merged.setdefault(identity, cause)
        seen = evidence.setdefault(identity, [])
        for identifier in cause_evidence_ids(cause):
            if identifier not in seen:
                seen.append(identifier)

    def order(identity: tuple[CauseKind, str]) -> tuple[int, str]:
        return (_CAUSE_ORDER[identity[0]], identity[1])

    return tuple(
        _with_evidence(merged[identity], tuple(evidence[identity]))
        for identity in sorted(merged, key=order)
    )
