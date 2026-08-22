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
from dependency_compat_mcp.domain.diagnostics import (
    CAUSE_KINDS,
    ClaimGuard,
    ConditionalClaim,
    Limitation,
    Notice,
    UnprovenClaim,
    cause_evidence_ids,
    cause_kind,
)
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


def check(
    source: str,
    outcome: str,
    *,
    target: Target = FRAMEWORK,
    role: str = "declaring",
    required: bool = True,
) -> SourceCheck:
    """A lookup record. Every check names the release it was made for."""
    return SourceCheck(
        source=source,  # pyrefly: ignore[bad-argument-type]
        target=target,
        role=role,  # pyrefly: ignore[bad-argument-type]
        outcome=outcome,  # pyrefly: ignore[bad-argument-type]
        required=required,
    )


OK_LOOKUPS = (check("pypi_json", "ok"),)


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


def causes(verdict: Verdict) -> tuple[str, ...]:
    """The cause kinds, in the order `summaries` reads them."""
    assert isinstance(verdict, Unknown)
    return tuple(cause_kind(cause) for cause in verdict.causes)


# --------------------------------------------------------------------------------------
# Step 0
# --------------------------------------------------------------------------------------


def test_required_lookup_failure_outranks_a_missing_release() -> None:
    """A failed lookup must not be reported as "no such release" (03 step 0)."""
    verdict = evaluate(
        evaluation(
            lookups=(check("pypi_json", "failed"),),
            declaring_release_found=False,
        )
    )
    assert verdict == Unknown(reason="lookup_failed", notices=(), limitations=())


def test_optional_lookup_failure_is_a_limitation_not_a_verdict() -> None:
    verdict = evaluate(
        evaluation(
            gate(),
            lookups=(check("curated_pack", "failed", required=False),),
        )
    )
    assert isinstance(verdict, Supported)
    assert codes(verdict) == ("source_unavailable",)


def test_skipped_required_lookup_is_a_limitation() -> None:
    verdict = evaluate(
        evaluation(
            gate(),
            lookups=(check("curated_pack", "skipped"),),
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
    # The cause cites the very gate whose range was open, so the caller can read it.
    assert verdict.causes == (
        UnprovenClaim(kind="open_upper_bound", evidence_ids=("ev-gate",)),
    )
    assert codes(verdict) == ()


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
    assert verdict.causes == (
        UnprovenClaim(kind="stale_lower_bound", evidence_ids=("ev-gate",)),
    )
    assert codes(verdict) == ()


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
    assert verdict.causes == (
        UnprovenClaim(kind="tier_c_only", evidence_ids=("ev-corroboration",)),
    )
    assert codes(verdict) == ()


def test_indeterminate_only_is_insufficient_evidence() -> None:
    verdict = evaluate(evaluation(gate("^3.10.0", scheme="semver")))
    assert isinstance(verdict, Unknown)
    assert verdict.reason == "insufficient_evidence"
    # A semver range against a PEP 440 version is never compared, so the cause says the
    # claim could not be read rather than that it excluded anything.
    assert verdict.causes == (
        UnprovenClaim(kind="uncomparable_claim", evidence_ids=("ev-gate",)),
    )


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
            lookups=(check("curated_pack", "failed", required=False),),
        )
    )
    assert codes(verdict) == (
        "curated_pack_missing",
        "marker_guarded_claim",
        "source_unavailable",
    )
    # Coverage gaps and the reasons the verdict stayed open are reported apart: neither
    # list restates the other, and only the second cites evidence.
    assert causes(verdict) == ("conditional_claim", "tier_c_only")


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


def test_a_cause_cannot_be_built_without_evidence() -> None:
    """Same rule as a verdict: the field exists to hand the caller something to read.

    Enforced in the domain rather than only by the response model's `minItems`, so an
    unevidenced cause is an impossible value instead of a serialisation failure.
    """
    with pytest.raises(InvariantViolation, match="must cite an evidence id"):
        UnprovenClaim(kind="tier_c_only", evidence_ids=())
    with pytest.raises(InvariantViolation, match="must cite an evidence id"):
        ConditionalClaim(
            condition=ClaimGuard(
                kind="extra_marker", expression='extra == "dev"', variables=("extra",)
            ),
            evidence_ids=(),
        )


def test_insufficient_evidence_cannot_be_built_without_a_cause() -> None:
    """The one reason that is a category rather than a fact must say which fact it is."""
    with pytest.raises(InvariantViolation, match="decision causes"):
        Unknown(reason="insufficient_evidence", notices=(), limitations=())


def test_no_other_reason_may_carry_a_cause() -> None:
    """`lookup_failed` and friends already name their own cause; a second one would
    invite the caller to read a claim the server never made."""
    with pytest.raises(InvariantViolation, match="decision causes"):
        Unknown(
            reason="lookup_failed",
            notices=(),
            limitations=(),
            causes=(UnprovenClaim(kind="tier_c_only", evidence_ids=("ev-1",)),),
        )


def test_causes_are_ordered_by_the_type_not_by_the_caller() -> None:
    """`summaries` narrates `causes[0]`, so the order cannot depend on construction site."""
    verdict = Unknown(
        reason="insufficient_evidence",
        notices=(),
        limitations=(),
        causes=(
            UnprovenClaim(kind="uncomparable_claim", evidence_ids=("ev-2",)),
            ConditionalClaim(
                condition=ClaimGuard(
                    kind="environment_marker",
                    expression='sys_platform == "win32"',
                    variables=("sys_platform",),
                ),
                evidence_ids=("ev-1",),
            ),
        ),
    )
    assert causes(verdict) == ("conditional_claim", "uncomparable_claim")


def test_causes_sharing_an_identity_are_merged_and_keep_every_source() -> None:
    verdict = Unknown(
        reason="insufficient_evidence",
        notices=(),
        limitations=(),
        causes=(
            UnprovenClaim(kind="tier_c_only", evidence_ids=("ev-1",)),
            UnprovenClaim(kind="tier_c_only", evidence_ids=("ev-2",)),
        ),
    )
    assert verdict.causes == (
        UnprovenClaim(kind="tier_c_only", evidence_ids=("ev-1", "ev-2")),
    )


def test_two_different_markers_stay_two_causes() -> None:
    """Merging them by kind would hide one of the two conditions from the caller."""

    def guarded(expression: str, variable: str, evidence: str) -> ConditionalClaim:
        return ConditionalClaim(
            condition=ClaimGuard(
                kind="environment_marker",
                expression=expression,
                variables=(variable,),
            ),
            evidence_ids=(evidence,),
        )

    verdict = Unknown(
        reason="insufficient_evidence",
        notices=(),
        limitations=(),
        causes=(
            guarded('sys_platform == "win32"', "sys_platform", "ev-1"),
            guarded('python_version < "3.11"', "python_version", "ev-2"),
        ),
    )
    assert len(verdict.causes) == 2


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
    ("supports_other",),
    ("excludes",),
    ("corroborates",),
    ("gate_ok", "supports"),
    ("gate_bad", "supports"),
    ("gate_open", "corroborates"),
    ("gate_open", "supports"),
    ("supports", "excludes"),
    ("corroborates", "gate_marker"),
    # Two claims that each decide nothing, for a differently-shaped pair of causes than
    # the marker/tier-C combination above.
    ("supports_other", "corroborates"),
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
    (check("pypi_json", "failed"),),
    (check("curated_pack", "failed", required=False),),
    (check("curated_pack", "skipped"),),
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
        case "supports_other":
            # A range the author really did write, about some *other* release. Reading it
            # as an exclusion of this one would be absence-as-evidence, so it decides
            # nothing - and that is a distinct reason from "could not be compared at all".
            return statement(
                "supports", rejected, about=about, evidence_id="ev-supports-other"
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


def test_every_insufficient_evidence_verdict_names_a_cause() -> None:
    """The property that matters, stated over the whole corpus rather than an example.

    `insufficient_evidence` is the one reason whose name is a category, so it is the one
    that can degenerate into "unknown, and the server will not say why". Asserting it here
    - across every generated combination of claims, facts, lookups, coverage and relation -
    is what makes it a general guarantee rather than a property of the cases someone
    happened to try.
    """
    considered = 0
    for evaluation_input in GENERATED:
        verdict = evaluate(evaluation_input)
        if not isinstance(verdict, Unknown):
            continue
        if verdict.reason == "insufficient_evidence":
            considered += 1
            assert verdict.causes, evaluation_input
        else:
            # And the converse: a reason that already names its own cause carries none.
            assert not verdict.causes, evaluation_input
    assert considered > 0


def test_every_cause_evidence_id_comes_from_the_claims() -> None:
    """A cause must point at something the caller can actually go and read."""
    for evaluation_input in GENERATED:
        verdict = evaluate(evaluation_input)
        if not isinstance(verdict, Unknown):
            continue
        claimed = {claim.evidence_id for claim in evaluation_input.claims}
        for cause in verdict.causes:
            assert cause_evidence_ids(cause)
            assert set(cause_evidence_ids(cause)) <= claimed, evaluation_input


def test_every_produced_cause_kind_is_reachable_from_the_corpus() -> None:
    """Guards against a cause kind that exists only to make one example read well.

    A kind the decision procedure cannot produce is dead weight in a closed set the caller
    is told to branch on, so the set is asserted against what 6k generated inputs actually
    yield rather than against the list itself.
    """
    produced = {
        cause_kind(cause)
        for evaluation_input in GENERATED
        for cause in getattr(evaluate(evaluation_input), "causes", ())
    }
    assert produced == set(CAUSE_KINDS), set(CAUSE_KINDS) - produced
