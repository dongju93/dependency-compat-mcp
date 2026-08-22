"""Tests for the application-owned TTL cache.

The properties that matter are the ones 01/03 rely on: an entry expires on elapsed time
and never comes back, memory is bounded regardless of how many distinct package names
arrive, and the key is whatever the caller says it is.
"""

import time

import pytest

from dependency_compat_mcp.infra.cache import TtlCache


class FakeClock:
    """A hand-advanced elapsed-time source, so no test sleeps."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_a_missing_key_reads_as_none() -> None:
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=60)
    assert cache.get("absent") is None


def test_a_stored_value_is_returned_before_the_ttl_elapses() -> None:
    clock = FakeClock()
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=60, clock=clock)
    cache.set("key", 7)
    clock.advance(59)
    assert cache.get("key") == 7


def test_a_value_expires_once_the_ttl_elapses() -> None:
    clock = FakeClock()
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=60, clock=clock)
    cache.set("key", 7)
    clock.advance(60)
    assert cache.get("key") is None


def test_an_expired_entry_is_dropped_and_cannot_be_resurrected() -> None:
    """A clock that moves backwards must not bring an expired entry back.

    This is why the default clock is monotonic: with a wall clock, an NTP step backwards
    would otherwise revive evidence the server had already decided was stale.
    """
    clock = FakeClock()
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=10, clock=clock)
    cache.set("key", 7)
    clock.advance(11)
    assert cache.get("key") is None
    clock.now = 1.0
    assert cache.get("key") is None
    assert len(cache) == 0


def test_the_default_clock_is_monotonic() -> None:
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=60)
    assert cache._clock is time.monotonic


def test_setting_a_key_again_refreshes_its_ttl() -> None:
    clock = FakeClock()
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=10, clock=clock)
    cache.set("key", 1)
    clock.advance(9)
    cache.set("key", 2)
    clock.advance(9)
    assert cache.get("key") == 2
    assert len(cache) == 1


def test_reaching_the_ceiling_evicts_the_least_recently_used_entry() -> None:
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=60, max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert len(cache) == 2


def test_reading_an_entry_makes_it_the_most_recent() -> None:
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=60, max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1  # "b" is now the eviction candidate
    cache.set("c", 3)
    assert cache.get("a") == 1
    assert cache.get("b") is None


def test_clear_drops_everything() -> None:
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=60)
    cache.set("a", 1)
    cache.clear()
    assert cache.get("a") is None
    assert len(cache) == 0


def test_the_identity_of_a_document_belongs_to_the_caller_s_key() -> None:
    """The cache promises only that different keys are different entries.

    Which fields identify a document is the caller's decision - a registry release is
    keyed by source and exact version, an official runtime index by its source alone.
    This test pins the contract that decision depends on.
    """
    cache: TtlCache[tuple[str, str, str, str], str] = TtlCache(ttl_seconds=60)
    old = ("pypi_json", "pypi", "sample-project", "5.2.1")
    new = ("pypi_json", "pypi", "sample-project", "5.3.0")
    cache.set(old, "the 5.2.1 document")
    assert cache.get(new) is None


@pytest.mark.parametrize(
    "kwargs",
    [{"ttl_seconds": 0}, {"ttl_seconds": -1}, {"ttl_seconds": 60, "max_entries": 0}],
)
def test_nonsensical_configuration_is_rejected(kwargs: dict[str, float | int]) -> None:
    with pytest.raises(ValueError):
        TtlCache(**kwargs)  # pyrefly: ignore[bad-argument-type]
