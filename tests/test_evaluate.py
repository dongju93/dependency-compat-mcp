"""Example tests for every branch of 03 steps 0-7, plus the three invariant properties.

The property tests use a hand-rolled generator (a cartesian product over small,
hand-picked option sets) rather than a randomised library: 03 requires the decision
procedure to be deterministic, and a test suite that generates a different corpus on each
run cannot make a determinism claim about the thing it is testing.
"""

from datetime import UTC, datetime
from itertools import product
from typing import Literal

import pytest

from dependency_compat_mcp.domain.claims import (
    Claim,
    CompatibilityStatement,
    Corroboration,
    InstallationGate,
    MarkerCondition,
    MarkerDecidability,
    ReleaseFacts,
    SourceCheck,
    YankedInfo,
)
from dependency_compat_mcp.domain.diagnostics import Limitation, Notice
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.evaluate import (
    CuratedCoverage,
    EvaluationInput,
    Supported,
    Unknown,
    Unsupported,
    Verdict,
    evaluate,
)
from dependency_compat_mcp.domain.relations import ResolvedRelation, resolve_relation
from dependency_compat_mcp.domain.targets import (
    Target,
    TargetId,
    VersionScheme,
    name_of,
    parse_target,
    version_of,
)
from dependency_compat_mcp.domain.versions import bounded_above

# --------------------------------------------------------------------------------------
# Fixtures. Names are placeholders; no real compatibility claim is made anywhere here.
# --------------------------------------------------------------------------------------

FRAMEWORK = parse_target("pypi", "example-framework", "5.2")
LIBRARY = parse_target("pypi", "example-library", "2.0")
PYTHON = parse_target("runtime", "python", "3.13")

FULLY_CURATED = CuratedCoverage(entry_present=True, verified_for_version=True)
OK_LOOKUPS = (SourceCheck(source="pypi_json", outcome="ok"),)


def relation_of(subject: Target, counterpart: Target) -> ResolvedRelation:
    resolution = resolve_relation(subject, counterpart)
    assert isinstance(resolution, ResolvedRelation)
    return resolution


PYTHON_RELATION = relation_of(FRAMEWORK, PYTHON)
REVERSED_RELATION = relation_of(PYTHON, FRAMEWORK)
DIST_RELATION = relation_of(FRAMEWORK, LIBRARY)


def moment(year: int) -> datetime:
    return datetime(year, 1, 1, tzinfo=UTC)


DECLARED_AT = moment(2025)
COUNTERPART_AT = moment(2024)


def facts(
    *,
    declaring_released_at: datetime | None = DECLARED_AT,
    declared_about_released_at: datetime | None = COUNTERPART_AT,
    declared_about_eol_at: datetime | None = None,
    declaring_yanked: YankedInfo | None = None,
    declared_about_yanked: YankedInfo | None = None,
) -> ReleaseFacts:
    return ReleaseFacts(
        declaring_released_at=declaring_released_at,
        declared_about_released_at=declared_about_released_at,
        declared_about_eol_at=declared_about_eol_at,
        declaring_yanked=declaring_yanked,
        declared_about_yanked=declared_about_yanked,
    )


def gate(
    expression: str = ">=3.10,<3.14",
    *,
    about: Target = PYTHON,
    scheme: VersionScheme = "pep440",
    condition: MarkerCondition | None = None,
    evidence_id: str = "ev-gate",
) -> InstallationGate:
    return InstallationGate(
        declared_about=TargetId.of(about),
        expression=expression,
        scheme=scheme,
        # Derived rather than passed so a fixture cannot claim a ceiling its own
        # expression does not have.
        bounded_above=bool(bounded_above(expression, scheme)),
        lower_bound=None,
        condition=condition,
        evidence_id=evidence_id,
    )


def statement(
    stance: Literal["supports", "excludes"] = "supports",
    expression: str = ">=3.10,<3.14",
    *,
    about: Target = PYTHON,
    scheme: VersionScheme = "pep440",
    evidence_id: str = "ev-statement",
) -> CompatibilityStatement:
    return CompatibilityStatement(
        declared_about=TargetId.of(about),
        stance=stance,
        expression=expression,
        scheme=scheme,
        evidence_id=evidence_id,
    )


def corroboration(
    *versions: str,
    about: Target = PYTHON,
    evidence_id: str = "ev-corroboration",
) -> Corroboration:
    return Corroboration(
        declared_about=TargetId.of(about),
        enumerated_versions=frozenset(versions or ("3.13",)),
        evidence_id=evidence_id,
    )


def marker(decidability: MarkerDecidability) -> MarkerCondition:
    return MarkerCondition(
        expression='python_version < "3.11"', decidability=decidability
    )


def evaluation(
    *claims: Claim,
    relation: ResolvedRelation = PYTHON_RELATION,
    release_facts: ReleaseFacts | None = None,
    lookups: tuple[SourceCheck, ...] = OK_LOOKUPS,
    curated: CuratedCoverage = FULLY_CURATED,
    declaring_release_found: bool = True,
    declared_about_release_found: bool = True,
) -> EvaluationInput:
    return EvaluationInput(
        relation=relation,
        claims=claims,
        facts=release_facts if release_facts is not None else facts(),
        lookups=lookups,
        curated=curated,
        declaring_release_found=declaring_release_found,
        declared_about_release_found=declared_about_release_found,
    )


def codes(verdict: Verdict) -> tuple[str, ...]:
    return tuple(limitation.code for limitation in verdict.limitations)


# --------------------------------------------------------------------------------------
# Step 0
# --------------------------------------------------------------------------------------


def test_required_lookup_failure_outranks_a_missing_release() -> None:
    """A failed lookup must not be reported as "no such release" (03 step 0)."""
    verdict = evaluate(
        evaluation(
            lookups=(SourceCheck(source="pypi_json", outcome="failed", required=True),),
            declaring_release_found=False,
        )
    )
    assert verdict == Unknown(reason="lookup_failed", notices=(), limitations=())


def test_optional_lookup_failure_is_a_limitation_not_a_verdict() -> None:
    verdict = evaluate(
        evaluation(
            gate(),
            lookups=(
                SourceCheck(source="curated_pack", outcome="failed", required=False),
            ),
        )
    )
    assert isinstance(verdict, Supported)
    assert codes(verdict) == ("source_unavailable",)


def test_skipped_required_lookup_is_a_limitation() -> None:
    verdict = evaluate(
        evaluation(
            gate(),
            lookups=(
                SourceCheck(source="curated_pack", outcome="skipped", required=True),
            ),
        )
    )
    assert codes(verdict) == ("source_unavailable",)


@pytest.mark.parametrize(
    ("declaring_found", "declared_about_found"),
    [(False, True), (True, False), (False, False)],
)
def test_missing_release_short_circuits(
    declaring_found: bool, declared_about_found: bool
) -> None:
    verdict = evaluate(
        evaluation(
            gate(),
            declaring_release_found=declaring_found,
            declared_about_release_found=declared_about_found,
        )
    )
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "release_not_found"


# --------------------------------------------------------------------------------------
# Step 1
# --------------------------------------------------------------------------------------


def test_environment_dependent_marker_is_indeterminate() -> None:
    verdict = evaluate(evaluation(gate(condition=marker("environment_dependent"))))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "insufficient_evidence"
    assert codes(verdict) == ("marker_guarded_claim",)


def test_extra_guarded_claim_is_indeterminate() -> None:
    verdict = evaluate(evaluation(gate(condition=marker("extra_guarded"))))
    assert isinstance(verdict, Unknown)
    assert codes(verdict) == ("extra_guarded_claim",)


def test_always_false_marker_drops_the_claim_without_a_limitation() -> None:
    """Nothing went unchecked: the dependency is simply not part of this install."""
    verdict = evaluate(evaluation(gate(condition=marker("always_false"))))
    assert verdict == Unknown(reason="evidence_not_found", notices=(), limitations=())


def test_always_true_marker_classifies_normally() -> None:
    verdict = evaluate(evaluation(gate(condition=marker("always_true"))))
    assert isinstance(verdict, Supported)


def test_scheme_mismatch_is_indeterminate() -> None:
    """A SemVer range is never compared against a PEP 440 version (03 [4])."""
    verdict = evaluate(evaluation(gate("^3.10.0", scheme="semver")))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "insufficient_evidence"
    assert codes(verdict) == ()


def test_statement_whose_range_misses_the_version_is_not_an_exclusion() -> None:
    """Absence is not evidence: `supports >=3.10,<3.13` says nothing about 3.13."""
    verdict = evaluate(evaluation(statement("supports", ">=3.10,<3.13")))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "insufficient_evidence"


def test_corroboration_matches_a_release_prefix() -> None:
    patch_release = parse_target("runtime", "python", "3.13.1")
    verdict = evaluate(
        evaluation(
            gate(">=3.10", about=patch_release),
            corroboration("3.13", about=patch_release),
            relation=relation_of(FRAMEWORK, patch_release),
            release_facts=facts(declared_about_released_at=moment(2026)),
        )
    )
    assert isinstance(verdict, Supported)
    assert verdict.verdict_evidence_ids == ("ev-gate", "ev-corroboration")


def test_corroboration_that_does_not_enumerate_the_version_is_dropped() -> None:
    verdict = evaluate(evaluation(corroboration("3.10", "3.11")))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "evidence_not_found"


def test_a_non_numeric_enumeration_label_never_matches() -> None:
    """Tier C labels carry no scheme, so anything unparseable is simply not a match."""
    verdict = evaluate(evaluation(corroboration("Implementation :: CPython", "3.x")))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "evidence_not_found"


# --------------------------------------------------------------------------------------
# Step 2
# --------------------------------------------------------------------------------------


def test_violated_gate_is_unsupported() -> None:
    verdict = evaluate(evaluation(gate(">=3.8,<3.12")))
    assert verdict == Unsupported(
        verdict_evidence_ids=("ev-gate",), notices=(), limitations=()
    )


def test_violated_gate_beats_a_contradicting_statement() -> None:
    """The verdict follows the mechanical fact; the contradiction is reported, not hidden."""
    verdict = evaluate(
        evaluation(gate(">=3.8,<3.12"), statement("supports", ">=3.10,<3.14"))
    )
    assert isinstance(verdict, Unsupported)
    assert verdict.verdict_evidence_ids == ("ev-gate",)
    assert verdict.notices == (
        Notice(
            code="gate_contradicts_statement", evidence_ids=("ev-gate", "ev-statement")
        ),
    )


def test_violated_gate_without_a_statement_raises_no_notice() -> None:
    verdict = evaluate(evaluation(gate(">=3.8,<3.12"), corroboration("3.13")))
    assert verdict.notices == ()


# --------------------------------------------------------------------------------------
# Steps 3 and 4
# --------------------------------------------------------------------------------------


def test_two_statements_disagreeing_is_conflicting_evidence() -> None:
    verdict = evaluate(
        evaluation(
            statement("supports", ">=3.10,<3.14", evidence_id="ev-supports"),
            statement("excludes", "==3.13", evidence_id="ev-excludes"),
        )
    )
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "conflicting_evidence"


def test_statement_excluding_the_version_is_unsupported() -> None:
    verdict = evaluate(evaluation(statement("excludes", ">=3.13")))
    assert verdict == Unsupported(
        verdict_evidence_ids=("ev-statement",), notices=(), limitations=()
    )


def test_satisfied_gate_does_not_override_an_explicit_exclusion() -> None:
    verdict = evaluate(
        evaluation(gate(">=3.10,<3.14"), statement("excludes", "==3.13"))
    )
    assert isinstance(verdict, Unsupported)
    assert verdict.verdict_evidence_ids == ("ev-statement",)


# --------------------------------------------------------------------------------------
# Step 5
# --------------------------------------------------------------------------------------


def test_closed_upper_bound_is_supported() -> None:
    verdict = evaluate(evaluation(gate(">=3.10,<3.14")))
    assert verdict == Supported(
        verdict_evidence_ids=("ev-gate",), notices=(), limitations=()
    )


def test_open_upper_bound_over_a_newer_counterpart_is_unknown() -> None:
    verdict = evaluate(
        evaluation(
            gate(">=3.10"), release_facts=facts(declared_about_released_at=moment(2026))
        )
    )
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "insufficient_evidence"
    assert codes(verdict) == ("open_upper_bound",)


def test_open_upper_bound_with_corroboration_is_supported() -> None:
    verdict = evaluate(
        evaluation(
            gate(">=3.10"),
            corroboration("3.13"),
            release_facts=facts(declared_about_released_at=moment(2026)),
        )
    )
    assert isinstance(verdict, Supported)
    assert verdict.verdict_evidence_ids == ("ev-gate", "ev-corroboration")


def test_open_upper_bound_with_a_statement_is_supported() -> None:
    verdict = evaluate(
        evaluation(
            gate(">=3.10"),
            statement("supports", "==3.13"),
            release_facts=facts(declared_about_released_at=moment(2026)),
        )
    )
    assert isinstance(verdict, Supported)
    assert verdict.verdict_evidence_ids == ("ev-gate", "ev-statement")


def test_counterpart_already_eol_at_release_time_is_unknown() -> None:
    verdict = evaluate(
        evaluation(
            gate(">=3.10"),
            release_facts=facts(
                declared_about_released_at=moment(2020),
                declared_about_eol_at=moment(2024),
            ),
        )
    )
    assert isinstance(verdict, Unknown)
    assert codes(verdict) == ("stale_lower_bound",)


def test_eol_counterpart_with_corroboration_is_supported() -> None:
    verdict = evaluate(
        evaluation(
            gate(">=3.10"),
            corroboration("3.13"),
            release_facts=facts(
                declared_about_released_at=moment(2020),
                declared_about_eol_at=moment(2024),
            ),
        )
    )
    assert isinstance(verdict, Supported)


def test_eol_after_the_declaring_release_does_not_fire() -> None:
    verdict = evaluate(
        evaluation(
            gate(">=3.10"),
            release_facts=facts(
                declared_about_released_at=moment(2020),
                declared_about_eol_at=moment(2030),
            ),
        )
    )
    assert isinstance(verdict, Supported)


def test_unknown_release_dates_do_not_fire_the_temporal_branches() -> None:
    verdict = evaluate(
        evaluation(gate(">=3.10"), release_facts=facts(declaring_released_at=None))
    )
    assert isinstance(verdict, Supported)
    assert codes(verdict) == ()


def test_pypi_by_pypi_has_no_eol_data_and_short_circuits_to_supported() -> None:
    """No release table exists for a package pair, so the floor check cannot fire."""
    verdict = evaluate(
        evaluation(
            gate(">=1.0", about=LIBRARY),
            relation=DIST_RELATION,
            release_facts=facts(
                declared_about_released_at=moment(2020), declared_about_eol_at=None
            ),
        )
    )
    assert verdict == Supported(
        verdict_evidence_ids=("ev-gate",), notices=(), limitations=()
    )


# --------------------------------------------------------------------------------------
# Step 6
# --------------------------------------------------------------------------------------


def test_statement_alone_is_supported() -> None:
    verdict = evaluate(evaluation(statement("supports", ">=3.10,<3.14")))
    assert verdict == Supported(
        verdict_evidence_ids=("ev-statement",), notices=(), limitations=()
    )


def test_corroboration_alone_is_never_supported() -> None:
    verdict = evaluate(evaluation(corroboration("3.13")))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "insufficient_evidence"
    assert codes(verdict) == ("tier_c_only",)


def test_indeterminate_only_is_insufficient_evidence() -> None:
    verdict = evaluate(evaluation(gate("^3.10.0", scheme="semver")))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "insufficient_evidence"


def test_no_claims_on_a_dependency_rule_is_no_declared_relationship() -> None:
    verdict = evaluate(evaluation(relation=DIST_RELATION))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "no_declared_relationship"


def test_no_claims_on_a_non_dependency_rule_is_evidence_not_found() -> None:
    verdict = evaluate(evaluation(relation=PYTHON_RELATION))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "evidence_not_found"


# --------------------------------------------------------------------------------------
# Yanked releases and limitations
# --------------------------------------------------------------------------------------


def test_yanked_notices_follow_the_callers_argument_order() -> None:
    verdict = evaluate(
        evaluation(
            gate(),
            release_facts=facts(
                declaring_yanked=YankedInfo(reason="broken wheel"),
                declared_about_yanked=YankedInfo(reason=None),
            ),
        )
    )
    assert verdict.notices == (Notice("subject_yanked"), Notice("counterpart_yanked"))


def test_reversed_direction_swaps_the_yanked_notices() -> None:
    """`declaring` is the counterpart here, so its yank is the *counterpart's*."""
    verdict = evaluate(
        evaluation(
            gate(),
            relation=REVERSED_RELATION,
            release_facts=facts(declaring_yanked=YankedInfo(reason=None)),
        )
    )
    assert REVERSED_RELATION.direction == "reversed"
    assert verdict.notices == (Notice("counterpart_yanked"),)


def test_reversed_direction_maps_a_yanked_declared_about_to_the_subject() -> None:
    verdict = evaluate(
        evaluation(
            gate(),
            relation=REVERSED_RELATION,
            release_facts=facts(declared_about_yanked=YankedInfo(reason=None)),
        )
    )
    assert verdict.notices == (Notice("subject_yanked"),)


def test_missing_curated_entry_is_reported_on_a_supported_verdict() -> None:
    verdict = evaluate(
        evaluation(
            gate(),
            curated=CuratedCoverage(entry_present=False, verified_for_version=False),
        )
    )
    assert isinstance(verdict, Supported)
    assert verdict.limitations == (Limitation("curated_pack_missing"),)


def test_unverified_curated_entry_is_reported() -> None:
    verdict = evaluate(
        evaluation(
            gate(),
            curated=CuratedCoverage(entry_present=True, verified_for_version=False),
        )
    )
    assert codes(verdict) == ("curated_not_verified_for_version",)


def test_limitations_are_ordered_by_the_declared_code_order() -> None:
    verdict = evaluate(
        evaluation(
            gate(">=3.10", condition=marker("environment_dependent")),
            corroboration("3.13"),
            curated=CuratedCoverage(entry_present=False, verified_for_version=False),
            lookups=(
                SourceCheck(source="curated_pack", outcome="failed", required=False),
            ),
        )
    )
    assert codes(verdict) == (
        "tier_c_only",
        "curated_pack_missing",
        "marker_guarded_claim",
        "source_unavailable",
    )


# --------------------------------------------------------------------------------------
# Invariant violations: server defects, never a normal `unknown`
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claim",
    [
        gate(">=1.0", about=LIBRARY),
        statement("supports", ">=1.0", about=LIBRARY),
        corroboration("2.0", about=LIBRARY),
    ],
)
def test_claim_about_another_target_cannot_be_evaluated(claim: Claim) -> None:
    """An adapter that normalised a claim for a different relation is a server defect."""
    with pytest.raises(InvariantViolation, match="declared about"):
        evaluation(claim)


def test_a_verdict_cannot_be_built_without_evidence() -> None:
    with pytest.raises(InvariantViolation):
        Supported(verdict_evidence_ids=(), notices=(), limitations=())
    with pytest.raises(InvariantViolation):
        Unsupported(verdict_evidence_ids=(), notices=(), limitations=())


# --------------------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------------------

_CLAIM_SETS: tuple[tuple[str, ...], ...] = (
    (),
    ("gate_ok",),
    ("gate_bad",),
    ("gate_open",),
    ("gate_marker",),
    ("gate_extra",),
    ("gate_dead",),
    ("gate_semver",),
    ("supports",),
    ("excludes",),
    ("corroborates",),
    ("gate_ok", "supports"),
    ("gate_bad", "supports"),
    ("gate_open", "corroborates"),
    ("gate_open", "supports"),
    ("supports", "excludes"),
    ("corroborates", "gate_marker"),
    ("gate_ok", "excludes"),
)

_FACT_SETS: tuple[ReleaseFacts, ...] = (
    facts(),
    facts(declared_about_released_at=moment(2026)),
    facts(declared_about_released_at=moment(2020), declared_about_eol_at=moment(2024)),
    facts(declaring_released_at=None, declared_about_released_at=None),
    facts(
        declaring_yanked=YankedInfo(reason=None),
        declared_about_yanked=YankedInfo(reason="withdrawn"),
    ),
)

_LOOKUP_SETS: tuple[tuple[SourceCheck, ...], ...] = (
    OK_LOOKUPS,
    (SourceCheck(source="pypi_json", outcome="failed", required=True),),
    (SourceCheck(source="curated_pack", outcome="failed", required=False),),
    (SourceCheck(source="curated_pack", outcome="skipped", required=True),),
)

_CURATED_SETS: tuple[CuratedCoverage, ...] = (
    FULLY_CURATED,
    CuratedCoverage(entry_present=True, verified_for_version=False),
    CuratedCoverage(entry_present=False, verified_for_version=False),
)

_RELATIONS: tuple[tuple[ResolvedRelation, Target], ...] = (
    (PYTHON_RELATION, PYTHON_RELATION.declared_about),
    (REVERSED_RELATION, REVERSED_RELATION.declared_about),
    (DIST_RELATION, DIST_RELATION.declared_about),
)

# Ranges are chosen per target so a generated input stays *valid* rather than merely
# well-typed: every claim must speak the scheme of the version it is compared against.
_RANGES: dict[str, tuple[str, str, str]] = {
    "example-library": ("==2.0", "==1.0", ">=1.0"),
    "python": ("==3.13", "==3.9", ">=3.10"),
}


def _claim(kind: str, about: Target) -> Claim:
    """Build one claim of ``kind`` aimed at ``about``."""
    admitted, rejected, floor = _RANGES[str(name_of(about))]
    enumerated = str(version_of(about))

    match kind:
        case "gate_ok":
            return gate(admitted, about=about, evidence_id="ev-gate")
        case "gate_bad":
            return gate(rejected, about=about, evidence_id="ev-gate")
        case "gate_open":
            return gate(floor, about=about, evidence_id="ev-gate")
        case "gate_marker":
            return gate(
                floor,
                about=about,
                condition=marker("environment_dependent"),
                evidence_id="ev-marker",
            )
        case "gate_extra":
            return gate(
                floor,
                about=about,
                condition=marker("extra_guarded"),
                evidence_id="ev-extra",
            )
        case "gate_dead":
            return gate(
                floor,
                about=about,
                condition=marker("always_false"),
                evidence_id="ev-dead",
            )
        case "gate_semver":
            return gate("^1.0.0", about=about, scheme="semver", evidence_id="ev-semver")
        case "supports":
            return statement(
                "supports", admitted, about=about, evidence_id="ev-supports"
            )
        case "excludes":
            return statement(
                "excludes", admitted, about=about, evidence_id="ev-excludes"
            )
        case "corroborates":
            return corroboration(enumerated, about=about, evidence_id="ev-corroborates")
        case _:  # pragma: no cover - the table above is closed
            raise AssertionError(f"unknown claim kind {kind!r}")


def _generated_inputs() -> list[EvaluationInput]:
    generated: list[EvaluationInput] = []
    for kinds, release_facts, lookups, curated, (relation, about), found in product(
        _CLAIM_SETS, _FACT_SETS, _LOOKUP_SETS, _CURATED_SETS, _RELATIONS, (True, False)
    ):
        generated.append(
            EvaluationInput(
                relation=relation,
                claims=tuple(_claim(kind, about) for kind in kinds),
                facts=release_facts,
                lookups=lookups,
                curated=curated,
                declaring_release_found=True,
                declared_about_release_found=found,
            )
        )
    return generated


GENERATED = _generated_inputs()


def test_the_generator_covers_a_meaningful_corpus() -> None:
    expected = (
        len(_CLAIM_SETS)
        * len(_FACT_SETS)
        * len(_LOOKUP_SETS)
        * len(_CURATED_SETS)
        * len(_RELATIONS)
        * 2
    )
    assert len(GENERATED) == expected


def test_evaluate_is_total_over_every_valid_input() -> None:
    """Every valid input yields one of exactly three variants - never an exception."""
    for evaluation_input in GENERATED:
        verdict = evaluate(evaluation_input)
        assert isinstance(verdict, Supported | Unsupported | Unknown)


def test_evaluate_is_deterministic() -> None:
    for evaluation_input in GENERATED:
        assert evaluate(evaluation_input) == evaluate(evaluation_input)


def test_tier_c_alone_never_yields_supported() -> None:
    considered = 0
    for evaluation_input in GENERATED:
        if not evaluation_input.claims:
            continue
        if not all(
            isinstance(claim, Corroboration) for claim in evaluation_input.claims
        ):
            continue
        considered += 1
        assert not isinstance(evaluate(evaluation_input), Supported)
    assert considered > 0


def test_decided_verdicts_always_cite_evidence() -> None:
    decided = 0
    for evaluation_input in GENERATED:
        verdict = evaluate(evaluation_input)
        if isinstance(verdict, Supported | Unsupported):
            decided += 1
            assert verdict.verdict_evidence_ids
            claimed = {claim.evidence_id for claim in evaluation_input.claims}
            assert set(verdict.verdict_evidence_ids) <= claimed
    assert decided > 0


def test_notice_evidence_ids_come_from_the_claims() -> None:
    for evaluation_input in GENERATED:
        claimed = {claim.evidence_id for claim in evaluation_input.claims}
        for notice in evaluate(evaluation_input).notices:
            assert set(notice.evidence_ids) <= claimed
