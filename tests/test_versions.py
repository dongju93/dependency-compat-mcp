"""Version-expression analysis: membership, ceiling shape, and floor (03 step 5).

``bounded_above`` and ``lower_bound`` are not conveniences - the whole "installs is not
supports" rule of 03 hangs on them. A gate read as closed when it is open turns an
honest ``unknown`` into a fabricated ``supported``, so both are pinned case by case here
and then tied back to ``admits`` by a property.

Version values are constructed directly instead of via ``parse_target`` because the
subject here is the expression, not the input contract; ``tests/test_targets.py`` covers
the parsing side.
"""

import random
from typing import Final

import nodesemver
import pytest
from packaging.version import Version

from dependency_compat_mcp.domain.targets import (
    ExactVersion,
    Pep440Version,
    SemverVersion,
    VersionScheme,
)
from dependency_compat_mcp.domain.versions import admits, bounded_above, lower_bound


def _pep(raw: str) -> Pep440Version:
    return Pep440Version(raw=raw, parsed=Version(raw))


def _sem(raw: str) -> SemverVersion:
    return SemverVersion(raw=raw, parsed=nodesemver.make_semver(raw, loose=False))


# --------------------------------------------------------------------------------------
# admits
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "version", "expected"),
    [
        (">=3.10", "3.13", True),
        (">=3.10", "3.9", False),
        (">=3.10,<3.14", "3.13", True),
        (">=3.10,<3.14", "3.14", False),
        (">=3.10,<3.14", "3.10", True),
        ("~=2.2", "2.3", True),
        ("~=2.2", "3.0", False),
        ("==5.2.*", "5.2.1", True),
        ("==5.2.*", "5.3", False),
        ("!=3.9", "3.9", False),
        ("!=3.9", "3.10", True),
        # An explicitly named pre-release is admitted: the caller asked about this release.
        (">=1.0", "2.0.0a1", True),
    ],
)
def test_admits_pep440(expression: str, version: str, expected: bool) -> None:
    assert admits(expression, "pep440", _pep(version)) is expected


@pytest.mark.parametrize(
    ("expression", "version", "expected"),
    [
        (">=18", "22.17.0", True),
        (">=18", "16.20.0", False),
        ("^22", "22.1.0", True),
        ("^22", "23.0.0", False),
        ("~22.17.0", "22.17.5", True),
        ("~22.17.0", "22.18.0", False),
        (">=18 || ^16.5.0", "16.9.0", True),
        (">=18 || ^16.5.0", "16.4.0", False),
        ("*", "1.0.0", True),
        (">=18 <20", "19.0.0", True),
        (">=18 <20", "20.0.0", False),
    ],
)
def test_admits_semver(expression: str, version: str, expected: bool) -> None:
    assert admits(expression, "semver", _sem(version)) is expected


@pytest.mark.parametrize(
    ("expression", "scheme", "version"),
    [
        # A PEP 440 range against a SemVer release, and the reverse: comparing across
        # ecosystems is not a near-miss to be coerced, it is undecidable here.
        (">=3.10", "pep440", _sem("22.17.0")),
        ("^22", "semver", _pep("3.13")),
        # A range that does not parse in the scheme it claims.
        ("^22", "pep440", _pep("3.13")),
        (">=3.10,<3.14", "semver", _sem("22.17.0")),
        ("not-a-range", "pep440", _pep("3.13")),
        ("not-a-range", "semver", _sem("22.17.0")),
    ],
)
def test_admits_returns_none_on_scheme_mismatch_or_unparseable_range(
    expression: str, scheme: VersionScheme, version: ExactVersion
) -> None:
    assert admits(expression, scheme, version) is None


# --------------------------------------------------------------------------------------
# bounded_above
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (">=3.10", False),
        (">=3.10,<3.14", True),
        # `~=2.2` is `>=2.2, ==2.*`: the compatible-release operator carries its own ceiling.
        ("~=2.2", True),
        ("==5.2.*", True),
        ("==5.2", True),
        # `!=` excludes a point; it never closes the set from above.
        ("!=3.9", False),
        (">=3.10,!=3.12", False),
        ("<=3.13", True),
        ("", False),
    ],
)
def test_bounded_above_pep440(expression: str, expected: bool) -> None:
    assert bounded_above(expression, "pep440") is expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (">=18", False),
        ("^22", True),
        ("~22.17.0", True),
        (">=18 <20", True),
        ("1.x", True),
        # A union is closed only if *every* branch is; the `>=18` branch is not.
        (">=18 || ^16.5.0", False),
        ("^22 || ^20", True),
        ("*", False),
    ],
)
def test_bounded_above_semver(expression: str, expected: bool) -> None:
    assert bounded_above(expression, "semver") is expected


def test_bounded_above_returns_none_for_an_unparseable_expression() -> None:
    assert bounded_above("^22", "pep440") is None
    assert bounded_above(">=3.10,<3.14", "semver") is None


# --------------------------------------------------------------------------------------
# lower_bound
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "scheme", "expected"),
    [
        (">=3.10", "pep440", (3, 10)),
        (">=3.10,<3.14", "pep440", (3, 10)),
        ("~=2.2", "pep440", (2, 2)),
        ("==5.2.*", "pep440", (5, 2)),
        # Two floors in one conjunction: the tighter one wins.
        (">=3.9,>=3.11", "pep440", (3, 11)),
        (">=18", "semver", (18, 0, 0)),
        ("^22", "semver", (22, 0, 0)),
        ("~22.17.0", "semver", (22, 17, 0)),
        # A union's floor is its lowest branch, not its highest.
        (">=18 || ^16.5.0", "semver", (16, 5, 0)),
    ],
)
def test_lower_bound_finds_the_floor(
    expression: str, scheme: VersionScheme, expected: tuple[int, ...]
) -> None:
    bound = lower_bound(expression, scheme)

    assert bound is not None
    assert bound.release == expected
    assert bound.scheme == scheme
    # The originating text is preserved so evidence can quote the constraint verbatim.
    assert bound.expression == expression


@pytest.mark.parametrize(
    ("expression", "scheme"),
    [
        # No floor at all.
        ("!=3.9", "pep440"),
        ("<3.14", "pep440"),
        ("", "pep440"),
        ("*", "semver"),
        ("<20", "semver"),
        # One unbounded branch leaves the whole union unbounded below.
        ("^22 || *", "semver"),
        # Unparseable in the claimed scheme.
        ("^22", "pep440"),
        (">=3.10,<3.14", "semver"),
    ],
)
def test_lower_bound_is_none_when_a_branch_has_no_floor(
    expression: str, scheme: VersionScheme
) -> None:
    assert lower_bound(expression, scheme) is None


# --------------------------------------------------------------------------------------
# Properties. Deterministic generation with a fixed seed; hypothesis is not a dependency.
# --------------------------------------------------------------------------------------

_SEED: Final = 20260812
# Digits are restricted to 0-2 and the length is capped so no generated expression can
# name a ceiling anywhere near `_FAR_ABOVE`, which the boundary property below relies on.
_EXPRESSION_ALPHABET: Final = "012.*<>=~^!|,-+ xX "
_MAX_JUNK_LENGTH: Final = 8
# Above every ceiling any known or generated expression can express, and still far below
# SemVer's safe-integer limit.
_FAR_ABOVE: Final = "999999999.0.0"

_KNOWN_EXPRESSIONS: Final[tuple[str, ...]] = (
    ">=3.10",
    ">=3.10,<3.14",
    "~=2.2",
    "==5.2.*",
    "!=3.9",
    "<=3.13",
    "===weird",
    ">=18",
    "^22",
    "~22.17.0",
    ">=18 <20",
    ">=18 || ^16.5.0",
    "1.x",
    "*",
    "",
)

_PROBE_VERSIONS: Final[tuple[ExactVersion, ...]] = (
    _pep("3.13"),
    _pep("2.0.0a1"),
    _sem("22.17.0"),
    _sem("1.0.0-rc.1"),
)


def _generated_expressions(count: int) -> list[str]:
    rng = random.Random(_SEED)
    junk = [
        "".join(
            rng.choice(_EXPRESSION_ALPHABET)
            for _ in range(rng.randrange(0, _MAX_JUNK_LENGTH + 1))
        )
        for _ in range(count)
    ]
    return [*_KNOWN_EXPRESSIONS, *junk]


def test_property_the_three_functions_never_raise() -> None:
    """Garbage in the expression position is a *value* problem, not an exception.

    These run on registry metadata, where a malformed ``requires_dist`` is entirely
    possible; an exception here would abort a request that should have degraded to
    ``indeterminate``.
    """
    for expression in _generated_expressions(3000):
        for scheme in ("pep440", "semver"):
            for version in _PROBE_VERSIONS:
                try:
                    verdict = admits(expression, scheme, version)
                    ceiling = bounded_above(expression, scheme)
                    floor = lower_bound(expression, scheme)
                except Exception as exc:
                    pytest.fail(
                        f"{expression!r} in {scheme} raised {type(exc).__name__}: {exc}"
                    )
                assert verdict is None or isinstance(verdict, bool)
                assert ceiling is None or isinstance(ceiling, bool)
                assert floor is None or floor.scheme == scheme


def test_property_a_closed_ceiling_excludes_a_version_above_every_ceiling() -> None:
    """``bounded_above`` must agree with ``admits`` at the top of the range.

    If a range reports a closed ceiling, some version has to be outside it; a release far
    above anything the generated expressions can mention is the witness. Disagreement here
    would mean 03 step 5 reads "enumerated support" off a range that never enumerated it.
    """
    far_above: dict[VersionScheme, ExactVersion] = {
        "pep440": _pep(_FAR_ABOVE),
        "semver": _sem(_FAR_ABOVE),
    }
    checked = 0
    for expression in _generated_expressions(3000):
        for scheme in ("pep440", "semver"):
            if bounded_above(expression, scheme) is not True:
                continue
            # A parseable range with a ceiling always yields a decidable membership test.
            assert admits(expression, scheme, far_above[scheme]) is False, expression
            checked += 1

    assert checked > 0
