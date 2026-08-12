"""Relation resolution and, above all, its direction policy (03 [2]).

The distinction under test is the one 02 and 03 both single out: reversing the arguments
of a ``canonicalizable`` relation asks the *same* question (so the server may read it
backwards and say so), while reversing a ``directional`` one asks a *different* question
(so the reverse key must never be consulted).
"""

import itertools
from typing import Final

import nodesemver
from packaging.version import Version

from dependency_compat_mcp.domain.relations import (
    RelationResolution,
    ResolvedRelation,
    UnsupportedRelation,
    resolve_relation,
)
from dependency_compat_mcp.domain.targets import (
    CanonicalName,
    NodeRuntimeTarget,
    NpmTarget,
    Pep440Version,
    PyPITarget,
    PythonRuntimeTarget,
    SemverVersion,
    Target,
    classify,
)

# Targets are built directly rather than through `parse_target`: the subject of this module
# is rule selection, and `tests/test_targets.py` already owns the parsing contract. Module
# constants also give the identity (`is`) assertions below something stable to point at.


def _pypi(name: str, version: str) -> PyPITarget:
    return PyPITarget(
        name=CanonicalName(name),
        version=Pep440Version(raw=version, parsed=Version(version)),
    )


def _npm(name: str, version: str) -> NpmTarget:
    return NpmTarget(name=CanonicalName(name), version=_semver(version))


def _semver(raw: str) -> SemverVersion:
    return SemverVersion(raw=raw, parsed=nodesemver.make_semver(raw, loose=False))


DJANGO: Final = _pypi("django", "5.2")
CELERY: Final = _pypi("celery", "5.5.0")
REACT: Final = _npm("react", "19.1.1")
REACT_DOM: Final = _npm("react-dom", "19.1.1")
PYTHON: Final = PythonRuntimeTarget(
    version=Pep440Version(raw="3.13", parsed=Version("3.13"))
)
NODE: Final = NodeRuntimeTarget(version=_semver("22.17.0"))

ALL_TARGETS: Final[tuple[Target, ...]] = (
    DJANGO,
    CELERY,
    REACT,
    REACT_DOM,
    PYTHON,
    NODE,
)


def _resolved(resolution: RelationResolution) -> ResolvedRelation:
    assert isinstance(resolution, ResolvedRelation), resolution
    return resolution


# --------------------------------------------------------------------------------------
# The four supported relations
# --------------------------------------------------------------------------------------


def test_pypi_with_python_resolves_requires_python() -> None:
    relation = _resolved(resolve_relation(DJANGO, PYTHON))

    assert relation.rule.name == "requires_python"
    assert relation.rule.policy == "canonicalizable"
    assert relation.direction == "as_given"
    assert relation.declaring is DJANGO
    assert relation.declared_about is PYTHON
    # `requires_python` absent means "no metadata", not "unrelated packages".
    assert relation.rule.declares_dependency is False


def test_pypi_with_pypi_resolves_requires_dist() -> None:
    relation = _resolved(resolve_relation(DJANGO, CELERY))

    assert relation.rule.name == "requires_dist"
    assert relation.rule.policy == "directional"
    assert relation.direction == "as_given"
    assert relation.declaring is DJANGO
    assert relation.declared_about is CELERY
    assert relation.rule.declares_dependency is True


def test_npm_with_node_resolves_engines_node() -> None:
    relation = _resolved(resolve_relation(REACT, NODE))

    assert relation.rule.name == "engines_node"
    assert relation.rule.policy == "canonicalizable"
    assert relation.direction == "as_given"
    assert relation.declaring is REACT
    assert relation.declared_about is NODE
    assert relation.rule.declares_dependency is False


def test_npm_with_npm_resolves_npm_dependency() -> None:
    relation = _resolved(resolve_relation(REACT_DOM, REACT))

    assert relation.rule.name == "npm_dependency"
    assert relation.rule.policy == "directional"
    assert relation.direction == "as_given"
    assert relation.declaring is REACT_DOM
    assert relation.declared_about is REACT
    assert relation.rule.declares_dependency is True


# --------------------------------------------------------------------------------------
# Direction policy
# --------------------------------------------------------------------------------------


def test_canonicalizable_relation_survives_argument_swap_with_only_direction_changed() -> (
    None
):
    """Asking about Django and Python in either word order is one question: either way
    the answer is read from Django's ``requires_python``.

    A model filling arguments in the user's word order must not fall off the rule table,
    so the reversed key is used - and the response says so via ``direction``.
    """
    forward = _resolved(resolve_relation(DJANGO, PYTHON))
    backward = _resolved(resolve_relation(PYTHON, DJANGO))

    assert backward.rule == forward.rule
    assert backward.declaring is DJANGO
    assert backward.declared_about is PYTHON
    assert (forward.direction, backward.direction) == ("as_given", "reversed")
    # The echoed input pair still reflects what the caller actually sent.
    assert (backward.subject, backward.counterpart) == (PYTHON, DJANGO)


def test_canonicalizable_npm_relation_also_reverses() -> None:
    forward = _resolved(resolve_relation(REACT, NODE))
    backward = _resolved(resolve_relation(NODE, REACT))

    assert backward.rule == forward.rule
    assert backward.declaring is REACT
    assert backward.declared_about is NODE
    assert backward.direction == "reversed"


def test_directional_relation_reads_the_subject_in_both_argument_orders() -> None:
    """``A -> B`` and ``B -> A`` are different questions, so neither is ever flipped.

    If the reverse key were consulted here, asking "does celery declare django?" would be
    silently answered with django's ``requires_dist`` - a different fact entirely.
    """
    forward = _resolved(resolve_relation(DJANGO, CELERY))
    backward = _resolved(resolve_relation(CELERY, DJANGO))

    assert forward.rule == backward.rule
    assert forward.direction == backward.direction == "as_given"
    assert forward.declaring is DJANGO
    assert forward.declared_about is CELERY
    assert backward.declaring is CELERY
    assert backward.declared_about is DJANGO


def test_directional_npm_relation_reads_the_subject_in_both_argument_orders() -> None:
    forward = _resolved(resolve_relation(REACT_DOM, REACT))
    backward = _resolved(resolve_relation(REACT, REACT_DOM))

    assert forward.direction == backward.direction == "as_given"
    assert forward.declaring is REACT_DOM
    assert backward.declaring is REACT


# --------------------------------------------------------------------------------------
# Unsupported pairs
# --------------------------------------------------------------------------------------


UNSUPPORTED_PAIRS: Final[tuple[tuple[Target, Target], ...]] = (
    (PYTHON, PYTHON),
    (PYTHON, NODE),
    (NODE, PYTHON),
    (NODE, NODE),
    (DJANGO, REACT),
    (REACT, DJANGO),
    (DJANGO, NODE),
    (NODE, DJANGO),
    (REACT, PYTHON),
    (PYTHON, REACT),
)


def test_unsupported_pairs_carry_only_the_input_pair() -> None:
    for subject, counterpart in UNSUPPORTED_PAIRS:
        resolution = resolve_relation(subject, counterpart)
        assert isinstance(resolution, UnsupportedRelation), (subject, counterpart)
        # Nothing but the inputs: an invented `declaring` would be a fabricated fact.
        assert resolution.subject is subject
        assert resolution.counterpart is counterpart
        assert not hasattr(resolution, "rule")
        assert not hasattr(resolution, "declaring")


# --------------------------------------------------------------------------------------
# Property
# --------------------------------------------------------------------------------------


def test_property_resolve_relation_is_total_and_self_consistent() -> None:
    """Every ordered pair resolves, and a resolution never contradicts its own rule.

    ``declaring``/``declared_about`` must be the two inputs, classified exactly as the
    chosen rule's key demands; otherwise a later stage would read evidence off the wrong
    side of the pair.
    """
    for subject, counterpart in itertools.product(ALL_TARGETS, repeat=2):
        resolution = resolve_relation(subject, counterpart)
        assert isinstance(resolution, ResolvedRelation | UnsupportedRelation)
        if isinstance(resolution, UnsupportedRelation):
            continue

        assert {id(resolution.declaring), id(resolution.declared_about)} == {
            id(subject),
            id(counterpart),
        }
        assert classify(resolution.declaring) == resolution.rule.declaring_class
        assert (
            classify(resolution.declared_about) == resolution.rule.declared_about_class
        )
        match resolution.direction:
            case "as_given":
                assert resolution.declaring is subject
            case "reversed":
                # Only a canonicalizable rule may be read backwards.
                assert resolution.rule.policy == "canonicalizable"
                assert resolution.declaring is counterpart
