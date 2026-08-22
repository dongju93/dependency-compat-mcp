"""Application service: the only place where I/O meets the pure decision procedure.

The pipeline of 03 runs here, in its documented order and with its documented purity
boundary::

    parse (02)  ->  resolve relation  ->  collect  ->  normalise  ->  evaluate  ->  assemble
       pure            pure               I/O          parsing        pure         pure

Three properties are the reason this module exists at all rather than being folded into
the MCP layer:

* **An unsupported relation costs nothing.** When no rule applies, the request ends before
  a single socket is opened, and the response says so.
* **Lookups are structured.** Both sides are fetched inside one ``asyncio.TaskGroup``, so a
  failure cancels its sibling and no task outlives the call.
* **What the server says it checked is what the verdict was computed from.** The same
  ``SourceCheck`` values are handed to ``evaluate`` and serialised as ``sources_checked``,
  one row per lookup rather than one per source. An earlier version merged rows by source
  and kept the worst outcome; on a ``pypi x pypi`` comparison that turned two lookups of
  ``pypi_json`` into a single row from which the caller could not tell which release had
  actually been confirmed to exist.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, assert_never

from dependency_compat_mcp.adapters.npm import NpmAdapter
from dependency_compat_mcp.adapters.protocol import (
    LookupFailed,
    ReleaseDocument,
    ReleaseLookup,
    ReleaseNotFound,
    select_claims,
)
from dependency_compat_mcp.adapters.pypi import PyPIAdapter
from dependency_compat_mcp.adapters.runtimes import (
    RuntimeReleaseAdapter,
    RuntimeReleaseFound,
    RuntimeReleaseMissing,
)
from dependency_compat_mcp.contracts.assembly import (
    build_check_result,
    build_context_result,
)
from dependency_compat_mcp.contracts.outputs import (
    CheckCompatibilityResult,
    GetCompatibilityContextResult,
)
from dependency_compat_mcp.curated.loader import (
    CuratedPack,
    PackEntry,
    change_evidence,
    statement_claims,
)
from dependency_compat_mcp.domain.claims import (
    Claim,
    CompatibilityStatement,
    Corroboration,
    Evidence,
    EvidenceId,
    InstallationGate,
    LookupRole,
    ReleaseFacts,
    SourceCheck,
    YankedInfo,
    claim_evidence_id,
)
from dependency_compat_mcp.domain.context import (
    ContextChange,
    ContextConstraint,
    ContextInput,
    build_context,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.evaluate import (
    CuratedCoverage,
    EvaluationInput,
    Unknown,
    evaluate,
)
from dependency_compat_mcp.domain.relations import (
    ResolvedRelation,
    UnsupportedRelation,
    resolve_relation,
)
from dependency_compat_mcp.domain.summaries import summarise_context, summarise_verdict
from dependency_compat_mcp.domain.targets import (
    NodeRuntimeTarget,
    NpmTarget,
    PyPITarget,
    PythonRuntimeTarget,
    Target,
    TargetId,
    name_of,
    namespace_of,
    version_of,
)
from dependency_compat_mcp.infra.cache import TtlCache
from dependency_compat_mcp.infra.http import DEFAULT_REQUEST_BUDGET, HttpxJsonFetcher

__all__ = ["DEFAULT_CACHE_TTL_SECONDS", "CompatibilityService"]

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_SECONDS: Final = 900.0

type CacheKey = tuple[str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class _Collected:
    """Everything one side of a comparison contributed."""

    document: ReleaseDocument | None
    released_at: datetime | None
    eol_at: datetime | None
    yanked: YankedInfo | None
    found: bool
    checks: tuple[SourceCheck, ...]


@dataclass
class CompatibilityService:
    """Owns the adapters, the curated pack and the cache for the process' lifetime.

    Nothing here is bound to a connection or a session: 01 requires each request to be
    self-contained, so shared state is owned by this application object with an explicit
    key and its own lifetime.
    """

    pypi: PyPIAdapter
    npm: NpmAdapter
    runtimes: RuntimeReleaseAdapter
    pack: CuratedPack
    fetcher: HttpxJsonFetcher | None = None
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    request_budget_seconds: float = DEFAULT_REQUEST_BUDGET
    _cache: TtlCache[CacheKey, ReleaseLookup] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.request_budget_seconds <= 0:
            raise ValueError("request_budget_seconds must be positive.")
        self._cache = TtlCache(ttl_seconds=self.cache_ttl_seconds)

    # ----------------------------------------------------------------------------------
    # check_compatibility
    # ----------------------------------------------------------------------------------

    async def check_compatibility(
        self, subject: Target, counterpart: Target
    ) -> CheckCompatibilityResult:
        """Answer the directed question of 02, or say honestly that it cannot be answered."""
        resolution = resolve_relation(subject, counterpart)
        match resolution:
            case UnsupportedRelation():
                return self._relation_not_supported(subject, counterpart, resolution)
            case ResolvedRelation():
                return await self._check_resolved(resolution)
            case _:
                assert_never(resolution)

    def _relation_not_supported(
        self,
        subject: Target,
        counterpart: Target,
        resolution: UnsupportedRelation,
    ) -> CheckCompatibilityResult:
        """End the request without any lookup (03 [2]).

        ``sources_checked`` comes back empty, and that emptiness is the record. A previous
        version emitted one ``skipped`` row per known source id; once a check names the
        target it was made for, those rows would have had to name a target no lookup was
        ever planned for, inventing the very fact the list exists to report. Emptiness is
        unambiguous instead: ``UnknownResult`` refuses to be built with an empty
        ``sources_checked`` under any other reason.
        """
        # No limitations: nothing was left unverified, because nothing needed verifying.
        verdict = Unknown(reason="relation_not_supported", notices=(), limitations=())
        return build_check_result(
            subject=subject,
            counterpart=counterpart,
            resolution=resolution,
            verdict=verdict,
            summary=summarise_verdict(verdict=verdict, resolution=resolution),
            evidence=(),
            referenced_evidence_ids=(),
            sources=(),
        )

    async def _check_resolved(
        self, relation: ResolvedRelation
    ) -> CheckCompatibilityResult:
        declaring_side, declared_side = await self._collect_both(
            relation.declaring, relation.declared_about
        )

        declared_about_id = TargetId.of(relation.declared_about)
        registry_claims: tuple[Claim, ...] = ()
        evidence: list[Evidence] = []
        if declaring_side.document is not None:
            registry_claims = select_claims(declaring_side.document, declared_about_id)
            evidence.extend(declaring_side.document.evidence)

        entry = self.pack.lookup(relation.declaring)
        curated_claims, curated_evidence = self._curated_claims(
            entry, declared_about_id
        )
        evidence.extend(curated_evidence)

        checks = (
            *declaring_side.checks,
            *declared_side.checks,
            self._pack_check(entry, relation.declaring),
        )

        facts = ReleaseFacts(
            declaring_released_at=declaring_side.released_at,
            declared_about_released_at=declared_side.released_at,
            declared_about_eol_at=declared_side.eol_at,
            declaring_yanked=declaring_side.yanked,
            declared_about_yanked=declared_side.yanked,
        )
        claims = (*registry_claims, *curated_claims)
        verdict = evaluate(
            EvaluationInput(
                relation=relation,
                claims=claims,
                facts=facts,
                lookups=checks,
                curated=self._coverage(entry, relation.declaring),
                declaring_release_found=declaring_side.found,
                declared_about_release_found=declared_side.found,
            )
        )
        referenced: set[EvidenceId] = {claim_evidence_id(claim) for claim in claims}
        return build_check_result(
            subject=relation.subject,
            counterpart=relation.counterpart,
            resolution=relation,
            verdict=verdict,
            summary=summarise_verdict(verdict=verdict, resolution=relation),
            evidence=evidence,
            referenced_evidence_ids=referenced,
            sources=checks,
        )

    # ----------------------------------------------------------------------------------
    # get_compatibility_context
    # ----------------------------------------------------------------------------------

    async def get_compatibility_context(
        self, target: Target
    ) -> GetCompatibilityContextResult:
        """Return comparison material for one release. This tool never judges."""
        side = await self._collect_one_with_budget(target, role="declaring")
        entry = self.pack.lookup(target)
        checks = (*side.checks, self._pack_check(entry, target))

        evidence: list[Evidence] = []
        constraints: list[ContextConstraint] = []
        changes: list[ContextChange] = []
        marker_guarded = False
        extra_guarded = False

        if side.document is not None:
            evidence.extend(side.document.evidence)
            for claim in side.document.claims:
                constraint, guards = _claim_to_constraint(claim)
                marker_guarded = marker_guarded or guards[0]
                extra_guarded = extra_guarded or guards[1]
                if constraint is not None:
                    constraints.append(constraint)

        if entry is not None:
            curated_claims, curated_evidence = self._curated_claims(entry, None)
            evidence.extend(curated_evidence)
            for claim in curated_claims:
                constraint, _ = _claim_to_constraint(claim)
                if constraint is not None:
                    constraints.append(constraint)
            change_items, change_evidence = self._curated_changes(entry)
            evidence.extend(change_evidence)
            changes.extend(change_items)

        outcome = build_context(
            ContextInput(
                target=target,
                release_found=side.found,
                constraints=tuple(constraints),
                changes=tuple(changes),
                evidence=tuple(evidence),
                lookups=checks,
                curated=self._coverage(entry, target),
                marker_guarded=marker_guarded,
                extra_guarded=extra_guarded,
            )
        )
        return build_context_result(
            target=target,
            outcome=outcome,
            summary=summarise_context(target=target, outcome=outcome),
            evidence=evidence,
            sources=checks,
        )

    # ----------------------------------------------------------------------------------
    # Collection
    # ----------------------------------------------------------------------------------

    async def _collect_both(
        self, declaring: Target, declared_about: Target
    ) -> tuple[_Collected, _Collected]:
        """Fetch both sides concurrently under one scope.

        ``TaskGroup`` binds the children's lifetime to this call: if one raises, its sibling
        is cancelled, cleanup is awaited, and the error leaves this scope - it cannot be
        left running past the response.
        """
        declaring_task: asyncio.Task[_Collected] | None = None
        declared_task: asyncio.Task[_Collected] | None = None
        try:
            async with asyncio.timeout(self.request_budget_seconds):
                async with asyncio.TaskGroup() as group:
                    declaring_task = group.create_task(
                        self._collect_one(declaring, role="declaring")
                    )
                    declared_task = group.create_task(
                        self._collect_one(declared_about, role="declared_about")
                    )
        except TimeoutError:
            return (
                self._completed_or_timeout(declaring_task, declaring, "declaring"),
                self._completed_or_timeout(
                    declared_task, declared_about, "declared_about"
                ),
            )
        if declaring_task is None or declared_task is None:  # pragma: no cover
            raise InvariantViolation("collection tasks were not created")
        return declaring_task.result(), declared_task.result()

    async def _collect_one_with_budget(
        self, target: Target, *, role: LookupRole
    ) -> _Collected:
        """Collect one target without letting it outlive the request budget."""
        try:
            async with asyncio.timeout(self.request_budget_seconds):
                return await self._collect_one(target, role=role)
        except TimeoutError:
            return self._timed_out(target, role)

    def _completed_or_timeout(
        self, task: asyncio.Task[_Collected] | None, target: Target, role: LookupRole
    ) -> _Collected:
        if task is not None and task.done() and not task.cancelled():
            return task.result()
        return self._timed_out(target, role)

    def _timed_out(self, target: Target, role: LookupRole) -> _Collected:
        """Represent an exhausted call-level budget as a required lookup failure."""
        match target:
            case PyPITarget() | NpmTarget():
                source = self._adapter_for(target).source_id
            case PythonRuntimeTarget() | NodeRuntimeTarget():
                source = self.runtimes.source_id_for(target)
            case _:
                assert_never(target)
        return _Collected(
            document=None,
            released_at=None,
            eol_at=None,
            yanked=None,
            found=False,
            checks=(
                SourceCheck(
                    source=source,
                    target=target,
                    role=role,
                    outcome="failed",
                    required=True,
                    detail="timeout",
                ),
            ),
        )

    async def _collect_one(self, target: Target, *, role: LookupRole) -> _Collected:
        match target:
            case PythonRuntimeTarget() | NodeRuntimeTarget():
                return self._collect_runtime(target, role=role)
            case PyPITarget() | NpmTarget():
                return await self._collect_registry(target, role=role)
            case _:
                assert_never(target)

    def _collect_runtime(self, target: Target, *, role: LookupRole) -> _Collected:
        source = self.runtimes.source_id_for(target)
        found = self.runtimes.fetch_release(target)
        match found:
            case RuntimeReleaseFound(released_at=released_at, eol_at=eol_at):
                return _Collected(
                    document=None,
                    released_at=released_at,
                    eol_at=eol_at,
                    yanked=None,
                    found=True,
                    checks=(
                        SourceCheck(
                            source=source, target=target, role=role, outcome="ok"
                        ),
                    ),
                )
            case RuntimeReleaseMissing():
                return _Collected(
                    document=None,
                    released_at=None,
                    eol_at=None,
                    yanked=None,
                    found=False,
                    checks=(
                        SourceCheck(
                            source=source, target=target, role=role, outcome="not_found"
                        ),
                    ),
                )
            case _:
                assert_never(found)

    async def _collect_registry(
        self, target: Target, *, role: LookupRole
    ) -> _Collected:
        adapter = self._adapter_for(target)
        key: CacheKey = (
            adapter.source_id,
            namespace_of(target),
            str(name_of(target)),
            str(version_of(target)),
            # A pack change really does change the evidence, so it invalidates the cache.
            self.pack.pack_version,
        )
        cached = self._cache.get(key)
        lookup = cached if cached is not None else await adapter.fetch_release(target)
        if cached is None:
            self._cache.set(key, lookup)

        match lookup:
            case ReleaseDocument():
                return _Collected(
                    document=lookup,
                    released_at=lookup.released_at,
                    eol_at=None,
                    yanked=lookup.yanked,
                    found=True,
                    checks=(
                        SourceCheck(
                            source=adapter.source_id,
                            target=target,
                            role=role,
                            outcome="ok",
                        ),
                    ),
                )
            case ReleaseNotFound():
                return _Collected(
                    document=None,
                    released_at=None,
                    eol_at=None,
                    yanked=None,
                    found=False,
                    checks=(
                        SourceCheck(
                            source=adapter.source_id,
                            target=target,
                            role=role,
                            outcome="not_found",
                        ),
                    ),
                )
            case LookupFailed(detail=detail):
                return _Collected(
                    document=None,
                    released_at=None,
                    eol_at=None,
                    yanked=None,
                    found=False,
                    checks=(
                        SourceCheck(
                            source=adapter.source_id,
                            target=target,
                            role=role,
                            outcome="failed",
                            # Both sides are required. The declaring side carries the
                            # declaration; the counterpart carries the premise that the
                            # exact release asked about exists at all, and step 5 reads
                            # its publication date. Marking the counterpart optional -
                            # as this once did - let a failed lookup fall through to
                            # `release_not_found`, which asserts as fact that the release
                            # does not exist when the server never got an answer.
                            required=True,
                            detail=detail,
                        ),
                    ),
                )
            case _:
                assert_never(lookup)

    def _adapter_for(self, target: Target) -> PyPIAdapter | NpmAdapter:
        match target:
            case PyPITarget():
                return self.pypi
            case NpmTarget():
                return self.npm
            case _:
                # Runtime targets never reach a registry adapter; the caller dispatches first.
                raise TypeError(f"no registry adapter for {target!r}")

    # ----------------------------------------------------------------------------------
    # Curated pack
    # ----------------------------------------------------------------------------------

    def _curated_claims(
        self, entry: PackEntry | None, declared_about: TargetId | None
    ) -> tuple[tuple[Claim, ...], tuple[Evidence, ...]]:
        if entry is None:
            return (), ()
        claims, evidence = statement_claims(entry, self.pack.pack_version)
        if declared_about is not None:
            claims = tuple(
                claim for claim in claims if claim.declared_about == declared_about
            )
        return claims, evidence

    def _curated_changes(
        self, entry: PackEntry
    ) -> tuple[tuple[ContextChange, ...], tuple[Evidence, ...]]:
        evidence = change_evidence(entry, self.pack.pack_version)
        changes = tuple(
            ContextChange(
                category=change.category,
                area=change.area,
                summary=change.summary,
                evidence_ids=(item.id,),
            )
            for change, item in zip(entry.changes, evidence, strict=True)
        )
        return changes, evidence

    def _coverage(self, entry: PackEntry | None, target: Target) -> CuratedCoverage:
        if entry is None:
            return CuratedCoverage(entry_present=False, verified_for_version=False)
        return CuratedCoverage(
            entry_present=True,
            verified_for_version=self.pack.verified_for(entry, target),
        )

    def _pack_check(self, entry: PackEntry | None, target: Target) -> SourceCheck:
        # The pack is always consulted, and "consulted, nothing there" is a real answer -
        # it is what separates `curated_pack_missing` from a failed lookup. It is looked up
        # for the declaring target only: the pack records what a release states about
        # others, so there is nothing to ask it about the counterpart.
        return SourceCheck(
            source="curated_pack",
            target=target,
            role="declaring",
            outcome="ok" if entry is not None else "not_found",
            required=False,
        )

    # ----------------------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------------------

    async def aclose(self) -> None:
        """Release the shared HTTP client, when this service owns one.

        The adapters are stateless views over a fetcher, so lifetime belongs to whoever
        created the fetcher. `cli.build_service` passes it in; a test wiring a fake fetcher
        passes nothing and this is a no-op.
        """
        if self.fetcher is not None:
            await self.fetcher.aclose()


def _claim_to_constraint(
    claim: Claim,
) -> tuple[ContextConstraint | None, tuple[bool, bool]]:
    """Project one claim onto a context constraint.

    Returns the constraint plus ``(marker_guarded, extra_guarded)``, because 03 wants those
    two facts recorded as limitations even when the guarded claim still produces material.
    """
    match claim:
        case InstallationGate(condition=condition):
            marker_guarded = (
                condition is not None
                and condition.decidability == "environment_dependent"
            )
            extra_guarded = (
                condition is not None and condition.decidability == "extra_guarded"
            )
            explanation = "Declared as a required constraint by the release metadata."
            if condition is not None:
                explanation += f" Conditional on: {condition.expression}"
            return (
                ContextConstraint(
                    relation="requires",
                    counterpart=claim.declared_about,
                    version_expression=claim.expression,
                    version_scheme=claim.scheme,
                    condition=condition,
                    explanation=explanation,
                    evidence_ids=(claim.evidence_id,),
                ),
                (marker_guarded, extra_guarded),
            )
        case CompatibilityStatement(stance=stance):
            return (
                ContextConstraint(
                    relation="supports" if stance == "supports" else "excludes",
                    counterpart=claim.declared_about,
                    version_expression=claim.expression,
                    version_scheme=claim.scheme,
                    # Curated statements carry no PEP 508 marker; the reviewer records the
                    # range that applies, not one guarded by an environment.
                    condition=None,
                    explanation=(
                        "Declared as a supported range."
                        if stance == "supports"
                        else "Declared as an excluded range."
                    ),
                    evidence_ids=(claim.evidence_id,),
                ),
                (False, False),
            )
        case Corroboration():
            # A tier-C enumeration is not a constraint: it carries no range, and 03 forbids
            # it from standing on its own. It stays out of `constraints` by construction.
            return None, (False, False)
        case _:
            assert_never(claim)
