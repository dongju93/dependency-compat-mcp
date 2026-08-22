"""Templated one-line summaries for both tools.

04 requires that ``summary`` never claim more than the evidence does, and 03 concedes that
no type can enforce that. The mechanism that does enforce it is this module: a summary is
selected from a closed template list by (verdict or availability, rule, direction, counts)
and filled with values taken from the same result the caller already received. There is no
code path that writes a sentence.

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
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.evaluate import (
    Supported,
    Unknown,
    UnknownReason,
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
_INSUFFICIENT_EVIDENCE: Final = (
    "{declaring} does not declare verified support for {declared_about}; "
    "{limitation_count} limitation(s) recorded."
)
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

_CONTEXT_REGISTRY_ONLY: Final = (
    "{target}: {constraint_count} constraint(s) and {change_count} change(s), "
    "all from registry metadata."
)
_CONTEXT_REGISTRY_AND_CURATED: Final = (
    "{target}: {constraint_count} constraint(s) and {change_count} change(s), "
    "including reviewed official statements."
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
    _INSUFFICIENT_EVIDENCE,
    _EVIDENCE_NOT_FOUND,
    _NO_DECLARED_RELATIONSHIP,
    _REVERSED_SUFFIX,
    _CONTEXT_REGISTRY_ONLY,
    _CONTEXT_REGISTRY_AND_CURATED,
    _CONTEXT_RELEASE_NOT_FOUND,
    _CONTEXT_LOOKUP_FAILED,
    _CONTEXT_EVIDENCE_NOT_FOUND,
)

_UNKNOWN_TEMPLATES: Final[dict[UnknownReason, str]] = {
    "release_not_found": _RELEASE_NOT_FOUND,
    "lookup_failed": _LOOKUP_FAILED,
    "relation_not_supported": _RELATION_NOT_SUPPORTED,
    "conflicting_evidence": _CONFLICTING_EVIDENCE,
    "insufficient_evidence": _INSUFFICIENT_EVIDENCE,
    "evidence_not_found": _EVIDENCE_NOT_FOUND,
    "no_declared_relationship": _NO_DECLARED_RELATIONSHIP,
}


def render_target(target: Target) -> str:
    """``namespace:name version`` - the identity the server actually evaluated (04)."""
    return f"{namespace_of(target)}:{name_of(target)} {version_of(target)}"


def _resolved_summary(verdict: Verdict, relation: ResolvedRelation) -> str:
    declaring = render_target(relation.declaring)
    declared_about = render_target(relation.declared_about)
    match verdict:
        case Supported(verdict_evidence_ids=ids):
            template = _SUPPORTED
            counts = {"evidence_count": len(ids)}
        case Unsupported(verdict_evidence_ids=ids):
            template = _UNSUPPORTED
            counts = {"evidence_count": len(ids)}
        case Unknown(reason=reason):
            if reason == "relation_not_supported":
                # The reason exists only for a relation that was never resolved; pairing it
                # with a resolved relation means the assembler mislabelled the result.
                raise InvariantViolation(
                    "relation_not_supported cannot describe a resolved relation"
                )
            template = _UNKNOWN_TEMPLATES[reason]
            counts = {"limitation_count": len(verdict.limitations)}
        case _:
            assert_never(verdict)

    summary = template.format(
        declaring=declaring,
        declared_about=declared_about,
        rule=relation.rule.name,
        **counts,
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
        case ContextAvailable(constraints=constraints, changes=changes, depth=depth):
            template = (
                _CONTEXT_REGISTRY_AND_CURATED
                if depth == "registry_and_curated"
                else _CONTEXT_REGISTRY_ONLY
            )
            return template.format(
                target=rendered,
                constraint_count=len(constraints),
                change_count=len(changes),
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
