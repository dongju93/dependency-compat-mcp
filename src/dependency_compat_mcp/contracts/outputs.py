"""The public tool output models (04).

Every invariant 04 lists is enforced here by construction or by an always-on validator,
because these models are the last place a malformed response can be caught before it
reaches a caller:

* three verdict variants as separate types behind a ``verdict`` discriminator, so
  ``reason`` cannot coexist with ``verdict_evidence_ids``;
* ``verdict_evidence_ids`` non-empty and resolvable inside the same response;
* ``decision_causes`` present exactly when ``reason`` is ``insufficient_evidence``, with
  every cause's evidence resolvable in the same response - and only ``UnknownResult``
  declares the field at all, so a decided verdict cannot carry one;
* a conditional cause carries a :class:`MarkerGuardOut`, not the wider condition union, so
  "conditional on nothing in particular" cannot be expressed;
* ``sources_checked`` empty exactly when no lookup was attempted, which happens only for
  ``relation_not_supported``;
* evidence split into constraint-carrying and narrative types instead of nullable fields;
* ``depth`` and the presence of curated provenance are each other's mirror.

A validator failing here is not bad external data - it means the server assembled
something it should not have been able to assemble, so the failure surfaces as a tool
error (03 step 7).
"""

from datetime import UTC, date, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_serializer,
    model_validator,
)

from dependency_compat_mcp.domain.claims import LookupRole, SourceId, SourceType
from dependency_compat_mcp.domain.diagnostics import (
    GuardKind,
    LimitationCode,
    NoticeCode,
    UnprovenKind,
)
from dependency_compat_mcp.domain.relations import Direction, RuleName
from dependency_compat_mcp.domain.targets import Namespace, VersionScheme

__all__ = [
    "ChangeOut",
    "CheckCompatibilityResult",
    "ConditionOut",
    "ConditionalClaimOut",
    "ConstraintOut",
    "ContextAvailableResult",
    "ContextResult",
    "ContextUnknownResult",
    "CuratedProvenanceOut",
    "DecidedMarkerOut",
    "DecisionCauseOut",
    "EvidenceOut",
    "FetchedProvenanceOut",
    "GetCompatibilityContextResult",
    "LimitationOut",
    "MarkerGuardOut",
    "NarrativeEvidenceOut",
    "NoticeOut",
    "RelationOut",
    "ResolvedRelationOut",
    "SourceCheckOut",
    "SupportedResult",
    "TargetIdOut",
    "TargetOut",
    "UnconditionalOut",
    "UnknownResult",
    "UnprovenClaimOut",
    "UnsupportedRelationOut",
    "UnsupportedResult",
    "VersionConstraintEvidenceOut",
]


class _Out(BaseModel):
    """Base for every response model: closed, and frozen so assembly cannot patch later."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------------------
# Shared leaves
# --------------------------------------------------------------------------------------


class TargetOut(_Out):
    """The canonical identity the server actually evaluated."""

    namespace: Namespace
    name: str
    version: str


class TargetIdOut(_Out):
    """A versionless identity, used where a declaration names a package but not a release."""

    namespace: Namespace
    name: str


class FetchedProvenanceOut(_Out):
    kind: Literal["fetched"] = "fetched"
    retrieved_at: datetime

    @field_serializer("retrieved_at")
    def _serialise_retrieved_at(self, value: datetime) -> str:
        # 04 spells this as `...Z`; pydantic would otherwise emit `+00:00`.
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class CuratedProvenanceOut(_Out):
    kind: Literal["curated"] = "curated"
    reviewed_at: date
    pack_version: str


type ProvenanceOut = Annotated[
    FetchedProvenanceOut | CuratedProvenanceOut, Field(discriminator="kind")
]


class VersionConstraintEvidenceOut(_Out):
    """A source carrying the very expression the verdict was computed from."""

    id: str
    source_type: SourceType
    title: str
    url: str
    substantiates: str
    expression: str
    scheme: VersionScheme
    provenance: ProvenanceOut


class NarrativeEvidenceOut(_Out):
    """A source with no machine-readable range - it does not have the fields at all."""

    id: str
    source_type: SourceType
    title: str
    url: str
    substantiates: str
    provenance: ProvenanceOut


type EvidenceOut = VersionConstraintEvidenceOut | NarrativeEvidenceOut


class NoticeOut(_Out):
    code: NoticeCode
    evidence_ids: tuple[str, ...] = ()


class LimitationOut(_Out):
    code: LimitationCode


class SourceCheckOut(_Out):
    """One lookup, named by what was opened *and for which release*.

    ``target`` and ``role`` are what make the list auditable: both sides of a same-registry
    comparison read the same ``source``, so without them one row cannot say whether the
    declaring release, the counterpart, or both were confirmed to exist. ``required`` is
    carried through rather than dropped, because it is the difference between a failure
    that decided the verdict and one that only narrowed coverage.
    """

    source: SourceId
    target: TargetOut
    role: LookupRole
    required: bool
    outcome: Literal["ok", "not_found", "failed", "skipped"]
    detail: str | None = None


# --------------------------------------------------------------------------------------
# Conditions and decision causes
# --------------------------------------------------------------------------------------


class UnconditionalOut(_Out):
    """The declaration carries no marker at all."""

    kind: Literal["unconditional"] = "unconditional"


class MarkerGuardOut(_Out):
    """A marker the server cannot settle, quoted verbatim with what it depends on.

    ``variables`` names the environment facts that would decide it, so a caller can tell a
    platform guard from an optional extra without re-parsing ``expression``. It can be
    empty: a marker the PEP 508 parser rejects is undecidable and names nothing.
    """

    kind: GuardKind
    expression: str
    variables: tuple[str, ...]


class DecidedMarkerOut(_Out):
    """A marker naming no environment variable, so one evaluation settles it.

    ``holds`` is that evaluation. Kept apart from :class:`MarkerGuardOut` because a decided
    marker has a truth value and an undecidable one does not - a single type would have
    needed a nullable field meaning "sometimes answered".
    """

    kind: Literal["decided_marker"] = "decided_marker"
    expression: str
    holds: bool


type ConditionOut = Annotated[
    UnconditionalOut | MarkerGuardOut | DecidedMarkerOut,
    Field(discriminator="kind"),
]


class ConditionalClaimOut(_Out):
    """A declaration that neither applies nor drops out until an environment is given."""

    kind: Literal["conditional_claim"] = "conditional_claim"
    # Narrower than `ConditionOut` on purpose: an unconditional or already-decided
    # declaration is not a reason the verdict stayed open, so it cannot be named here.
    condition: MarkerGuardOut
    evidence_ids: Annotated[tuple[str, ...], Field(min_length=1)]


class UnprovenClaimOut(_Out):
    """Evidence that was read and understood, but stops short of settling the question."""

    # The domain's own set, not a restatement of it: two lists to keep in step is how a
    # published schema drifts from the values the server can actually produce.
    kind: UnprovenKind
    evidence_ids: Annotated[tuple[str, ...], Field(min_length=1)]


type DecisionCauseOut = Annotated[
    ConditionalClaimOut | UnprovenClaimOut, Field(discriminator="kind")
]


# --------------------------------------------------------------------------------------
# relation
# --------------------------------------------------------------------------------------


class ResolvedRelationOut(_Out):
    status: Literal["resolved"] = "resolved"
    rule: RuleName
    direction: Direction
    declaring: TargetOut
    declared_about: TargetOut


class UnsupportedRelationOut(_Out):
    """No rule applied. Carries the input pair only - nothing else is known to be true."""

    status: Literal["unsupported"] = "unsupported"
    subject: TargetOut
    counterpart: TargetOut


type RelationOut = Annotated[
    ResolvedRelationOut | UnsupportedRelationOut, Field(discriminator="status")
]


# --------------------------------------------------------------------------------------
# check_compatibility
# --------------------------------------------------------------------------------------


def _check_evidence_references(
    evidence: tuple[EvidenceOut, ...],
    notices: tuple[NoticeOut, ...],
    verdict_evidence_ids: tuple[str, ...] = (),
    extra_reference_groups: tuple[tuple[str, ...], ...] = (),
) -> None:
    known = {item.id for item in evidence}
    if len(known) != len(evidence):
        raise ValueError("evidence ids must be unique within a response")
    referenced: list[tuple[str, str]] = [
        ("verdict_evidence_ids", identifier) for identifier in verdict_evidence_ids
    ]
    for notice in notices:
        referenced.extend(("notices[].evidence_ids", i) for i in notice.evidence_ids)
    for group in extra_reference_groups:
        referenced.extend(("evidence_ids", identifier) for identifier in group)
    dangling = sorted({f"{where}:{i}" for where, i in referenced if i not in known})
    if dangling:
        raise ValueError(f"evidence references do not resolve: {', '.join(dangling)}")


class _VerdictBase(_Out):
    # Declared here purely to fix its position: 04's first principle is that the caller
    # reads the conclusion before anything else, and pydantic keeps a base field's slot
    # when a subclass narrows it. Each variant replaces this with its own Literal.
    verdict: str

    subject: TargetOut
    counterpart: TargetOut
    relation: RelationOut
    summary: str
    evidence: tuple[EvidenceOut, ...]
    notices: tuple[NoticeOut, ...]
    limitations: tuple[LimitationOut, ...]
    sources_checked: tuple[SourceCheckOut, ...]


class SupportedResult(_VerdictBase):
    verdict: Literal["supported"] = "supported"
    verdict_evidence_ids: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        _check_evidence_references(
            self.evidence, self.notices, self.verdict_evidence_ids
        )
        if not self.sources_checked:
            raise ValueError("a decided verdict must record the lookups it rests on")
        return self


class UnsupportedResult(_VerdictBase):
    verdict: Literal["unsupported"] = "unsupported"
    verdict_evidence_ids: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        _check_evidence_references(
            self.evidence, self.notices, self.verdict_evidence_ids
        )
        if not self.sources_checked:
            raise ValueError("a decided verdict must record the lookups it rests on")
        return self


class UnknownResult(_VerdictBase):
    """Not a weak success and not a failure: nothing available proves either side."""

    verdict: Literal["unknown"] = "unknown"
    reason: Literal[
        "release_not_found",
        "lookup_failed",
        "relation_not_supported",
        "conflicting_evidence",
        "insufficient_evidence",
        "evidence_not_found",
        "no_declared_relationship",
    ]
    decision_causes: tuple[DecisionCauseOut, ...] = ()

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        _check_evidence_references(
            self.evidence,
            self.notices,
            extra_reference_groups=tuple(
                cause.evidence_ids for cause in self.decision_causes
            ),
        )
        if bool(self.decision_causes) != (self.reason == "insufficient_evidence"):
            raise ValueError(
                "decision_causes must be present exactly for insufficient_evidence"
            )
        # An empty lookup list is a claim in itself - "nothing was opened" - and it is only
        # true when no rule applied, because every resolved relation consults the pack even
        # when the registries are unreachable.
        if (not self.sources_checked) != (self.reason == "relation_not_supported"):
            raise ValueError(
                "sources_checked is empty exactly when the relation was not supported"
            )
        return self


type CheckResult = Annotated[
    SupportedResult | UnsupportedResult | UnknownResult, Field(discriminator="verdict")
]


class CheckCompatibilityResult(RootModel[CheckResult]):
    """Root wrapper so the tool's output schema is the three-variant union itself."""


# --------------------------------------------------------------------------------------
# get_compatibility_context
# --------------------------------------------------------------------------------------


class ConstraintOut(_Out):
    """One declared relationship, with the condition under which it applies.

    ``condition`` is structured rather than folded into ``explanation`` for the same reason
    ``decision_causes`` exists: a caller deciding whether this constraint binds its own
    environment should read a field, not parse a sentence.
    """

    relation: Literal["requires", "supports", "excludes"]
    counterpart: TargetIdOut
    version_expression: str
    version_scheme: VersionScheme
    condition: ConditionOut
    explanation: str
    evidence_ids: Annotated[tuple[str, ...], Field(min_length=1)]


class ChangeOut(_Out):
    category: Literal["breaking_change", "removal", "deprecation", "migration_required"]
    area: str
    summary: str
    evidence_ids: Annotated[tuple[str, ...], Field(min_length=1)]


class _ContextBase(_Out):
    # Same reason as `_VerdictBase.verdict`: availability, then depth, then the body.
    availability: str

    depth: Literal["registry_only", "registry_and_curated"]
    target: TargetOut
    summary: str
    constraints: tuple[ConstraintOut, ...]
    changes: tuple[ChangeOut, ...]
    notices: tuple[NoticeOut, ...]
    limitations: tuple[LimitationOut, ...]
    sources_checked: tuple[SourceCheckOut, ...]
    evidence: tuple[EvidenceOut, ...]

    @model_validator(mode="after")
    def _validate_context(self) -> Self:
        _check_evidence_references(
            self.evidence,
            self.notices,
            extra_reference_groups=tuple(
                [constraint.evidence_ids for constraint in self.constraints]
                + [change.evidence_ids for change in self.changes]
            ),
        )
        if not self.sources_checked:
            raise ValueError("a context response must record the lookups it rests on")
        has_curated = any(item.provenance.kind == "curated" for item in self.evidence)
        if has_curated != (self.depth == "registry_and_curated"):
            raise ValueError(
                "depth must be registry_and_curated exactly when curated evidence is present"
            )
        return self


class ContextAvailableResult(_ContextBase):
    availability: Literal["available"] = "available"

    @model_validator(mode="after")
    def _require_material(self) -> Self:
        if not self.constraints and not self.changes:
            raise ValueError("an available context must carry a constraint or a change")
        return self


class ContextUnknownResult(_ContextBase):
    availability: Literal["unknown"] = "unknown"
    reason: Literal["release_not_found", "lookup_failed", "evidence_not_found"]

    @model_validator(mode="after")
    def _require_emptiness(self) -> Self:
        if self.constraints or self.changes:
            raise ValueError("an unknown context must not carry constraints or changes")
        return self


type ContextResult = Annotated[
    ContextAvailableResult | ContextUnknownResult, Field(discriminator="availability")
]


class GetCompatibilityContextResult(RootModel[ContextResult]):
    """Root wrapper so the tool's output schema is the two-variant union itself."""
