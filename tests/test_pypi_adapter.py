"""Fixture tests for the PyPI adapter.

03 "테스트 전략" asks adapters to be tested against recorded response shapes plus the
abnormal cases: malformed JSON, missing fields, an oversized response and a rate limit.
Those four arrive as :class:`HttpFailed` values here, because :mod:`infra.http` has already
turned them into codes; what this file checks is that the adapter preserves the code
instead of collapsing every failure into one.

No test touches the network: the adapter is given a fake :class:`JsonFetcher`.
"""

import asyncio
import json
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dependency_compat_mcp.adapters.protocol import (
    LookupFailed,
    ReleaseDocument,
    ReleaseNotFound,
    evidence_index,
    select_claims,
)
from dependency_compat_mcp.adapters.pypi import PyPIAdapter, parse_release
from dependency_compat_mcp.domain.claims import (
    Corroboration,
    Fetched,
    InstallationGate,
    NarrativeEvidence,
    VersionConstraintEvidence,
    claim_evidence_id,
)
from dependency_compat_mcp.domain.targets import (
    CanonicalName,
    PyPITarget,
    TargetId,
    parse_target,
)
from dependency_compat_mcp.infra.http import (
    HttpFailed,
    HttpNotFound,
    HttpOk,
    HttpResult,
)

FIXTURES = Path(__file__).parent / "fixtures" / "registry"
RETRIEVED_AT = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
PYTHON = TargetId(namespace="runtime", name=CanonicalName("python"))
SAMPLE_URL = "https://pypi.org/pypi/sample-project/5.2.1/json"
SAMPLE_PAGE = "https://pypi.org/project/sample-project/5.2.1/"


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive one coroutine to completion without an async pytest plugin."""
    return asyncio.run(coroutine)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def pypi_target(name: str = "sample-project", version: str = "5.2.1") -> PyPITarget:
    target = parse_target("pypi", name, version)
    assert isinstance(target, PyPITarget)
    return target


class FakeFetcher:
    """A :class:`JsonFetcher` that answers from memory and records what was asked for."""

    def __init__(self, result: HttpResult) -> None:
        self.result = result
        self.requested: list[str] = []

    async def get_json(self, url: str) -> HttpResult:
        self.requested.append(url)
        return self.result


def ok(payload: object, url: str = SAMPLE_URL) -> FakeFetcher:
    return FakeFetcher(HttpOk(url=url, payload=payload, retrieved_at=RETRIEVED_AT))


def fetch(fetcher: FakeFetcher, target: PyPITarget | None = None) -> Any:
    return run(PyPIAdapter(fetcher).fetch_release(target or pypi_target()))


def sample_document() -> ReleaseDocument:
    document = parse_release(
        pypi_target(), load("pypi_sample_project_5.2.1.json"), retrieved_at=RETRIEVED_AT
    )
    assert isinstance(document, ReleaseDocument)
    return document


# --------------------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------------------


def test_the_exact_release_endpoint_is_requested() -> None:
    fetcher = ok(load("pypi_sample_project_5.2.1.json"))
    fetch(fetcher)
    # The version is in the path, so PyPI - not the adapter - decides whether it exists.
    assert fetcher.requested == [SAMPLE_URL]


def test_the_adapter_advertises_its_namespace_and_source() -> None:
    adapter = PyPIAdapter(ok({}))
    assert adapter.namespace == "pypi"
    assert adapter.source_id == "pypi_json"


def test_a_target_from_another_namespace_is_refused_without_a_request() -> None:
    fetcher = ok({})
    result = run(
        PyPIAdapter(fetcher).fetch_release(parse_target("npm", "left-pad", "1.3.0"))
    )
    assert isinstance(result, LookupFailed)
    assert result.detail == "unsupported_namespace"
    assert fetcher.requested == []


# --------------------------------------------------------------------------------------
# Claim normalisation
# --------------------------------------------------------------------------------------


def test_requires_python_becomes_a_tier_a_gate_about_the_python_runtime() -> None:
    document = sample_document()
    gate = one_gate(document, "pypi:requires_python")
    assert gate.declared_about == PYTHON
    assert gate.expression == ">=3.10,<3.14"
    assert gate.scheme == "pep440"
    assert gate.bounded_above is True
    assert gate.lower_bound is not None
    assert gate.lower_bound.release == (3, 10)
    assert gate.condition is None


def test_requires_python_evidence_carries_the_expression_and_the_project_page() -> None:
    evidence = evidence_index(sample_document())["pypi:requires_python"]
    assert isinstance(evidence, VersionConstraintEvidence)
    assert evidence.tier == "A"
    assert evidence.source_type == "registry_metadata"
    assert evidence.title == "Distribution metadata for sample-project 5.2.1"
    # 04: a URL the caller can open, not the JSON document the server read.
    assert evidence.url == SAMPLE_PAGE
    assert evidence.expression == ">=3.10,<3.14"
    assert evidence.scheme == "pep440"
    assert evidence.provenance == Fetched(retrieved_at=RETRIEVED_AT)


def test_each_requires_dist_entry_becomes_a_gate_about_the_named_package() -> None:
    document = sample_document()
    gate = one_gate(document, "pypi:requires_dist:sample-client")
    assert gate.declared_about == TargetId("pypi", CanonicalName("sample-client"))
    assert gate.scheme == "pep440"
    assert gate.bounded_above is True


def test_a_requirement_name_is_canonicalised_before_it_becomes_an_identity() -> None:
    document = sample_document()
    # `Sample_Normalise` in the metadata; `sample-normalise` as a domain identity.
    gate = one_gate(document, "pypi:requires_dist:sample-normalise")
    assert gate.declared_about == TargetId("pypi", CanonicalName("sample-normalise"))


def test_an_empty_specifier_is_a_gate_that_admits_everything() -> None:
    """PEP 440: an empty specifier set matches every version, so it has no ceiling.

    Recording it as a gate with an empty expression keeps it distinct from "no constraint
    was declared", which produces no claim at all.
    """
    gate = one_gate(sample_document(), "pypi:requires_dist:typed-extras")
    assert gate.expression == ""
    assert gate.bounded_above is False
    assert gate.lower_bound is None


def test_a_direct_reference_does_not_become_a_pypi_version_gate() -> None:
    payload = load("pypi_sample_project_5.2.1.json")
    payload["info"]["requires_dist"] = [
        "direct-dep @ https://example.invalid/direct-dep-1.0.whl"
    ]

    document = parse_release(pypi_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    assert select_claims(document, TargetId("pypi", CanonicalName("direct-dep"))) == ()


def test_a_marker_is_preserved_as_a_condition_rather_than_assumed_true() -> None:
    document = sample_document()
    conditional = one_gate(document, "pypi:requires_dist:sample-client#2")
    assert conditional.condition is not None
    assert conditional.condition.expression == 'python_version < "3.11"'
    assert conditional.condition.decidability == "environment_dependent"

    guarded = one_gate(document, "pypi:requires_dist:sample-plugin")
    assert guarded.condition is not None
    assert guarded.condition.decidability == "extra_guarded"


def test_the_marker_survives_into_the_human_readable_evidence() -> None:
    evidence = evidence_index(sample_document())["pypi:requires_dist:sample-client#2"]
    assert 'python_version < "3.11"' in evidence.substantiates


def test_two_requirements_on_one_package_get_distinct_ids() -> None:
    document = sample_document()
    ids = [claim_evidence_id(claim) for claim in document.claims]
    assert ids.count("pypi:requires_dist:sample-client") == 1
    assert ids.count("pypi:requires_dist:sample-client#2") == 1


def test_an_unparseable_requirement_is_skipped_rather_than_guessed() -> None:
    # The fixture holds `"not a requirement!!"` and a bare integer.
    document = sample_document()
    names = {
        claim.declared_about.name.value
        for claim in document.claims
        if isinstance(claim, InstallationGate)
    }
    assert names == {
        "python",
        "sample-client",
        "typed-extras",
        "sample-normalise",
        "sample-plugin",
    }


def test_dotted_python_classifiers_become_one_tier_c_corroboration() -> None:
    document = sample_document()
    corroborations = [
        claim for claim in document.claims if isinstance(claim, Corroboration)
    ]
    assert len(corroborations) == 1
    assert corroborations[0].declared_about == PYTHON
    # `3` and `3 :: Only` say nothing about which 3.x releases were tested.
    assert corroborations[0].enumerated_versions == frozenset(
        {"3.10", "3.11", "3.12", "3.13"}
    )


def test_classifier_evidence_is_narrative_because_an_enumeration_is_not_a_range() -> (
    None
):
    evidence = evidence_index(sample_document())["pypi:classifiers"]
    assert isinstance(evidence, NarrativeEvidence)
    assert evidence.tier == "C"
    assert evidence.source_type == "registry_classifier"


def test_released_at_is_the_earliest_upload_of_the_release() -> None:
    # The sdist landed two minutes before the wheel; the release appeared with the first.
    assert sample_document().released_at == datetime(2026, 2, 2, 9, 58, tzinfo=UTC)


def test_a_healthy_release_is_not_marked_yanked() -> None:
    assert sample_document().yanked is None


# --------------------------------------------------------------------------------------
# Sparse and withdrawn releases
# --------------------------------------------------------------------------------------


def test_metadata_that_declares_nothing_produces_no_claims() -> None:
    """Absence is not evidence (03 absolute rule 2), so nothing is invented here."""
    document = parse_release(
        pypi_target("bare-metadata", "1.0"),
        load("pypi_bare_metadata_1.0.json"),
        retrieved_at=RETRIEVED_AT,
    )
    assert isinstance(document, ReleaseDocument)
    assert document.claims == ()
    assert document.evidence == ()
    assert document.released_at is None
    assert document.yanked is None


def test_a_yanked_release_is_still_parsed_and_carries_its_reason() -> None:
    document = parse_release(
        pypi_target("yanked-release", "2.0.0"),
        load("pypi_yanked_release_2.0.0.json"),
        retrieved_at=RETRIEVED_AT,
    )
    assert isinstance(document, ReleaseDocument)
    assert document.yanked is not None
    assert document.yanked.reason == "Broken wheel; use 2.0.1."
    # A distribution state, not a compatibility judgement: the claims are still there.
    assert one_gate(document, "pypi:requires_python").expression == ">=3.12"


def test_a_partially_yanked_release_is_not_treated_as_withdrawn() -> None:
    payload = load("pypi_sample_project_5.2.1.json")
    payload["urls"][0]["yanked"] = True
    document = parse_release(pypi_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    # One installable file remains, so the release itself is not withdrawn.
    assert document.yanked is None


def test_a_requirement_name_the_boundary_rejects_is_skipped() -> None:
    """``packaging`` and 02's name grammar do not agree everywhere.

    Where they disagree the stricter one wins: an identity the tool boundary would refuse
    must not be able to enter the domain through a dependency list.
    """
    payload = load("pypi_sample_project_5.2.1.json")
    payload["info"]["requires_dist"] = ["a" * 300 + ">=1"]
    document = parse_release(pypi_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    assert not any(
        claim.declared_about.namespace == "pypi" for claim in document.claims
    )


def test_a_third_requirement_on_one_package_continues_the_suffix() -> None:
    payload = load("pypi_sample_project_5.2.1.json")
    payload["info"]["requires_dist"] = ["dup>=1", "dup>=2", "dup>=3"]
    document = parse_release(pypi_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    ids = [claim_evidence_id(claim) for claim in document.claims]
    assert [entry for entry in ids if entry.startswith("pypi:requires_dist:")] == [
        "pypi:requires_dist:dup",
        "pypi:requires_dist:dup#2",
        "pypi:requires_dist:dup#3",
    ]


@pytest.mark.parametrize("upload_time", ["not a timestamp", "", None, 20260202])
def test_an_unreadable_upload_time_does_not_break_the_release_date(
    upload_time: object,
) -> None:
    payload = load("pypi_sample_project_5.2.1.json")
    payload["urls"][1]["upload_time_iso_8601"] = upload_time
    document = parse_release(pypi_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    # The remaining file still dates the release.
    assert document.released_at == datetime(2026, 2, 2, 10, 0, 0, 123456, tzinfo=UTC)


def test_a_release_whose_every_file_is_yanked_is_withdrawn() -> None:
    payload = load("pypi_sample_project_5.2.1.json")
    for entry in payload["urls"]:
        entry["yanked"] = True
        entry["yanked_reason"] = None
    payload["urls"][1]["yanked_reason"] = "Superseded."
    document = parse_release(pypi_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    assert document.yanked is not None
    assert document.yanked.reason == "Superseded."


def test_a_version_spelling_we_cannot_parse_is_not_read_as_a_mismatch() -> None:
    # Refusing to compare is not the same as finding a difference.
    payload = load("pypi_sample_project_5.2.1.json")
    payload["info"]["version"] = "v5.2.1"
    assert isinstance(fetch(ok(payload)), ReleaseDocument)


# --------------------------------------------------------------------------------------
# Determinism and referential integrity
# --------------------------------------------------------------------------------------


def test_parsing_the_same_fixture_twice_is_identical() -> None:
    """03 "결정성": the same inputs must produce the same values, byte for byte."""
    assert sample_document() == sample_document()


def test_every_claim_resolves_to_evidence_in_the_same_document() -> None:
    document = sample_document()
    index = evidence_index(document)
    assert len(index) == len(document.evidence)  # ids are unique within the document
    for claim in document.claims:
        assert claim_evidence_id(claim) in index


def test_select_claims_filters_by_what_a_claim_is_about() -> None:
    document = sample_document()
    about_python = select_claims(document, PYTHON)
    assert {claim_evidence_id(claim) for claim in about_python} == {
        "pypi:requires_python",
        "pypi:classifiers",
    }
    assert select_claims(document, TargetId("pypi", CanonicalName("absent"))) == ()


# --------------------------------------------------------------------------------------
# Failure and absence
# --------------------------------------------------------------------------------------


def test_a_404_is_absence() -> None:
    result = fetch(FakeFetcher(HttpNotFound(url=SAMPLE_URL)))
    assert result == ReleaseNotFound(target=pypi_target())


@pytest.mark.parametrize(
    "detail",
    [
        "timeout",
        "rate_limited",
        "response_too_large",
        "invalid_json",
        "transport_error",
    ],
)
def test_a_lookup_failure_keeps_its_code(detail: str) -> None:
    """The code reaches ``SourceCheck.detail``, so it must not be flattened."""
    result = fetch(FakeFetcher(HttpFailed(url=SAMPLE_URL, detail=detail)))
    assert result == LookupFailed(target=pypi_target(), detail=detail)


@pytest.mark.parametrize(
    "payload", [None, [], "a string", 7, {}, {"info": None}, {"info": []}]
)
def test_a_body_that_is_not_a_release_document_is_a_failure_not_an_empty_document(
    payload: object,
) -> None:
    result = fetch(ok(payload))
    assert result == LookupFailed(target=pypi_target(), detail="invalid_document")


def test_missing_optional_fields_are_tolerated() -> None:
    result = fetch(ok({"info": {"name": "sample-project"}}))
    assert isinstance(result, ReleaseDocument)
    assert result.claims == ()


def test_a_document_for_another_release_is_never_substituted() -> None:
    payload = load("pypi_sample_project_5.2.1.json")
    payload["info"]["version"] = "5.2.2"
    assert fetch(ok(payload)) == ReleaseNotFound(target=pypi_target())


def test_an_equivalent_version_spelling_is_still_this_release() -> None:
    payload = load("pypi_sample_project_5.2.1.json")
    payload["info"]["version"] = "5.2.1.0"
    # PEP 440 says 5.2.1 == 5.2.1.0, so this is the release that was asked for.
    assert isinstance(fetch(ok(payload)), ReleaseDocument)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def one_gate(document: ReleaseDocument, evidence_id: str) -> InstallationGate:
    gates = [
        claim
        for claim in document.claims
        if isinstance(claim, InstallationGate) and claim.evidence_id == evidence_id
    ]
    assert len(gates) == 1, f"expected exactly one gate for {evidence_id}"
    return gates[0]
