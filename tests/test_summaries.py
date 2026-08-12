"""Tests pinning the summary templates (04 "`summary`는 evidence보다 강한 주장을 하지 않는다").

No type can prove that a sentence does not overclaim, so the guarantee is procedural: the
full template list is asserted here verbatim, and every summary the two tools can produce
is asserted to be one of those templates instantiated. Adding or reshaping a sentence
therefore fails this test until a human agrees to the new wording.
"""

import pytest

from dependency_compat_mcp.domain.claims import ReleaseFacts, SourceCheck
from dependency_compat_mcp.domain.context import (
    ContextAvailable,
    ContextChange,
    ContextConstraint,
    ContextUnknown,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.evaluate import (
    CuratedCoverage,
    EvaluationInput,
    Supported,
    Unknown,
    UnknownReason,
    Unsupported,
    evaluate,
)
from dependency_compat_mcp.domain.relations import (
    ResolvedRelation,
    UnsupportedRelation,
    resolve_relation,
)
from dependency_compat_mcp.domain.summaries import (
    TEMPLATES,
    render_target,
    summarise_context,
    summarise_verdict,
)
from dependency_compat_mcp.domain.targets import Target, TargetId, parse_target

FRAMEWORK = parse_target("pypi", "example-framework", "5.2")
PYTHON = parse_target("runtime", "python", "3.13")
PYTHON_ID = TargetId.of(PYTHON)


def relation_of(subject: Target, counterpart: Target) -> ResolvedRelation:
    resolution = resolve_relation(subject, counterpart)
    assert isinstance(resolution, ResolvedRelation)
    return resolution


PYTHON_RELATION = relation_of(FRAMEWORK, PYTHON)
REVERSED_RELATION = relation_of(PYTHON, FRAMEWORK)
DIST_RELATION = relation_of(FRAMEWORK, parse_target("pypi", "example-library", "2.0"))

EXPECTED_TEMPLATES: tuple[str, ...] = (
    "{declaring} declares support for {declared_about} via {rule}, "
    "backed by {evidence_count} source(s).",
    "{declaring} excludes {declared_about} via {rule}, "
    "backed by {evidence_count} source(s).",
    "No release was found for {declaring} or {declared_about}, "
    "so the pair was not evaluated.",
    "A required source could not be read, so {declaring} and {declared_about} "
    "were not evaluated.",
    "No relation rule is registered for {subject} and {counterpart} "
    "in an allowed direction.",
    "Explicit statements about {declared_about} disagree, so {declaring} was not judged.",
    "{declaring} does not declare verified support for {declared_about}; "
    "{limitation_count} limitation(s) recorded.",
    "No source relating {declaring} to {declared_about} was found.",
    "{declaring} declares no relationship to {declared_about}.",
    " The arguments were read in reverse: the declaration comes from {declaring}.",
    "{target}: {constraint_count} constraint(s) and {change_count} change(s), "
    "all from registry metadata.",
    "{target}: {constraint_count} constraint(s) and {change_count} change(s), "
    "including reviewed official statements.",
    "No release was found for {target}, so no compatibility context was collected.",
    "A required source for {target} could not be read, "
    "so no compatibility context was collected.",
    "No compatibility context was found for {target}.",
)

UNKNOWN_REASONS: tuple[UnknownReason, ...] = (
    "release_not_found",
    "lookup_failed",
    "relation_not_supported",
    "conflicting_evidence",
    "insufficient_evidence",
    "evidence_not_found",
    "no_declared_relationship",
)


def test_the_template_list_is_pinned() -> None:
    assert TEMPLATES == EXPECTED_TEMPLATES


def test_templates_are_unique() -> None:
    assert len(set(TEMPLATES)) == len(TEMPLATES)


def test_render_target_shows_the_evaluated_identity() -> None:
    assert render_target(FRAMEWORK) == "pypi:example-framework 5.2"
    assert render_target(PYTHON) == "runtime:python 3.13"


# --------------------------------------------------------------------------------------
# check_compatibility summaries
# --------------------------------------------------------------------------------------


def test_supported_summary_names_both_sides_and_the_rule() -> None:
    summary = summarise_verdict(
        Supported(verdict_evidence_ids=("ev-1",), notices=(), limitations=()),
        PYTHON_RELATION,
    )
    assert summary == (
        "pypi:example-framework 5.2 declares support for runtime:python 3.13 "
        "via requires_python, backed by 1 source(s)."
    )


def test_unsupported_summary_counts_the_verdict_evidence() -> None:
    summary = summarise_verdict(
        Unsupported(verdict_evidence_ids=("ev-1", "ev-2"), notices=(), limitations=()),
        DIST_RELATION,
    )
    assert "backed by 2 source(s)" in summary
    assert "via requires_dist" in summary


def test_a_reversed_reading_is_disclosed() -> None:
    """Without this clause the caller cannot tell it swapped the arguments (04)."""
    summary = summarise_verdict(
        Supported(verdict_evidence_ids=("ev-1",), notices=(), limitations=()),
        REVERSED_RELATION,
    )
    assert summary.endswith(
        " The arguments were read in reverse: the declaration comes from "
        "pypi:example-framework 5.2."
    )


@pytest.mark.parametrize(
    "reason",
    [reason for reason in UNKNOWN_REASONS if reason != "relation_not_supported"],
)
def test_every_unknown_reason_has_a_summary(reason: UnknownReason) -> None:
    summary = summarise_verdict(
        Unknown(reason=reason, notices=(), limitations=()), PYTHON_RELATION
    )
    assert summary
    assert "{" not in summary


def test_an_unresolved_relation_gets_the_relation_not_supported_summary() -> None:
    unresolved = UnsupportedRelation(subject=PYTHON, counterpart=PYTHON)
    summary = summarise_verdict(
        Unknown(reason="relation_not_supported", notices=(), limitations=()), unresolved
    )
    assert summary == (
        "No relation rule is registered for runtime:python 3.13 and "
        "runtime:python 3.13 in an allowed direction."
    )


def test_relation_not_supported_cannot_describe_a_resolved_relation() -> None:
    with pytest.raises(InvariantViolation):
        summarise_verdict(
            Unknown(reason="relation_not_supported", notices=(), limitations=()),
            PYTHON_RELATION,
        )


def test_an_unresolved_relation_cannot_carry_a_decided_verdict() -> None:
    with pytest.raises(InvariantViolation):
        summarise_verdict(
            Supported(verdict_evidence_ids=("ev-1",), notices=(), limitations=()),
            UnsupportedRelation(subject=PYTHON, counterpart=PYTHON),
        )


# --------------------------------------------------------------------------------------
# get_compatibility_context summaries
# --------------------------------------------------------------------------------------


def _constraint() -> ContextConstraint:
    return ContextConstraint(
        relation="requires",
        counterpart=PYTHON_ID,
        version_expression=">=3.10",
        version_scheme="pep440",
        explanation="The distribution requires this runtime range.",
        evidence_ids=("ev-1",),
    )


def _change() -> ContextChange:
    return ContextChange(
        category="removal",
        area="removed_api",
        summary="The API was removed in this release.",
        evidence_ids=("ev-2",),
    )


def test_registry_only_context_says_so() -> None:
    summary = summarise_context(
        ContextAvailable(
            depth="registry_only",
            constraints=(_constraint(),),
            changes=(),
            notices=(),
            limitations=(),
        ),
        FRAMEWORK,
    )
    assert summary == (
        "pypi:example-framework 5.2: 1 constraint(s) and 0 change(s), "
        "all from registry metadata."
    )


def test_curated_context_says_so() -> None:
    summary = summarise_context(
        ContextAvailable(
            depth="registry_and_curated",
            constraints=(_constraint(),),
            changes=(_change(),),
            notices=(),
            limitations=(),
        ),
        FRAMEWORK,
    )
    assert summary.endswith("including reviewed official statements.")
    assert "1 constraint(s) and 1 change(s)" in summary


@pytest.mark.parametrize(
    "reason", ["release_not_found", "lookup_failed", "evidence_not_found"]
)
def test_every_context_reason_has_a_summary(reason: str) -> None:
    summary = summarise_context(
        ContextUnknown(
            reason=reason,  # pyrefly: ignore[bad-argument-type]
            depth="registry_only",
            notices=(),
            limitations=(),
        ),
        FRAMEWORK,
    )
    assert summary
    assert "{" not in summary


# --------------------------------------------------------------------------------------
# Every produced summary is a template instance
# --------------------------------------------------------------------------------------


def _skeleton(summary: str) -> str:
    """Strip the values a template interpolates, leaving the sentence's fixed skeleton."""
    for value in (
        "pypi:example-framework 5.2",
        "runtime:python 3.13",
        "pypi:example-library 2.0",
        "requires_python",
        "requires_dist",
    ):
        summary = summary.replace(value, "*")
    return "".join("#" if character.isdigit() else character for character in summary)


def _template_skeletons() -> set[str]:
    return {
        _skeleton(
            template.format(
                declaring="pypi:example-framework 5.2",
                declared_about="runtime:python 3.13",
                subject="runtime:python 3.13",
                counterpart="runtime:python 3.13",
                target="pypi:example-framework 5.2",
                rule="requires_python",
                evidence_count=0,
                limitation_count=0,
                constraint_count=0,
                change_count=0,
            )
        )
        for template in TEMPLATES
    }


def test_every_generated_verdict_summary_instantiates_a_pinned_template() -> None:
    skeletons = _template_skeletons()
    curated = CuratedCoverage(entry_present=True, verified_for_version=True)
    release_facts = ReleaseFacts(
        declaring_released_at=None,
        declared_about_released_at=None,
        declared_about_eol_at=None,
        declaring_yanked=None,
        declared_about_yanked=None,
    )
    for relation in (PYTHON_RELATION, REVERSED_RELATION, DIST_RELATION):
        verdict = evaluate(
            EvaluationInput(
                relation=relation,
                claims=(),
                facts=release_facts,
                lookups=(SourceCheck(source="pypi_json", outcome="ok"),),
                curated=curated,
                declaring_release_found=True,
                declared_about_release_found=True,
            )
        )
        summary = summarise_verdict(verdict, relation)
        # A reversed reading appends a second template; split it back off before matching.
        head, _, tail = summary.partition(" The arguments were read in reverse:")
        assert _skeleton(head) in skeletons
        if tail:
            assert _skeleton(f" The arguments were read in reverse:{tail}") in skeletons
