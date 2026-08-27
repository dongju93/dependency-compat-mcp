"""Context assembly for ``get_compatibility_context`` (03 "`get_compatibility_context` 처리").

This tool does not judge. It collects comparable material about one target and hands it to
the MCP client, which is the party that actually holds the codebase (02). So there is no
verdict type here - the outcome only says whether any material was found.

Every constraint reported here comes from the release's own registry metadata, fetched for
this request. There is no second, slower-moving tier of material and therefore no ``depth``
field: a response either carries comparable material or says it found none, and
``sources_checked`` says exactly what was read to reach that.

A runtime release declares no version constraints at all, so under a constraints-only
definition of "material" it could never be answered - the one namespace that means
something in only one of the two tools. :class:`ReleaseLifecycle` is the second kind of
material this tool may carry: the two facts its own publisher states about a runtime
release, when it was published and when its line stops being supported. Reporting them is
not a verdict, so it does not make this tool judge; it is the same "here is what the
source says, you compare it" contract a constraint already has.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, assert_never

from dependency_compat_mcp.domain.claims import (
    EolPublished,
    EolUnavailable,
    EolUnpublished,
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
    "ReleaseLifecycle",
    "RuntimeEol",
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


type RuntimeEol = EolPublished | EolUnpublished | EolUnavailable
"""End of life as it can be reported *for a runtime release*.

Three of :data:`~dependency_compat_mcp.domain.claims.EolStatus`'s four cases, not by
loosening the distinction but by applying it one level further:
:class:`~dependency_compat_mcp.domain.claims.EolNotApplicable` means "this target has no
support lifecycle at all", and for such a target there is no lifecycle block to put it in.
Admitting it here would create a state that says "a runtime is not a runtime", plus a
summary sentence no request could ever produce.
"""


@dataclass(frozen=True, slots=True)
class ReleaseLifecycle:
    """What a runtime's own publisher states about one release: when, and until when.

    Carries no evidence ids. A constraint cites them because it quotes an expression that a
    caller then compares; these two facts are the document itself, and the row in
    ``sources_checked`` naming the index and the schedule is what they rest on.
    """

    released_at: datetime
    eol: RuntimeEol


@dataclass(frozen=True, slots=True)
class ContextInput:
    """Everything the assembler may see. Referential integrity is checked on construction.

    A constraint pointing at an evidence id that is not in the catalogue is a server
    defect, not thin data, so it cannot be handed to :func:`build_context` at all (03 [6]).
    """

    target: Target
    release_found: bool
    constraints: tuple[ContextConstraint, ...]
    lifecycle: ReleaseLifecycle | None
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
    """At least one piece of comparable material was found.

    The "an available context is never empty" invariant is unchanged; only what counts as
    material is wider than it was. A registry release brings constraints and a runtime
    release brings a lifecycle, so in practice exactly one of the two fields is populated -
    but the check is on emptiness rather than on the target's kind, because it is emptiness
    that would make ``available`` a lie.
    """

    constraints: tuple[ContextConstraint, ...]
    lifecycle: ReleaseLifecycle | None
    notices: tuple[Notice, ...]
    limitations: tuple[Limitation, ...]

    def __post_init__(self) -> None:
        if not self.constraints and self.lifecycle is None:
            raise InvariantViolation(
                "an available context must carry a constraint or a lifecycle"
            )


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

    if not context.constraints and context.lifecycle is None:
        return _unknown("evidence_not_found", notices, limitations)

    return ContextAvailable(
        constraints=context.constraints,
        lifecycle=context.lifecycle,
        notices=sorted_notices(notices),
        limitations=sorted_limitations(limitations),
    )
