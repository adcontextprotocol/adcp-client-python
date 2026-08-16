"""Tests for InMemoryReplayStore correctness under thread and clock pressure."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from adcp.signing import AtomicReplayStore, InMemoryReplayStore


@dataclass
class _FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def test_thread_safety_concurrent_inserts() -> None:
    clock = _FakeClock()
    store = InMemoryReplayStore(per_keyid_cap=10_000, clock=clock)
    thread_count = 100
    nonces_per_thread = 10

    def worker(tid: int) -> None:
        for i in range(nonces_per_thread):
            nonce = f"t{tid}-n{i}"
            assert not store.seen("kid", nonce)
            store.remember("kid", nonce, ttl_seconds=3600.0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No lost increments / races — count matches total inserts exactly.
    assert store._counts["kid"] == thread_count * nonces_per_thread


def test_concurrent_claim_has_exactly_one_winner() -> None:
    store = InMemoryReplayStore()
    barrier = threading.Barrier(16)
    results: list[str] = []

    def worker() -> None:
        barrier.wait()
        results.append(store.claim("kid", "shared", ttl_seconds=60.0))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("claimed") == 1
    assert results.count("replayed") == 15


def test_monotonic_clock_ttl_expires_and_decrements_count() -> None:
    clock = _FakeClock(now=0.0)
    store = InMemoryReplayStore(per_keyid_cap=5, clock=clock)
    store.remember("kid", "n1", ttl_seconds=10.0)
    store.remember("kid", "n2", ttl_seconds=10.0)
    assert store._counts["kid"] == 2

    clock.now = 20.0  # past both TTLs
    # seen() sweeps the specific key on access
    assert not store.seen("kid", "n1")
    assert store._counts.get("kid", 0) == 1
    assert not store.seen("kid", "n2")
    assert "kid" not in store._counts


def test_at_capacity_flips_when_entries_expire() -> None:
    clock = _FakeClock(now=0.0)
    store = InMemoryReplayStore(per_keyid_cap=3, clock=clock)
    for i in range(3):
        store.remember("kid", f"n{i}", ttl_seconds=10.0)
    assert store.at_capacity("kid") is True

    clock.now = 100.0
    # Force a sweep via a new remember() call for the same keyid.
    store.remember("kid", "new-nonce", ttl_seconds=10.0)
    # After the sweep, only the new entry remains.
    assert store._counts["kid"] == 1
    assert store.at_capacity("kid") is False


def test_remember_reports_capacity_refusal() -> None:
    store = InMemoryReplayStore(per_keyid_cap=2, global_cap=10)
    assert store.remember("kid", "n1", ttl_seconds=60.0) is True
    assert store.remember("kid", "n2", ttl_seconds=60.0) is True
    assert store.remember("kid", "n3", ttl_seconds=60.0) is False
    assert store.seen("kid", "n3") is False


def test_in_memory_store_satisfies_atomic_protocol() -> None:
    assert isinstance(InMemoryReplayStore(), AtomicReplayStore)


def test_at_capacity_is_o1_after_sweep() -> None:
    # Smoke test that at_capacity doesn't do an O(n) scan — we check the count
    # dict directly (construction and check).
    clock = _FakeClock()
    store = InMemoryReplayStore(per_keyid_cap=100, clock=clock)
    for i in range(50):
        store.remember("kid", f"n{i}", ttl_seconds=3600.0)
    assert store._counts["kid"] == 50
    assert store.at_capacity("kid") is False


def test_mark_cap_hit_still_works() -> None:
    store = InMemoryReplayStore()
    store.mark_cap_hit("kid")
    assert store.at_capacity("kid") is True


def test_global_cap_bounds_rotating_keyids_and_recovers_after_expiry() -> None:
    clock = _FakeClock(now=0.0)
    store = InMemoryReplayStore(per_keyid_cap=10, global_cap=3, clock=clock)
    assert store.claim("kid-1", "n", 10.0) == "claimed"
    assert store.claim("kid-2", "n", 10.0) == "claimed"
    assert store.claim("kid-3", "n", 10.0) == "claimed"
    assert store.claim("kid-4", "n", 10.0) == "capacity"
    assert len(store._entries) == 3

    clock.now = 11.0
    assert store.claim("kid-4", "n", 10.0) == "claimed"
    assert len(store._entries) == 1


def test_claim_does_not_copy_the_entry_table() -> None:
    class _NoItemsDict(dict[tuple[str, str], float]):
        def items(self):  # type: ignore[no-untyped-def]
            raise AssertionError("accepted claim copied/scanned the full entry table")

    store = InMemoryReplayStore(global_cap=100)
    store._entries = _NoItemsDict()
    for index in range(50):
        assert store.claim(f"kid-{index}", "nonce", 60.0) == "claimed"


def test_renewal_heap_remains_bounded() -> None:
    store = InMemoryReplayStore(global_cap=3)
    for index in range(50):
        store.remember("kid", "nonce", float(index + 1))
    assert len(store._entries) == 1
    assert len(store._expiry_heap) <= 6
