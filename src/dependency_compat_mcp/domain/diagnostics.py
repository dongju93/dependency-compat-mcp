"""Closed code sets for what was observed but does not change the verdict, and for what
could not be checked at all.

03 insists these be codes rather than prose: a caller has to be able to branch on them,
and they have to be computed rather than written. Keeping the two apart is the point -
``notices`` are confirmed facts, ``limitations`` are unchecked scope, and mixing them into
one "caveats" list would make an honest ``unknown`` indistinguishable from hedging.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal

from dependency_compat_mcp.domain.claims import EvidenceId

__all__ = [
    "LIMITATION_CODES",
    "NOTICE_CODES",
    "Limitation",
    "LimitationCode",
    "Notice",
    "NoticeCode",
    "sorted_limitations",
    "sorted_notices",
]

type LimitationCode = Literal[
    "open_upper_bound",
    "stale_lower_bound",
    "tier_c_only",
    "curated_pack_missing",
    "curated_not_verified_for_version",
    "marker_guarded_claim",
    "extra_guarded_claim",
    "source_unavailable",
]

type NoticeCode = Literal[
    "subject_yanked",
    "counterpart_yanked",
    "gate_contradicts_statement",
]

LIMITATION_CODES: Final[tuple[LimitationCode, ...]] = (
    "open_upper_bound",
    "stale_lower_bound",
    "tier_c_only",
    "curated_pack_missing",
    "curated_not_verified_for_version",
    "marker_guarded_claim",
    "extra_guarded_claim",
    "source_unavailable",
)

NOTICE_CODES: Final[tuple[NoticeCode, ...]] = (
    "subject_yanked",
    "counterpart_yanked",
    "gate_contradicts_statement",
)


@dataclass(frozen=True, slots=True)
class Limitation:
    """Scope the server did *not* verify."""

    code: LimitationCode


@dataclass(frozen=True, slots=True)
class Notice:
    """A confirmed fact that does not move the verdict, with the sources it rests on."""

    code: NoticeCode
    evidence_ids: tuple[EvidenceId, ...] = field(default=())


def sorted_limitations(limitations: Iterable[Limitation]) -> tuple[Limitation, ...]:
    """De-duplicate and order by the declared code order, for byte-stable output."""
    unique = {limitation.code: limitation for limitation in limitations}
    return tuple(unique[code] for code in LIMITATION_CODES if code in unique)


def sorted_notices(notices: Iterable[Notice]) -> tuple[Notice, ...]:
    """Merge notices sharing a code, union their evidence, and order by the code order."""
    merged: dict[NoticeCode, set[EvidenceId]] = {}
    for notice in notices:
        merged.setdefault(notice.code, set()).update(notice.evidence_ids)
    return tuple(
        Notice(code=code, evidence_ids=tuple(sorted(merged[code])))
        for code in NOTICE_CODES
        if code in merged
    )
