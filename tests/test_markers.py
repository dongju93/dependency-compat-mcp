"""PEP 508 marker analysis (03 [5] step 1, "marker 처리").

The server is never given the environment a marker would be evaluated in (02 refuses
codebase input), so ``analyse_marker`` may only report what holds across *every*
environment. The failure this guards against is assuming a marker true: a conditional
dependency silently promoted to an unconditional one would strengthen a verdict with no
trace left for the caller.
"""

import pytest

from dependency_compat_mcp.domain.claims import MarkerDecidability, analyse_marker


@pytest.mark.parametrize(
    "expression",
    [
        'extra == "dev"',
        'extra == "test" and python_version >= "3.10"',
        'python_version >= "3.10" and extra == "docs"',
    ],
)
def test_extra_guarded_markers_are_recognised_before_evaluation(
    expression: str,
) -> None:
    """An extra is not part of a default install, so it is never treated as satisfied.

    The check precedes evaluation because ``Marker.evaluate`` needs an ``extra`` value it
    would have to invent, and any invented value decides the claim.
    """
    assert analyse_marker(expression).decidability == "extra_guarded"


@pytest.mark.parametrize(
    "expression",
    [
        'python_version < "3.11"',
        'sys_platform == "win32"',
        'platform_machine == "arm64"',
        'implementation_name == "pypy"',
        'python_version >= "3.10" and sys_platform == "linux"',
    ],
)
def test_environment_dependent_markers_stay_undecided(expression: str) -> None:
    assert analyse_marker(expression).decidability == "environment_dependent"


@pytest.mark.parametrize(
    "expression",
    [
        # Sound-looking tautologies are still undecided: proving one needs reasoning over
        # the variable's whole domain, which `analyse_marker` deliberately does not attempt.
        'python_version >= "0"',
        'python_version > "1.0" or python_version <= "1.0"',
        'os_name != "definitely-not-an-os-name"',
        # These are the cases a sampled-environment implementation got WRONG, calling them
        # contradictions and dropping the claim. Each is satisfiable in a real environment
        # the sample simply did not contain, and dropping the claim would have made the
        # verdict stronger than its evidence.
        'python_version >= "3.15"',
        'sys_platform == "freebsd"',
        'platform_machine == "riscv64"',
        'implementation_name == "graalpy"',
        'python_version < "3.0" and python_version >= "4.0"',
        'sys_platform == "definitely-not-a-platform"',
    ],
)
def test_markers_naming_any_variable_stay_undecided(expression: str) -> None:
    """Naming a variable is enough to be undecidable, whatever the comparison says.

    The alternative - sampling representative environments and calling unanimity a proof -
    is unsound over open domains, and it decays: every new Python release moves past the
    sampled ceiling and turns another live marker into a false contradiction.
    """
    assert analyse_marker(expression).decidability == "environment_dependent"


def test_a_variable_named_only_inside_a_string_literal_is_not_a_variable() -> None:
    """`sys_platform == "extra"` compares against the word, it is not an extra guard."""
    assert (
        analyse_marker('sys_platform == "extra"').decidability
        == "environment_dependent"
    )


@pytest.mark.parametrize(
    "expression",
    [
        "python_version <",
        "not a marker at all",
        "",
        'python_version >= "3.10" and',
        '=== "3.10"',
        "python_version >= 3.10)",
        'undefined_variable == "x"',
    ],
)
def test_invalid_marker_syntax_degrades_instead_of_raising(expression: str) -> None:
    """Malformed metadata is a fact about the world, not a bug in the server.

    ``requires_dist`` comes from a third party; a raised exception here would fail a whole
    request over one unparseable line, when the honest answer is "this claim cannot be
    decided".
    """
    condition = analyse_marker(expression)

    assert condition.decidability == "environment_dependent"
    # The original text is preserved so it can be quoted back in evidence.
    assert condition.expression == expression


def test_decidability_is_always_one_of_the_four_declared_values() -> None:
    expressions = [
        'extra == "dev"',
        'python_version < "3.11"',
        'python_version >= "0"',
        'python_version < "3.0" and python_version >= "4.0"',
        "garbage",
        "",
        "\x00",
        'python_version == "3.13" or extra == "dev"',
        'python_full_version >= "3.13.0"',
    ]
    allowed: set[MarkerDecidability] = {
        "always_true",
        "always_false",
        "environment_dependent",
        "extra_guarded",
    }

    for expression in expressions:
        assert analyse_marker(expression).decidability in allowed
