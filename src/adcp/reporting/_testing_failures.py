"""Deterministic boundary controls, exported by :mod:`adcp.reporting.testing`."""

from __future__ import annotations

import asyncio
import math
import threading
from collections import defaultdict, deque
from collections.abc import Mapping
from typing import Any

from adcp.reporting.inline_source import ReportingSealStore, ReportingStagingStore, SealedSlice


class ReportingTestBarrier:
    """Pause an async operation or a synchronous adapter running in a worker.

    Construct inside the test's event loop, await :meth:`wait` to observe the
    boundary, and always call :meth:`release` in test cleanup. The timeout is a
    real-time deadlock watchdog; it does not advance the reporting clock.
    """

    def __init__(self, *, timeout: float = 10) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("a reporting test barrier needs a finite positive timeout")
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._timeout = timeout
        self._entered = asyncio.Event()
        self._released = asyncio.Event()
        self._thread_released = threading.Event()

    async def wait(self) -> None:
        """Wait until the operation reaches this boundary, without polling."""
        await asyncio.wait_for(self._entered.wait(), self._timeout)

    async def pause(self) -> None:
        self._entered.set()
        await asyncio.wait_for(self._released.wait(), self._timeout)

    def pause_sync(self) -> None:
        """Pause a worker thread; never block the event-loop thread."""
        if threading.get_ident() == self._loop_thread:
            raise RuntimeError("pause_sync must run outside the barrier's event-loop thread")
        self._loop.call_soon_threadsafe(self._entered.set)
        if not self._thread_released.wait(self._timeout):
            raise TimeoutError("reporting test barrier was not released")

    def release(self) -> None:
        """Release all waiters, including a synchronous in-flight fetch."""
        self._thread_released.set()
        self._loop.call_soon_threadsafe(self._released.set)


class ReportingFailurePlan:
    """FIFO faults and barriers at named operation boundaries.

    ``at("seal.after", OSError("lost acknowledgement"))`` fails once *after*
    the seal store commits. ``None`` consumes one successful visit. Unplanned
    visits succeed. Use :meth:`assert_consumed` to catch a misspelled or
    unreached boundary. Adopter wrappers can use ``hit`` / ``hit_sync`` for
    additional destination, notification, and receipt boundaries.

    Queue access is thread-safe, but tests that need a particular ordering of
    concurrent calls must impose it with barriers. Errors are test inputs and
    must not contain real credentials or provider responses.
    """

    def __init__(self) -> None:
        self._steps: dict[str, deque[BaseException | ReportingTestBarrier | None]] = defaultdict(
            deque
        )
        self._hits: list[str] = []
        self._lock = threading.Lock()

    def at(self, point: str, *steps: BaseException | ReportingTestBarrier | None) -> None:
        if not point or not steps:
            raise ValueError("a failure point needs a name and at least one step")
        if any(
            step is not None and not isinstance(step, (BaseException, ReportingTestBarrier))
            for step in steps
        ):
            raise TypeError("failure steps must be exceptions, barriers, or None")
        with self._lock:
            self._steps[point].extend(steps)

    @property
    def hits(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._hits)

    def _take(self, point: str) -> BaseException | ReportingTestBarrier | None:
        with self._lock:
            self._hits.append(point)
            steps = self._steps.get(point)
            return steps.popleft() if steps else None

    async def hit(self, point: str) -> None:
        step = self._take(point)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, ReportingTestBarrier):
            await step.pause()

    def hit_sync(self, point: str) -> None:
        step = self._take(point)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, ReportingTestBarrier):
            step.pause_sync()

    def assert_consumed(self) -> None:
        with self._lock:
            remaining = {point: len(steps) for point, steps in self._steps.items() if steps}
        if remaining:
            raise AssertionError(f"unreached reporting failure steps: {remaining}")


class FaultInjectingReportingStagingStore:
    """Borrow a memory or durable store with ``stage`` / ``read`` fault points.

    Each operation visits ``<operation>.before`` and ``<operation>.after``.
    A fault after staging retains the real store's write. Schema creation and
    resource ownership stay with the caller; this wrapper copies no state.
    """

    def __init__(self, store: ReportingStagingStore, failures: ReportingFailurePlan) -> None:
        self._store = store
        self._failures = failures

    async def stage(
        self, *, account_id: str, source_execution_key: str, ordinal: int, payload: bytes
    ) -> tuple[str, str]:
        await self._failures.hit("stage.before")
        result = await self._store.stage(
            account_id=account_id,
            source_execution_key=source_execution_key,
            ordinal=ordinal,
            payload=payload,
        )
        await self._failures.hit("stage.after")
        return result

    async def read(
        self,
        *,
        object_ref: str,
        object_generation: str,
        account_id: str,
        source_scope: Mapping[str, Any],
        cancel: asyncio.Event,
    ) -> bytes:
        await self._failures.hit("read.before")
        result = await self._store.read(
            object_ref=object_ref,
            object_generation=object_generation,
            account_id=account_id,
            source_scope=source_scope,
            cancel=cancel,
        )
        await self._failures.hit("read.after")
        return result


class FaultInjectingReportingSealStore:
    """Borrow a seal store with ``seal.get`` and ``seal`` before/after points.

    In particular, ``seal.after`` models a lost acknowledgement: replay must
    discover the original seal instead of refetching or publishing new bytes.
    """

    def __init__(self, store: ReportingSealStore, failures: ReportingFailurePlan) -> None:
        self._store = store
        self._failures = failures

    async def get(self, *, account_id: str, source_execution_key: str) -> SealedSlice | None:
        await self._failures.hit("seal.get.before")
        result = await self._store.get(
            account_id=account_id, source_execution_key=source_execution_key
        )
        await self._failures.hit("seal.get.after")
        return result

    async def put(
        self, *, account_id: str, source_execution_key: str, sealed: SealedSlice
    ) -> SealedSlice:
        await self._failures.hit("seal.before")
        result = await self._store.put(
            account_id=account_id, source_execution_key=source_execution_key, sealed=sealed
        )
        await self._failures.hit("seal.after")
        return result
