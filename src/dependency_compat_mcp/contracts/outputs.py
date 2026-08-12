"""The public tool output models (04).

Every invariant 04 lists is enforced here by construction or by an always-on validator,
because these models are the last place a malformed response can be caught before it
reaches a caller:

* three verdict variants as separate types behind a ``verdict`` discriminator, so
  ``reason`` cannot coexist with ``verdict_evidence_ids``;
* ``verdict_evidence_ids`` non-empty and resolvable inside the same response;
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

from dependency_compat_mcp.domain.claims import SourceId, SourceType
from dependency_compat_mcp.domain.diagnostics import LimitationCode, NoticeCode
from dependency_compat_mcp.domain.relations import Direction, RuleName
from dependency_compat_mcp.domain.targets import Namespace, VersionScheme

__all__ = [
    "ChangeOut",
    "CheckCompatibilityResult",
    "ConstraintOut",
    "ContextAvailableResult",
    "ContextResult",
    "ContextUnknownResult",
    "CuratedProvenanceOut",
    "EvidenceOut",
    "FetchedProvenanceOut",
    "GetCompatibilityContextResult",
    "LimitationOut",
    "NarrativeEvidenceOut",
    "NoticeOut",
    "RelationOut",
    "ResolvedRelationOut",
    "SourceCheckOut",
    "SupportedResult",
    "TargetIdOut",
    "TargetOut",
    "UnknownResult",
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
    source: SourceId
    outcome: Literal["ok", "not_found", "failed", "skipped"]
    detail: str | None = None


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
        return self


class UnsupportedResult(_VerdictBase):
    verdict: Literal["unsupported"] = "unsupported"
    verdict_evidence_ids: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        _check_evidence_references(
            self.evidence, self.notices, self.verdict_evidence_ids
        )
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

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        _check_evidence_references(self.evidence, self.notices)
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
    relation: Literal["requires", "supports", "excludes"]
    counterpart: TargetIdOut
    version_expression: str
    version_scheme: VersionScheme
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
