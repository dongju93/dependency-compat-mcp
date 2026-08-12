"""Version-expression analysis, one function per ecosystem, never mixed.

03 forbids translating a range from one ecosystem's syntax into another's. The two
implementations below therefore stay separate and every entry point takes the
:class:`VersionScheme` tag that the value already carries; a scheme mismatch is answered
with ``None`` ("cannot be decided here") rather than a coerced comparison.

Besides "does this range admit that version", the decision procedure needs two shape
questions about a gate (03 step 5): is the upper bound closed, and where does the lower
bound sit? Both are answered structurally, from the parsed range, not by string matching.
"""

from dataclasses import dataclass
from typing import Final, assert_never

import nodesemver
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from dependency_compat_mcp.domain.targets import (
    ExactVersion,
    Pep440Version,
    SemverVersion,
    VersionScheme,
)

__all__ = [
    "BoundedVersion",
    "admits",
    "bounded_above",
    "lower_bound",
    "parse_expression",
    "parse_semver_range",
    "parse_specifier_set",
    "release_tuple",
    "valid_expression",
]

# PEP 440 operators that place a ceiling on the admitted set. `!=` never does; `~=` does
# (`~=2.2` means `>=2.2, ==2.*`); `==` with or without a `.*` suffix always does.
_PEP440_UPPER_OPS: Final[frozenset[str]] = frozenset({"<", "<=", "==", "===", "~="})
_PEP440_LOWER_OPS: Final[frozenset[str]] = frozenset({">", ">=", "==", "===", "~="})
_SEMVER_UPPER_OPS: Final[frozenset[str]] = frozenset({"<", "<="})
_SEMVER_LOWER_OPS: Final[frozenset[str]] = frozenset({">", ">=", "="})


@dataclass(frozen=True, slots=True)
class BoundedVersion:
    """The lowest release a gate admits, kept in its own scheme.

    Only used for the staleness check in 03 step 5, so it stores just enough to compare
    against a runtime release table entry.
    """

    expression: str
    scheme: VersionScheme
    release: tuple[int, ...]


def valid_expression(expression: str, scheme: VersionScheme) -> bool:
    """Return whether ``expression`` parses as a range in ``scheme``."""
    return parse_expression(expression, scheme) is not None


def parse_specifier_set(expression: str) -> SpecifierSet | None:
    """Parse a PEP 440 specifier set, or ``None`` when it is not one."""
    try:
        return SpecifierSet(expression)
    except InvalidSpecifier:
        return None


def parse_semver_range(expression: str) -> nodesemver.Range | None:
    """Parse an npm SemVer range, or ``None`` when it is not one."""
    try:
        return nodesemver.make_range(expression, loose=False)
    except Exception:
        return None


def parse_expression(
    expression: str, scheme: VersionScheme
) -> SpecifierSet | nodesemver.Range | None:
    """Dispatch to the parser for ``scheme``.

    Each scheme has its own precisely typed parser above; this exists for callers that
    only need "does it parse". Callers that go on to *use* the parsed value take the
    scheme-specific function, so no one has to re-narrow a union.
    """
    match scheme:
        case "pep440":
            return parse_specifier_set(expression)
        case "semver":
            return parse_semver_range(expression)
        case _:
            assert_never(scheme)


def admits(
    expression: str, scheme: VersionScheme, version: ExactVersion
) -> bool | None:
    """Does ``expression`` admit ``version``?

    Returns ``None`` when the two sides do not share a scheme or the expression does not
    parse - the caller turns that into ``indeterminate`` instead of guessing a side.
    """
    match (scheme, version):
        case ("pep440", Pep440Version(parsed=parsed)):
            specifier = parse_specifier_set(expression)
            if specifier is None:
                return None
            # `prereleases=True` matches what installers do for an explicitly named
            # release: the caller asked about this exact version, including a pre-release.
            return specifier.contains(parsed, prereleases=True)
        case ("semver", SemverVersion(raw=raw)):
            if parse_semver_range(expression) is None:
                return None
            # npm's own default: a pre-release only satisfies a range that names one with
            # the same [major, minor, patch]. Not overridden here.
            return bool(nodesemver.satisfies(raw, expression, loose=False))
        case _:
            return None


def bounded_above(expression: str, scheme: VersionScheme) -> bool | None:
    """Is the admitted set closed above?

    An open ceiling is what makes 03 step 5 refuse to read "installs" as "supported": a
    ``>=3.10`` written before 3.14 existed cannot be a statement about 3.14.
    """
    match scheme:
        case "pep440":
            specifier = parse_specifier_set(expression)
            if specifier is None:
                return None
            # A PEP 440 specifier set is a pure conjunction: one ceiling bounds the whole set.
            return any(spec.operator in _PEP440_UPPER_OPS for spec in specifier)
        case "semver":
            parsed = parse_semver_range(expression)
            if parsed is None:
                return None
            # A SemVer range is a union of conjunctions; the union is bounded only if
            # *every* branch is.
            return all(
                any(
                    comparator.operator in _SEMVER_UPPER_OPS
                    for comparator in branch
                    if comparator.operator is not None
                )
                for branch in parsed.set
            )
        case _:
            assert_never(scheme)


def lower_bound(expression: str, scheme: VersionScheme) -> BoundedVersion | None:
    """The lowest release ``expression`` admits, or ``None`` when it has no floor."""
    match scheme:
        case "pep440":
            specifier = parse_specifier_set(expression)
            if specifier is None:
                return None
            floors = [
                _pep440_release(spec.version)
                for spec in specifier
                if spec.operator in _PEP440_LOWER_OPS
            ]
            usable = [floor for floor in floors if floor is not None]
            if not usable:
                return None
            return BoundedVersion(
                expression=expression, scheme="pep440", release=max(usable)
            )
        case "semver":
            parsed = parse_semver_range(expression)
            if parsed is None:
                return None
            branch_floors: list[tuple[int, ...]] = []
            for branch in parsed.set:
                floors = [
                    floor
                    for comparator in branch
                    if comparator.operator in _SEMVER_LOWER_OPS
                    and (floor := _semver_release(comparator.semver)) is not None
                ]
                if not floors:
                    # One unbounded branch leaves the union unbounded below.
                    return None
                branch_floors.append(max(floors))
            if not branch_floors:
                return None
            return BoundedVersion(
                expression=expression, scheme="semver", release=min(branch_floors)
            )
        case _:
            assert_never(scheme)


def release_tuple(version: ExactVersion) -> tuple[int, ...]:
    """The numeric release segment, used only to compare against a runtime release table."""
    match version:
        case Pep440Version(parsed=parsed):
            return parsed.release
        case SemverVersion(parsed=parsed):
            return (parsed.major, parsed.minor, parsed.patch)
        case _:
            assert_never(version)


def _semver_release(semver: object) -> tuple[int, int, int] | None:
    """The numeric release of a comparator's version, or ``None`` for the ANY sentinel.

    node-semver represents an unbounded comparator (``*``) with a sentinel object rather
    than a version, so the fields have to be probed instead of assumed.
    """
    major = getattr(semver, "major", None)
    minor = getattr(semver, "minor", None)
    patch = getattr(semver, "patch", None)
    if isinstance(major, int) and isinstance(minor, int) and isinstance(patch, int):
        return (major, minor, patch)
    return None


def _pep440_release(raw: str) -> tuple[int, ...] | None:
    try:
        return Version(raw.rstrip(".*")).release
    except InvalidVersion:
        return None
