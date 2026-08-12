"""Fixture tests for the npm adapter.

Two decisions from 03 carry most of the weight here and are pinned by name:

* ``engines.node`` is tier **B**, not a tier-A gate, because npm only warns unless
  ``engine-strict`` is set and the server is never told the caller's npm configuration.
* Only ``versions[<exact version>]`` is ever read. A dist-tag, a nearby release or an
  entry that survives in ``time`` after an unpublish are all absence.

No test touches the network: the adapter is given a fake :class:`JsonFetcher`.
"""

import asyncio
import json
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dependency_compat_mcp.adapters.npm import NpmAdapter, parse_packument
from dependency_compat_mcp.adapters.protocol import (
    LookupFailed,
    ReleaseDocument,
    ReleaseNotFound,
    evidence_index,
    select_claims,
)
from dependency_compat_mcp.domain.claims import (
    CompatibilityStatement,
    Fetched,
    InstallationGate,
    VersionConstraintEvidence,
    claim_evidence_id,
)
from dependency_compat_mcp.domain.targets import (
    CanonicalName,
    NpmTarget,
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
NODE = TargetId(namespace="runtime", name=CanonicalName("node"))
SAMPLE_URL = "https://registry.npmjs.org/sample-package"
SAMPLE_PAGE = "https://www.npmjs.com/package/sample-package/v/4.19.2"


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive one coroutine to completion without an async pytest plugin."""
    return asyncio.run(coroutine)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def npm_target(name: str = "sample-package", version: str = "4.19.2") -> NpmTarget:
    target = parse_target("npm", name, version)
    assert isinstance(target, NpmTarget)
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


def fetch(fetcher: FakeFetcher, target: NpmTarget) -> Any:
    return run(NpmAdapter(fetcher).fetch_release(target))


def sample_document(target: NpmTarget | None = None) -> ReleaseDocument:
    document = parse_packument(
        target or npm_target(),
        load("npm_sample_package.json"),
        retrieved_at=RETRIEVED_AT,
    )
    assert isinstance(document, ReleaseDocument)
    return document


def facts(document: ReleaseDocument) -> tuple[Any, ...]:
    """Everything a document parsed *except* its target.

    ``nodesemver.SemVer`` compares by identity, so two separately parsed ``NpmTarget``
    values are never equal even when they name the same release. Determinism is therefore
    asserted over the parsed content, which contains no ``SemVer``; comparisons that do
    involve a target reuse one instance instead.
    """
    return (document.claims, document.evidence, document.released_at, document.yanked)


# --------------------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------------------


def test_the_packument_endpoint_is_requested() -> None:
    fetcher = ok(load("npm_sample_package.json"))
    fetch(fetcher, npm_target())
    assert fetcher.requested == [SAMPLE_URL]


def test_a_scoped_name_is_encoded_as_one_path_segment() -> None:
    fetcher = ok(load("npm_scoped_package.json"))
    fetch(fetcher, npm_target("@example-scope/sample-helper", "3.1.0"))
    assert fetcher.requested == [
        "https://registry.npmjs.org/@example-scope%2Fsample-helper"
    ]


def test_the_adapter_advertises_its_namespace_and_source() -> None:
    adapter = NpmAdapter(ok({}))
    assert adapter.namespace == "npm"
    assert adapter.source_id == "npm_registry"


def test_a_target_from_another_namespace_is_refused_without_a_request() -> None:
    fetcher = ok({})
    result = run(
        NpmAdapter(fetcher).fetch_release(
            parse_target("pypi", "sample-project", "5.2.1")
        )
    )
    assert isinstance(result, LookupFailed)
    assert result.detail == "unsupported_namespace"
    assert fetcher.requested == []


# --------------------------------------------------------------------------------------
# Exact version selection
# --------------------------------------------------------------------------------------


def test_only_the_exact_version_is_read() -> None:
    older = sample_document(npm_target(version="4.18.0"))
    statement = one_statement(older)
    # 4.18.0's own engines range, not the newest version's.
    assert statement.expression == ">= 0.10.0"


def test_a_version_absent_from_the_packument_is_not_found() -> None:
    target = npm_target(version="9.9.9")
    assert fetch(ok(load("npm_sample_package.json")), target) == ReleaseNotFound(
        target=target
    )


def test_an_unpublished_version_is_absence_even_though_time_remembers_it() -> None:
    """npm has no yank: withdrawal removes the version, so it maps to absence."""
    payload = load("npm_sample_package.json")
    assert "4.19.3" in payload["time"]
    assert "4.19.3" not in payload["versions"]
    target = npm_target(version="4.19.3")
    assert fetch(ok(payload), target) == ReleaseNotFound(target=target)


def test_a_dist_tag_is_never_substituted_for_a_version() -> None:
    payload = load("npm_sample_package.json")
    assert payload["dist-tags"]["latest"] == "4.19.2"
    target = npm_target(version="5.0.0")
    assert fetch(ok(payload), target) == ReleaseNotFound(target=target)


# --------------------------------------------------------------------------------------
# Claim normalisation
# --------------------------------------------------------------------------------------


def test_engines_node_is_a_tier_b_statement_not_an_installation_gate() -> None:
    """npm only warns on a mismatch unless ``engine-strict`` is set, and 02 keeps the
    caller's npm configuration out of the request, so the server must not claim an
    install failure it cannot know about."""
    document = sample_document()
    statement = one_statement(document)
    assert statement.declared_about == NODE
    assert statement.stance == "supports"
    assert statement.expression == ">= 18.17.0"
    assert statement.scheme == "semver"
    assert not any(
        isinstance(claim, InstallationGate) and claim.declared_about == NODE
        for claim in document.claims
    )


def test_engines_node_evidence_is_tier_b_registry_metadata() -> None:
    evidence = evidence_index(sample_document())["npm:engines.node"]
    assert isinstance(evidence, VersionConstraintEvidence)
    assert evidence.tier == "B"
    assert evidence.source_type == "registry_metadata"
    assert evidence.expression == ">= 18.17.0"
    assert evidence.scheme == "semver"
    assert evidence.provenance == Fetched(retrieved_at=RETRIEVED_AT)


def test_evidence_points_at_the_display_page_which_is_never_fetched() -> None:
    # www.npmjs.com is deliberately outside ALLOWED_HOSTS: it is shown, not requested.
    assert evidence_index(sample_document())["npm:engines.node"].url == SAMPLE_PAGE


def test_dependencies_become_tier_a_gates() -> None:
    gate = one_gate(sample_document(), "npm:dependencies:sample-util")
    assert gate.declared_about == TargetId("npm", CanonicalName("sample-util"))
    assert gate.expression == "^1.4.0"
    assert gate.scheme == "semver"
    assert gate.bounded_above is True
    assert gate.lower_bound is not None
    assert gate.lower_bound.release == (1, 4, 0)
    assert gate.condition is None


def test_peer_dependencies_become_gates_with_their_own_id_prefix() -> None:
    gate = one_gate(sample_document(), "npm:peerDependencies:sample-framework")
    assert gate.expression == "^7.0.0 || ^8.0.0"
    # A union of two closed branches is still closed above.
    assert gate.bounded_above is True


def test_an_open_ended_range_is_recorded_as_unbounded_above() -> None:
    document = parse_packument(
        npm_target("@example-scope/sample-helper", "3.1.0"),
        load("npm_scoped_package.json"),
        retrieved_at=RETRIEVED_AT,
    )
    assert isinstance(document, ReleaseDocument)
    gate = one_gate(document, "npm:peerDependencies:sample-package")
    assert gate.expression == ">=4.0.0"
    assert gate.bounded_above is False


def test_a_scoped_dependency_name_survives_intact() -> None:
    gate = one_gate(sample_document(), "npm:dependencies:@example-scope/sample-helper")
    assert gate.declared_about == TargetId(
        "npm", CanonicalName("@example-scope/sample-helper")
    )


def test_specifiers_that_do_not_name_a_registry_range_are_skipped() -> None:
    """A ``file:``, ``git+``, alias, shorthand or dist-tag specifier cannot be compared
    against an exact version, so no claim is made about it at all."""
    names = {
        claim.declared_about.name.value
        for claim in sample_document().claims
        if isinstance(claim, InstallationGate)
    }
    assert names == {"sample-util", "@example-scope/sample-helper", "sample-framework"}


def test_dev_dependencies_are_not_claims() -> None:
    # They are not installed for a consumer of the package.
    assert "npm:devDependencies:sample-test-runner" not in evidence_index(
        sample_document()
    )


def test_released_at_comes_from_the_packument_time_map() -> None:
    assert sample_document().released_at == datetime(
        2026, 3, 20, 11, 24, 31, 451000, tzinfo=UTC
    )


def test_npm_releases_are_never_yanked() -> None:
    assert sample_document().yanked is None


def test_a_manifest_without_engines_produces_no_statement() -> None:
    document = parse_packument(
        npm_target("@example-scope/sample-helper", "3.1.0"),
        load("npm_scoped_package.json"),
        retrieved_at=RETRIEVED_AT,
    )
    assert isinstance(document, ReleaseDocument)
    assert not any(
        isinstance(claim, CompatibilityStatement) for claim in document.claims
    )


@pytest.mark.parametrize("node_range", ["", "   ", None, 18])
def test_an_engines_node_that_is_not_a_range_string_produces_no_statement(
    node_range: object,
) -> None:
    payload = load("npm_sample_package.json")
    payload["versions"]["4.19.2"]["engines"] = {"node": node_range}
    document = parse_packument(npm_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    assert not any(
        isinstance(claim, CompatibilityStatement) for claim in document.claims
    )


def test_a_dependency_name_the_boundary_rejects_is_skipped() -> None:
    """npm names are lowercase; an identity the tool boundary would refuse must not
    enter the domain through a dependency list."""
    payload = load("npm_sample_package.json")
    payload["versions"]["4.19.2"]["dependencies"] = {"UPPERCASE-Name": "^1.0.0"}
    document = parse_packument(npm_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    assert not any(
        claim_evidence_id(claim).startswith("npm:dependencies:")
        for claim in document.claims
    )


@pytest.mark.parametrize("published", ["not a timestamp", "", 20260320, None])
def test_an_unreadable_publish_time_leaves_released_at_empty(published: object) -> None:
    payload = load("npm_sample_package.json")
    payload["time"]["4.19.2"] = published
    document = parse_packument(npm_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    assert document.released_at is None


def test_a_packument_without_a_time_map_leaves_released_at_empty() -> None:
    payload = load("npm_sample_package.json")
    del payload["time"]
    document = parse_packument(npm_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(document, ReleaseDocument)
    assert document.released_at is None


# --------------------------------------------------------------------------------------
# Determinism and referential integrity
# --------------------------------------------------------------------------------------


def test_parsing_the_same_fixture_twice_is_identical() -> None:
    """03 "결정성": the same inputs must produce the same values, byte for byte."""
    assert facts(sample_document()) == facts(sample_document())


def test_claim_order_does_not_depend_on_the_registry_key_order() -> None:
    payload = load("npm_sample_package.json")
    manifest = payload["versions"]["4.19.2"]
    manifest["dependencies"] = dict(reversed(list(manifest["dependencies"].items())))
    reordered = parse_packument(npm_target(), payload, retrieved_at=RETRIEVED_AT)
    assert isinstance(reordered, ReleaseDocument)
    assert facts(reordered) == facts(sample_document())


def test_every_claim_resolves_to_evidence_in_the_same_document() -> None:
    document = sample_document()
    index = evidence_index(document)
    assert len(index) == len(document.evidence)  # ids are unique within the document
    for claim in document.claims:
        assert claim_evidence_id(claim) in index


def test_select_claims_filters_by_what_a_claim_is_about() -> None:
    document = sample_document()
    assert {claim_evidence_id(claim) for claim in select_claims(document, NODE)} == {
        "npm:engines.node"
    }


# --------------------------------------------------------------------------------------
# Failure and absence
# --------------------------------------------------------------------------------------


def test_a_404_is_absence() -> None:
    target = npm_target()
    assert fetch(FakeFetcher(HttpNotFound(url=SAMPLE_URL)), target) == ReleaseNotFound(
        target=target
    )


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
    """A large packument that trips the size ceiling must stay distinguishable from a
    package that does not exist."""
    target = npm_target()
    result = fetch(FakeFetcher(HttpFailed(url=SAMPLE_URL, detail=detail)), target)
    assert result == LookupFailed(target=target, detail=detail)


@pytest.mark.parametrize(
    "payload", [None, [], "a string", 7, {}, {"versions": None}, {"versions": []}]
)
def test_a_body_that_is_not_a_packument_is_a_failure_not_an_empty_document(
    payload: object,
) -> None:
    target = npm_target()
    assert fetch(ok(payload), target) == LookupFailed(
        target=target, detail="invalid_document"
    )


def test_a_manifest_that_is_not_an_object_is_absence() -> None:
    target = npm_target()
    assert fetch(ok({"versions": {"4.19.2": "not a manifest"}}), target) == (
        ReleaseNotFound(target=target)
    )


def test_missing_optional_fields_are_tolerated() -> None:
    result = fetch(
        ok({"versions": {"4.19.2": {"name": "sample-package"}}}), npm_target()
    )
    assert isinstance(result, ReleaseDocument)
    assert result.claims == ()
    assert result.released_at is None


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


def one_statement(document: ReleaseDocument) -> CompatibilityStatement:
    statements = [
        claim for claim in document.claims if isinstance(claim, CompatibilityStatement)
    ]
    assert len(statements) == 1
    return statements[0]
