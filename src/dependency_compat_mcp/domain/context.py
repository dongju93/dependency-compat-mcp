"""Context assembly for ``get_compatibility_context`` (03 "`get_compatibility_context` 처리").

This tool does not judge. It collects comparable material about one target and hands it to
the MCP client, which is the party that actually holds the codebase (02). So there is no
verdict type here - the outcome only says whether any material was found.

Every constraint reported here comes from the release's own registry metadata, fetched for
this request. There is no second, slower-moving tier of material and therefore no ``depth``
field: a response either carries declared constraints or says it found none, and
``sources_checked`` says exactly what was read to reach that.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

from dependency_compat_mcp.domain.claims import (
    Evidence,
    EvidenceId,
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
from dependency_compat_mcp.domain.evaluate import coverage_limitations
from dependency_compat_mcp.domain.targets import Target, TargetId, VersionScheme

__all__ = [
    "ContextAvailable",
    "ContextConstraint",
    "ContextInput",
    "ContextOutcome",
    "ContextUnknown",
    "ContextUnknownReason",
    "build_context",
]

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
class ContextInput:
    """Everything the assembler may see. Referential integrity is checked on construction.

    A constraint pointing at an evidence id that is not in the catalogue is a server
    defect, not thin data, so it cannot be handed to :func:`build_context` at all (03 [6]).
    """

    target: Target
    release_found: bool
    constraints: tuple[ContextConstraint, ...]
    evidence: tuple[Evidence, ...]
    lookups: tuple[SourceCheck, ...]
    marker_guarded: bool
    extra_guarded: bool

    def __post_init__(self) -> None:
        known = {_evidence_id(item) for item in self.evidence}
        dangling = sorted(
            {
                identifier
                for constraint in self.constraints
                for identifier in constraint.evidence_ids
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
    """At least one declared constraint was found."""

    constraints: tuple[ContextConstraint, ...]
    notices: tuple[Notice, ...]
    limitations: tuple[Limitation, ...]

    def __post_init__(self) -> None:
        if not self.constraints:
            raise InvariantViolation("an available context must carry a constraint")


@dataclass(frozen=True, slots=True)
class ContextUnknown:
    """Nothing comparable was found, and which of the three reasons that was."""

    reason: ContextUnknownReason
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


def _limitations(context: ContextInput) -> list[Limitation]:
    limitations = list(coverage_limitations(context.lookups))
    if context.marker_guarded:
        limitations.append(Limitation("marker_guarded_claim"))
    if context.extra_guarded:
        limitations.append(Limitation("extra_guarded_claim"))
    return limitations


def _unknown(
    reason: ContextUnknownReason,
    notices: Iterable[Notice],
    limitations: Sequence[Limitation],
) -> ContextUnknown:
    return ContextUnknown(
        reason=reason,
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
        return _unknown("lookup_failed", notices, limitations)
    if not context.release_found:
        return _unknown("release_not_found", notices, limitations)

    if not context.constraints:
        return _unknown("evidence_not_found", notices, limitations)

    return ContextAvailable(
        constraints=context.constraints,
        notices=sorted_notices(notices),
        limitations=sorted_limitations(limitations),
    )
