"""What every registry adapter returns, and the three ways a lookup can end.

03 [3] and [4] draw the line this module encodes: an adapter finishes parsing, so nothing
downstream ever sees a ``dict`` or an HTTP response, and a lookup failure arrives as a
*value* so that the decision procedure has to handle it explicitly rather than inherit it
from a caught exception.

:class:`ReleaseDocument` carries claims and evidence together because the two are only
meaningful as a pair: every ``Claim.evidence_id`` must resolve inside the same document.
:func:`evidence_index` is how a caller checks that, and it is why the referential
integrity check in 03 [6] can be a lookup rather than a search.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, assert_never

from dependency_compat_mcp.domain.claims import (
    Claim,
    CompatibilityStatement,
    Corroboration,
    Evidence,
    EvidenceId,
    InstallationGate,
    SourceId,
    YankedInfo,
)
from dependency_compat_mcp.domain.targets import Namespace, Target, TargetId

__all__ = [
    "LookupFailed",
    "RegistryAdapter",
    "ReleaseDocument",
    "ReleaseLookup",
    "ReleaseNotFound",
    "claim_declared_about",
    "evidence_index",
    "select_claims",
]


@dataclass(frozen=True, slots=True)
class ReleaseDocument:
    """One exact release, fully parsed.

    ``claims`` holds *every* claim the release declares, not only those about some
    counterpart, because the same document also feeds ``get_compatibility_context``, which
    reports all declared constraints. Filtering to a counterpart is
    :func:`select_claims`'s job and stays a pure function.
    """

    target: Target
    released_at: datetime | None
    yanked: YankedInfo | None
    claims: tuple[Claim, ...]
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True, slots=True)
class ReleaseNotFound:
    """The registry answered, and this exact release does not exist.

    Distinct from :class:`LookupFailed` because 03 step 0 checks failure *before* absence:
    reporting a failed lookup as absence would turn "we could not look" into "it is not
    there".
    """

    target: Target


@dataclass(frozen=True, slots=True)
class LookupFailed:
    """The registry could not be consulted. ``detail`` is a stable code, never prose.

    The codes are the ones :mod:`dependency_compat_mcp.infra.http` produces, plus
    ``invalid_document`` (a 2xx body that is not the shape the registry documents) and
    ``unsupported_namespace`` (an adapter was handed a target it does not serve).
    """

    target: Target
    detail: str


type ReleaseLookup = ReleaseDocument | ReleaseNotFound | LookupFailed


class RegistryAdapter(Protocol):
    """The single shape every registry source satisfies.

    ``source_id`` is here so the caller can build a ``SourceCheck`` from the adapter that
    produced the result. If the adapter did not carry it, the value reported in
    ``sources_checked`` would be re-derived at the call site and could drift from the
    source actually consulted - exactly what 03 [3] forbids.
    """

    namespace: Namespace
    source_id: SourceId

    async def fetch_release(self, target: Target) -> ReleaseLookup: ...


def evidence_index(document: ReleaseDocument) -> dict[EvidenceId, Evidence]:
    """Index a document's evidence by id, for referential-integrity checks and assembly.

    Adapters guarantee ids are unique within a document, so this never loses an entry;
    a shrinking index would mean an adapter broke that guarantee.
    """
    return {item.id: item for item in document.evidence}


def claim_declared_about(claim: Claim) -> TargetId:
    """The identity a claim speaks about, across all three tiers."""
    match claim:
        case InstallationGate(declared_about=declared_about):
            return declared_about
        case CompatibilityStatement(declared_about=declared_about):
            return declared_about
        case Corroboration(declared_about=declared_about):
            return declared_about
        case _:  # pragma: no cover - exhaustive over the Claim sum type
            assert_never(claim)


def select_claims(
    document: ReleaseDocument, declared_about: TargetId
) -> tuple[Claim, ...]:
    """Every claim in ``document`` that is about ``declared_about``, in document order.

    Pure, and order-preserving: 03 [6] requires the same inputs to serialise to the same
    bytes, so selection must not reorder what the adapter produced.
    """
    return tuple(
        claim
        for claim in document.claims
        if claim_declared_about(claim) == declared_about
    )
