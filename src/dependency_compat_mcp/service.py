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
    EolPublished,
    EolStatus,
    EolUnavailable,
    EolUnpublished,
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
    ReleaseLifecycle,
    RuntimeEol,
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


@dataclass(slots=True)
class _RuntimeProgress:
    """The runtime release index, held from the moment it answers until the call ends.

    A runtime side reads two documents concurrently and only one of them is required. If
    the call-level budget expires while the *optional* schedule is still in flight, the
    index has already answered and its answer is already a fact the request paid for.
    Letting it die with the cancelled task would report the *required* lookup as ``failed``
    and produce ``unknown / lookup_failed`` about a release whose existence and publication
    date the server had in fact confirmed - the one outcome the optional/required split
    exists to prevent.

    A mutable cell rather than a return value, because after cancellation there is no
    return value left to read. Written only where a lifecycle lookup was actually opened,
    so a set ``index`` *means* "the required document answered and only the optional
    schedule was outstanding" - the timeout path cannot then name a source that was never
    opened.
    """

    index: RuntimeIndexLookup | None = None


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
        """Return comparison material for one release. This tool never judges.

        A runtime target reaches this with nothing to declare - the release indexes carry
        no version constraints - so the material it contributes is the pair of facts its
        publisher does state, assembled by :func:`_lifecycle_of`. That is why the support
        schedule is fetched here as well as on the verdict path.
        """
        side = await self._collect_one_with_budget(
            target, role="declaring", with_lifecycle=True
        )
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
                lifecycle=_lifecycle_of(target, side),
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
        declaring_progress = _RuntimeProgress()
        declared_progress = _RuntimeProgress()
        try:
            async with asyncio.timeout(self.request_budget_seconds):
                async with asyncio.TaskGroup() as group:
                    declaring_task = group.create_task(
                        self._collect_one(
                            declaring,
                            role="declaring",
                            with_lifecycle=False,
                            progress=declaring_progress,
                        )
                    )
                    declared_task = group.create_task(
                        self._collect_one(
                            declared_about,
                            role="declared_about",
                            with_lifecycle=True,
                            progress=declared_progress,
                        )
                    )
        except TimeoutError:
            return (
                self._completed_or_timeout(
                    declaring_task, declaring, "declaring", declaring_progress
                ),
                self._completed_or_timeout(
                    declared_task, declared_about, "declared_about", declared_progress
                ),
            )
        if declaring_task is None or declared_task is None:  # pragma: no cover
            raise InvariantViolation("collection tasks were not created")
        return declaring_task.result(), declared_task.result()

    async def _collect_one_with_budget(
        self, target: Target, *, role: LookupRole, with_lifecycle: bool
    ) -> _Collected:
        """Collect one target without letting it outlive the request budget."""
        progress = _RuntimeProgress()
        try:
            async with asyncio.timeout(self.request_budget_seconds):
                return await self._collect_one(
                    target,
                    role=role,
                    with_lifecycle=with_lifecycle,
                    progress=progress,
                )
        except TimeoutError:
            return self._timed_out(target, role, progress)

    def _completed_or_timeout(
        self,
        task: asyncio.Task[_Collected] | None,
        target: Target,
        role: LookupRole,
        progress: _RuntimeProgress,
    ) -> _Collected:
        if task is not None and task.done() and not task.cancelled():
            return task.result()
        return self._timed_out(target, role, progress)

    def _timed_out(
        self, target: Target, role: LookupRole, progress: _RuntimeProgress
    ) -> _Collected:
        """Represent an exhausted call-level budget as a lookup failure.

        A recorded index in ``progress`` settles which lookup the budget actually stopped:
        the required document had answered and only the optional schedule was outstanding.
        That is an optional failure, and it is reported as exactly the answer the server
        would have given had the schedule failed for any other reason - the release fact
        the index established, plus a non-required ``timeout`` row and the
        ``EolUnavailable`` that stops step 5 from calling an open-ended gate ``supported``.
        Reporting a required failure there would throw away a confirmed release date, or a
        confirmed *missing* release, that had already been read from an official source.

        With no recorded index, only the required source is reported. Which document was
        still in flight is not knowable here, and the optional one is not what decided the
        outcome: one failed required lookup already means ``lookup_failed``, and naming a
        second source the server cannot prove it opened would put an invented row in
        ``sources_checked``.
        """
        match target:
            case PyPITarget() | NpmTarget():
                source = self._adapter_for(target).source_id
            case PythonRuntimeTarget() | NodeRuntimeTarget():
                if progress.index is not None:
                    return _runtime_collected(
                        target,
                        index=progress.index,
                        lifecycle=RuntimeSourceFailed(detail="timeout"),
                        role=role,
                    )
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

    async def _collect_one(
        self,
        target: Target,
        *,
        role: LookupRole,
        with_lifecycle: bool,
        progress: _RuntimeProgress,
    ) -> _Collected:
        match target:
            case PythonRuntimeTarget() | NodeRuntimeTarget():
                return await self._collect_runtime(
                    target,
                    role=role,
                    with_lifecycle=with_lifecycle,
                    progress=progress,
                )
            case PyPITarget() | NpmTarget():
                # A registry release is one document and one required lookup, so there is
                # no partial answer a timeout could discard.
                return await self._collect_registry(target, role=role)
            case _:
                assert_never(target)

    async def _collect_runtime(
        self,
        target: Target,
        *,
        role: LookupRole,
        with_lifecycle: bool,
        progress: _RuntimeProgress,
    ) -> _Collected:
        """Read a runtime release from its official index, and its line's end of life.

        The support schedule is consulted only where the response will rest on it, so that
        ``sources_checked`` never names a document nothing was read from. There are exactly
        two such places, and the caller says which one this is rather than the role being
        read as a proxy for it: the side a declaration is *about*, whose open-ended gate
        the end-of-life fact bounds, and ``get_compatibility_context``, which reports the
        date as a fact about the release itself. The context tool passes ``declaring`` for
        its single target, so ``role`` could not have told the two apart.
        """
        if with_lifecycle:
            # Structured: both documents are fetched under one scope, and a failure in one
            # cancels the other rather than leaving it running past the response.
            #
            # The required index is awaited in the group's own body while only the optional
            # schedule runs as a child task, so the split between them is in the shape of
            # the code rather than in a comment about it. It is also what makes the index
            # recoverable: `progress` is written the instant the index answers, which is
            # before the group begins waiting on the schedule, so a budget that expires
            # during that wait no longer takes the index down with it.
            async with asyncio.TaskGroup() as group:
                lifecycle_task = group.create_task(self._runtime_lifecycle(target))
                index = await self._runtime_index(target)
                progress.index = index
            lifecycle: RuntimeLifecycleLookup | None = lifecycle_task.result()
        else:
            # Nothing to record: with no sibling to wait for, this returns as soon as the
            # index answers, and a `progress` written here could only invent a schedule row
            # for a document this call never opened.
            index = await self._runtime_index(target)
            lifecycle = None

        return _runtime_collected(target, index=index, lifecycle=lifecycle, role=role)

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


def _runtime_collected(
    target: Target,
    *,
    index: RuntimeIndexLookup,
    lifecycle: RuntimeLifecycleLookup | None,
    role: LookupRole,
) -> _Collected:
    """Turn a runtime's fetched documents into one side's contribution. Pure and total.

    Both the completed path and the budget-expiry path assemble their answer here, so a
    row in ``sources_checked`` cannot describe a different lookup from the one the verdict
    was computed from, whichever path produced it.

    ``lifecycle`` is ``None`` only where no schedule lookup was made at all, which
    :func:`~dependency_compat_mcp.adapters.runtimes.select_eol` reports as
    ``EolNotApplicable`` - distinct from a schedule that was asked for and could not be
    read, which must remain able to block a decided verdict.
    """
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


def _runtime_eol(eol: EolStatus) -> RuntimeEol:
    """Narrow a collected status to the three cases a runtime release can be in.

    ``EolNotApplicable`` means "this target has no support lifecycle", which is true of a
    registry package and of nothing else. Reaching it here would mean the caller built a
    lifecycle for a package, so it raises for the same reason
    :func:`~dependency_compat_mcp.adapters.runtimes.runtime_of` does: a defect surfaces as
    a tool error rather than being smuggled into a response as a fourth status.
    """
    match eol:
        case EolPublished() | EolUnpublished() | EolUnavailable():
            return eol
        case EolNotApplicable():
            raise InvariantViolation(
                "a registry release has no support lifecycle to report"
            )
        case _:
            assert_never(eol)


def _lifecycle_of(target: Target, side: _Collected) -> ReleaseLifecycle | None:
    """The lifecycle block ``get_compatibility_context`` reports, if the target has one.

    ``None`` in the two cases where there is nothing to state rather than something
    unknown: a registry release, which has no support lifecycle at all, and a runtime
    release the index does not list or could not be read for - both of which
    :func:`~dependency_compat_mcp.domain.context.build_context` already answers as
    ``release_not_found`` or ``lookup_failed``, from the same lookups.
    """
    match target:
        case PyPITarget() | NpmTarget():
            return None
        case PythonRuntimeTarget() | NodeRuntimeTarget():
            if not side.found:
                return None
            if (
                side.released_at is None
            ):  # pragma: no cover - a found release has a date
                raise InvariantViolation(
                    "a runtime release found in the official index must carry its date"
                )
            return ReleaseLifecycle(
                released_at=side.released_at, eol=_runtime_eol(side.eol)
            )
        case _:
            assert_never(target)


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
