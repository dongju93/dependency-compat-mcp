"""The tool description contract of 02 ("도구 설명 텍스트 계약").

02 makes the description part of the interface, because the caller deciding whether to
invoke a tool is a language model reading exactly this text. So the four mandated elements
are asserted here as structure, not reviewed as prose - and the three banned kinds of
content are asserted absent, because each one degrades the text over time rather than
being wrong on the day it is written.

The MCP contract test compares these same constants against what ``tools/list`` publishes;
this module checks the constants themselves, so a failure points at the text rather than
at the registration.
"""

import re
from typing import Final

import pytest

from dependency_compat_mcp.descriptions import (
    ARGUMENT_DESCRIPTIONS,
    CHECK_COMPATIBILITY_DESCRIPTION,
    GET_COMPATIBILITY_CONTEXT_DESCRIPTION,
    REQUIRED_ELEMENTS,
    SERVER_INSTRUCTIONS,
)
from dependency_compat_mcp.server import TOOL_ARGUMENT_NAMES

TOOL_DESCRIPTIONS: Final[dict[str, str]] = {
    "check_compatibility": CHECK_COMPATIBILITY_DESCRIPTION,
    "get_compatibility_context": GET_COMPATIBILITY_CONTEXT_DESCRIPTION,
}


def _first_index(text: str, probes: tuple[str, ...]) -> int:
    return min(text.index(probe) for probe in probes if probe in text)


# --------------------------------------------------------------------------------------
# The four mandated elements
# --------------------------------------------------------------------------------------


def test_the_four_required_elements_are_the_ones_02_lists() -> None:
    assert tuple(element.name for element in REQUIRED_ELEMENTS) == (
        "question",
        "when_to_call",
        "argument_order",
        "unknown_semantics",
    )


@pytest.mark.parametrize("tool", sorted(TOOL_DESCRIPTIONS))
def test_every_tool_description_contains_all_four_elements(tool: str) -> None:
    text = TOOL_DESCRIPTIONS[tool].lower()

    missing = {
        element.name: [probe for probe in element.probes if probe not in text]
        for element in REQUIRED_ELEMENTS
    }
    assert not any(missing.values()), missing


@pytest.mark.parametrize("tool", sorted(TOOL_DESCRIPTIONS))
def test_the_four_elements_appear_in_the_mandated_order(tool: str) -> None:
    """02 fixes the order, not just the presence.

    A model that stops reading early must still have seen the question and the call
    condition; burying them behind the argument-order paragraph defeats the point.
    """
    text = TOOL_DESCRIPTIONS[tool].lower()

    positions = [_first_index(text, element.probes) for element in REQUIRED_ELEMENTS]
    assert positions == sorted(positions), dict(
        zip((element.name for element in REQUIRED_ELEMENTS), positions, strict=True)
    )


@pytest.mark.parametrize("tool", sorted(TOOL_DESCRIPTIONS))
def test_the_unknown_element_frames_unknown_as_a_result_rather_than_a_failure(
    tool: str,
) -> None:
    """The single most load-bearing sentence: without it a caller retries or distrusts.

    ``unknown`` is a designed first-class result. If the description does not say so, that
    design is void at the point of consumption.
    """
    text = TOOL_DESCRIPTIONS[tool].lower()

    assert "unknown is a normal result" in text
    assert "not an error" in text
    assert "not a reason to retry" in text
    assert "limitations" in text
    assert "sources_checked" in text


@pytest.mark.parametrize("tool", sorted(TOOL_DESCRIPTIONS))
def test_the_argument_order_element_names_both_kinds_of_rule(tool: str) -> None:
    """Both halves of the 02 convention must be present, or the caller learns the wrong one.

    Saying only "order matters" makes a model shy away from the reversed reading it is
    allowed to rely on; saying only "either order works" invites it to flip a directional
    dependency question.
    """
    text = TOOL_DESCRIPTIONS[tool].lower()

    # Order fixes the declaring side for the two registry-to-registry rules ...
    assert "pypi -> pypi" in text
    assert "npm -> npm" in text
    # ... while the kind of target fixes it for the two runtime rules.
    assert "runtime" in text
    assert "kind of target" in text
    # And the response says which reading was used.
    assert "relation.direction" in text


# --------------------------------------------------------------------------------------
# Content 02 forbids
# --------------------------------------------------------------------------------------

_LATENCY_WORDS: Final[tuple[str, ...]] = (
    "millisecond",
    "latency",
    "fast",
    "slow",
    "quick",
    "performance",
    "throughput",
    "response time",
    "seconds",
    "timeout",
    "cache",
)

# Naming packages would date the text on every pack release; coverage belongs in `depth`
# and `limitations`, which are computed per response.
_PACKAGE_NAMES: Final[tuple[str, ...]] = (
    "django",
    "flask",
    "numpy",
    "pandas",
    "requests",
    "react",
    "express",
    "lodash",
    "typescript",
    "webpack",
)


@pytest.mark.parametrize("tool", sorted(TOOL_DESCRIPTIONS))
def test_descriptions_carry_no_example_payloads(tool: str) -> None:
    text = TOOL_DESCRIPTIONS[tool].lower()

    assert "```" not in text
    assert "example" not in text
    assert "{" not in text and "}" not in text
    assert '"namespace"' not in text


@pytest.mark.parametrize("tool", sorted(TOOL_DESCRIPTIONS))
def test_descriptions_make_no_performance_claims(tool: str) -> None:
    text = TOOL_DESCRIPTIONS[tool].lower()

    for word in _LATENCY_WORDS:
        assert word not in text, word
    assert re.search(r"\bms\b", text) is None


@pytest.mark.parametrize("tool", sorted(TOOL_DESCRIPTIONS))
def test_descriptions_list_no_packages_and_quote_no_coverage_numbers(tool: str) -> None:
    text = TOOL_DESCRIPTIONS[tool].lower()

    for package in _PACKAGE_NAMES:
        assert package not in text, package
    # No digit can appear at all, which rules out a coverage count or a package total.
    assert re.search(r"\d", text) is None
    assert "%" not in text
    assert "supported packages" not in text


# --------------------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(TOOL_DESCRIPTIONS))
def test_descriptions_stay_within_the_length_budget(tool: str) -> None:
    """Long enough to carry four elements, short enough that the first is still read."""
    words = len(TOOL_DESCRIPTIONS[tool].split())

    assert 90 <= words <= 150, words


def test_descriptions_are_single_line_constants() -> None:
    """One paragraph each: MCP clients render these inline, and a newline is not markup."""
    for text in (*TOOL_DESCRIPTIONS.values(), SERVER_INSTRUCTIONS):
        assert "\n" not in text
        assert text == text.strip()


def test_server_instructions_point_at_both_tools_and_keep_the_same_boundaries() -> None:
    text = SERVER_INSTRUCTIONS.lower()

    for tool in TOOL_DESCRIPTIONS:
        assert tool in text
    assert "unknown" in text
    assert "example" not in text
    assert "```" not in text


# --------------------------------------------------------------------------------------
# Per-argument text
# --------------------------------------------------------------------------------------


def test_every_accepted_argument_has_exactly_one_description() -> None:
    """`TOOL_ARGUMENT_NAMES` is the single source of truth for what a tool accepts.

    An argument missing here would be published with no text at all, and a stale entry
    would describe an argument the middleware rejects.
    """
    assert {tool: set(text) for tool, text in ARGUMENT_DESCRIPTIONS.items()} == {
        tool: set(names) for tool, names in TOOL_ARGUMENT_NAMES.items()
    }


def test_argument_descriptions_are_single_line_and_distinct() -> None:
    """The two sides of a pair must not read the same, or order is undocumented."""
    for tool, texts in ARGUMENT_DESCRIPTIONS.items():
        assert len(set(texts.values())) == len(texts), tool
        for name, text in texts.items():
            assert "\n" not in text, (tool, name)
            assert text == text.strip(), (tool, name)


def test_each_argument_says_it_takes_one_exact_release() -> None:
    """The most common misuse is passing a range; every argument states the rule itself.

    A caller filling in one field may never have read the tool description in full.
    """
    for tool, texts in ARGUMENT_DESCRIPTIONS.items():
        for name, text in texts.items():
            lowered = text.lower()
            assert "exact release" in lowered, (tool, name)
            assert "namespace, name and version" in lowered, (tool, name)


def test_the_pair_names_the_declaring_side_and_the_single_argument_denies_it() -> None:
    """The asymmetry of 02, stated where the argument is filled in rather than only above."""
    pair = ARGUMENT_DESCRIPTIONS["check_compatibility"]
    for text in pair.values():
        assert "declaring side" in text.lower()
    assert "swapping the two arguments asks a different question" in pair["subject"]

    single = ARGUMENT_DESCRIPTIONS["get_compatibility_context"]["target"].lower()
    assert "no argument order" in single
