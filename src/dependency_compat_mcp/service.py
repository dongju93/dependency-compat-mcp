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
from collections.abc import Awaitable, Callable
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
    RuntimeIndexLookup,
    RuntimeLifecycleLookup,
    RuntimeReleaseAbsent,
    RuntimeReleaseAdapter,
    RuntimeReleaseFound,
    RuntimeReleaseUnavailable,
    RuntimeSourceFailed,
    index_check,
    index_source_id,
    lifecycle_check,
    lifecycle_source_id,
    select_eol,
    select_release,
)
from dependency_compat_mcp.contracts.assembly import (
    build_check_result,
    build_context_result,
)
from dependency_compat_mcp.contracts.outputs import (
    CheckCompatibilityResult,
    GetCompatibilityContextResult,
)
from dependency_compat_mcp.domain.claims import (
    Claim,
    CompatibilityStatement,
    Corroboration,
    EolNotApplicable,
    EolStatus,
    EolUnavailable,
    Evidence,
    EvidenceId,
    InstallationGate,
    LookupRole,
    ReleaseFacts,
    SourceCheck,
    SourceId,
    YankedInfo,
    claim_evidence_id,
)
from dependency_compat_mcp.domain.context import (
    ContextConstraint,
    ContextInput,
    build_context,
)
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.evaluate import (
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

# One registry document per exact release; one runtime document per official source. The
# runtime documents answer every version, so they are keyed by source alone - a per-version
# key would re-fetch the same 300 KB index for each release asked about.
type ReleaseCacheKey = tuple[SourceId, str, str, str]
# Two release indexes and two support schedules; nothing else can enter these caches.
_RUNTIME_CACHE_ENTRIES: Final = 2


@dataclass(frozen=True, slots=True)
class _Collected:
    """Everything one side of a comparison contributed.

    ``eol`` is a four-way status rather than an optional date: a registry package has no
    support lifecycle, a runtime line may have none published, and the schedule may simply
    not have been readable. Only the last of those may block a decided verdict, so the
    three must not share a representation.
    """

    document: ReleaseDocument | None
    released_at: datetime | None
    eol: EolStatus
    yanked: YankedInfo | None
    found: bool
    checks: tuple[SourceCheck, ...]


@dataclass
class CompatibilityService:
    """Owns the adapters and the caches for the process' lifetime.

    Nothing here is bound to a connection or a session: 01 requires each request to be
    self-contained, so shared state is owned by this application object with an explicit
    key and its own lifetime.

    The server holds no compatibility facts of its own. Everything it answers with is
    fetched from an official source for the request that needed it, so the only state
    between requests is a bounded, time-limited cache of those documents.
    """

    pypi: PyPIAdapter
    npm: NpmAdapter
    runtimes: RuntimeReleaseAdapter
    fetcher: HttpxJsonFetcher | None = None
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    request_budget_seconds: float = DEFAULT_REQUEST_BUDGET
    _cache: TtlCache[ReleaseCacheKey, ReleaseLookup] = field(init=False, repr=False)
    _index_cache: TtlCache[SourceId, RuntimeIndexLookup] = field(init=False, repr=False)
    _lifecycle_cache: TtlCache[SourceId, RuntimeLifecycleLookup] = field(
        init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.request_budget_seconds <= 0:
            raise ValueError("request_budget_seconds must be positive.")
        self._cache = TtlCache(ttl_seconds=self.cache_ttl_seconds)
        self._index_cache = TtlCache(
            ttl_seconds=self.cache_ttl_seconds, max_entries=_RUNTIME_CACHE_ENTRIES
        )
        self._lifecycle_cache = TtlCache(
            ttl_seconds=self.cache_ttl_seconds, max_entries=_RUNTIME_CACHE_ENTRIES
        )

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
        claims: tuple[Claim, ...] = ()
        evidence: list[Evidence] = []
        if declaring_side.document is not None:
            claims = select_claims(declaring_side.document, declared_about_id)
            evidence.extend(declaring_side.document.evidence)

        checks = (*declaring_side.checks, *declared_side.checks)

        facts = ReleaseFacts(
            declaring_released_at=declaring_side.released_at,
            declared_about_released_at=declared_side.released_at,
            declared_about_eol=declared_side.eol,
            declaring_yanked=declaring_side.yanked,
            declared_about_yanked=declared_side.yanked,
        )
        verdict = evaluate(
            EvaluationInput(
                relation=relation,
                claims=claims,
                facts=facts,
                lookups=checks,
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
        checks = side.checks

        evidence: list[Evidence] = []
        constraints: list[ContextConstraint] = []
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

        outcome = build_context(
            ContextInput(
                target=target,
                release_found=side.found,
                constraints=tuple(constraints),
                evidence=tuple(evidence),
                lookups=checks,
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
        """Represent an exhausted call-level budget as a required lookup failure.

        Only the required source is reported. Which of a runtime's two documents was still
        in flight when the budget ran out is not knowable here, and the optional one is not
        what decided the outcome: one failed required lookup already means
        ``lookup_failed``, and naming a second source the server cannot prove it opened
        would put an invented row in ``sources_checked``.
        """
        match target:
            case PyPITarget() | NpmTarget():
                source = self._adapter_for(target).source_id
            case PythonRuntimeTarget() | NodeRuntimeTarget():
                source = index_source_id(target)
            case _:
                assert_never(target)
        return _Collected(
            document=None,
            released_at=None,
            eol=EolUnavailable(detail="timeout"),
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
                return await self._collect_runtime(target, role=role)
            case PyPITarget() | NpmTarget():
                return await self._collect_registry(target, role=role)
            case _:
                assert_never(target)

    async def _collect_runtime(self, target: Target, *, role: LookupRole) -> _Collected:
        """Read a runtime release from its official index, and its line's end of life.

        The support schedule is only consulted for the side the declaration is *about*:
        the end-of-life fact exists to bound an open-ended gate declared by the other
        side, and ``get_compatibility_context`` reports declarations rather than judging
        them. Fetching it anywhere else would put a row in ``sources_checked`` for a
        document nothing in the response rests on.
        """
        if role == "declared_about":
            # Structured: both documents are fetched under one scope, and a failure in one
            # cancels the other rather than leaving it running past the response.
            async with asyncio.TaskGroup() as group:
                index_task = group.create_task(self._runtime_index(target))
                lifecycle_task = group.create_task(self._runtime_lifecycle(target))
            index = index_task.result()
            lifecycle: RuntimeLifecycleLookup | None = lifecycle_task.result()
        else:
            index = await self._runtime_index(target)
            lifecycle = None

        release = select_release(index, target)
        checks = [index_check(target, release, role=role)]
        if lifecycle is not None:
            checks.append(lifecycle_check(target, lifecycle, role=role))

        match release:
            case RuntimeReleaseFound(released_at=released_at):
                released, found = released_at, True
            case RuntimeReleaseAbsent() | RuntimeReleaseUnavailable():
                released, found = None, False
            case _:
                assert_never(release)

        return _Collected(
            document=None,
            released_at=released,
            eol=select_eol(lifecycle, target),
            yanked=None,
            found=found,
            checks=tuple(checks),
        )

    async def _runtime_index(self, target: Target) -> RuntimeIndexLookup:
        return await self._cached_document(
            self._index_cache,
            index_source_id(target),
            lambda: self.runtimes.fetch_index(target),
        )

    async def _runtime_lifecycle(self, target: Target) -> RuntimeLifecycleLookup:
        return await self._cached_document(
            self._lifecycle_cache,
            lifecycle_source_id(target),
            lambda: self.runtimes.fetch_lifecycle(target),
        )

    @staticmethod
    async def _cached_document[T](
        cache: TtlCache[SourceId, T],
        key: SourceId,
        fetch: Callable[[], Awaitable[T]],
    ) -> T:
        """Return the cached official document for ``key``, fetching it when absent.

        Only successes are cached. A runtime document is keyed by source rather than by
        release, so caching a failure would replay one bad minute to *every* runtime
        question for the whole TTL - unlike a registry entry, whose key confines a cached
        failure to the one release that failed.

        ``fetch`` is a factory rather than an awaitable so that a cache hit costs nothing:
        the request is never even constructed.
        """
        cached = cache.get(key)
        if cached is not None:
            return cached
        document = await fetch()
        if not isinstance(document, RuntimeSourceFailed):
            cache.set(key, document)
        return document

    async def _collect_registry(
        self, target: Target, *, role: LookupRole
    ) -> _Collected:
        adapter = self._adapter_for(target)
        key: ReleaseCacheKey = (
            adapter.source_id,
            namespace_of(target),
            str(name_of(target)),
            str(version_of(target)),
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
                    eol=EolNotApplicable(),
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
                    eol=EolNotApplicable(),
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
                    eol=EolNotApplicable(),
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
                    # A support statement carries no PEP 508 marker: `engines.node` is a
                    # bare range, not one guarded by an environment.
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
