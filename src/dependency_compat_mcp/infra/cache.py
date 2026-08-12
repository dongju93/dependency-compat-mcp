"""A TTL cache owned by the application, not by a connection.

01 requires the server to be stateless per request and 03 [3] pins the consequence: the
cache belongs to the application service, so two connections asking the same question get
the same answer and a dropped connection does not discard warm entries.

**The caller builds the key, and the caller is responsible for what belongs in it.** Two
rules follow from 03:

* ``(namespace, name, version)`` identifies a registry document.
* ``pack_version`` must be part of any key whose value was derived from the curated pack.
  A pack release really does change the evidence behind a verdict, so a cached value from
  the previous pack is not merely stale, it is *wrong*. Conversely ``Fetched.retrieved_at``
  must never enter a key: it changes on every fetch and would defeat the cache entirely.

This module deliberately has no async surface. Entries are plain values, every operation
is O(1) and non-blocking, so there is no await point at which a task could be cancelled
mid-mutation.
"""

import time
from collections.abc import Callable, Hashable
from typing import Final

__all__ = ["DEFAULT_MAX_ENTRIES", "TtlCache"]

DEFAULT_MAX_ENTRIES: Final = 512


class TtlCache[K: Hashable, V]:
    """A bounded, time-limited, least-recently-used map.

    Args:
        ttl_seconds: How long an entry stays valid after :meth:`set`.
        clock: Elapsed-time source. Defaults to :func:`time.monotonic` and **must** be
            monotonic: with a wall clock, an NTP step backwards would resurrect entries
            that had already expired, and a step forwards would evict live ones.
        max_entries: Hard ceiling on retained entries. Reaching it evicts the
            least-recently-used entry, which bounds memory against an unbounded stream of
            distinct package names.
    """

    __slots__ = ("_clock", "_entries", "_max_entries", "_ttl_seconds")

    def __init__(
        self,
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1.")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._max_entries = max_entries
        # Insertion order is recency order: the first key is the eviction candidate.
        self._entries: dict[K, tuple[float, V]] = {}

    def get(self, key: K) -> V | None:
        """Return the live value for ``key``, or ``None`` when absent or expired.

        An expired entry is dropped on the way out, so a clock that later moves backwards
        cannot bring it back.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        # Re-insert to mark the entry as most recently used.
        del self._entries[key]
        self._entries[key] = entry
        return value

    def set(self, key: K, value: V) -> None:
        """Store ``value`` under ``key``, refreshing its TTL and its recency."""
        self._entries.pop(key, None)
        self._entries[key] = (self._clock() + self._ttl_seconds, value)
        while len(self._entries) > self._max_entries:
            oldest = next(iter(self._entries))
            del self._entries[oldest]

    def clear(self) -> None:
        """Drop every entry. Used when the curated pack is reloaded, and by tests."""
        self._entries.clear()

    def __len__(self) -> int:
        """Retained entries, including any that have expired but not yet been read."""
        return len(self._entries)
