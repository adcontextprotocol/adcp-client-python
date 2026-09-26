"""Bounded exact-byte validation proofs; no caller-owned objects are retained."""

from __future__ import annotations

import sys
from collections import OrderedDict
from threading import RLock


class ValidationCache:
    """An internal LRU of immutable keys and values, bounded in two dimensions.

    Count shared bytes again when sizing entries: this deliberately overcounts
    retained payloads. The per-entry allowance also covers mapping/lock metadata.
    A cache hit is equality of the complete bytes, never equality of a digest.
    Eviction or an oversized entry merely causes full validation on the next use.
    """

    def __init__(self, *, entries: int, byte_budget: int) -> None:
        if entries < 1 or byte_budget < 1:
            raise ValueError("positive validation cache bounds required")
        self._entries = entries
        self._byte_budget = byte_budget
        self._retained_bytes = 0
        self._values: OrderedDict[tuple[bytes, ...], tuple[tuple[bytes, ...], int]] = OrderedDict()
        self._lock = RLock()

    def get(self, key: tuple[bytes, ...]) -> tuple[bytes, ...] | None:
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            self._values.move_to_end(key)
            return entry[0]

    def put(self, key: tuple[bytes, ...], value: tuple[bytes, ...]) -> None:
        if (
            type(key) is not tuple
            or type(value) is not tuple
            or any(type(part) is not bytes for part in (*key, *value))
        ):
            raise TypeError("immutable validation proof required")
        size = 1024 + sum(sys.getsizeof(parts) for parts in (key, value))
        size += sum(sys.getsizeof(part) for part in (*key, *value))
        with self._lock:
            previous = self._values.pop(key, None)
            if previous is not None:
                self._retained_bytes -= previous[1]
            if size > self._byte_budget:
                return
            while self._values and (
                len(self._values) >= self._entries
                or self._retained_bytes + size > self._byte_budget
            ):
                _, (_, released) = self._values.popitem(last=False)
                self._retained_bytes -= released
            self._values[key] = value, size
            self._retained_bytes += size


# At most 64 MiB of conservatively counted proofs across both caches. They are
# performance aids, not durable state, and contain neither auth grants nor raw
# seller diagnostics. A cold process still validates the complete stored bytes.
PLANS = ValidationCache(entries=16, byte_budget=32 * 1024 * 1024)
CONFIRMATIONS = ValidationCache(entries=256, byte_budget=32 * 1024 * 1024)
