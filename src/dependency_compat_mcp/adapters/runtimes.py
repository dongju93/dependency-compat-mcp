"""Runtime release facts for ``runtime:python`` and ``runtime:node``.

This adapter satisfies the same role as the registry adapters - turn a source into domain
values - but it never touches the network. 03 [2] decided the runtime release lists are
static committed data: they change slowly, they are small, and a request that needs an EOL
date must not be able to fail because nodejs.org is down. The table is generated once by
``scripts/build_runtime_releases.py`` and reviewed as a diff.

The result is a sum type rather than ``RuntimeRelease | None`` so that "this target has no
release table at all" and "this exact version is not in the snapshot" stay distinguishable.
They lead to different ``sources_checked`` outcomes (``skipped`` versus ``not_found``), and
04 requires those to be reported apart.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, assert_never

from dependency_compat_mcp.curated.loader import RuntimeRelease, RuntimeReleaseTable
from dependency_compat_mcp.domain.claims import LookupRole, SourceCheck, SourceId
from dependency_compat_mcp.domain.errors import InvariantViolation
from dependency_compat_mcp.domain.targets import (
    ExactVersion,
    NodeRuntimeTarget,
    NpmTarget,
    PyPITarget,
    PythonRuntimeTarget,
    Target,
)

__all__ = [
    "RuntimeReleaseAdapter",
    "RuntimeReleaseFound",
    "RuntimeReleaseLookup",
    "RuntimeReleaseMissing",
]

type MissingReason = Literal["not_a_runtime", "version_not_in_table"]


def _as_utc(day: date) -> datetime:
    """Midnight UTC on ``day``.

    ``ReleaseFacts`` compares release instants, so the table's day-precision dates have to
    become aware datetimes somewhere. Doing it here, once, keeps naive datetimes out of
    the domain entirely - a naive/aware comparison raises ``TypeError`` deep inside the
    decision procedure, which is exactly the kind of failure that must be impossible.
    """
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RuntimeReleaseFound:
    """The snapshot has this exact runtime release."""

    version: ExactVersion
    released_at: datetime
    eol_at: datetime | None


@dataclass(frozen=True, slots=True)
class RuntimeReleaseMissing:
    """No row for this target, and why.

    ``not_a_runtime`` means the question does not apply (a PyPI package has no runtime
    EOL); ``version_not_in_table`` means the snapshot predates the release or the version
    does not exist. The first is a ``skipped`` lookup, the second a ``not_found`` one.
    """

    target: Target
    reason: MissingReason


type RuntimeReleaseLookup = RuntimeReleaseFound | RuntimeReleaseMissing


@dataclass(frozen=True, slots=True)
class RuntimeReleaseAdapter:
    """Reads the committed runtime release table. Performs no I/O."""

    table: RuntimeReleaseTable

    def source_id_for(self, target: Target) -> SourceId:
        """Which source id a lookup for ``target`` would be reported under.

        Raises :class:`InvariantViolation` for a non-runtime target. There is no honest
        answer - a PyPI package is not covered by either release table - and returning a
        plausible one would put a source in ``sources_checked`` that was never consulted.
        Callers resolve the relation first, so reaching this is a server defect, and 03
        step 7 requires defects to surface as tool errors rather than hide in an
        ``unknown``.
        """
        match target:
            case PythonRuntimeTarget():
                return "python_release_table"
            case NodeRuntimeTarget():
                return "node_release_table"
            case PyPITarget() | NpmTarget():
                raise InvariantViolation(
                    f"{type(target).__name__} has no runtime release table; "
                    "classify the target before asking for its source id."
                )
            case _:
                assert_never(target)

    def fetch_release(self, target: Target) -> RuntimeReleaseLookup:
        """Look up ``target`` in the static table.

        Named ``fetch_release`` to mirror ``RegistryAdapter`` so the service layer treats
        all evidence sources the same way, but it is a pure dictionary read: no network,
        no timeout, no cancellation point.
        """
        match target:
            case PyPITarget() | NpmTarget():
                return RuntimeReleaseMissing(target=target, reason="not_a_runtime")
            case PythonRuntimeTarget() | NodeRuntimeTarget():
                release = self.table.lookup(target)
                if release is None:
                    return RuntimeReleaseMissing(
                        target=target, reason="version_not_in_table"
                    )
                return _found(release)
            case _:
                assert_never(target)

    def source_check(
        self,
        target: Target,
        lookup: RuntimeReleaseLookup,
        *,
        role: LookupRole = "declaring",
    ) -> SourceCheck:
        """The ``SourceCheck`` for a completed lookup.

        Derived from the lookup value the verdict was computed from, so what the server
        reports it consulted and what it actually used cannot diverge (03 [3]). The check
        names the target it was made for, so two lookups of the same table stay two rows.
        """
        match lookup:
            case RuntimeReleaseFound():
                return SourceCheck(
                    source=self.source_id_for(target),
                    target=target,
                    role=role,
                    outcome="ok",
                )
            case RuntimeReleaseMissing(reason="not_a_runtime"):
                # No source id exists for this target, so nothing was opened. The caller
                # simply omits this check; representing it would require inventing one.
                raise InvariantViolation(
                    "a non-runtime target produces no runtime release SourceCheck."
                )
            case RuntimeReleaseMissing():
                return SourceCheck(
                    source=self.source_id_for(target),
                    target=target,
                    role=role,
                    outcome="not_found",
                    detail="version_not_in_snapshot",
                )
            case _:
                assert_never(lookup)


def _found(release: RuntimeRelease) -> RuntimeReleaseFound:
    return RuntimeReleaseFound(
        version=release.version,
        released_at=_as_utc(release.released_at),
        eol_at=None if release.eol_at is None else _as_utc(release.eol_at),
    )
