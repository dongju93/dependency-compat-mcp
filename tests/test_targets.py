"""The 02 input contract, exercised through :func:`parse_target`.

Two properties matter more than any single case here:

* the parser never *silently corrects* anything, so a spelling that ``packaging`` or
  ``node-semver`` would rewrite is an input error rather than a quiet rewrite, and
* the parser is total on strings: every string either yields a ``Target`` or an
  :class:`InputError`. Any other exception type would escape the MCP boundary as an
  unclassified crash instead of a contract violation the caller can act on.
"""

import random
from typing import Final

import nodesemver
import pytest
from packaging.version import Version

from dependency_compat_mcp.domain.errors import InputError
from dependency_compat_mcp.domain.targets import (
    MAX_NAME_LENGTH,
    NodeRuntimeTarget,
    NpmTarget,
    Pep440Version,
    PyPITarget,
    PythonRuntimeTarget,
    RegistryClass,
    RuntimeClass,
    SemverVersion,
    Target,
    classify,
    name_of,
    namespace_of,
    parse_target,
    version_of,
)


def _semver(raw: str) -> SemverVersion:
    """Build a SemVer value directly, for tests whose subject is not SemVer parsing."""
    return SemverVersion(raw=raw, parsed=nodesemver.make_semver(raw, loose=False))


# --------------------------------------------------------------------------------------
# Accepted input
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("namespace", "name", "version", "expected_name"),
    [
        # The *name* is canonicalised (PyPI folds case and separators); the version is not.
        ("pypi", "Django", "5.2", "django"),
        ("pypi", "zope.interface", "5.0", "zope-interface"),
        ("pypi", "Flask_SQLAlchemy", "3.1.1", "flask-sqlalchemy"),
        ("pypi", "a", "1.0", "a"),
        # Trailing zeros are canonical PEP 440; `packaging` does not strip them.
        ("pypi", "django", "5.2.0.0", "django"),
        ("pypi", "django", "1.0.0a0", "django"),
        ("pypi", "django", "2.0.0+local", "django"),
        pytest.param("npm", "react", "19.1.1", "react"),
        pytest.param("npm", "@scope/pkg~ish", "1.0.0", "@scope/pkg~ish"),
        pytest.param("npm", "~tilde-start", "0.0.1", "~tilde-start"),
        pytest.param("npm", "node-fetch", "3.3.2-beta.1", "node-fetch"),
        ("runtime", "python", "3.13", "python"),
        pytest.param("runtime", "node", "22.17.0", "node"),
    ],
)
def test_valid_input_parses_and_preserves_the_version_spelling(
    namespace: str, name: str, version: str, expected_name: str
) -> None:
    target = parse_target(namespace, name, version)

    assert namespace_of(target) == namespace
    assert str(name_of(target)) == expected_name
    # Round-trip: the caller's version string survives byte for byte.
    assert str(version_of(target)) == version


def test_registry_and_runtime_targets_get_the_expected_variant() -> None:
    assert isinstance(parse_target("pypi", "django", "5.2"), PyPITarget)
    assert isinstance(parse_target("runtime", "python", "3.13"), PythonRuntimeTarget)


# --------------------------------------------------------------------------------------
# Rejected input: one table, because every row is the same contract
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("namespace", "name", "version", "code", "field"),
    [
        # Missing / empty strings.
        ("", "django", "5.2", "empty_value", "namespace"),
        ("pypi", "", "5.2", "empty_value", "name"),
        ("pypi", "django", "", "empty_value", "version"),
        # Surrounding whitespace is never trimmed for the caller.
        (" pypi", "django", "5.2", "surrounding_whitespace", "namespace"),
        ("pypi", " django", "5.2", "surrounding_whitespace", "name"),
        ("pypi", "django ", "5.2", "surrounding_whitespace", "name"),
        ("pypi", "django", " 5.2", "surrounding_whitespace", "version"),
        ("pypi", "django", "5.2\n", "surrounding_whitespace", "version"),
        # Control characters.
        ("pypi", "dj\x00ango", "5.2", "control_character", "name"),
        ("pypi", "django", "5.\x1f2", "control_character", "version"),
        ("pypi", "django", "5.2\x7f", "control_character", "version"),
        # Over-length.
        ("p" * 33, "django", "5.2", "value_too_long", "namespace"),
        ("pypi", "d" * (MAX_NAME_LENGTH + 1), "5.2", "value_too_long", "name"),
        ("pypi", "django", "1." + "0" * 100, "value_too_long", "version"),
        # An npm name beyond npm's own 214-character ceiling. The shared 200-character
        # limit of 02 fires first, which is why `_NPM_MAX_NAME_LENGTH` is unreachable.
        ("npm", "n" * 215, "1.0.0", "value_too_long", "name"),
        # Namespace pattern violations (uppercase, leading digit, illegal character).
        ("Pypi", "django", "5.2", "namespace_syntax", "namespace"),
        ("1pypi", "django", "5.2", "namespace_syntax", "namespace"),
        ("py.pi", "django", "5.2", "namespace_syntax", "namespace"),
        # Syntactically fine but not registered.
        ("maven", "org.example:lib", "1.0", "namespace_not_registered", "namespace"),
        ("product", "postgres", "17", "namespace_not_registered", "namespace"),
        ("cargo", "serde", "1.0.0", "namespace_not_registered", "namespace"),
        # Registered namespace, unregistered runtime.
        ("runtime", "ruby", "3.3.0", "runtime_not_registered", "name"),
        ("runtime", "deno", "2.0.0", "runtime_not_registered", "name"),
        ("runtime", "Python", "3.13", "runtime_not_registered", "name"),
        # Name grammar.
        ("pypi", "_leading-underscore", "1.0", "name_syntax", "name"),
        ("pypi", "trailing-", "1.0", "name_syntax", "name"),
        ("pypi", "has space", "1.0", "name_syntax", "name"),
        ("npm", "UPPERCASE", "1.0.0", "name_syntax", "name"),
        ("npm", ".hidden", "1.0.0", "name_syntax", "name"),
        ("npm", "@scope/", "1.0.0", "name_syntax", "name"),
        ("npm", "@Scope/pkg", "1.0.0", "name_syntax", "name"),
        # A URL is not a name, in either registry.
        ("pypi", "https://example.invalid/pkg", "1.0", "name_syntax", "name"),
        ("npm", "https://example.invalid/pkg", "1.0.0", "name_syntax", "name"),
        # Ranges, wildcards and unions are not exact versions.
        ("pypi", "django", ">=3.10,<3.14", "version_syntax", "version"),
        ("pypi", "django", "^19", "version_syntax", "version"),
        ("pypi", "django", "1.x", "version_syntax", "version"),
        ("pypi", "django", "1.2 || 2.0", "version_syntax", "version"),
        ("pypi", "django", "*", "version_syntax", "version"),
        ("runtime", "python", ">=3.10,<3.14", "version_syntax", "version"),
        ("npm", "react", ">=18", "version_syntax", "version"),
        ("npm", "react", "^19", "version_syntax", "version"),
        ("npm", "react", "1.x", "version_syntax", "version"),
        ("npm", "react", "1.2 || 2.0", "version_syntax", "version"),
        ("npm", "react", "~19.1", "version_syntax", "version"),
        ("runtime", "node", "*", "version_syntax", "version"),
        # Exact but non-canonical: the parser would rewrite these, so it refuses instead.
        ("runtime", "node", "v22.17.0", "version_not_canonical", "version"),
        ("npm", "react", "v19.1.1", "version_not_canonical", "version"),
        ("npm", "react", "19.1.1+build.5", "version_not_canonical", "version"),
        ("pypi", "django", "1.0.0-alpha", "version_not_canonical", "version"),
        ("pypi", "django", "1.0.0-RC1", "version_not_canonical", "version"),
        ("pypi", "django", "1.0.0.RC1", "version_not_canonical", "version"),
        ("pypi", "django", "01.2", "version_not_canonical", "version"),
        ("pypi", "django", "5.2.dev01", "version_not_canonical", "version"),
        ("runtime", "python", "v3.13", "version_not_canonical", "version"),
    ],
)
def test_invalid_input_is_an_input_error_with_a_stable_code(
    namespace: str, name: str, version: str, code: str, field: str
) -> None:
    with pytest.raises(InputError) as caught:
        parse_target(namespace, name, version)

    assert caught.value.code == code
    assert caught.value.field == field


@pytest.mark.parametrize(
    ("namespace", "name", "version"),
    [
        ("pypi", "django", "1.0.0-alpha"),
        ("runtime", "python", "v3.13"),
        ("npm", "react", "v19.1.1"),
    ],
)
def test_non_canonical_version_error_says_the_server_does_not_normalise(
    namespace: str, name: str, version: str
) -> None:
    """The message has to explain *why* a parseable version was refused.

    Without it the caller reads "invalid version" for a string its own tooling accepts and
    has no way to learn that the fix is to send the canonical spelling.
    """
    with pytest.raises(InputError) as caught:
        parse_target(namespace, name, version)

    message = str(caught.value).lower()
    assert "canonical" in message
    assert "does not normalise" in message or "does not strip" in message


def test_semver_canonicality_compares_serialised_forms() -> None:
    """Regression: `nodesemver.valid` returns a ``SemVer``, not a ``str``.

    Comparing that object with the raw string is always unequal, which once made the
    parser reject every well-formed npm and node version as "non-canonical". The check has
    to serialise first - which is also exactly the round-trip the rule asks for.
    """
    assert nodesemver.valid("19.1.1", loose=False) != "19.1.1"
    assert str(nodesemver.valid("19.1.1", loose=False)) == "19.1.1"

    assert str(version_of(parse_target("npm", "react", "19.1.1"))) == "19.1.1"
    with pytest.raises(InputError) as caught:
        parse_target("npm", "react", "v19.1.1")
    assert caught.value.code == "version_not_canonical"


# --------------------------------------------------------------------------------------
# Properties. Hand-rolled deterministic generation: hypothesis is not a dependency, and a
# fixed seed keeps a failure reproducible from the test name alone.
# --------------------------------------------------------------------------------------

_SEED: Final = 20260812
_JUNK_ALPHABET: Final = (
    "abcXYZ019._-~@/:\\ \t\n\x00\x1f>=<^*|,+!?'\"()[]{}%$#ü中\U0001f600"
)
_NAMESPACE_POOL: Final = (
    "pypi",
    "npm",
    "runtime",
    "maven",
    "product",
    "PYPI",
    "runtime ",
    "",
)
_NAME_POOL: Final = (
    "django",
    "Django",
    "react",
    "@scope/pkg",
    "python",
    "node",
    "ruby",
    "",
    "x" * 300,
    "https://example.invalid/p",
    "ünicode",
)
_VERSION_POOL: Final = (
    "5.2",
    "3.13",
    "1.0.0",
    "22.17.0",
    "19.1.1",
    "v22.17.0",
    "1.0.0-alpha",
    ">=3.10,<3.14",
    "^19",
    "1.x",
    "1.2 || 2.0",
    "*",
    "",
    "0" * 300,
    "ü1.0",
)


def _junk(rng: random.Random) -> str:
    return "".join(rng.choice(_JUNK_ALPHABET) for _ in range(rng.randrange(0, 24)))


def _generated_inputs(count: int) -> list[tuple[str, str, str]]:
    rng = random.Random(_SEED)
    return [
        (
            rng.choice((*_NAMESPACE_POOL, _junk(rng))),
            rng.choice((*_NAME_POOL, _junk(rng))),
            rng.choice((*_VERSION_POOL, _junk(rng))),
        )
        for _ in range(count)
    ]


def test_property_parse_target_is_total_over_arbitrary_strings() -> None:
    """Every string is either a ``Target`` or an ``InputError`` - never a stray exception.

    A ``TypeError`` or ``re`` error escaping here would surface to the caller as an
    unclassified tool crash, losing the ``code``/``field`` the MCP layer reports.
    """
    accepted = 0
    for namespace, name, version in _generated_inputs(4000):
        try:
            target = parse_target(namespace, name, version)
        except InputError:
            continue
        except Exception as exc:
            pytest.fail(
                f"parse_target({namespace!r}, {name!r}, {version!r}) raised "
                f"{type(exc).__name__}: {exc}"
            )
        accepted += 1
        assert isinstance(
            target, PyPITarget | NpmTarget | PythonRuntimeTarget | NodeRuntimeTarget
        )

    # A generator that accepts nothing would make the property vacuous.
    assert accepted > 0


def test_property_accepted_targets_round_trip_their_version_string() -> None:
    checked = 0
    for namespace, name, version in _generated_inputs(4000):
        try:
            target = parse_target(namespace, name, version)
        except InputError:
            continue
        assert str(version_of(target)) == version
        checked += 1

    assert checked > 0


def test_property_classify_returns_runtime_class_exactly_for_runtime_targets() -> None:
    """Runtimes are keyed by name, registries by namespace (03 [2]).

    The rule table is keyed on this classification, so a registry target that classified as
    a runtime - or the reverse - would select a rule for the wrong pair of things.
    """
    targets: tuple[Target, ...] = (
        parse_target("pypi", "django", "5.2"),
        parse_target("npm", "react", "19.1.1"),
        parse_target("runtime", "python", "3.13"),
        parse_target("runtime", "node", "22.17.0"),
        # A directly constructed value must classify identically: `classify` reads the
        # variant, never the construction path.
        NodeRuntimeTarget(version=_semver("20.0.0")),
        PyPITarget(
            name=parse_target("pypi", "a", "1.0").name,
            version=Pep440Version(raw="1.0", parsed=Version("1.0")),
        ),
    )

    for target in targets:
        is_runtime = isinstance(target, PythonRuntimeTarget | NodeRuntimeTarget)
        target_class = classify(target)
        assert isinstance(target_class, RuntimeClass) is is_runtime
        assert isinstance(target_class, RegistryClass) is not is_runtime
        if isinstance(target_class, RuntimeClass):
            assert target_class.name == str(name_of(target))
            assert namespace_of(target) == "runtime"
