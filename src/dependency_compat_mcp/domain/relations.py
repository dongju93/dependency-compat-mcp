"""Relation rules and their direction policy (03 [2]).

The rule table is keyed by a pair of :class:`TargetClass` values rather than namespaces,
because ``runtime:python`` and ``runtime:node`` share a namespace but not a rule.

Each rule owns a direction policy, which is the whole point of this module:

* ``canonicalizable`` - the declaring side is determined by the *kind* of target, not by
  argument order, so the reversed key may be used and the answer echoes
  ``direction = "reversed"``.
* ``directional`` - argument order picks the declaring side. ``A -> B`` and ``B -> A`` are
  different questions, so the reversed key is never consulted.

Failing to find a rule ends the request here: :class:`UnsupportedRelation` carries only
the input pair, so no fabricated ``declaring`` can reach the response.
"""

from dataclasses import dataclass
from typing import Final, Literal, assert_never

from dependency_compat_mcp.domain.targets import (
    RegistryClass,
    RuntimeClass,
    Target,
    TargetClass,
    classify,
)

__all__ = [
    "RULES",
    "Direction",
    "DirectionPolicy",
    "RelationResolution",
    "RelationRule",
    "ResolvedRelation",
    "RuleName",
    "UnsupportedRelation",
    "direction_of",
    "requires_declared_relationship",
    "resolve_relation",
]

type RuleName = Literal[
    "requires_python", "requires_dist", "engines_node", "npm_dependency"
]
type DirectionPolicy = Literal["canonicalizable", "directional"]
type Direction = Literal["as_given", "reversed"]


@dataclass(frozen=True, slots=True)
class RelationRule:
    """One entry of the relation table.

    ``declares_dependency`` marks rules whose whole premise is a declared dependency
    edge. Only those can produce ``no_declared_relationship``: for ``requires_python`` the
    absence of a declaration means "no metadata", while for ``requires_dist`` it means
    "these two packages are unrelated", and 04 keeps those answers apart.
    """

    name: RuleName
    policy: DirectionPolicy
    declaring_class: TargetClass
    declared_about_class: TargetClass
    declares_dependency: bool


_RULE_LIST: Final[tuple[RelationRule, ...]] = (
    RelationRule(
        name="requires_python",
        policy="canonicalizable",
        declaring_class=RegistryClass("pypi"),
        declared_about_class=RuntimeClass("python"),
        declares_dependency=False,
    ),
    RelationRule(
        name="requires_dist",
        policy="directional",
        declaring_class=RegistryClass("pypi"),
        declared_about_class=RegistryClass("pypi"),
        declares_dependency=True,
    ),
    RelationRule(
        name="engines_node",
        policy="canonicalizable",
        declaring_class=RegistryClass("npm"),
        declared_about_class=RuntimeClass("node"),
        declares_dependency=False,
    ),
    RelationRule(
        name="npm_dependency",
        policy="directional",
        declaring_class=RegistryClass("npm"),
        declared_about_class=RegistryClass("npm"),
        declares_dependency=True,
    ),
)

RULES: Final[dict[tuple[TargetClass, TargetClass], RelationRule]] = {
    (rule.declaring_class, rule.declared_about_class): rule for rule in _RULE_LIST
}


@dataclass(frozen=True, slots=True)
class ResolvedRelation:
    """A rule was found in an allowed direction."""

    rule: RelationRule
    direction: Direction
    declaring: Target
    declared_about: Target
    subject: Target
    counterpart: Target


@dataclass(frozen=True, slots=True)
class UnsupportedRelation:
    """No rule applies. Only the input pair survives - nothing else exists to report."""

    subject: Target
    counterpart: Target


type RelationResolution = ResolvedRelation | UnsupportedRelation


def resolve_relation(subject: Target, counterpart: Target) -> RelationResolution:
    """Pick the rule for ``(subject, counterpart)``, honouring each rule's direction policy."""
    subject_class = classify(subject)
    counterpart_class = classify(counterpart)

    forward = RULES.get((subject_class, counterpart_class))
    if forward is not None:
        return ResolvedRelation(
            rule=forward,
            direction="as_given",
            declaring=subject,
            declared_about=counterpart,
            subject=subject,
            counterpart=counterpart,
        )

    reverse = RULES.get((counterpart_class, subject_class))
    if reverse is not None and reverse.policy == "canonicalizable":
        return ResolvedRelation(
            rule=reverse,
            direction="reversed",
            declaring=counterpart,
            declared_about=subject,
            subject=subject,
            counterpart=counterpart,
        )

    # A `directional` rule found only on the reverse key is deliberately *not* used: that
    # would answer a different question than the caller asked.
    return UnsupportedRelation(subject=subject, counterpart=counterpart)


def requires_declared_relationship(relation: ResolvedRelation) -> bool:
    return relation.rule.declares_dependency


def direction_of(resolution: RelationResolution) -> Direction | None:
    match resolution:
        case ResolvedRelation(direction=direction):
            return direction
        case UnsupportedRelation():
            return None
        case _:
            assert_never(resolution)
