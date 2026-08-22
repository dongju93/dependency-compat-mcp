"""Templated one-line summaries for both tools.

04 requires that ``summary`` never claim more than the evidence does, and 03 concedes that
no type can enforce that. The mechanism that does enforce it is this module: a summary is
selected from a closed template list by (verdict or availability, rule, direction, counts,
leading decision cause) and filled with values taken from the same result the caller
already received. There is no code path that writes a sentence.

The last selector is what keeps the summary honest about an ``unknown``. It is not a
second, independently written explanation - it is a projection of ``decision_causes``,
rendered from the *first* cause in the order fixed by
:data:`~dependency_compat_mcp.domain.diagnostics.CAUSE_KINDS`. A summary therefore cannot
name a reason the structured result does not also carry, and cannot silently pick a
different leading reason between two identical requests.

:data:`TEMPLATES` is pinned by a test. Adding a sentence therefore has to be a deliberate,
reviewed change rather than something that drifts in with a feature.

The strings stay in English: 04 assigns the final user-facing wording and language to the
MCP client, so this text is a machine-readable gloss of the structured result, not a
translation surface.
"""

from typing import Final, assert_never

from dependency_compat_mcp.domain.context import (
    ContextAvailable,
    ContextOutcome,
    ContextUnknown,
)
from dependency_compat_mcp.domain.diagnostics import (
    ConditionalClaim,
    DecisionCause,
    UnprovenClaim,
    UnprovenKind,
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
    name_of,
    namespace_of,
    version_of,
)

__all__ = [
    "TEMPLATES",
    "render_target",
    "summarise_context",
    "summarise_verdict",
]

# --------------------------------------------------------------------------------------
# check_compatibility
# --------------------------------------------------------------------------------------

_SUPPORTED: Final = (
    "{declared_about} satisfies {declaring}'s {rule}, "
    "backed by {evidence_count} source(s)."
)
_UNSUPPORTED: Final = (
    "{declaring} excludes {declared_about} via {rule}, "
    "backed by {evidence_count} source(s)."
)
_RELEASE_NOT_FOUND: Final = (
    "No release was found for {declaring} or {declared_about}, "
    "so the pair was not evaluated."
)
_LOOKUP_FAILED: Final = (
    "A required source could not be read, so {declaring} and {declared_about} "
    "were not evaluated."
)
_RELATION_NOT_SUPPORTED: Final = (
    "No relation rule is registered for {subject} and {counterpart} "
    "in an allowed direction."
)
_CONFLICTING_EVIDENCE: Final = (
    "Explicit statements about {declared_about} disagree, "
    "so {declaring} was not judged."
)
# One per `CauseKind`, plus the two readings of a conditional claim. `insufficient_evidence`
# has no template of its own: the reason is a category, and a sentence naming the category
# rather than the cause is precisely what this list replaced.
_CONDITIONAL_ENVIRONMENT: Final = (
    "{declaring}'s requirement for {declared_about} is conditional on {expression}; "
    "the request carries no environment, so compatibility is unknown."
)
_CONDITIONAL_EXTRA: Final = (
    "{declaring}'s requirement for {declared_about} applies only when {expression} is "
    "selected; the request selects no extra, so compatibility is unknown."
)
_OPEN_UPPER_BOUND: Final = (
    "{declaring}'s {rule} has no upper bound and {declared_about} was released after it, "
    "so support for it was never stated."
)
_STALE_LOWER_BOUND: Final = (
    "{declared_about} had already reached end of life when {declaring} was released, "
    "so {declaring}'s {rule} never stated support for it."
)
_LIFECYCLE_UNAVAILABLE: Final = (
    "{declaring}'s {rule} has no upper bound and {declared_about}'s official support "
    "schedule could not be read, so whether it was still supported was not checked."
)
_TIER_C_ONLY: Final = (
    "{declaring} only enumerates {declared_about} in classifiers, "
    "which cannot carry a verdict on their own."
)
_CLAIM_OUTSIDE_RANGE: Final = (
    "{declaring}'s stated range does not cover {declared_about}, "
    "so it neither includes nor excludes it."
)
_UNCOMPARABLE_CLAIM: Final = (
    "{declaring}'s declared expression could not be compared with {declared_about} "
    "under a single version scheme."
)
_ADDITIONAL_CAUSES: Final = (
    " {additional_count} further cause(s) are listed in decision_causes."
)

# Total over `UnprovenKind`. Keyed by the domain type rather than by `str`, so a new cause
# kind with no sentence for it is a type error here rather than a KeyError at request time.
_UNPROVEN_TEMPLATES: Final[dict[UnprovenKind, str]] = {
    "open_upper_bound": _OPEN_UPPER_BOUND,
    "stale_lower_bound": _STALE_LOWER_BOUND,
    "lifecycle_unavailable": _LIFECYCLE_UNAVAILABLE,
    "tier_c_only": _TIER_C_ONLY,
    "claim_outside_range": _CLAIM_OUTSIDE_RANGE,
    "uncomparable_claim": _UNCOMPARABLE_CLAIM,
}
_EVIDENCE_NOT_FOUND: Final = (
    "No source relating {declaring} to {declared_about} was found."
)
_NO_DECLARED_RELATIONSHIP: Final = (
    "{declaring} declares no relationship to {declared_about}."
)
_REVERSED_SUFFIX: Final = (
    " The arguments were read in reverse: the declaration comes from {declaring}."
)

# --------------------------------------------------------------------------------------
# get_compatibility_context
# --------------------------------------------------------------------------------------

_CONTEXT_AVAILABLE: Final = (
    "{target}: {constraint_count} declared constraint(s) from registry metadata."
)
_CONTEXT_RELEASE_NOT_FOUND: Final = (
    "No release was found for {target}, so no compatibility context was collected."
)
_CONTEXT_LOOKUP_FAILED: Final = (
    "A required source for {target} could not be read, "
    "so no compatibility context was collected."
)
_CONTEXT_EVIDENCE_NOT_FOUND: Final = "No compatibility context was found for {target}."

TEMPLATES: Final[tuple[str, ...]] = (
    _SUPPORTED,
    _UNSUPPORTED,
    _RELEASE_NOT_FOUND,
    _LOOKUP_FAILED,
    _RELATION_NOT_SUPPORTED,
    _CONFLICTING_EVIDENCE,
    _EVIDENCE_NOT_FOUND,
    _NO_DECLARED_RELATIONSHIP,
    _CONDITIONAL_ENVIRONMENT,
    _CONDITIONAL_EXTRA,
    _OPEN_UPPER_BOUND,
    _STALE_LOWER_BOUND,
    _LIFECYCLE_UNAVAILABLE,
    _TIER_C_ONLY,
    _CLAIM_OUTSIDE_RANGE,
    _UNCOMPARABLE_CLAIM,
    _ADDITIONAL_CAUSES,
    _REVERSED_SUFFIX,
    _CONTEXT_AVAILABLE,
    _CONTEXT_RELEASE_NOT_FOUND,
    _CONTEXT_LOOKUP_FAILED,
    _CONTEXT_EVIDENCE_NOT_FOUND,
)


def render_target(target: Target) -> str:
    """``namespace:name version`` - the identity the server actually evaluated (04)."""
    return f"{namespace_of(target)}:{name_of(target)} {version_of(target)}"


def _cause_template(cause: DecisionCause) -> tuple[str, dict[str, str]]:
    """Pick the sentence for one cause, plus the values only that sentence needs."""
    match cause:
        case ConditionalClaim(condition=condition):
            template = (
                _CONDITIONAL_ENVIRONMENT
                if condition.kind == "environment_marker"
                else _CONDITIONAL_EXTRA
            )
            # The marker is quoted verbatim from the registry entry. Paraphrasing it would
            # be the one thing 04 forbids: a summary asserting more than the evidence.
            return template, {"expression": condition.expression}
        case UnprovenClaim(kind=kind):
            return _UNPROVEN_TEMPLATES[kind], {}
        case _:
            assert_never(cause)


def _unknown_template(verdict: Unknown) -> tuple[str, dict[str, str]]:
    match verdict.reason:
        case "relation_not_supported":
            # The reason exists only for a relation that was never resolved; pairing it
            # with a resolved relation means the assembler mislabelled the result.
            raise InvariantViolation(
                "relation_not_supported cannot describe a resolved relation"
            )
        case "release_not_found":
            return _RELEASE_NOT_FOUND, {}
        case "lookup_failed":
            return _LOOKUP_FAILED, {}
        case "conflicting_evidence":
            return _CONFLICTING_EVIDENCE, {}
        case "evidence_not_found":
            return _EVIDENCE_NOT_FOUND, {}
        case "no_declared_relationship":
            return _NO_DECLARED_RELATIONSHIP, {}
        case "insufficient_evidence":
            if not verdict.causes:  # pragma: no cover - refused by `Unknown`
                raise InvariantViolation(
                    "insufficient_evidence reached the summariser without a cause"
                )
            template, values = _cause_template(verdict.causes[0])
            if len(verdict.causes) > 1:
                # The rest are not dropped, only not narrated: `decision_causes` still
                # carries every one of them, and the count says how many to go read.
                template += _ADDITIONAL_CAUSES
                values = {
                    **values,
                    "additional_count": str(len(verdict.causes) - 1),
                }
            return template, values
        case never:
            assert_never(never)


def _resolved_summary(verdict: Verdict, relation: ResolvedRelation) -> str:
    declaring = render_target(relation.declaring)
    declared_about = render_target(relation.declared_about)
    values: dict[str, object]
    match verdict:
        case Supported(verdict_evidence_ids=ids):
            template = _SUPPORTED
            values = {"evidence_count": len(ids)}
        case Unsupported(verdict_evidence_ids=ids):
            template = _UNSUPPORTED
            values = {"evidence_count": len(ids)}
        case Unknown():
            template, cause_values = _unknown_template(verdict)
            values = dict(cause_values)
        case _:
            assert_never(verdict)

    summary = template.format(
        declaring=declaring,
        declared_about=declared_about,
        rule=relation.rule.name,
        **values,
    )
    if relation.direction == "reversed":
        # Without this the caller cannot tell that the server answered by reading the other
        # argument's declaration, which is the one mistake 04 wants surfaced.
        summary += _REVERSED_SUFFIX.format(declaring=declaring)
    return summary


def summarise_verdict(verdict: Verdict, resolution: RelationResolution) -> str:
    """Render the one-line summary for a ``check_compatibility`` result."""
    match resolution:
        case ResolvedRelation():
            return _resolved_summary(verdict, resolution)
        case UnsupportedRelation(subject=subject, counterpart=counterpart):
            match verdict:
                case Unknown(reason="relation_not_supported"):
                    return _RELATION_NOT_SUPPORTED.format(
                        subject=render_target(subject),
                        counterpart=render_target(counterpart),
                    )
                case _:
                    # No rule was found, so no verdict about the pair can be justified.
                    raise InvariantViolation(
                        "an unresolved relation can only carry relation_not_supported"
                    )
        case _:
            assert_never(resolution)


def summarise_context(outcome: ContextOutcome, target: Target) -> str:
    """Render the one-line summary for a ``get_compatibility_context`` result."""
    rendered = render_target(target)
    match outcome:
        case ContextAvailable(constraints=constraints):
            return _CONTEXT_AVAILABLE.format(
                target=rendered, constraint_count=len(constraints)
            )
        case ContextUnknown(reason=reason):
            match reason:
                case "release_not_found":
                    template = _CONTEXT_RELEASE_NOT_FOUND
                case "lookup_failed":
                    template = _CONTEXT_LOOKUP_FAILED
                case "evidence_not_found":
                    template = _CONTEXT_EVIDENCE_NOT_FOUND
                case never:
                    assert_never(never)
            return template.format(target=rendered)
        case _:
            assert_never(outcome)
