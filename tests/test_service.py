"""End-to-end behaviour of the orchestration layer, without a socket.

These tests are the ones that would catch a seam splitting: the adapters, the decision
procedure and the assembler are each tested in isolation elsewhere, so what is left is
whether the *composition* still answers 04's question. Every case below is stated as a
caller-visible outcome - verdict, reason, direction, limitations - not as an internal call.
"""

import asyncio
from typing import Any, override

import pytest

from dependency_compat_mcp.curated.loader import CuratedPack
from dependency_compat_mcp.domain.targets import parse_target
from dependency_compat_mcp.infra.http import HttpResult
from dependency_compat_mcp.service import CompatibilityService
from tests.conftest import (
    FakeFetcher,
    build_service,
    npm_packument,
    npm_url,
    pypi_release,
    pypi_url,
)


class SlowFetcher(FakeFetcher):
    """A cancellable registry stub for the call-level budget contract."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = 0

    @override
    async def get_json(self, url: str) -> HttpResult:
        self.calls.append(url)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("the request budget did not cancel the registry lookup")


def _check(
    service: CompatibilityService,
    subject: tuple[str, str, str],
    counterpart: tuple[str, str, str],
) -> dict[str, Any]:
    return asyncio.run(
        service.check_compatibility(parse_target(*subject), parse_target(*counterpart))
    ).model_dump(mode="json")


def _context(
    service: CompatibilityService, target: tuple[str, str, str]
) -> dict[str, Any]:
    return asyncio.run(
        service.get_compatibility_context(parse_target(*target))
    ).model_dump(mode="json")


def _codes(result: dict[str, Any], key: str) -> list[str]:
    return [item["code"] for item in result[key]]


def _outcome(
    result: dict[str, Any], source: str, name: str | None = None
) -> str | None:
    """The outcome of one lookup, optionally narrowed to the target it was made for.

    Both sides of a same-registry comparison produce a row for the same source, so a test
    that means one of them has to say which.
    """
    for check in result["sources_checked"]:
        if check["source"] == source and (
            name is None or check["target"]["name"] == name
        ):
            return check["outcome"]
    return None


def _kinds(result: dict[str, Any]) -> list[str]:
    return [cause["kind"] for cause in result["decision_causes"]]


# --------------------------------------------------------------------------------------
# check_compatibility
# --------------------------------------------------------------------------------------


def test_closed_upper_bound_admitting_the_version_is_supported() -> None:
    """A declared ceiling means the author enumerated a range, so 3.13 was in scope."""
    fetcher = FakeFetcher(
        payloads={
            pypi_url("django", "5.2"): pypi_release(
                "Django", "5.2", requires_python=">=3.10,<3.14"
            )
        }
    )
    result = _check(
        build_service(fetcher),
        ("pypi", "django", "5.2"),
        ("runtime", "python", "3.13.0"),
    )

    assert result["verdict"] == "supported"
    assert len(result["verdict_evidence_ids"]) >= 1
    assert set(result["verdict_evidence_ids"]) <= {e["id"] for e in result["evidence"]}
    assert result["relation"]["rule"] == "requires_python"
    assert result["relation"]["direction"] == "as_given"
    # The pack ships empty, so every real call says so rather than implying coverage.
    assert "curated_pack_missing" in _codes(result, "limitations")


def test_a_violated_installation_gate_is_unsupported() -> None:
    fetcher = FakeFetcher(
        payloads={
            pypi_url("django", "4.0"): pypi_release(
                "Django", "4.0", requires_python=">=3.8,<3.12"
            )
        }
    )
    result = _check(
        build_service(fetcher),
        ("pypi", "django", "4.0"),
        ("runtime", "python", "3.13.0"),
    )

    assert result["verdict"] == "unsupported"
    gate = next(
        e for e in result["evidence"] if e["id"] in result["verdict_evidence_ids"]
    )
    # 04: the caller must not have to re-parse the applied range out of English.
    assert gate["expression"] == ">=3.8,<3.12"
    assert gate["scheme"] == "pep440"


def test_an_open_ceiling_on_a_later_runtime_is_unknown_not_supported() -> None:
    """Installable is not the same as supported, and the response says which one it is."""
    fetcher = FakeFetcher(
        payloads={
            pypi_url("example-pkg", "1.0"): pypi_release(
                "example-pkg",
                "1.0",
                requires_python=">=3.10",
                uploaded="2023-01-01T00:00:00.000000Z",
            )
        }
    )
    result = _check(
        build_service(fetcher),
        ("pypi", "example-pkg", "1.0"),
        ("runtime", "python", "3.13.0"),
    )

    assert result["verdict"] == "unknown"
    assert result["reason"] == "insufficient_evidence"
    assert _kinds(result) == ["open_upper_bound"]
    # The cause cites the gate itself, so the caller can read the open range it names.
    assert result["decision_causes"][0]["evidence_ids"] == ["evidence-1"]
    # The gate is still returned: "it installs" is exactly the fact the caller needs.
    assert any(e.get("expression") == ">=3.10" for e in result["evidence"])
    assert "verdict_evidence_ids" not in result


def test_classifiers_alone_never_produce_supported() -> None:
    """03's absolute rule 3, observed from outside: tier C corroborates, it never decides."""
    fetcher = FakeFetcher(
        payloads={
            pypi_url("example-pkg", "1.0"): pypi_release(
                "example-pkg",
                "1.0",
                requires_python=None,
                classifiers=[
                    "Programming Language :: Python :: 3",
                    "Programming Language :: Python :: 3.13",
                ],
            )
        }
    )
    result = _check(
        build_service(fetcher),
        ("pypi", "example-pkg", "1.0"),
        ("runtime", "python", "3.13.0"),
    )

    assert result["verdict"] == "unknown"
    assert _kinds(result) == ["tier_c_only"]


def test_a_reversed_question_is_answered_and_says_so() -> None:
    """ "Does Python 3.13 run Django 5.2?" is the same question with the arguments swapped.

    The rule is `canonicalizable`, so the server answers it - and reports `reversed` plus
    the real declaring side, which is the only way the caller can notice the swap.
    """
    fetcher = FakeFetcher(
        payloads={
            pypi_url("django", "5.2"): pypi_release(
                "Django", "5.2", requires_python=">=3.10,<3.14"
            )
        }
    )
    result = _check(
        build_service(fetcher),
        ("runtime", "python", "3.13.0"),
        ("pypi", "django", "5.2"),
    )

    assert result["verdict"] == "supported"
    assert result["relation"]["direction"] == "reversed"
    assert result["relation"]["declaring"]["name"] == "django"
    assert result["subject"]["namespace"] == "runtime"


def test_a_directional_rule_is_never_flipped() -> None:
    """`A -> B` and `B -> A` read different declarations and may disagree."""
    fetcher = FakeFetcher(
        payloads={
            pypi_url("app", "1.0"): pypi_release(
                "app", "1.0", requires_dist=["library>=2.0"]
            ),
            pypi_url("library", "2.5"): pypi_release("library", "2.5"),
        }
    )
    service = build_service(fetcher)

    forward = _check(service, ("pypi", "app", "1.0"), ("pypi", "library", "2.5"))
    backward = _check(service, ("pypi", "library", "2.5"), ("pypi", "app", "1.0"))

    assert forward["relation"]["direction"] == "as_given"
    assert forward["relation"]["declaring"]["name"] == "app"
    assert forward["verdict"] == "supported"

    assert backward["relation"]["direction"] == "as_given"
    assert backward["relation"]["declaring"]["name"] == "library"
    assert backward["verdict"] == "unknown"
    # "these two are unrelated" is a different answer from "we found nothing".
    assert backward["reason"] == "no_declared_relationship"


def test_an_unsupported_relation_costs_no_lookup() -> None:
    fetcher = FakeFetcher()
    result = _check(
        build_service(fetcher),
        ("runtime", "python", "3.13.0"),
        ("runtime", "node", "22.17.0"),
    )

    assert result["verdict"] == "unknown"
    assert result["reason"] == "relation_not_supported"
    assert result["relation"]["status"] == "unsupported"
    # The variant carries the input pair and nothing invented.
    assert set(result["relation"]) == {"status", "subject", "counterpart"}
    assert fetcher.calls == []
    # No lookup was planned, so there is no lookup to report. Inventing rows here would
    # have meant naming a target the server never intended to open.
    assert result["sources_checked"] == []


def test_a_missing_release_is_reported_apart_from_a_failed_lookup() -> None:
    service = build_service(FakeFetcher())
    missing = _check(
        service, ("pypi", "example-pkg", "9.9"), ("runtime", "python", "3.13.0")
    )
    assert missing["verdict"] == "unknown"
    assert missing["reason"] == "release_not_found"
    assert _outcome(missing, "pypi_json") == "not_found"

    failing = _check(
        build_service(
            FakeFetcher(failures={pypi_url("example-pkg", "9.9"): "timeout"})
        ),
        ("pypi", "example-pkg", "9.9"),
        ("runtime", "python", "3.13.0"),
    )
    assert failing["reason"] == "lookup_failed"
    assert _outcome(failing, "pypi_json") == "failed"


def test_the_call_level_budget_cancels_owned_lookups_and_returns_unknown() -> None:
    fetcher = SlowFetcher()
    result = _check(
        build_service(fetcher, request_budget_seconds=0.01),
        ("pypi", "app", "1.0"),
        ("pypi", "dependency", "1.0"),
    )

    assert result["verdict"] == "unknown"
    assert result["reason"] == "lookup_failed"
    assert _outcome(result, "pypi_json") == "failed"
    assert fetcher.cancelled == 2


def test_a_marker_guarded_dependency_stays_undecided() -> None:
    """The server is never given the environment, so it does not assume one."""
    fetcher = FakeFetcher(
        payloads={
            pypi_url("app", "1.0"): pypi_release(
                "app", "1.0", requires_dist=['backport>=1.0; python_version < "3.11"']
            ),
            pypi_url("backport", "1.5"): pypi_release("backport", "1.5"),
        }
    )
    result = _check(
        build_service(fetcher), ("pypi", "app", "1.0"), ("pypi", "backport", "1.5")
    )

    assert result["verdict"] == "unknown"
    assert "marker_guarded_claim" in _codes(result, "limitations")


def test_an_extra_guarded_dependency_stays_undecided() -> None:
    fetcher = FakeFetcher(
        payloads={
            pypi_url("app", "1.0"): pypi_release(
                "app", "1.0", requires_dist=['extras-only>=1.0; extra == "dev"']
            ),
            pypi_url("extras-only", "1.5"): pypi_release("extras-only", "1.5"),
        }
    )
    result = _check(
        build_service(fetcher), ("pypi", "app", "1.0"), ("pypi", "extras-only", "1.5")
    )

    assert "extra_guarded_claim" in _codes(result, "limitations")


def test_a_yanked_release_still_gets_a_verdict_plus_a_notice() -> None:
    """Yanking is a distribution state, not a compatibility judgement."""
    fetcher = FakeFetcher(
        payloads={
            pypi_url("django", "5.2"): pypi_release(
                "Django",
                "5.2",
                requires_python=">=3.10,<3.14",
                yanked=True,
                yanked_reason="Broken sdist",
            )
        }
    )
    result = _check(
        build_service(fetcher),
        ("pypi", "django", "5.2"),
        ("runtime", "python", "3.13.0"),
    )

    assert result["verdict"] == "supported"
    assert "subject_yanked" in _codes(result, "notices")


def test_engines_node_is_a_statement_rather_than_an_installation_gate() -> None:
    """npm only warns on an engines mismatch unless `engine-strict` is set.

    The server is not told the caller's npm configuration, so it does not predict an
    install failure; it reports the publisher's stated range instead.
    """
    fetcher = FakeFetcher(
        payloads={
            npm_url("react"): npm_packument("react", "19.1.1", engines={"node": ">=18"})
        }
    )
    result = _check(
        build_service(fetcher),
        ("npm", "react", "19.1.1"),
        ("runtime", "node", "22.17.0"),
    )

    assert result["verdict"] == "supported"
    statement = next(
        e for e in result["evidence"] if e["id"] in result["verdict_evidence_ids"]
    )
    assert statement["scheme"] == "semver"
    assert statement["expression"] == ">=18"


def test_a_curated_statement_contradicting_a_gate_loses_but_is_still_returned(
    curated_fixture_pack: CuratedPack,
) -> None:
    """03's gate-first rule, and its companion: report the conflict, do not hide it."""
    fetcher = FakeFetcher(
        payloads={
            pypi_url("example-framework", "5.2"): pypi_release(
                "example-framework", "5.2", requires_python=">=3.8,<3.12"
            )
        }
    )
    service = build_service(fetcher, pack=curated_fixture_pack)
    result = _check(
        service,
        ("pypi", "example-framework", "5.2"),
        ("runtime", "python", "3.13.0"),
    )

    assert result["verdict"] == "unsupported"
    assert "gate_contradicts_statement" in _codes(result, "notices")
    # The contradicting policy stays in the catalogue but not in the verdict's own evidence.
    curated = [e for e in result["evidence"] if e["provenance"]["kind"] == "curated"]
    assert curated, "the opposing statement must not be dropped"
    assert not set(result["verdict_evidence_ids"]) & {e["id"] for e in curated}


# --------------------------------------------------------------------------------------
# get_compatibility_context
# --------------------------------------------------------------------------------------


def test_registry_only_context_says_so_at_the_top_level() -> None:
    fetcher = FakeFetcher(
        payloads={
            pypi_url("django", "5.2"): pypi_release(
                "Django",
                "5.2",
                requires_python=">=3.10,<3.14",
                requires_dist=["asgiref>=3.8"],
            )
        }
    )
    result = _context(build_service(fetcher), ("pypi", "django", "5.2"))

    assert result["availability"] == "available"
    assert result["depth"] == "registry_only"
    assert result["changes"] == []
    assert {c["counterpart"]["name"] for c in result["constraints"]} == {
        "python",
        "asgiref",
    }
    for constraint in result["constraints"]:
        assert constraint["evidence_ids"]


def test_curated_context_reports_changes_and_the_deeper_depth(
    curated_fixture_pack: CuratedPack,
) -> None:
    fetcher = FakeFetcher(
        payloads={
            pypi_url("example-framework", "5.2"): pypi_release(
                "example-framework", "5.2", requires_python=">=3.10,<3.14"
            )
        }
    )
    result = _context(
        build_service(fetcher, pack=curated_fixture_pack),
        ("pypi", "example-framework", "5.2"),
    )

    assert result["depth"] == "registry_and_curated"
    assert result["changes"], "a curated entry is the only source of changes"
    assert any(e["provenance"]["kind"] == "curated" for e in result["evidence"])
    assert all(change["evidence_ids"] for change in result["changes"])


def test_curated_context_lookup_failure_is_a_normal_unknown(
    curated_fixture_pack: CuratedPack,
) -> None:
    fetcher = FakeFetcher(failures={pypi_url("example-framework", "5.2"): "timeout"})
    result = _context(
        build_service(fetcher, pack=curated_fixture_pack),
        ("pypi", "example-framework", "5.2"),
    )

    assert result["availability"] == "unknown"
    assert result["reason"] == "lookup_failed"
    assert result["depth"] == "registry_only"
    assert result["constraints"] == []
    assert result["changes"] == []
    assert result["evidence"] == []


def test_context_collection_obeys_the_call_level_budget() -> None:
    fetcher = SlowFetcher()
    result = _context(
        build_service(fetcher, request_budget_seconds=0.01),
        ("pypi", "slow-package", "1.0"),
    )

    assert result["availability"] == "unknown"
    assert result["reason"] == "lookup_failed"
    assert _outcome(result, "pypi_json") == "failed"
    assert fetcher.cancelled == 1


def test_a_release_with_no_material_is_unknown_rather_than_an_empty_available() -> None:
    fetcher = FakeFetcher(
        payloads={pypi_url("bare-pkg", "1.0"): pypi_release("bare-pkg", "1.0")}
    )
    result = _context(build_service(fetcher), ("pypi", "bare-pkg", "1.0"))

    assert result["availability"] == "unknown"
    assert result["reason"] == "evidence_not_found"
    assert result["constraints"] == []
    assert result["changes"] == []
    assert result["depth"] == "registry_only"


def test_the_server_never_claims_anything_about_a_codebase() -> None:
    """02 refuses codebase input, so no response field may reference one."""
    fetcher = FakeFetcher(
        payloads={
            pypi_url("django", "5.2"): pypi_release(
                "Django", "5.2", requires_python=">=3.10,<3.14"
            )
        }
    )
    result = _context(build_service(fetcher), ("pypi", "django", "5.2"))
    forbidden = {"repository_path", "project", "codebase", "lockfile", "manifest"}
    assert not forbidden & set(result)


# --------------------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------------------


def test_a_repeated_lookup_is_served_from_cache() -> None:
    fetcher = FakeFetcher(
        payloads={
            pypi_url("django", "5.2"): pypi_release(
                "Django", "5.2", requires_python=">=3.10,<3.14"
            )
        }
    )
    service = build_service(fetcher)
    first = _check(service, ("pypi", "django", "5.2"), ("runtime", "python", "3.13.0"))
    calls_after_first = len(fetcher.calls)
    second = _check(service, ("pypi", "django", "5.2"), ("runtime", "python", "3.12.0"))

    assert len(fetcher.calls) == calls_after_first
    assert first["verdict"] == second["verdict"] == "supported"


@pytest.mark.parametrize(
    ("subject", "counterpart"),
    [
        (("pypi", "django", "5.2"), ("npm", "react", "19.1.1")),
        (("pypi", "django", "5.2"), ("runtime", "node", "22.17.0")),
        (("npm", "react", "19.1.1"), ("runtime", "python", "3.13.0")),
        (("runtime", "python", "3.13.0"), ("runtime", "python", "3.12.0")),
    ],
)
def test_every_unregistered_pair_ends_in_relation_not_supported(
    subject: tuple[str, str, str], counterpart: tuple[str, str, str]
) -> None:
    result = _check(build_service(FakeFetcher()), subject, counterpart)
    assert result["reason"] == "relation_not_supported"


# --------------------------------------------------------------------------------------
# Decision causes and per-target lookups
#
# These are the properties the response contract now rests on. Each one used to be
# derivable only by re-reading prose - a summary sentence, an `explanation` string, or a
# merged `sources_checked` row - and each is asserted here as structure instead.
# --------------------------------------------------------------------------------------


def _app_requiring(marker: str, dependency: str = "helper") -> FakeFetcher:
    return FakeFetcher(
        payloads={
            pypi_url("app", "1.0"): pypi_release(
                "app", "1.0", requires_dist=[f"{dependency}>=1.0; {marker}"]
            ),
            pypi_url(dependency, "1.5"): pypi_release(dependency, "1.5"),
        }
    )


def test_an_environment_marker_reaches_the_response_verbatim() -> None:
    """The marker survives registry text -> claim -> cause -> response without a rewrite."""
    result = _check(
        build_service(_app_requiring('sys_platform == "win32"')),
        ("pypi", "app", "1.0"),
        ("pypi", "helper", "1.5"),
    )

    assert result["reason"] == "insufficient_evidence"
    (cause,) = result["decision_causes"]
    assert cause["kind"] == "conditional_claim"
    assert cause["condition"] == {
        "kind": "environment_marker",
        "expression": 'sys_platform == "win32"',
        "variables": ["sys_platform"],
    }
    # And the summary quotes that same string rather than paraphrasing the condition.
    assert 'sys_platform == "win32"' in result["summary"]


def test_an_extra_guard_is_reported_as_an_extra_not_an_environment() -> None:
    """`extra` is selected by the installing project, not read from the environment."""
    result = _check(
        build_service(_app_requiring('extra == "argon2"')),
        ("pypi", "app", "1.0"),
        ("pypi", "helper", "1.5"),
    )

    (cause,) = result["decision_causes"]
    assert cause["condition"]["kind"] == "extra_marker"
    assert cause["condition"]["expression"] == 'extra == "argon2"'
    assert "selects no extra" in result["summary"]


def test_every_cause_cites_evidence_that_resolves_in_the_same_response() -> None:
    result = _check(
        build_service(_app_requiring('sys_platform == "win32"')),
        ("pypi", "app", "1.0"),
        ("pypi", "helper", "1.5"),
    )

    known = {item["id"] for item in result["evidence"]}
    cited = {i for cause in result["decision_causes"] for i in cause["evidence_ids"]}
    assert cited
    assert cited <= known


def test_a_decided_verdict_carries_no_decision_causes() -> None:
    """Only an `unknown` has a question left open, so only it can name a cause."""
    result = _check(
        build_service(
            FakeFetcher(
                payloads={
                    pypi_url("app", "1.0"): pypi_release(
                        "app", "1.0", requires_dist=["helper>=1.0"]
                    ),
                    pypi_url("helper", "1.5"): pypi_release("helper", "1.5"),
                }
            )
        ),
        ("pypi", "app", "1.0"),
        ("pypi", "helper", "1.5"),
    )

    assert result["verdict"] == "supported"
    assert "decision_causes" not in result


def test_the_same_source_read_for_two_targets_stays_two_rows() -> None:
    """A merged row cannot say which release was actually confirmed to exist."""
    result = _check(
        build_service(
            FakeFetcher(
                payloads={
                    pypi_url("app", "1.0"): pypi_release(
                        "app", "1.0", requires_dist=["helper>=1.0"]
                    ),
                    pypi_url("helper", "1.5"): pypi_release("helper", "1.5"),
                }
            )
        ),
        ("pypi", "app", "1.0"),
        ("pypi", "helper", "1.5"),
    )

    registry = [c for c in result["sources_checked"] if c["source"] == "pypi_json"]
    assert [(c["role"], c["target"]["name"], c["outcome"]) for c in registry] == [
        ("declaring", "app", "ok"),
        ("declared_about", "helper", "ok"),
    ]
    assert all(check["required"] for check in registry)


def test_one_side_failing_does_not_swallow_the_other_side_succeeding() -> None:
    result = _check(
        build_service(
            FakeFetcher(
                payloads={
                    pypi_url("app", "1.0"): pypi_release(
                        "app", "1.0", requires_dist=["helper>=1.0"]
                    )
                },
                failures={pypi_url("helper", "1.5"): "timeout"},
            )
        ),
        ("pypi", "app", "1.0"),
        ("pypi", "helper", "1.5"),
    )

    assert _outcome(result, "pypi_json", "app") == "ok"
    assert _outcome(result, "pypi_json", "helper") == "failed"


def test_a_failed_counterpart_lookup_is_not_reported_as_a_missing_release() -> None:
    """The server never got an answer, so it must not assert that the release is absent.

    Both releases are premises of the verdict, so both lookups are required. Treating the
    counterpart as optional - as an earlier version did - let a timeout fall through to
    `release_not_found`, which states as fact something the server did not learn.
    """
    failing = _check(
        build_service(
            FakeFetcher(
                payloads={
                    pypi_url("app", "1.0"): pypi_release(
                        "app", "1.0", requires_dist=["helper>=1.0"]
                    )
                },
                failures={pypi_url("helper", "1.5"): "timeout"},
            )
        ),
        ("pypi", "app", "1.0"),
        ("pypi", "helper", "1.5"),
    )
    assert failing["reason"] == "lookup_failed"

    # A confirmed 404 is a different answer and keeps its own reason.
    absent = _check(
        build_service(
            FakeFetcher(
                payloads={
                    pypi_url("app", "1.0"): pypi_release(
                        "app", "1.0", requires_dist=["helper>=1.0"]
                    )
                }
            )
        ),
        ("pypi", "app", "1.0"),
        ("pypi", "helper", "1.5"),
    )
    assert absent["reason"] == "release_not_found"


def test_a_reversed_reading_labels_roles_by_the_declaration_not_the_arguments() -> None:
    """Under `direction: reversed` the roles must follow the declaring side, not input order."""
    result = _check(
        build_service(
            FakeFetcher(
                payloads={
                    pypi_url("app", "1.0"): pypi_release(
                        "app", "1.0", requires_python=">=3.10,<3.14"
                    )
                }
            )
        ),
        ("runtime", "python", "3.13.0"),
        ("pypi", "app", "1.0"),
    )

    assert result["relation"]["direction"] == "reversed"
    roles = {
        (check["role"], check["target"]["namespace"])
        for check in result["sources_checked"]
    }
    assert ("declaring", "pypi") in roles
    assert ("declared_about", "runtime") in roles
    assert result["summary"].endswith(
        "The arguments were read in reverse: the declaration comes from pypi:app 1.0."
    )


def test_a_marker_that_cannot_be_settled_is_never_reported_as_decided() -> None:
    """A marker naming nothing PEP 508 defines is undecidable, not constant.

    `"a" == "a"` looks like a tautology, but `packaging` reads the right-hand side as an
    environment key, so evaluating it without an environment fails. The conservative
    reading is the correct one: the server does not know, and says so.
    """
    result = _context(
        build_service(
            FakeFetcher(
                payloads={
                    pypi_url("app", "1.0"): pypi_release(
                        "app", "1.0", requires_dist=['helper>=1.0; "a" == "a"']
                    )
                }
            )
        ),
        ("pypi", "app", "1.0"),
    )

    (constraint,) = result["constraints"]
    assert constraint["condition"]["kind"] == "environment_marker"
    assert constraint["condition"]["variables"] == []


def test_context_constraints_carry_a_structured_condition() -> None:
    result = _context(
        build_service(
            FakeFetcher(
                payloads={
                    pypi_url("app", "1.0"): pypi_release(
                        "app",
                        "1.0",
                        requires_dist=[
                            "plain>=1.0",
                            'guarded>=1.0; sys_platform == "win32"',
                            'optional>=1.0; extra == "fast"',
                        ],
                    )
                }
            )
        ),
        ("pypi", "app", "1.0"),
    )

    by_name = {c["counterpart"]["name"]: c["condition"] for c in result["constraints"]}
    assert by_name["plain"] == {"kind": "unconditional"}
    assert by_name["guarded"]["kind"] == "environment_marker"
    assert by_name["guarded"]["variables"] == ["sys_platform"]
    assert by_name["optional"]["kind"] == "extra_marker"
