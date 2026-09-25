"""Owned lifecycle and admission for the adapter-first reporting service.

This module owns process lifetime only. Account activation, durable leases and
production-tier dependency proofs remain the responsibility of their reporting
components. No PostgreSQL driver is imported by this lifecycle surface.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypeVar

from adcp.reporting._settlement import cancel_and_settle, settle_task

_T = TypeVar("_T")

__all__ = [
    "ReliableReportingServiceError",
    "ReliableReportingShutdownTimeoutError",
    "ReliableReportingState",
    "ReliableReportingUnavailableError",
    "ReportingServiceResource",
]


class ReliableReportingState(str, Enum):
    NEW = "new"
    STARTING = "starting"
    INITIALIZED = "initialized"
    READY = "ready"
    STOPPING = "stopping"
    CLOSED = "closed"
    FAILED = "failed"


class ReliableReportingUnavailableError(RuntimeError):
    """The service is not admitting new reporting work."""

    def __init__(self, state: ReliableReportingState) -> None:
        self.state = state
        super().__init__(f"reporting service is unavailable ({state.value})")


FailureComponent = Literal[
    "startup", "configuration", "materialization", "notification", "service", "shutdown"
]


class ReliableReportingServiceError(RuntimeError):
    """Closed diagnostic for an unexpected failure; contains no provider body."""

    def __init__(self, component: FailureComponent) -> None:
        if component not in (
            "startup",
            "configuration",
            "materialization",
            "notification",
            "service",
            "shutdown",
        ):
            raise ValueError("unknown reporting service failure component")
        self.component = component
        super().__init__(f"reporting service failed ({component})")


class ReliableReportingShutdownTimeoutError(TimeoutError):
    """A close waiter timed out; the service still owns the unsettled work."""


ResourceCallback = Callable[[], object | Awaitable[object]]


@dataclass(frozen=True)
class ReportingServiceResource:
    """Explicit transfer of a resource's lifetime to one service.

    Resources open in declaration order before schema initialization and close
    once, in reverse order, after all admitted work settles. Ownership transfers
    at construction, so ``close`` must tolerate unopened or partially opened
    resources (including a service that never starts). Injected stores, adapters,
    workers, senders and pools are otherwise borrowed.

    Use async callbacks for loop-bound resources. Blocking synchronous callbacks
    run in a thread and are settled before cleanup can advance. Do not transfer
    the same resource to more than one owner.
    """

    close: ResourceCallback = field(repr=False)
    open: ResourceCallback | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not callable(self.close) or (self.open is not None and not callable(self.open)):
            raise TypeError("resource callbacks must be callable")


async def _invoke(callback: ResourceCallback) -> None:
    if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        getattr(callback, "__call__", None)
    ):
        result = callback()
        if inspect.isawaitable(result):
            await result
    else:

        async def run() -> None:
            result = await asyncio.to_thread(callback)
            if inspect.isawaitable(result):
                await result

        await settle_task(asyncio.create_task(run()))


class _ServiceLifecycle:
    """One loop-owned, locked state machine shared by tasks and mounted RPCs."""

    def __init__(
        self,
        initialize: Callable[[], Awaitable[None]],
        *,
        resources: Sequence[ReportingServiceResource],
        configuration_error: type[Exception],
    ) -> None:
        self.state = ReliableReportingState.NEW
        self.failure: ReliableReportingServiceError | None = None
        self._initialize = initialize
        self._configuration_error = configuration_error
        self._resources = tuple(resources)
        if any(not isinstance(item, ReportingServiceResource) for item in self._resources):
            raise TypeError("owned_resources must contain ReportingServiceResource values")
        if any(
            item.close == earlier.close
            for index, item in enumerate(self._resources)
            for earlier in self._resources[:index]
        ):
            raise ValueError("a resource close callback cannot be transferred twice")
        self._lock = asyncio.Lock()
        self._initialization: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._shutdown: asyncio.Task[None] | None = None
        self._admissions: dict[asyncio.Task[Any], int] = {}
        self._idle = asyncio.Event()
        self._idle.set()
        self._stop = asyncio.Event()
        self._stopped = asyncio.Event()

    @property
    def ready(self) -> bool:
        return self.state is ReliableReportingState.READY

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def require_ready(self) -> None:
        if not self.ready:
            raise ReliableReportingUnavailableError(self.state)

    async def initialize(self) -> None:
        async with self._lock:
            if self.state in (ReliableReportingState.INITIALIZED, ReliableReportingState.READY):
                return
            if self.state is ReliableReportingState.NEW:
                self.state = ReliableReportingState.STARTING
                self._initialization = asyncio.create_task(
                    self._open(), name="adcp-reporting-startup"
                )
            elif self.state is not ReliableReportingState.STARTING:
                raise ReliableReportingUnavailableError(self.state)
            task = self._initialization
        assert task is not None
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The opener may hold a transaction or an uninterruptible thread.
            # Stop admission and retain it for the shared shutdown task.
            await self.request_stop()
            raise
        except Exception:
            await self.close()
            raise
        if self.state not in (ReliableReportingState.INITIALIZED, ReliableReportingState.READY):
            raise ReliableReportingUnavailableError(self.state)

    async def _open(self) -> None:
        try:
            for resource in self._resources:
                if self.stopping:
                    return
                if resource.open is not None:
                    await _invoke(resource.open)
            if self.stopping:
                return
            await self._initialize()
            async with self._lock:
                if self.state is ReliableReportingState.STARTING:
                    self.state = ReliableReportingState.INITIALIZED
        except BaseException as error:
            failure = await self.fail("startup")
            if isinstance(error, self._configuration_error):
                raise
            raise failure from None

    async def activate(self, run: Callable[[], Awaitable[None]] | None = None) -> None:
        async with self._lock:
            if self.state not in (ReliableReportingState.INITIALIZED, ReliableReportingState.READY):
                raise ReliableReportingUnavailableError(self.state)
            if run is not None and self._worker is None:
                self._worker = asyncio.create_task(
                    self._supervise(run), name="adcp-reliable-reporting"
                )
            self.state = ReliableReportingState.READY

    async def _supervise(self, run: Callable[[], Awaitable[None]]) -> None:
        try:
            await run()
        except BaseException:
            if not self.stopping:
                await self.fail("service")
        else:
            if not self.stopping:
                await self.fail("service")

    async def call(
        self, operation: Callable[[], Awaitable[_T]], *, before_start: bool = False
    ) -> _T:
        """Own an admitted task and propagate cancellation to it only once.

        Repeated cancellation of the transport/caller cannot interrupt the
        operation's transaction cleanup. Context variables are inherited, but
        callers must use low-level store APIs for ambient transaction batches:
        high-level service operations own their tasks and transaction lifetime.
        """

        async def admitted() -> _T:
            async with self.admit(before_start=before_start):
                return await operation()

        task = asyncio.create_task(admitted(), name="adcp-reporting-admission")
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await cancel_and_settle(task)
            raise

    @asynccontextmanager
    async def admit(self, *, before_start: bool = False) -> AsyncIterator[None]:
        task = asyncio.current_task()
        assert task is not None
        async with self._lock:
            if not self.ready and not (
                before_start
                and self.state
                in (
                    ReliableReportingState.NEW,
                    ReliableReportingState.STARTING,
                    ReliableReportingState.INITIALIZED,
                )
            ):
                raise ReliableReportingUnavailableError(self.state)
            self._admissions[task] = self._admissions.get(task, 0) + 1
            self._idle.clear()
        try:
            yield
        finally:
            # No await: repeated cancellation cannot interrupt deregistration.
            # All admission bookkeeping is confined to this service's loop.
            remaining = self._admissions[task] - 1
            if remaining:
                self._admissions[task] = remaining
            else:
                del self._admissions[task]
            if not self._admissions:
                self._idle.set()

    def _stop_locked(self) -> None:
        if self._shutdown is None:
            self.state = ReliableReportingState.STOPPING
            self._stop.set()
            self._shutdown = asyncio.create_task(self._drain(), name="adcp-reporting-shutdown")

    async def request_stop(self) -> None:
        async with self._lock:
            self._stop_locked()

    async def fail(self, component: FailureComponent) -> ReliableReportingServiceError:
        async with self._lock:
            if self.failure is None:
                self.failure = ReliableReportingServiceError(component)
            self._stop_locked()
            return self.failure

    async def wait_for_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def close(self, *, timeout: float | None = None) -> None:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("close timeout must be a finite nonnegative number")
        current = asyncio.current_task()
        if (
            current in self._admissions
            or current is self._initialization
            or current is self._shutdown
        ):
            raise RuntimeError("close must be awaited outside an admitted reporting operation")
        await self.request_stop()
        assert self._shutdown is not None
        try:
            await asyncio.wait_for(asyncio.shield(self._shutdown), timeout=timeout)
        except asyncio.TimeoutError:
            raise ReliableReportingShutdownTimeoutError(
                "reporting service is still stopping; admitted work has not settled"
            ) from None

    async def _drain(self) -> None:
        for task in (self._initialization, self._worker):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        await self._idle.wait()
        for resource in reversed(self._resources):
            try:
                await _invoke(resource.close)
            except BaseException:
                # A failed closer must not strand earlier resources. Preserve
                # the first failure, without retaining a resource's raw error.
                await self.fail("shutdown")
        async with self._lock:
            self.state = (
                ReliableReportingState.FAILED
                if self.failure is not None
                else ReliableReportingState.CLOSED
            )
            self._stopped.set()

    async def wait(self) -> None:
        await self._stopped.wait()
        if self.failure is not None:
            raise self.failure from None
