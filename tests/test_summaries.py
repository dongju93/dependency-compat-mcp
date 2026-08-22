"""Tests pinning the summary templates (04 "`summary`는 evidence보다 강한 주장을 하지 않는다").

No type can prove that a sentence does not overclaim, so the guarantee is procedural: the
full template list is asserted here verbatim, and every summary the two tools can produce
is asserted to be one of those templates instantiated. Adding or reshaping a sentence
therefore fails this test until a human agrees to the new wording.
"""

import pytest

from dependency_compat_mcp.domain.claims import (
    EolNotApplicable,
    ReleaseFacts,
    SourceCheck,
)
from dependency_compat_mcp.domain.context import (
    ContextAvailable,
    ContextConstraint,
    ContextUnknown,
)
from dependency_compat_mcp.domain.diagnostics import (
    CAUSE_KINDS,
    ClaimGuard,
    ConditionalClaim,
    DecisionCause,
    UnprovenClaim,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.evaluate import (
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
    "{declared_about} satisfies {declaring}'s {rule}, "
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
    "No source relating {declaring} to {declared_about} was found.",
    "{declaring} declares no relationship to {declared_about}.",
    "{declaring}'s requirement for {declared_about} is conditional on {expression}; "
    "the request carries no environment, so compatibility is unknown.",
    "{declaring}'s requirement for {declared_about} applies only when {expression} is "
    "selected; the request selects no extra, so compatibility is unknown.",
    "{declaring}'s {rule} has no upper bound and {declared_about} was released after it, "
    "so support for it was never stated.",
    "{declared_about} had already reached end of life when {declaring} was released, "
    "so {declaring}'s {rule} never stated support for it.",
    "{declaring}'s {rule} has no upper bound and {declared_about}'s official support "
    "schedule could not be read, so whether it was still supported was not checked.",
    "{declaring} only enumerates {declared_about} in classifiers, "
    "which cannot carry a verdict on their own.",
    "{declaring}'s stated range does not cover {declared_about}, "
    "so it neither includes nor excludes it.",
    "{declaring}'s declared expression could not be compared with {declared_about} "
    "under a single version scheme.",
    " {additional_count} further cause(s) are listed in decision_causes.",
    " The arguments were read in reverse: the declaration comes from {declaring}.",
    "{target}: {constraint_count} declared constraint(s) from registry metadata.",
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
        "runtime:python 3.13 satisfies pypi:example-framework 5.2's requires_python, "
        "backed by 1 source(s)."
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
    [
        reason
        for reason in UNKNOWN_REASONS
        # Handled by the cause-specific tests below: it is the one reason that cannot be
        # constructed without a cause, and the one whose sentence is chosen from one.
        if reason not in ("relation_not_supported", "insufficient_evidence")
    ],
)
def test_every_unknown_reason_has_a_summary(reason: UnknownReason) -> None:
    summary = summarise_verdict(
        Unknown(reason=reason, notices=(), limitations=()), PYTHON_RELATION
    )
    assert summary
    assert "{" not in summary


# --------------------------------------------------------------------------------------
# `insufficient_evidence` is narrated from its causes
# --------------------------------------------------------------------------------------


def _insufficient(*causes: DecisionCause) -> Unknown:
    return Unknown(
        reason="insufficient_evidence", notices=(), limitations=(), causes=causes
    )


def _unproven(kind: str) -> DecisionCause:
    return UnprovenClaim(
        kind=kind,  # pyrefly: ignore[bad-argument-type]
        evidence_ids=("ev-1",),
    )


def _guarded(kind: str, expression: str, *variables: str) -> DecisionCause:
    return ConditionalClaim(
        condition=ClaimGuard(
            kind=kind,  # pyrefly: ignore[bad-argument-type]
            expression=expression,
            variables=variables,
        ),
        evidence_ids=("ev-1",),
    )


@pytest.mark.parametrize("kind", CAUSE_KINDS)
def test_every_cause_kind_has_a_summary(kind: str) -> None:
    """A cause with no sentence would silently fall back to naming the category."""
    cause = (
        _guarded("environment_marker", 'sys_platform == "win32"', "sys_platform")
        if kind == "conditional_claim"
        else _unproven(kind)
    )
    summary = summarise_verdict(_insufficient(cause), PYTHON_RELATION)
    assert summary
    assert "{" not in summary


def test_an_environment_marker_cause_quotes_the_marker() -> None:
    summary = summarise_verdict(
        _insufficient(
            _guarded("environment_marker", 'sys_platform == "win32"', "sys_platform")
        ),
        PYTHON_RELATION,
    )
    assert summary == (
        "pypi:example-framework 5.2's requirement for runtime:python 3.13 is conditional "
        'on sys_platform == "win32"; the request carries no environment, so '
        "compatibility is unknown."
    )


def test_an_extra_marker_cause_says_it_is_an_extra() -> None:
    summary = summarise_verdict(
        _insufficient(_guarded("extra_marker", 'extra == "argon2"', "extra")),
        PYTHON_RELATION,
    )
    assert 'extra == "argon2"' in summary
    assert "selects no extra" in summary


def test_the_summary_narrates_the_first_cause_and_counts_the_rest() -> None:
    """The leading cause is fixed by `CAUSE_KINDS`, so the sentence cannot drift."""
    verdict = _insufficient(
        _unproven("tier_c_only"),
        _guarded("environment_marker", 'sys_platform == "win32"', "sys_platform"),
    )
    summary = summarise_verdict(verdict, PYTHON_RELATION)

    assert verdict.causes[0] == _guarded(
        "environment_marker", 'sys_platform == "win32"', "sys_platform"
    )
    assert summary.startswith(
        "pypi:example-framework 5.2's requirement for runtime:python 3.13 is conditional"
    )
    assert summary.endswith(" 1 further cause(s) are listed in decision_causes.")


def test_a_single_cause_gets_no_additional_clause() -> None:
    summary = summarise_verdict(
        _insufficient(_unproven("tier_c_only")), PYTHON_RELATION
    )
    assert "further cause" not in summary


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
        condition=None,
        explanation="The distribution requires this runtime range.",
        evidence_ids=("ev-1",),
    )


def test_an_available_context_counts_the_constraints_it_carries() -> None:
    summary = summarise_context(
        ContextAvailable(constraints=(_constraint(),), notices=(), limitations=()),
        FRAMEWORK,
    )
    assert summary == (
        "pypi:example-framework 5.2: 1 declared constraint(s) from registry metadata."
    )


@pytest.mark.parametrize(
    "reason", ["release_not_found", "lookup_failed", "evidence_not_found"]
)
def test_every_context_reason_has_a_summary(reason: str) -> None:
    summary = summarise_context(
        ContextUnknown(
            reason=reason,  # pyrefly: ignore[bad-argument-type]
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
        'sys_platform == "win32"',
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
                constraint_count=0,
                change_count=0,
                additional_count=0,
                expression='sys_platform == "win32"',
            )
        )
        for template in TEMPLATES
    }


def test_every_generated_verdict_summary_instantiates_a_pinned_template() -> None:
    skeletons = _template_skeletons()
    release_facts = ReleaseFacts(
        declaring_released_at=None,
        declared_about_released_at=None,
        declared_about_eol=EolNotApplicable(),
        declaring_yanked=None,
        declared_about_yanked=None,
    )
    for relation in (PYTHON_RELATION, REVERSED_RELATION, DIST_RELATION):
        verdict = evaluate(
            EvaluationInput(
                relation=relation,
                claims=(),
                facts=release_facts,
                lookups=(
                    SourceCheck(
                        source="pypi_json",
                        target=FRAMEWORK,
                        role="declaring",
                        outcome="ok",
                    ),
                ),
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
