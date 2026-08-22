"""Tests for `get_compatibility_context` assembly (03 "`get_compatibility_context` 처리").

The two facts this tool has to get right are ``availability`` (is there any material at
all) and ``depth`` (is any of it human-reviewed). Both are computed, so both are asserted
here against every shape the assembler can be handed.
"""

from datetime import UTC, date, datetime

import pytest

from dependency_compat_mcp.domain.claims import (
    Curated,
    Evidence,
    Fetched,
    NarrativeEvidence,
    SourceCheck,
    VersionConstraintEvidence,
)
from dependency_compat_mcp.domain.context import (
    ContextAvailable,
    ContextChange,
    ContextConstraint,
    ContextInput,
    ContextUnknown,
    build_context,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.evaluate import CuratedCoverage
from dependency_compat_mcp.domain.targets import TargetId, parse_target

FRAMEWORK = parse_target("pypi", "example-framework", "5.2")
PYTHON_ID = TargetId.of(parse_target("runtime", "python", "3.13"))

FULLY_CURATED = CuratedCoverage(entry_present=True, verified_for_version=True)


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
REVIEWED = Curated(reviewed_at=date(2026, 8, 10), pack_version="2026.08.1")


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


def curated_evidence(identifier: str = "ev-curated") -> Evidence:
    return NarrativeEvidence(
        id=identifier,
        tier="B",
        source_type="official_release_note",
        title="Version 5.2 release notes",
        url="https://example.invalid/release-notes",
        substantiates="The documented API removal.",
        provenance=REVIEWED,
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


def change(*evidence_ids: str) -> ContextChange:
    return ContextChange(
        category="removal",
        area="removed_api",
        summary="The API was removed in this release.",
        evidence_ids=evidence_ids or ("ev-curated",),
    )


CATALOGUE: tuple[Evidence, ...] = (registry_evidence(), curated_evidence())


def context(
    *,
    release_found: bool = True,
    constraints: tuple[ContextConstraint, ...] = (),
    changes: tuple[ContextChange, ...] = (),
    evidence: tuple[Evidence, ...] = CATALOGUE,
    lookups: tuple[SourceCheck, ...] = OK_LOOKUPS,
    curated: CuratedCoverage = FULLY_CURATED,
    marker_guarded: bool = False,
    extra_guarded: bool = False,
) -> ContextInput:
    return ContextInput(
        target=FRAMEWORK,
        release_found=release_found,
        constraints=constraints,
        changes=changes,
        evidence=evidence,
        lookups=lookups,
        curated=curated,
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
    assert outcome.depth == "registry_only"


def test_a_single_constraint_makes_the_context_available() -> None:
    outcome = build_context(context(constraints=(constraint(),)))
    assert isinstance(outcome, ContextAvailable)
    assert outcome.constraints == (constraint(),)
    assert outcome.changes == ()


def test_a_single_change_makes_the_context_available() -> None:
    outcome = build_context(context(changes=(change(),)))
    assert isinstance(outcome, ContextAvailable)


# --------------------------------------------------------------------------------------
# depth
# --------------------------------------------------------------------------------------


def test_registry_only_material_reports_registry_only() -> None:
    outcome = build_context(context(constraints=(constraint("ev-registry"),)))
    assert outcome.depth == "registry_only"


def test_curated_material_reports_registry_and_curated() -> None:
    outcome = build_context(context(changes=(change("ev-curated"),)))
    assert outcome.depth == "registry_and_curated"


def test_unused_curated_evidence_does_not_raise_the_depth() -> None:
    """Depth describes what the response *says*, not what the catalogue happens to hold."""
    outcome = build_context(context(constraints=(constraint("ev-registry"),)))
    assert outcome.depth == "registry_only"


def test_depth_is_reported_even_when_nothing_was_found() -> None:
    outcome = build_context(context(release_found=False))
    assert outcome.depth == "registry_only"


# --------------------------------------------------------------------------------------
# limitations
# --------------------------------------------------------------------------------------


def test_limitations_use_the_same_rules_as_the_verdict_path() -> None:
    outcome = build_context(
        context(
            constraints=(constraint(),),
            curated=CuratedCoverage(entry_present=False, verified_for_version=False),
            lookups=(check("curated_pack", "failed", required=False),),
            marker_guarded=True,
            extra_guarded=True,
        )
    )
    assert tuple(limitation.code for limitation in outcome.limitations) == (
        "curated_pack_missing",
        "marker_guarded_claim",
        "extra_guarded_claim",
        "source_unavailable",
    )


def test_an_unverified_curated_entry_is_reported() -> None:
    outcome = build_context(
        context(
            constraints=(constraint(),),
            curated=CuratedCoverage(entry_present=True, verified_for_version=False),
        )
    )
    assert tuple(limitation.code for limitation in outcome.limitations) == (
        "curated_not_verified_for_version",
    )


def test_limitations_are_reported_on_an_unknown_context_too() -> None:
    outcome = build_context(
        context(
            curated=CuratedCoverage(entry_present=False, verified_for_version=False)
        )
    )
    assert tuple(limitation.code for limitation in outcome.limitations) == (
        "curated_pack_missing",
    )


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


def test_a_change_must_cite_evidence() -> None:
    with pytest.raises(InvariantViolation):
        ContextChange(category="removal", area="api", summary="", evidence_ids=())


def test_a_dangling_evidence_reference_cannot_be_assembled() -> None:
    with pytest.raises(InvariantViolation, match="ev-missing"):
        context(constraints=(constraint("ev-missing"),))


def test_a_dangling_change_reference_cannot_be_assembled() -> None:
    with pytest.raises(InvariantViolation, match="ev-missing"):
        context(changes=(change("ev-missing"),))


def test_an_available_context_cannot_be_empty() -> None:
    with pytest.raises(InvariantViolation):
        ContextAvailable(
            depth="registry_only",
            constraints=(),
            changes=(),
            notices=(),
            limitations=(),
        )


# --------------------------------------------------------------------------------------
# Totality
# --------------------------------------------------------------------------------------


def test_build_context_is_total_and_deterministic() -> None:
    generated = [
        context(
            release_found=release_found,
            constraints=constraints,
            changes=changes,
            lookups=lookups,
            curated=curated,
        )
        for release_found in (True, False)
        for constraints in ((), (constraint(),))
        for changes in ((), (change(),))
        for lookups in (
            OK_LOOKUPS,
            (check("pypi_json", "failed"),),
        )
        for curated in (
            FULLY_CURATED,
            CuratedCoverage(entry_present=False, verified_for_version=False),
        )
    ]
    for candidate in generated:
        outcome = build_context(candidate)
        assert isinstance(outcome, ContextAvailable | ContextUnknown)
        assert build_context(candidate) == outcome
