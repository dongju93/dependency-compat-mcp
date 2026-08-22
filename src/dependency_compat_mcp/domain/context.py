"""Context assembly for ``get_compatibility_context`` (03 "`get_compatibility_context` 처리").

This tool does not judge. It collects comparable material about one target and hands it to
the MCP client, which is the party that actually holds the codebase (02). So there is no
verdict type here - the outcome only says whether any material was found and how deep it
goes.

``depth`` is the load-bearing field. ``requires_python`` and ``requires_dist`` exist on
almost every PyPI distribution, so a response containing only those is a restatement of
what the client already read from its lockfile. Surfacing "curated evidence present or not"
at the top level is what stops the server from quietly presenting its own coverage limit as
an answer.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

from dependency_compat_mcp.domain.claims import (
    Curated,
    Evidence,
    EvidenceId,
    Fetched,
    MarkerCondition,
    NarrativeEvidence,
    SourceCheck,
    VersionConstraintEvidence,
)
from dependency_compat_mcp.domain.diagnostics import (
    Limitation,
    Notice,
    sorted_limitations,
    sorted_notices,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.evaluate import CuratedCoverage, coverage_limitations
from dependency_compat_mcp.domain.targets import Target, TargetId, VersionScheme

__all__ = [
    "ContextAvailable",
    "ContextChange",
    "ContextConstraint",
    "ContextInput",
    "ContextOutcome",
    "ContextUnknown",
    "ContextUnknownReason",
    "Depth",
    "build_context",
]

type Depth = Literal["registry_only", "registry_and_curated"]
type ContextUnknownReason = Literal[
    "release_not_found", "lookup_failed", "evidence_not_found"
]


# --------------------------------------------------------------------------------------
# Material
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextConstraint:
    """One declared or stated version relationship, with the sources it rests on.

    ``condition`` carries the declaration's own marker rather than leaving it to be read
    out of ``explanation``. This tool never judges, so whether a constraint binds is a
    question only the caller can answer - and it can only answer it from a field.
    """

    relation: Literal["requires", "supports", "excludes"]
    counterpart: TargetId
    version_expression: str
    version_scheme: VersionScheme
    condition: MarkerCondition | None
    explanation: str
    evidence_ids: tuple[EvidenceId, ...]

    def __post_init__(self) -> None:
        if not self.evidence_ids:
            raise InvariantViolation(
                "a context constraint must cite at least one evidence id"
            )


@dataclass(frozen=True, slots=True)
class ContextChange:
    """One reviewed change a caller has to check its code against.

    Only the curated pack can produce these: registry metadata carries no breaking-change
    information, and interpreting release notes at request time would need a model.
    """

    category: Literal["breaking_change", "removal", "deprecation", "migration_required"]
    area: str
    summary: str
    evidence_ids: tuple[EvidenceId, ...]

    def __post_init__(self) -> None:
        if not self.evidence_ids:
            raise InvariantViolation(
                "a context change must cite at least one evidence id"
            )


@dataclass(frozen=True, slots=True)
class ContextInput:
    """Everything the assembler may see. Referential integrity is checked on construction.

    A constraint pointing at an evidence id that is not in the catalogue is a server
    defect, not thin data, so it cannot be handed to :func:`build_context` at all (03 [6]).
    """

    target: Target
    release_found: bool
    constraints: tuple[ContextConstraint, ...]
    changes: tuple[ContextChange, ...]
    evidence: tuple[Evidence, ...]
    lookups: tuple[SourceCheck, ...]
    curated: CuratedCoverage
    marker_guarded: bool
    extra_guarded: bool

    def __post_init__(self) -> None:
        known = {_evidence_id(item) for item in self.evidence}
        dangling = sorted(
            {
                identifier
                for group in (
                    *(constraint.evidence_ids for constraint in self.constraints),
                    *(change.evidence_ids for change in self.changes),
                )
                for identifier in group
                if identifier not in known
            }
        )
        if dangling:
            raise InvariantViolation(
                f"context references evidence that is not in the catalogue: "
                f"{', '.join(dangling)}"
            )


# --------------------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextAvailable:
    """At least one constraint or change was found."""

    depth: Depth
    constraints: tuple[ContextConstraint, ...]
    changes: tuple[ContextChange, ...]
    notices: tuple[Notice, ...]
    limitations: tuple[Limitation, ...]

    def __post_init__(self) -> None:
        if not self.constraints and not self.changes:
            raise InvariantViolation(
                "an available context must carry a constraint or a change"
            )


@dataclass(frozen=True, slots=True)
class ContextUnknown:
    """Nothing comparable was found. ``depth`` is still reported, as 04 requires."""

    reason: ContextUnknownReason
    depth: Depth
    notices: tuple[Notice, ...]
    limitations: tuple[Limitation, ...]


type ContextOutcome = ContextAvailable | ContextUnknown


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------


def _evidence_id(evidence: Evidence) -> EvidenceId:
    match evidence:
        case VersionConstraintEvidence(id=identifier):
            return identifier
        case NarrativeEvidence(id=identifier):
            return identifier
        case _:
            assert_never(evidence)


def _is_curated(evidence: Evidence) -> bool:
    match evidence.provenance:
        case Curated():
            return True
        case Fetched():
            return False
        case _:
            assert_never(evidence.provenance)


def _depth(context: ContextInput) -> Depth:
    """``registry_and_curated`` exactly when *used* evidence includes a curated source.

    Deliberately computed from the evidence a constraint or change actually cites, not from
    the whole catalogue: a curated entry that contributed nothing to the response must not
    advertise depth the response does not have.
    """
    referenced = {
        identifier
        for group in (
            *(constraint.evidence_ids for constraint in context.constraints),
            *(change.evidence_ids for change in context.changes),
        )
        for identifier in group
    }
    return (
        "registry_and_curated"
        if any(
            _evidence_id(item) in referenced and _is_curated(item)
            for item in context.evidence
        )
        else "registry_only"
    )


def _limitations(context: ContextInput) -> list[Limitation]:
    limitations = list(coverage_limitations(context.curated, context.lookups))
    if context.marker_guarded:
        limitations.append(Limitation("marker_guarded_claim"))
    if context.extra_guarded:
        limitations.append(Limitation("extra_guarded_claim"))
    return limitations


def _unknown(
    reason: ContextUnknownReason,
    depth: Depth,
    notices: Iterable[Notice],
    limitations: Sequence[Limitation],
) -> ContextUnknown:
    return ContextUnknown(
        reason=reason,
        depth=depth,
        notices=sorted_notices(notices),
        limitations=sorted_limitations(limitations),
    )


def build_context(context: ContextInput) -> ContextOutcome:
    """Assemble the context outcome for one target. Pure and total."""
    limitations = _limitations(context)
    # Nothing observed here changes a fact without changing the material itself, so there
    # is no notice source for this tool yet; the field exists because 04 requires the key
    # to be present rather than omitted when empty.
    notices: tuple[Notice, ...] = ()

    # Same ordering argument as the verdict's step 0: a failed required lookup must not be
    # reported as a missing release.
    if any(check.outcome == "failed" and check.required for check in context.lookups):
        # The unknown variant intentionally carries no constraints or changes. Its depth
        # must describe that emitted material, not curated input that this branch discards.
        return _unknown("lookup_failed", "registry_only", notices, limitations)
    if not context.release_found:
        return _unknown("release_not_found", "registry_only", notices, limitations)

    if not context.constraints and not context.changes:
        return _unknown("evidence_not_found", "registry_only", notices, limitations)

    return ContextAvailable(
        depth=_depth(context),
        constraints=context.constraints,
        changes=context.changes,
        notices=sorted_notices(notices),
        limitations=sorted_limitations(limitations),
    )
