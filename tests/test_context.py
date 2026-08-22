"""Tests for `get_compatibility_context` assembly (03 "`get_compatibility_context` 처리").

The fact this tool has to get right is ``availability``: is there any declared material at
all, and if not, which of the three reasons applies. It is computed, so it is asserted here
against every shape the assembler can be handed.

``depth`` used to be the second such fact. It is gone with the evidence tier it described -
every constraint now comes from the release's own registry metadata, fetched for this
request, so there is no shallower and deeper answer to distinguish.
"""

from datetime import UTC, datetime

import pytest

from dependency_compat_mcp.domain.claims import (
    Evidence,
    Fetched,
    SourceCheck,
    VersionConstraintEvidence,
)
from dependency_compat_mcp.domain.context import (
    ContextAvailable,
    ContextConstraint,
    ContextInput,
    ContextUnknown,
    build_context,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.targets import TargetId, parse_target

FRAMEWORK = parse_target("pypi", "example-framework", "5.2")
PYTHON_ID = TargetId.of(parse_target("runtime", "python", "3.13"))


def check(source: str, outcome: str, *, required: bool = True) -> SourceCheck:
    """A lookup record. Every check names the release it was made for."""
    return SourceCheck(
        source=source,  # pyrefly: ignore[bad-argument-type]
        target=FRAMEWORK,
        role="declaring",
        outcome=outcome,  # pyrefly: ignore[bad-argument-type]
        required=required,
    )


OK_LOOKUPS = (check("pypi_json", "ok"),)

FETCHED = Fetched(retrieved_at=datetime(2026, 8, 12, tzinfo=UTC))


def registry_evidence(identifier: str = "ev-registry") -> Evidence:
    return VersionConstraintEvidence(
        id=identifier,
        tier="A",
        source_type="registry_metadata",
        title="Distribution metadata for example-framework 5.2",
        url="https://pypi.org/pypi/example-framework/5.2/json",
        substantiates="The declared installation gate.",
        expression=">=3.10",
        scheme="pep440",
        provenance=FETCHED,
    )


def constraint(*evidence_ids: str) -> ContextConstraint:
    return ContextConstraint(
        relation="requires",
        counterpart=PYTHON_ID,
        version_expression=">=3.10",
        version_scheme="pep440",
        condition=None,
        explanation="The distribution requires this runtime range.",
        evidence_ids=evidence_ids or ("ev-registry",),
    )


CATALOGUE: tuple[Evidence, ...] = (registry_evidence(),)


def context(
    *,
    release_found: bool = True,
    constraints: tuple[ContextConstraint, ...] = (),
    evidence: tuple[Evidence, ...] = CATALOGUE,
    lookups: tuple[SourceCheck, ...] = OK_LOOKUPS,
    marker_guarded: bool = False,
    extra_guarded: bool = False,
) -> ContextInput:
    return ContextInput(
        target=FRAMEWORK,
        release_found=release_found,
        constraints=constraints,
        evidence=evidence,
        lookups=lookups,
        marker_guarded=marker_guarded,
        extra_guarded=extra_guarded,
    )


# --------------------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------------------


def test_a_required_lookup_failure_outranks_a_missing_release() -> None:
    outcome = build_context(
        context(
            release_found=False,
            lookups=(check("pypi_json", "failed"),),
        )
    )
    assert isinstance(outcome, ContextUnknown)
    assert outcome.reason == "lookup_failed"


def test_a_missing_release_is_reported_as_such() -> None:
    outcome = build_context(context(release_found=False))
    assert isinstance(outcome, ContextUnknown)
    assert outcome.reason == "release_not_found"


def test_no_material_is_unknown_rather_than_an_empty_available() -> None:
    outcome = build_context(context())
    assert isinstance(outcome, ContextUnknown)
    assert outcome.reason == "evidence_not_found"


def test_a_single_constraint_makes_the_context_available() -> None:
    outcome = build_context(context(constraints=(constraint(),)))
    assert isinstance(outcome, ContextAvailable)
    assert outcome.constraints == (constraint(),)


# --------------------------------------------------------------------------------------
# limitations
# --------------------------------------------------------------------------------------


def test_limitations_use_the_same_rules_as_the_verdict_path() -> None:
    outcome = build_context(
        context(
            constraints=(constraint(),),
            lookups=(
                check("pypi_json", "ok"),
                check("python_release_cycle", "failed", required=False),
            ),
            marker_guarded=True,
            extra_guarded=True,
        )
    )
    assert tuple(limitation.code for limitation in outcome.limitations) == (
        "marker_guarded_claim",
        "extra_guarded_claim",
        "source_unavailable",
    )


def test_limitations_are_reported_on_an_unknown_context_too() -> None:
    outcome = build_context(
        context(lookups=(check("python_release_cycle", "skipped"),))
    )
    assert tuple(limitation.code for limitation in outcome.limitations) == (
        "source_unavailable",
    )


def test_a_clean_lookup_leaves_no_limitations() -> None:
    """With no snapshot to be missing, the ordinary response has nothing to disclaim."""
    outcome = build_context(context(constraints=(constraint(),)))
    assert outcome.limitations == ()


# --------------------------------------------------------------------------------------
# Invariant violations
# --------------------------------------------------------------------------------------


def test_a_constraint_must_cite_evidence() -> None:
    with pytest.raises(InvariantViolation):
        ContextConstraint(
            relation="requires",
            counterpart=PYTHON_ID,
            version_expression=">=3.10",
            version_scheme="pep440",
            condition=None,
            explanation="",
            evidence_ids=(),
        )


def test_a_dangling_evidence_reference_cannot_be_assembled() -> None:
    with pytest.raises(InvariantViolation, match="ev-missing"):
        context(constraints=(constraint("ev-missing"),))


def test_an_available_context_cannot_be_empty() -> None:
    with pytest.raises(InvariantViolation):
        ContextAvailable(constraints=(), notices=(), limitations=())


# --------------------------------------------------------------------------------------
# Totality
# --------------------------------------------------------------------------------------


def test_build_context_is_total_and_deterministic() -> None:
    generated = [
        context(
            release_found=release_found,
            constraints=constraints,
            lookups=lookups,
            marker_guarded=marker_guarded,
        )
        for release_found in (True, False)
        for constraints in ((), (constraint(),))
        for lookups in (
            OK_LOOKUPS,
            (check("pypi_json", "failed"),),
            (check("python_release_cycle", "failed", required=False),),
        )
        for marker_guarded in (True, False)
    ]
    for candidate in generated:
        outcome = build_context(candidate)
        assert isinstance(outcome, ContextAvailable | ContextUnknown)
        assert build_context(candidate) == outcome
