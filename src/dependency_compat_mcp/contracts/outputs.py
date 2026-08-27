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
* evidence split into constraint-carrying and narrative types instead of nullable fields.

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
    OtherUnprovenKind,
)
from dependency_compat_mcp.domain.relations import Direction, RuleName
from dependency_compat_mcp.domain.targets import Namespace, VersionScheme

__all__ = [
    "CheckCompatibilityResult",
    "ConditionOut",
    "ConditionalClaimOut",
    "ConstraintOut",
    "ContextAvailableResult",
    "ContextResult",
    "ContextUnknownResult",
    "DecidedMarkerOut",
    "DecisionCauseOut",
    "EolOut",
    "EolPublishedOut",
    "EolUnavailableOut",
    "EolUnpublishedOut",
    "EvidenceOut",
    "FetchedProvenanceOut",
    "GetCompatibilityContextResult",
    "LifecycleOut",
    "LimitationOut",
    "MarkerGuardOut",
    "NarrativeEvidenceOut",
    "NoticeOut",
    "OpenUpperBoundOut",
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
    """When this process retrieved the source. The only provenance a response can carry.

    ``kind`` stays even though nothing competes with it: it names what the timestamp
    means, and a caller reading ``retrieved_at`` should not have to infer that from the
    field's absence of alternatives.
    """

    kind: Literal["fetched"] = "fetched"
    retrieved_at: datetime

    @field_serializer("retrieved_at")
    def _serialise_retrieved_at(self, value: datetime) -> str:
        # 04 spells this as `...Z`; pydantic would otherwise emit `+00:00`.
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class VersionConstraintEvidenceOut(_Out):
    """A source carrying the very expression the verdict was computed from."""

    id: str
    source_type: SourceType
    title: str
    url: str
    substantiates: str
    expression: str
    scheme: VersionScheme
    provenance: FetchedProvenanceOut


class NarrativeEvidenceOut(_Out):
    """A source with no machine-readable range - it does not have the fields at all."""

    id: str
    source_type: SourceType
    title: str
    url: str
    substantiates: str
    provenance: FetchedProvenanceOut


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

    kind: OtherUnprovenKind
    evidence_ids: Annotated[tuple[str, ...], Field(min_length=1)]


class OpenUpperBoundOut(_Out):
    """An open gate whose declaring release predates the release asked about.

    A later declaring release is the next exact-version question the tool can answer. The
    action is part of this variant rather than an optional field on every cause, so an open
    upper bound with no path forward has no public representation.
    """

    kind: Literal["open_upper_bound"] = "open_upper_bound"
    evidence_ids: Annotated[tuple[str, ...], Field(min_length=1)]
    next_actions: tuple[Literal["check_newer_declaring_release"]] = (
        "check_newer_declaring_release",
    )


type DecisionCauseOut = Annotated[
    ConditionalClaimOut | OpenUpperBoundOut | UnprovenClaimOut,
    Field(discriminator="kind"),
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
        # true when no rule applied, because a resolved relation always records a lookup
        # for each side, even one that failed.
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


class EolPublishedOut(_Out):
    """Upstream has announced a day-precision end of life for this release line."""

    status: Literal["published"] = "published"
    # `date`, not `datetime`: the schedules publish a day, which the adapter widens to
    # midnight UTC only so it has one comparable type internally. Narrowing back here
    # keeps the advertised schema (`format: date`) and the emitted value the same claim -
    # a `datetime` would publish a time of day upstream never announced, and a serialiser
    # that printed only the day would leave the schema saying otherwise.
    at: date


class EolUnpublishedOut(_Out):
    """The schedule was read and announces no end-of-life date for this line.

    It has no date field at all, rather than a null one: "read, nothing announced" is a
    complete answer, and a key spelled ``at: null`` invites a caller to treat it as the
    same missing value :class:`EolUnavailableOut` would have produced.
    """

    status: Literal["unpublished"] = "unpublished"


class EolUnavailableOut(_Out):
    """The schedule could not be read, so nothing about end of life is known.

    ``detail`` is the same stable code the matching ``sources_checked`` row carries, so a
    caller can act on the failure without correlating two lists by hand.
    """

    status: Literal["unavailable"] = "unavailable"
    detail: str


type EolOut = Annotated[
    EolPublishedOut | EolUnpublishedOut | EolUnavailableOut,
    Field(discriminator="status"),
]


class LifecycleOut(_Out):
    """What a runtime's publisher states about one release: when, and until when.

    Present only for a runtime target. A registry release has no support lifecycle, and
    ``null`` says that more honestly than a fourth end-of-life status meaning "not a
    runtime" would - the same reason the domain narrows its four-way status to three here.

    No ``evidence_ids``: unlike a constraint, this is not an expression quoted out of a
    document for the caller to compare, it is the document's own answer. The ``sources_
    checked`` rows for the release index and the support schedule are what it rests on.
    """

    released_at: date
    end_of_life: EolOut


class _ContextBase(_Out):
    # Same reason as `_VerdictBase.verdict`: availability first, then the body.
    availability: str

    target: TargetOut
    summary: str
    constraints: tuple[ConstraintOut, ...]
    # Always present, ``null`` when the target has no lifecycle - the same rule `notices`
    # follows. A key that disappears would make "this is a package" and "this field was
    # forgotten" the same observation on the wire.
    lifecycle: LifecycleOut | None
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
                constraint.evidence_ids for constraint in self.constraints
            ),
        )
        if not self.sources_checked:
            raise ValueError("a context response must record the lookups it rests on")
        return self


class ContextAvailableResult(_ContextBase):
    availability: Literal["available"] = "available"

    @model_validator(mode="after")
    def _require_material(self) -> Self:
        if not self.constraints and self.lifecycle is None:
            raise ValueError(
                "an available context must carry a constraint or a lifecycle"
            )
        return self


class ContextUnknownResult(_ContextBase):
    availability: Literal["unknown"] = "unknown"
    reason: Literal["release_not_found", "lookup_failed", "evidence_not_found"]

    @model_validator(mode="after")
    def _require_emptiness(self) -> Self:
        if self.constraints or self.lifecycle is not None:
            raise ValueError("an unknown context must carry no material")
        return self


type ContextResult = Annotated[
    ContextAvailableResult | ContextUnknownResult, Field(discriminator="availability")
]


class GetCompatibilityContextResult(RootModel[ContextResult]):
    """Root wrapper so the tool's output schema is the two-variant union itself."""
