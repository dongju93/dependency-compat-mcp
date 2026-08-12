"""Curated pack loading: one failing fixture per rule, and one real pack for the rest.

03 makes pack loading a start-up gate — "스키마 위반은 기동 실패로 처리한다" — so every
test here asserts that a violation *raises*, not that it is skipped. The positive path uses
a committed fixture pack rather than the shipped one, because the shipped pack is
deliberately empty: 03 forbids inventing entries to make it look populated.

No test in this file touches the network.
"""

from datetime import date
from pathlib import Path

import pytest

from dependency_compat_mcp.curated.loader import (
    OFFICIAL_HOSTS,
    CuratedPack,
    PackLoadError,
    change_evidence,
    load_curated_pack,
    parse_exact_version,
    statement_claims,
)
from dependency_compat_mcp.domain.claims import (
    CompatibilityStatement,
    Curated,
    NarrativeEvidence,
    VersionConstraintEvidence,
)
from dependency_compat_mcp.domain.targets import (
    CanonicalName,
    NpmTarget,
    PyPITarget,
    TargetId,
    parse_target,
)

FIXTURES = Path(__file__).parent / "fixtures" / "packs"


def _pypi(name: str, version: str) -> PyPITarget:
    target = parse_target("pypi", name, version)
    assert isinstance(target, PyPITarget)
    return target


def _npm(name: str, version: str) -> NpmTarget:
    target = parse_target("npm", name, version)
    assert isinstance(target, NpmTarget)
    return target


@pytest.fixture(scope="module")
def pack() -> CuratedPack:
    return load_curated_pack(FIXTURES / "valid")


# --------------------------------------------------------------------------------------
# The shipped pack
# --------------------------------------------------------------------------------------


def test_shipped_pack_loads_and_is_deliberately_empty() -> None:
    """03: an empty `entries` is valid, and evidence is never invented to fill it."""
    shipped = load_curated_pack()
    assert shipped.pack_version
    assert shipped.entries == ()


# --------------------------------------------------------------------------------------
# Positive path
# --------------------------------------------------------------------------------------


def test_entries_are_parsed_through_the_namespace_parsers(pack: CuratedPack) -> None:
    """`Example-Framework` is canonicalised exactly as tool input would be."""
    names = [entry.name.value for entry in pack.entries]
    assert names == ["@example/toolkit", "example-framework"]
    assert pack.pack_version == "2026.08.1-fixture"


def test_lookup_matches_namespace_name_and_range(pack: CuratedPack) -> None:
    entry = pack.lookup(_pypi("example-framework", "5.2.1"))
    assert entry is not None
    assert entry.applies_to == ">=5.2,<5.3"
    assert entry.reviewed_at == date(2026, 8, 10)
    assert entry.reviewed_by == "fixture-reviewer"


def test_lookup_returns_none_outside_applies_to(pack: CuratedPack) -> None:
    assert pack.lookup(_pypi("example-framework", "5.3")) is None


def test_lookup_returns_none_for_another_package(pack: CuratedPack) -> None:
    assert pack.lookup(_pypi("other-framework", "5.2.1")) is None


def test_lookup_uses_the_npm_scheme_for_npm_entries(pack: CuratedPack) -> None:
    entry = pack.lookup(_npm("@example/toolkit", "4.1.0"))
    assert entry is not None
    assert entry.scheme == "semver"
    assert entry.statements[0].counterpart == TargetId("runtime", CanonicalName("node"))


def test_verified_for_separates_claimed_range_from_checked_versions(
    pack: CuratedPack,
) -> None:
    """Both versions sit inside `applies_to`; only one was actually reviewed."""
    entry = pack.lookup(_pypi("example-framework", "5.2.1"))
    assert entry is not None
    assert pack.verified_for(entry, _pypi("example-framework", "5.2.1")) is True
    assert pack.verified_for(entry, _pypi("example-framework", "5.2.7")) is False


def test_verified_for_rejects_a_target_from_another_entry(pack: CuratedPack) -> None:
    entry = pack.lookup(_pypi("example-framework", "5.2.1"))
    assert entry is not None
    assert pack.verified_for(entry, _npm("@example/toolkit", "4.1.0")) is False


def test_statement_claims_builds_tier_b_claims_and_constraint_evidence(
    pack: CuratedPack,
) -> None:
    entry = pack.lookup(_pypi("example-framework", "5.2.1"))
    assert entry is not None
    claims, evidence = statement_claims(entry, pack.pack_version)

    assert len(claims) == len(evidence) == 2
    statements = [
        claim for claim in claims if isinstance(claim, CompatibilityStatement)
    ]
    constraints = [
        item for item in evidence if isinstance(item, VersionConstraintEvidence)
    ]
    assert len(statements) == len(constraints) == 2
    assert [claim.stance for claim in statements] == ["supports", "excludes"]
    assert {item.tier for item in constraints} == {"B"}
    assert [item.expression for item in constraints] == [">=3.10,<3.14", "<3.10"]
    # Every claim resolves to evidence in the same batch: 04's referential integrity.
    assert {claim.evidence_id for claim in claims} == {item.id for item in evidence}


def test_statement_evidence_carries_curated_provenance(pack: CuratedPack) -> None:
    """A reviewed source's freshness is the review date, never a retrieval time."""
    entry = pack.lookup(_pypi("example-framework", "5.2.1"))
    assert entry is not None
    _, evidence = statement_claims(entry, pack.pack_version)
    assert evidence[0].provenance == Curated(
        reviewed_at=date(2026, 8, 10), pack_version="2026.08.1-fixture"
    )


def test_change_evidence_is_narrative_and_carries_no_expression(
    pack: CuratedPack,
) -> None:
    entry = pack.lookup(_pypi("example-framework", "5.2.1"))
    assert entry is not None
    evidence = change_evidence(entry, pack.pack_version)
    assert len(evidence) == 1
    assert isinstance(evidence[0], NarrativeEvidence)
    assert evidence[0].source_type == "official_release_note"
    assert not hasattr(evidence[0], "expression")


def test_evidence_ids_are_deterministic_and_unique(pack: CuratedPack) -> None:
    """Byte-stable output (03 [6]) requires the same ids on every load."""
    reloaded = load_curated_pack(FIXTURES / "valid")
    identifiers: list[str] = []
    for source in (pack, reloaded):
        for entry in source.entries:
            _, evidence = statement_claims(entry, source.pack_version)
            identifiers.extend(item.id for item in evidence)
            identifiers.extend(
                item.id for item in change_evidence(entry, source.pack_version)
            )
    first_half = identifiers[: len(identifiers) // 2]
    assert identifiers == first_half * 2
    assert len(set(first_half)) == len(first_half)


def test_empty_entries_is_a_valid_pack() -> None:
    empty = load_curated_pack(FIXTURES / "empty_entries")
    assert empty.entries == ()
    assert empty.lookup(_pypi("example-framework", "5.2.1")) is None


# --------------------------------------------------------------------------------------
# One failing fixture per validation rule
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("missing_source_url", "pack schema"),
        ("disallowed_host", "not an official evidence host"),
        ("non_https", "must use https"),
        ("bad_name", "not a valid PyPI project name"),
        ("bad_applies_to", "is not a valid pep440 range"),
        ("missing_reviewed_at", "pack schema"),
        ("empty_reviewed_by", "pack schema"),
        ("bad_verified_version", "canonical PEP 440 form"),
        ("duplicate_entry", "duplicate entry"),
        ("unknown_field", "pack schema"),
        ("pack_version_mismatch", "differs from"),
    ],
)
def test_violations_are_start_up_failures(fixture: str, expected: str) -> None:
    with pytest.raises(PackLoadError, match=expected):
        load_curated_pack(FIXTURES / fixture)


def test_missing_directory_is_a_start_up_failure(tmp_path: Path) -> None:
    with pytest.raises(PackLoadError, match="does not exist"):
        load_curated_pack(tmp_path / "absent")


def test_directory_without_pack_files_is_a_start_up_failure(tmp_path: Path) -> None:
    """No file means no `pack_version`, and provenance depends on it."""
    with pytest.raises(PackLoadError, match="no pack files"):
        load_curated_pack(tmp_path)


def test_malformed_json_is_a_start_up_failure(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PackLoadError):
        load_curated_pack(tmp_path)


# --------------------------------------------------------------------------------------
# The host allowlist itself
# --------------------------------------------------------------------------------------


def test_allowlist_holds_only_first_party_hosts() -> None:
    """A regression guard for the review contract, not a style check.

    Adding a host widens what the whole server accepts as evidence, so summary sites,
    forums and blog platforms must never appear here by accident.
    """
    banned = {
        "endoflife.date",
        "stackoverflow.com",
        "discuss.python.org",
        "medium.com",
        "blogspot.com",
        "reddit.com",
    }
    assert OFFICIAL_HOSTS.isdisjoint(banned)
    assert "docs.python.org" in OFFICIAL_HOSTS
    assert "nodejs.org" in OFFICIAL_HOSTS


def test_parse_exact_version_uses_the_spine_parsers() -> None:
    """The pack cannot admit a spelling tool input would reject."""
    assert str(parse_exact_version("pep440", "3.13.0")) == "3.13.0"
    assert str(parse_exact_version("semver", "22.17.0")) == "22.17.0"
    with pytest.raises(ValueError, match="canonical SemVer form"):
        parse_exact_version("semver", "v22.17.0")
