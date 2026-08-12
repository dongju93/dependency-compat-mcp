"""The decision procedure (03 [5]): a pure, total function over a valid input.

The whole of steps 0-7 lives here and nothing else does. The function knows nothing about
networks, clocks, randomness or models; an adapter has already turned raw JSON into
:class:`~dependency_compat_mcp.domain.claims.Claim` values and
:class:`~dependency_compat_mcp.domain.claims.SourceCheck` records, so the same input always
produces the same verdict. That is what makes the SDK cache hints of 01 meaningful and what
makes the determinism test of 03 able to fail if a model is ever inserted into this path.

Three structural decisions carry most of the correctness:

* The result is a sum type. ``Supported``/``Unsupported`` *cannot be constructed* without
  at least one supporting evidence id, and ``Unknown`` has no evidence-id field at all, so
  04's "a verdict is never unevidenced" invariant has no representable counterexample.
* Claim classification is a separate, closed step. Every claim lands in exactly one bucket
  (or is dropped because its own marker rules it out), and the verdict steps only read
  buckets - so "which evidence supported the verdict" is never re-derived later.
* Anything outside the documented combinations raises
  :class:`~dependency_compat_mcp.domain.errors.InvariantViolation` rather than degrading to
  ``unknown``. An internal defect must not be able to hide inside a normal result.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

from dependency_compat_mcp.domain.claims import (
    Claim,
    CompatibilityStatement,
    Corroboration,
    EvidenceId,
    InstallationGate,
    ReleaseFacts,
    SourceCheck,
)
from dependency_compat_mcp.domain.diagnostics import (
    Limitation,
    LimitationCode,
    Notice,
    sorted_limitations,
    sorted_notices,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.relations import ResolvedRelation
from dependency_compat_mcp.domain.targets import ExactVersion, TargetId, version_of
from dependency_compat_mcp.domain.versions import admits, release_tuple

__all__ = [
    "CuratedCoverage",
    "EvaluationInput",
    "Supported",
    "Unknown",
    "UnknownReason",
    "Unsupported",
    "Verdict",
    "coverage_limitations",
    "evaluate",
]

type UnknownReason = Literal[
    "release_not_found",
    "lookup_failed",
    "relation_not_supported",
    "conflicting_evidence",
    "insufficient_evidence",
    "evidence_not_found",
    "no_declared_relationship",
]

type _Bucket = Literal[
    "gate_violated",
    "gate_satisfied",
    "stated_excludes",
    "stated_includes",
    "corroborates",
    "indeterminate",
]


# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CuratedCoverage:
    """What the curated pack had to say about the declaring target.

    Kept separate from the claims themselves because absence of a curated entry is not a
    claim - it is unchecked scope, and 03 reports it as a ``limitation``.
    """

    entry_present: bool
    verified_for_version: bool


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    """Everything the decision procedure is allowed to see, already normalised.

    The constructor refuses the one combination the rest of this module would otherwise
    have to defend against on every read: a claim that talks about some *other* target than
    the relation's ``declared_about``. Such an input is an adapter defect, not thin
    evidence, so it must be impossible to hand to :func:`evaluate`.
    """

    relation: ResolvedRelation
    claims: tuple[Claim, ...]
    facts: ReleaseFacts
    lookups: tuple[SourceCheck, ...]
    curated: CuratedCoverage
    declaring_release_found: bool
    declared_about_release_found: bool

    def __post_init__(self) -> None:
        expected = TargetId.of(self.relation.declared_about)
        for claim in self.claims:
            if claim.declared_about != expected:
                raise InvariantViolation(
                    f"claim {_claim_evidence_id(claim)!r} is about "
                    f"{claim.declared_about!r}, but the relation is declared about "
                    f"{expected!r}; the adapter normalised the claim for a different "
                    "relation."
                )


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Supported:
    """Explicit evidence admits the pair. Never constructible without that evidence."""

    verdict_evidence_ids: tuple[EvidenceId, ...]
    notices: tuple[Notice, ...]
    limitations: tuple[Limitation, ...]

    def __post_init__(self) -> None:
        if not self.verdict_evidence_ids:
            raise InvariantViolation(
                "a supported verdict must cite at least one evidence id"
            )


@dataclass(frozen=True, slots=True)
class Unsupported:
    """Explicit evidence rejects the pair. Never constructible without that evidence."""

    verdict_evidence_ids: tuple[EvidenceId, ...]
    notices: tuple[Notice, ...]
    limitations: tuple[Limitation, ...]

    def __post_init__(self) -> None:
        if not self.verdict_evidence_ids:
            raise InvariantViolation(
                "an unsupported verdict must cite at least one evidence id"
            )


@dataclass(frozen=True, slots=True)
class Unknown:
    """Neither side is provable from what was found. A normal result, not a failure.

    It has no ``verdict_evidence_ids`` field: an ``unknown`` that cited supporting evidence
    would be a contradiction, so the type does not offer the field to fill in.
    """

    reason: UnknownReason
    notices: tuple[Notice, ...]
    limitations: tuple[Limitation, ...]


type Verdict = Supported | Unsupported | Unknown


# --------------------------------------------------------------------------------------
# Step 1: claim classification
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Applies:
    """The claim says something about the exact version under evaluation."""

    bucket: _Bucket
    evidence_id: EvidenceId
    limitation: LimitationCode | None = None
    closes_ceiling: bool = False


@dataclass(frozen=True, slots=True)
class _DoesNotApply:
    """The claim's own condition removes it from the question entirely.

    Distinct from ``indeterminate``: a marker that is false in every environment does not
    leave the answer open, it means the dependency is simply not part of this install. No
    limitation is recorded, because nothing went unchecked.
    """


type _Outcome = _Applies | _DoesNotApply


@dataclass(frozen=True, slots=True)
class _Buckets:
    """Step 1's output: claim evidence ids grouped by what they establish."""

    gate_violated: tuple[EvidenceId, ...]
    gate_satisfied: tuple[EvidenceId, ...]
    stated_excludes: tuple[EvidenceId, ...]
    stated_includes: tuple[EvidenceId, ...]
    corroborates: tuple[EvidenceId, ...]
    indeterminate: tuple[EvidenceId, ...]
    closed_ceiling: bool
    limitations: tuple[LimitationCode, ...]


def _claim_evidence_id(claim: Claim) -> EvidenceId:
    match claim:
        case InstallationGate(evidence_id=identifier):
            return identifier
        case CompatibilityStatement(evidence_id=identifier):
            return identifier
        case Corroboration(evidence_id=identifier):
            return identifier
        case _:
            assert_never(claim)


def _classify_gate(gate: InstallationGate, version: ExactVersion) -> _Outcome:
    condition = gate.condition
    if condition is not None:
        match condition.decidability:
            case "always_false":
                return _DoesNotApply()
            case "environment_dependent":
                # The server is never given the environment that would settle the marker
                # (02), and guessing a truth value would be invisible to the caller.
                return _Applies(
                    bucket="indeterminate",
                    evidence_id=gate.evidence_id,
                    limitation="marker_guarded_claim",
                )
            case "extra_guarded":
                return _Applies(
                    bucket="indeterminate",
                    evidence_id=gate.evidence_id,
                    limitation="extra_guarded_claim",
                )
            case "always_true":
                pass
            case never:
                assert_never(never)

    admitted = admits(gate.expression, gate.scheme, version)
    if admitted is None:
        # Different schemes, or an expression this ecosystem's parser rejects. Comparing
        # across schemes is forbidden by 03, so the claim decides nothing here.
        return _Applies(bucket="indeterminate", evidence_id=gate.evidence_id)
    if admitted:
        return _Applies(
            bucket="gate_satisfied",
            evidence_id=gate.evidence_id,
            closes_ceiling=gate.bounded_above,
        )
    return _Applies(bucket="gate_violated", evidence_id=gate.evidence_id)


def _classify_statement(
    statement: CompatibilityStatement, version: ExactVersion
) -> _Outcome:
    admitted = admits(statement.expression, statement.scheme, version)
    if admitted is None or not admitted:
        # A range that does not cover the version says nothing *about* that version.
        # Reading "supports >=3.10,<3.14" as an exclusion of 3.14 would be absence-as-
        # evidence, which 03 forbids outright.
        return _Applies(bucket="indeterminate", evidence_id=statement.evidence_id)
    match statement.stance:
        case "supports":
            return _Applies(bucket="stated_includes", evidence_id=statement.evidence_id)
        case "excludes":
            return _Applies(bucket="stated_excludes", evidence_id=statement.evidence_id)
        case never:
            assert_never(never)


def _dotted_release(entry: str) -> tuple[int, ...] | None:
    """Parse an enumerated version label into its numeric release, or ``None``.

    Tier C labels (PyPI classifiers and the like) are plain strings with no scheme
    attached, so they are read numerically rather than through either ecosystem's parser.
    """
    parts = entry.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def _classify_corroboration(
    corroboration: Corroboration, version: ExactVersion
) -> _Outcome:
    raw = str(version)
    enumerated = raw in corroboration.enumerated_versions
    if not enumerated:
        release = release_tuple(version)
        for entry in corroboration.enumerated_versions:
            parsed = _dotted_release(entry)
            # `3.13` enumerates `3.13.1`; `3.1` does not, because the comparison is on
            # release components rather than on the printed string.
            if parsed is not None and release[: len(parsed)] == parsed:
                enumerated = True
                break
    if not enumerated:
        # Absence from an enumeration is never evidence against (03, absolute rule 2).
        return _DoesNotApply()
    return _Applies(bucket="corroborates", evidence_id=corroboration.evidence_id)


def _classify(claim: Claim, version: ExactVersion) -> _Outcome:
    match claim:
        case InstallationGate():
            return _classify_gate(claim, version)
        case CompatibilityStatement():
            return _classify_statement(claim, version)
        case Corroboration():
            return _classify_corroboration(claim, version)
        case _:
            assert_never(claim)


def _classify_all(claims: Sequence[Claim], version: ExactVersion) -> _Buckets:
    grouped: dict[_Bucket, list[EvidenceId]] = {
        "gate_violated": [],
        "gate_satisfied": [],
        "stated_excludes": [],
        "stated_includes": [],
        "corroborates": [],
        "indeterminate": [],
    }
    limitations: list[LimitationCode] = []
    closed_ceiling = False

    for claim in claims:
        outcome = _classify(claim, version)
        match outcome:
            case _DoesNotApply():
                continue
            case _Applies(bucket=bucket, evidence_id=identifier):
                if identifier not in grouped[bucket]:
                    grouped[bucket].append(identifier)
                if outcome.limitation is not None:
                    limitations.append(outcome.limitation)
                # One closed ceiling bounds the conjunction of gates, so `any` is the
                # right reading of "the author enumerated the supported range".
                closed_ceiling = closed_ceiling or outcome.closes_ceiling
            case _:
                assert_never(outcome)

    return _Buckets(
        gate_violated=tuple(grouped["gate_violated"]),
        gate_satisfied=tuple(grouped["gate_satisfied"]),
        stated_excludes=tuple(grouped["stated_excludes"]),
        stated_includes=tuple(grouped["stated_includes"]),
        corroborates=tuple(grouped["corroborates"]),
        indeterminate=tuple(grouped["indeterminate"]),
        closed_ceiling=closed_ceiling,
        limitations=tuple(limitations),
    )


# --------------------------------------------------------------------------------------
# Diagnostics shared with `get_compatibility_context`
# --------------------------------------------------------------------------------------


def coverage_limitations(
    curated: CuratedCoverage, lookups: Iterable[SourceCheck]
) -> tuple[Limitation, ...]:
    """Limitations derivable from coverage alone, independent of any verdict.

    Both tools compute these the same way on purpose (03): what the server failed to open
    is a fact about the request, not about the answer, so it must not change shape between
    ``check_compatibility`` and ``get_compatibility_context``.
    """
    limitations: list[Limitation] = []
    if not curated.entry_present:
        limitations.append(Limitation("curated_pack_missing"))
    elif not curated.verified_for_version:
        limitations.append(Limitation("curated_not_verified_for_version"))

    for check in lookups:
        # A required source that failed is reported as `lookup_failed`, not as a
        # limitation; these two cases are the ones that leave optional scope unchecked.
        if (not check.required and check.outcome == "failed") or (
            check.required and check.outcome == "skipped"
        ):
            limitations.append(Limitation("source_unavailable"))
            break

    return tuple(limitations)


def _release_state_notices(
    relation: ResolvedRelation, facts: ReleaseFacts
) -> list[Notice]:
    """Map yanked releases onto the *caller's* subject/counterpart vocabulary.

    The notice codes are relative to the argument order the caller used, while the facts
    are relative to the declaring side. Under ``direction = "reversed"`` those differ, so
    the mapping is computed rather than assumed.
    """
    notices: list[Notice] = []
    declaring_is_subject = relation.declaring == relation.subject
    if facts.declaring_yanked is not None:
        notices.append(
            Notice("subject_yanked" if declaring_is_subject else "counterpart_yanked")
        )
    if facts.declared_about_yanked is not None:
        notices.append(
            Notice("counterpart_yanked" if declaring_is_subject else "subject_yanked")
        )
    return notices


# --------------------------------------------------------------------------------------
# Result constructors
# --------------------------------------------------------------------------------------


def _dedup(identifiers: Iterable[EvidenceId]) -> tuple[EvidenceId, ...]:
    seen: dict[EvidenceId, None] = {}
    for identifier in identifiers:
        seen.setdefault(identifier, None)
    return tuple(seen)


def _supported(
    evidence_ids: Iterable[EvidenceId],
    notices: Iterable[Notice],
    limitations: Iterable[Limitation],
) -> Supported:
    return Supported(
        verdict_evidence_ids=_dedup(evidence_ids),
        notices=sorted_notices(notices),
        limitations=sorted_limitations(limitations),
    )


def _unsupported(
    evidence_ids: Iterable[EvidenceId],
    notices: Iterable[Notice],
    limitations: Iterable[Limitation],
) -> Unsupported:
    return Unsupported(
        verdict_evidence_ids=_dedup(evidence_ids),
        notices=sorted_notices(notices),
        limitations=sorted_limitations(limitations),
    )


def _unknown(
    reason: UnknownReason,
    notices: Iterable[Notice],
    limitations: Iterable[Limitation],
) -> Unknown:
    return Unknown(
        reason=reason,
        notices=sorted_notices(notices),
        limitations=sorted_limitations(limitations),
    )


# --------------------------------------------------------------------------------------
# Steps 5 and 6
# --------------------------------------------------------------------------------------


def _declared_about_is_newer(facts: ReleaseFacts) -> bool:
    """Did the counterpart appear only after the declaring release was published?

    ``None`` on either side means the question cannot be asked, and an unanswerable
    question must not fire a branch - so the check is written to fail closed.
    """
    if facts.declaring_released_at is None or facts.declared_about_released_at is None:
        return False
    return facts.declared_about_released_at > facts.declaring_released_at


def _declared_about_was_eol(facts: ReleaseFacts) -> bool:
    """Was the counterpart already end-of-life when the declaring release shipped?

    Relations with no release table (``pypi`` x ``pypi``) carry no ``eol_at``, so this
    branch never fires there and step 5 short-circuits to ``supported``.
    """
    if facts.declaring_released_at is None or facts.declared_about_eol_at is None:
        return False
    return facts.declared_about_eol_at <= facts.declaring_released_at


def _step_five(
    buckets: _Buckets,
    facts: ReleaseFacts,
    notices: Sequence[Notice],
    limitations: Sequence[Limitation],
) -> Verdict:
    gates = buckets.gate_satisfied
    corroborating = _dedup(buckets.stated_includes + buckets.corroborates)

    if buckets.closed_ceiling:
        # An enumerated range *is* the statement: the author named where support stops.
        return _supported(gates, notices, limitations)

    if _declared_about_is_newer(facts):
        if corroborating:
            return _supported(gates + corroborating, notices, limitations)
        return _unknown(
            "insufficient_evidence",
            notices,
            [*limitations, Limitation("open_upper_bound")],
        )

    if _declared_about_was_eol(facts):
        # The mirror image of the open ceiling: a runtime already dead at release time was
        # no more verified by the author than one that did not yet exist.
        if corroborating:
            return _supported(gates + corroborating, notices, limitations)
        return _unknown(
            "insufficient_evidence",
            notices,
            [*limitations, Limitation("stale_lower_bound")],
        )

    return _supported(gates, notices, limitations)


def _step_six(
    relation: ResolvedRelation,
    buckets: _Buckets,
    notices: Sequence[Notice],
    limitations: Sequence[Limitation],
) -> Verdict:
    if buckets.stated_includes:
        return _supported(buckets.stated_includes, notices, limitations)

    if buckets.corroborates:
        # Absolute rule 3 of 03, expressed as an outcome: tier C alone never carries a
        # verdict, however many positive labels it enumerates.
        return _unknown(
            "insufficient_evidence", notices, [*limitations, Limitation("tier_c_only")]
        )

    if buckets.indeterminate:
        return _unknown("insufficient_evidence", notices, limitations)

    if relation.rule.declares_dependency:
        # "These two packages are unrelated" is a different answer than "nothing was
        # found", and 04 keeps them apart because they lead to opposite user advice.
        return _unknown("no_declared_relationship", notices, limitations)

    return _unknown("evidence_not_found", notices, limitations)


# --------------------------------------------------------------------------------------
# The decision procedure
# --------------------------------------------------------------------------------------


def evaluate(evaluation: EvaluationInput) -> Verdict:
    """Decide one exact-version pair. Total over every valid :class:`EvaluationInput`."""
    relation = evaluation.relation
    facts = evaluation.facts
    notices = _release_state_notices(relation, facts)
    limitations = list(coverage_limitations(evaluation.curated, evaluation.lookups))

    # Step 0. Failure is checked before absence: reporting "no such release" while a
    # required source never answered would turn a failed lookup into a factual claim.
    if any(
        check.outcome == "failed" and check.required for check in evaluation.lookups
    ):
        return _unknown("lookup_failed", notices, limitations)
    if not (
        evaluation.declaring_release_found and evaluation.declared_about_release_found
    ):
        return _unknown("release_not_found", notices, limitations)

    # Step 1.
    buckets = _classify_all(evaluation.claims, version_of(relation.declared_about))
    limitations.extend(Limitation(code) for code in buckets.limitations)

    # Step 2. A violated gate is a mechanical fact, not an opinion, so it outranks any
    # statement to the contrary - which is preserved in a notice rather than suppressed.
    if buckets.gate_violated:
        if buckets.stated_includes:
            notices.append(
                Notice(
                    "gate_contradicts_statement",
                    evidence_ids=_dedup(
                        buckets.gate_violated + buckets.stated_includes
                    ),
                )
            )
        return _unsupported(buckets.gate_violated, notices, limitations)

    # Step 3. Only tier B against tier B is a conflict the server refuses to resolve.
    if buckets.stated_includes and buckets.stated_excludes:
        return _unknown("conflicting_evidence", notices, limitations)

    # Step 4.
    if buckets.stated_excludes:
        return _unsupported(buckets.stated_excludes, notices, limitations)

    # Step 5.
    if buckets.gate_satisfied:
        return _step_five(buckets, facts, notices, limitations)

    # Step 6.
    if not buckets.gate_violated:
        return _step_six(relation, buckets, notices, limitations)

    # Step 7. Steps 0-6 exhaust every combination a valid input can produce, so arriving
    # here means a constructor let through a state it should have rejected. That is a
    # server defect and is surfaced as a tool error, never as a normal `unknown`.
    raise InvariantViolation(  # pragma: no cover - unreachable by construction
        "the decision procedure fell through every documented step"
    )
