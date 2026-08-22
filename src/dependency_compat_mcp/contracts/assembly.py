"""Domain values -> public response models (03 [6]).

Two jobs live here and nowhere else.

**Renumbering.** Adapters mint stable internal evidence ids (``pypi:requires_python``,
``curated:pypi:django:statement:0``) so their output is deterministic and diffable. The
public response uses ``evidence-1``, ``evidence-2``, ... in the 04 order. Doing the
renumbering in one place means the ids in ``verdict_evidence_ids``, ``notices[]`` and
``constraints[]`` cannot drift apart from the catalogue.

**Pruning.** 04 says the catalogue holds "only the sources actually used in this response".
The caller passes the set of ids the response references; anything else is dropped rather
than shipped as decoration.

Reference integrity is then re-checked by the models themselves. If assembly produced a
dangling id the model raises, and the MCP layer turns that into a tool error - never into a
quiet ``unknown`` (03 step 7).
"""

from collections.abc import Iterable, Mapping
from typing import assert_never

from dependency_compat_mcp.contracts.outputs import (
    ChangeOut,
    CheckCompatibilityResult,
    ConditionalClaimOut,
    ConditionOut,
    ConstraintOut,
    ContextAvailableResult,
    ContextUnknownResult,
    CuratedProvenanceOut,
    DecidedMarkerOut,
    DecisionCauseOut,
    EvidenceOut,
    FetchedProvenanceOut,
    GetCompatibilityContextResult,
    LimitationOut,
    MarkerGuardOut,
    NarrativeEvidenceOut,
    NoticeOut,
    RelationOut,
    ResolvedRelationOut,
    SourceCheckOut,
    SupportedResult,
    TargetIdOut,
    TargetOut,
    UnconditionalOut,
    UnknownResult,
    UnprovenClaimOut,
    UnsupportedRelationOut,
    UnsupportedResult,
    VersionConstraintEvidenceOut,
)
from dependency_compat_mcp.domain.claims import (
    Curated,
    Evidence,
    EvidenceId,
    Fetched,
    MarkerCondition,
    NarrativeEvidence,
    Provenance,
    SourceCheck,
    VersionConstraintEvidence,
    evidence_sort_key,
    source_check_sort_key,
)
from dependency_compat_mcp.domain.context import (
    ContextAvailable,
    ContextChange,
    ContextConstraint,
    ContextOutcome,
    ContextUnknown,
)
from dependency_compat_mcp.domain.diagnostics import (
    ConditionalClaim,
    DecisionCause,
    Limitation,
    Notice,
    UnprovenClaim,
    cause_evidence_ids,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.evaluate import (
    Supported,
    Unknown,
    Unsupported,
    Verdict,
)
from dependency_compat_mcp.domain.relations import (
    RelationResolution,
    ResolvedRelation,
    UnsupportedRelation,
)
from dependency_compat_mcp.domain.targets import (
    Target,
    TargetId,
    name_of,
    namespace_of,
    version_of,
)

__all__ = [
    "build_check_result",
    "build_context_result",
    "condition_out",
    "target_out",
]


def target_out(target: Target) -> TargetOut:
    """The canonical identity actually evaluated - never a nearby release."""
    return TargetOut(
        namespace=namespace_of(target),
        name=str(name_of(target)),
        version=str(version_of(target)),
    )


def _target_id_out(identity: TargetId) -> TargetIdOut:
    return TargetIdOut(namespace=identity.namespace, name=str(identity.name))


def _provenance_out(
    provenance: Provenance,
) -> FetchedProvenanceOut | CuratedProvenanceOut:
    match provenance:
        case Fetched(retrieved_at=retrieved_at):
            return FetchedProvenanceOut(retrieved_at=retrieved_at)
        case Curated(reviewed_at=reviewed_at, pack_version=pack_version):
            return CuratedProvenanceOut(
                reviewed_at=reviewed_at, pack_version=pack_version
            )
        case _:
            assert_never(provenance)


def _relation_out(resolution: RelationResolution) -> RelationOut:
    match resolution:
        case ResolvedRelation(
            rule=rule,
            direction=direction,
            declaring=declaring,
            declared_about=declared_about,
        ):
            return ResolvedRelationOut(
                rule=rule.name,
                direction=direction,
                declaring=target_out(declaring),
                declared_about=target_out(declared_about),
            )
        case UnsupportedRelation(subject=subject, counterpart=counterpart):
            return UnsupportedRelationOut(
                subject=target_out(subject), counterpart=target_out(counterpart)
            )
        case _:
            assert_never(resolution)


def _renumber(
    catalogue: Iterable[Evidence], referenced: Iterable[EvidenceId]
) -> tuple[tuple[EvidenceOut, ...], Mapping[EvidenceId, str]]:
    """Prune to what the response references, order it stably, and assign public ids."""
    wanted = set(referenced)
    by_id: dict[EvidenceId, Evidence] = {}
    for item in catalogue:
        if item.id in wanted:
            by_id[item.id] = item

    missing = sorted(wanted - by_id.keys())
    if missing:
        # A reference with no source behind it is a server defect, not bad external data.
        raise InvariantViolation(
            f"response references evidence that was never collected: {', '.join(missing)}"
        )

    ordered = sorted(by_id.values(), key=evidence_sort_key)
    mapping = {
        item.id: f"evidence-{index}" for index, item in enumerate(ordered, start=1)
    }
    return tuple(_evidence_out(item, mapping[item.id]) for item in ordered), mapping


def _evidence_out(evidence: Evidence, public_id: str) -> EvidenceOut:
    match evidence:
        case VersionConstraintEvidence():
            return VersionConstraintEvidenceOut(
                id=public_id,
                source_type=evidence.source_type,
                title=evidence.title,
                url=evidence.url,
                substantiates=evidence.substantiates,
                expression=evidence.expression,
                scheme=evidence.scheme,
                provenance=_provenance_out(evidence.provenance),
            )
        case NarrativeEvidence():
            return NarrativeEvidenceOut(
                id=public_id,
                source_type=evidence.source_type,
                title=evidence.title,
                url=evidence.url,
                substantiates=evidence.substantiates,
                provenance=_provenance_out(evidence.provenance),
            )
        case _:
            assert_never(evidence)


def _notices_out(
    notices: Iterable[Notice], mapping: Mapping[EvidenceId, str]
) -> tuple[NoticeOut, ...]:
    return tuple(
        NoticeOut(
            code=notice.code,
            evidence_ids=tuple(
                mapping[identifier] for identifier in notice.evidence_ids
            ),
        )
        for notice in notices
    )


def _limitations_out(limitations: Iterable[Limitation]) -> tuple[LimitationOut, ...]:
    return tuple(LimitationOut(code=limitation.code) for limitation in limitations)


def condition_out(condition: MarkerCondition | None) -> ConditionOut:
    """Project a claim's marker onto the public condition union.

    The four ``decidability`` values map onto three shapes rather than being flattened to
    "has a marker or not": a caller has to be able to tell "no marker" from "a marker that
    evaluates to false for everyone", and neither of those is "a marker I must decide".
    """
    if condition is None:
        return UnconditionalOut()
    match condition.decidability:
        case "environment_dependent":
            return MarkerGuardOut(
                kind="environment_marker",
                expression=condition.expression,
                variables=condition.variables,
            )
        case "extra_guarded":
            return MarkerGuardOut(
                kind="extra_marker",
                expression=condition.expression,
                variables=condition.variables,
            )
        case "always_true":
            return DecidedMarkerOut(expression=condition.expression, holds=True)
        case "always_false":
            return DecidedMarkerOut(expression=condition.expression, holds=False)
        case never:
            assert_never(never)


def _causes_out(
    causes: Iterable[DecisionCause], mapping: Mapping[EvidenceId, str]
) -> tuple[DecisionCauseOut, ...]:
    out: list[DecisionCauseOut] = []
    for cause in causes:
        public_ids = tuple(mapping[i] for i in cause_evidence_ids(cause))
        match cause:
            case ConditionalClaim(condition=condition):
                out.append(
                    ConditionalClaimOut(
                        condition=MarkerGuardOut(
                            kind=condition.kind,
                            expression=condition.expression,
                            variables=condition.variables,
                        ),
                        evidence_ids=public_ids,
                    )
                )
            case UnprovenClaim(kind=kind):
                out.append(UnprovenClaimOut(kind=kind, evidence_ids=public_ids))
            case _:
                assert_never(cause)
    return tuple(out)


def _sources_out(sources: Iterable[SourceCheck]) -> tuple[SourceCheckOut, ...]:
    return tuple(
        SourceCheckOut(
            source=check.source,
            target=target_out(check.target),
            role=check.role,
            required=check.required,
            outcome=check.outcome,
            detail=check.detail,
        )
        for check in sorted(sources, key=source_check_sort_key)
    )


def build_check_result(
    *,
    subject: Target,
    counterpart: Target,
    resolution: RelationResolution,
    verdict: Verdict,
    summary: str,
    evidence: Iterable[Evidence],
    referenced_evidence_ids: Iterable[EvidenceId],
    sources: Iterable[SourceCheck],
) -> CheckCompatibilityResult:
    """Assemble a ``check_compatibility`` response from domain values.

    ``referenced_evidence_ids`` is the union of the verdict's own evidence, every notice's
    evidence, every decision cause's evidence, and the evidence behind the claims that were
    considered. Including the last group is what lets an ``unknown / conflicting_evidence``
    return *both* sides of the conflict, which 04 requires.
    """
    catalogue = list(evidence)
    notices = tuple(verdict.notices)
    referenced = set(referenced_evidence_ids)
    for notice in notices:
        referenced.update(notice.evidence_ids)
    match verdict:
        case (
            Supported(verdict_evidence_ids=ids) | Unsupported(verdict_evidence_ids=ids)
        ):
            referenced.update(ids)
        case Unknown(causes=causes):
            for cause in causes:
                referenced.update(cause_evidence_ids(cause))
        case _:
            assert_never(verdict)

    evidence_out, mapping = _renumber(catalogue, referenced)
    shared = {
        "subject": target_out(subject),
        "counterpart": target_out(counterpart),
        "relation": _relation_out(resolution),
        "summary": summary,
        "evidence": evidence_out,
        "notices": _notices_out(notices, mapping),
        "limitations": _limitations_out(verdict.limitations),
        "sources_checked": _sources_out(sources),
    }

    match verdict:
        case Supported(verdict_evidence_ids=ids):
            return CheckCompatibilityResult(
                SupportedResult(
                    verdict_evidence_ids=tuple(mapping[i] for i in ids), **shared
                )
            )
        case Unsupported(verdict_evidence_ids=ids):
            return CheckCompatibilityResult(
                UnsupportedResult(
                    verdict_evidence_ids=tuple(mapping[i] for i in ids), **shared
                )
            )
        case Unknown(reason=reason, causes=causes):
            return CheckCompatibilityResult(
                UnknownResult(
                    reason=reason,
                    decision_causes=_causes_out(causes, mapping),
                    **shared,
                )
            )
        case _:
            assert_never(verdict)


def _constraint_out(
    constraint: ContextConstraint, mapping: Mapping[EvidenceId, str]
) -> ConstraintOut:
    return ConstraintOut(
        relation=constraint.relation,
        counterpart=_target_id_out(constraint.counterpart),
        version_expression=constraint.version_expression,
        version_scheme=constraint.version_scheme,
        condition=condition_out(constraint.condition),
        explanation=constraint.explanation,
        evidence_ids=tuple(mapping[i] for i in constraint.evidence_ids),
    )


def _change_out(change: ContextChange, mapping: Mapping[EvidenceId, str]) -> ChangeOut:
    return ChangeOut(
        category=change.category,
        area=change.area,
        summary=change.summary,
        evidence_ids=tuple(mapping[i] for i in change.evidence_ids),
    )


def build_context_result(
    *,
    target: Target,
    outcome: ContextOutcome,
    summary: str,
    evidence: Iterable[Evidence],
    sources: Iterable[SourceCheck],
) -> GetCompatibilityContextResult:
    """Assemble a ``get_compatibility_context`` response from domain values.

    ``ContextUnknown`` has no ``constraints``/``changes`` fields at all - that is the point
    of the sum type - so those are read only inside the branch where they exist. Reading
    them up front would have made the "no material at all" path the one that crashes.
    """
    constraints: tuple[ContextConstraint, ...] = ()
    changes: tuple[ContextChange, ...] = ()
    match outcome:
        case ContextAvailable():
            constraints = outcome.constraints
            changes = outcome.changes
        case ContextUnknown():
            pass
        case _:
            assert_never(outcome)

    referenced: set[EvidenceId] = set()
    for constraint in constraints:
        referenced.update(constraint.evidence_ids)
    for change in changes:
        referenced.update(change.evidence_ids)
    for notice in outcome.notices:
        referenced.update(notice.evidence_ids)

    evidence_out, mapping = _renumber(evidence, referenced)
    shared = {
        "depth": outcome.depth,
        "target": target_out(target),
        "summary": summary,
        "constraints": tuple(
            _constraint_out(constraint, mapping) for constraint in constraints
        ),
        "changes": tuple(_change_out(change, mapping) for change in changes),
        "notices": _notices_out(outcome.notices, mapping),
        "limitations": _limitations_out(outcome.limitations),
        "sources_checked": _sources_out(sources),
        "evidence": evidence_out,
    }

    match outcome:
        case ContextAvailable():
            return GetCompatibilityContextResult(ContextAvailableResult(**shared))
        case ContextUnknown(reason=reason):
            return GetCompatibilityContextResult(
                ContextUnknownResult(reason=reason, **shared)
            )
        case _:
            assert_never(outcome)
