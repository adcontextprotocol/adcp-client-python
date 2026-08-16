"""Replay dedup store for the AdCP request-signing profile.

Stores `(keyid, nonce)` pairs that have already been accepted, with a TTL that
mirrors the signature's `expires` parameter plus skew. Per-keyid and global
caps prevent unbounded growth — when either cap is hit, new signatures are
rejected with `request_signature_rate_abuse` rather than silently evicting
older entries (which would create a replay window under attack).

Thread-safe within a process; not shared across processes — see issue #187 for
a Redis adapter for multi-instance verifiers.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Literal, Protocol, TypeGuard, runtime_checkable

ReplayClaimResult = Literal["claimed", "replayed", "capacity"]


class ReplayStore(Protocol):
    """Legacy-compatible interface a replay backend must expose.

    New backends should additionally implement :class:`AtomicReplayStore`.
    Keeping the atomic operation on a separate Protocol lets applications
    written against the pre-6.6 replay API continue to type-check while the
    verifier provides a visible, race-prone compatibility fallback.
    """

    def seen(self, keyid: str, nonce: str) -> bool:
        raise NotImplementedError

    def remember(self, keyid: str, nonce: str, ttl_seconds: float) -> bool | None:
        raise NotImplementedError

    def at_capacity(self, keyid: str) -> bool:
        raise NotImplementedError


@runtime_checkable
class AtomicReplayStore(ReplayStore, Protocol):
    """Replay backend that can reserve a nonce without a check/write race."""

    def claim(self, keyid: str, nonce: str, ttl_seconds: float) -> ReplayClaimResult:
        """Atomically reserve a nonce, or report why it cannot be reserved."""
        raise NotImplementedError


def supports_atomic_claim(store: ReplayStore) -> TypeGuard[AtomicReplayStore]:
    """Return whether ``store`` exposes a trustworthy atomic claim operation.

    Delegating wrappers can declare their resolved backend's capability through
    ``supports_atomic_claim()``. Bare stores are checked structurally against
    the runtime-checkable :class:`AtomicReplayStore` contract.
    """
    declared = getattr(store, "supports_atomic_claim", None)
    if callable(declared):
        return bool(declared())
    return isinstance(store, AtomicReplayStore)


# Cap on the number of expired entries swept per mutating call. Bounded so that
# a single `remember` / `seen` stays O(1) amortized on a well-behaved workload;
# natural inserts and lookups sweep incrementally.
_SWEEP_BATCH = 16


class InMemoryReplayStore:
    """Process-local replay store. Uses a monotonic clock for TTL bookkeeping so
    wall-clock jumps (NTP adjustments, VM suspend/resume) don't race eviction.

    ``global_cap`` bounds attacker-controlled key rotation as well as nonce
    volume. An indexed min-heap expires entries incrementally without copying
    or scanning the nonce table on accepted requests.
    """

    def __init__(
        self,
        *,
        per_keyid_cap: int = 1_000_000,
        global_cap: int = 1_000_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_keyid_cap <= 0:
            raise ValueError("per_keyid_cap must be greater than zero")
        if global_cap <= 0:
            raise ValueError("global_cap must be greater than zero")
        self._per_keyid_cap = per_keyid_cap
        self._global_cap = global_cap
        self._clock = clock
        self._entries: dict[tuple[str, str], float] = {}
        self._expiry_heap: list[tuple[float, tuple[str, str]]] = []
        self._heap_positions: dict[tuple[str, str], int] = {}
        self._counts: dict[str, int] = {}
        self._cap_hit: set[str] = set()
        self._lock = threading.RLock()

    def seen(self, keyid: str, nonce: str) -> bool:
        with self._lock:
            self._expire_one(keyid, nonce)
            return (keyid, nonce) in self._entries

    def remember(self, keyid: str, nonce: str, ttl_seconds: float) -> bool:
        """Record a nonce, returning ``False`` when capacity refuses it."""
        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            key = (keyid, nonce)
            if key not in self._entries:
                if (
                    len(self._entries) >= self._global_cap
                    or self._counts.get(keyid, 0) >= self._per_keyid_cap
                ):
                    return False
                self._counts[keyid] = self._counts.get(keyid, 0) + 1
            expiry = now + ttl_seconds
            self._entries[key] = expiry
            self._push_expiry(key, expiry)
            return True

    def at_capacity(self, keyid: str) -> bool:
        with self._lock:
            self._purge_expired(self._clock())
            if keyid in self._cap_hit:
                return True
            return (
                len(self._entries) >= self._global_cap
                or self._counts.get(keyid, 0) >= self._per_keyid_cap
            )

    def claim(self, keyid: str, nonce: str, ttl_seconds: float) -> ReplayClaimResult:
        """Atomically check capacity/replay state and reserve ``nonce``."""
        with self._lock:
            now = self._clock()
            self._expire_one(keyid, nonce)
            if (keyid, nonce) in self._entries:
                return "replayed"
            self._purge_expired(now)
            if (
                keyid in self._cap_hit
                or len(self._entries) >= self._global_cap
                or self._counts.get(keyid, 0) >= self._per_keyid_cap
            ):
                return "capacity"
            key = (keyid, nonce)
            expiry = now + ttl_seconds
            self._entries[key] = expiry
            self._counts[keyid] = self._counts.get(keyid, 0) + 1
            self._push_expiry(key, expiry)
            return "claimed"

    def mark_cap_hit(self, keyid: str) -> None:
        """Test-harness hook — simulate the cap being reached for this keyid."""
        with self._lock:
            self._cap_hit.add(keyid)

    def _expire_one(self, keyid: str, nonce: str) -> None:
        key = (keyid, nonce)
        expiry = self._entries.get(key)
        if expiry is not None and expiry < self._clock():
            del self._entries[key]
            self._remove_expiry(key)
            self._counts[keyid] = self._counts.get(keyid, 1) - 1
            if self._counts[keyid] <= 0:
                self._counts.pop(keyid, None)

    def _push_expiry(self, key: tuple[str, str], expiry: float) -> None:
        position = self._heap_positions.get(key)
        if position is None:
            position = len(self._expiry_heap)
            self._expiry_heap.append((expiry, key))
            self._heap_positions[key] = position
            self._sift_up(position)
            return
        old_expiry = self._expiry_heap[position][0]
        self._expiry_heap[position] = (expiry, key)
        if expiry < old_expiry:
            self._sift_up(position)
        else:
            self._sift_down(position)

    def _remove_expiry(self, key: tuple[str, str]) -> None:
        position = self._heap_positions.pop(key, None)
        if position is None:
            return
        last = self._expiry_heap.pop()
        if position == len(self._expiry_heap):
            return
        self._expiry_heap[position] = last
        self._heap_positions[last[1]] = position
        if position > 0 and self._expiry_heap[position] < self._expiry_heap[(position - 1) // 2]:
            self._sift_up(position)
        else:
            self._sift_down(position)

    def _sift_up(self, position: int) -> None:
        while position > 0:
            parent = (position - 1) // 2
            if self._expiry_heap[parent] <= self._expiry_heap[position]:
                return
            self._swap_heap(parent, position)
            position = parent

    def _sift_down(self, position: int) -> None:
        size = len(self._expiry_heap)
        while (left := position * 2 + 1) < size:
            right = left + 1
            child = (
                right
                if right < size and self._expiry_heap[right] < self._expiry_heap[left]
                else left
            )
            if self._expiry_heap[position] <= self._expiry_heap[child]:
                return
            self._swap_heap(position, child)
            position = child

    def _swap_heap(self, left: int, right: int) -> None:
        self._expiry_heap[left], self._expiry_heap[right] = (
            self._expiry_heap[right],
            self._expiry_heap[left],
        )
        self._heap_positions[self._expiry_heap[left][1]] = left
        self._heap_positions[self._expiry_heap[right][1]] = right

    def _purge_expired(self, now: float) -> None:
        examined = 0
        while self._expiry_heap and examined < _SWEEP_BATCH:
            expiry, key = self._expiry_heap[0]
            if expiry >= now:
                return
            self._remove_expiry(key)
            examined += 1
            del self._entries[key]
            keyid = key[0]
            self._counts[keyid] = self._counts.get(keyid, 1) - 1
            if self._counts[keyid] <= 0:
                self._counts.pop(keyid, None)
