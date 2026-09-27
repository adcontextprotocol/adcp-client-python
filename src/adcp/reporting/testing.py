"""Deterministic adapter, failure, and service lifecycle test helpers.

Adapter authors can exercise the exact production wrapper without constructing
manifests or staged objects themselves.  The conformance runner executes the
same frozen slice twice and verifies replay identity plus every staged byte.
Named barriers work with synchronous provider SDKs as well as async adapters.
Store wrappers and restart assertions use the public memory/PG store contracts.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from adcp.reporting._testing_failures import (
    FaultInjectingReportingSealStore,
    FaultInjectingReportingStagingStore,
    ReportingFailurePlan,
    ReportingTestBarrier,
)
from adcp.reporting._testing_lifecycle import (
    ReportingLifecycleSnapshot,
    assert_reporting_lifecycle_replay,
    capture_reporting_lifecycle,
)
from adcp.reporting.conformance import run_reporting_source_replay_conformance
from adcp.reporting.inline_source import (
    InlineFetchResult,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
    ReportingSealStore,
    ReportingStagingStore,
)
from adcp.reporting.service import ReportingAdapter
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    ReportingSourceSliceRequestV1,
    SourceBatchManifestV1,
)

__all__ = [
    "DeterministicReportingClock",
    "FaultInjectingReportingSealStore",
    "FaultInjectingReportingStagingStore",
    "ReportingFailurePlan",
    "ReportingLifecycleSnapshot",
    "ReportingTestBarrier",
    "ScriptedReportingAdapter",
    "assert_reporting_lifecycle_replay",
    "capture_reporting_lifecycle",
    "run_reporting_adapter_conformance",
]


class DeterministicReportingClock:
    """A small aware UTC clock that tests can advance without sleeping."""

    def __init__(self, now: datetime) -> None:
        self.set(now)

    def __call__(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("DeterministicReportingClock requires an aware datetime")
        self._now = value.astimezone(timezone.utc)
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("a deterministic reporting clock cannot move backward")
        return self.set(self._now + delta)


class ScriptedReportingAdapter:
    """Queue source answers or exceptions for deterministic service tests.

    Set ``asynchronous=True`` to exercise the async provider-SDK path.  The
    default synchronous path is run in a worker thread by
    :class:`InlineReportingSource`, just like a synchronous production SDK.
    With ``failures``, each call visits ``fetch.before`` before consuming its
    queued answer and ``fetch.after`` after a successful answer. These points
    accept either an exception or a :class:`ReportingTestBarrier`.
    """

    def __init__(
        self,
        capabilities: ReportingSourceCapabilitiesV1,
        results: Sequence[
            InlineFetchResult | Sequence[Mapping[str, Any]] | None | BaseException
        ] = (),
        *,
        asynchronous: bool = False,
        failures: ReportingFailurePlan | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._results = deque(results)
        self._lock = threading.Lock()
        self._failures = failures
        self.asynchronous = asynchronous
        self.calls: list[ReportingSourceSliceRequestV1] = []

    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        return self._capabilities

    def queue(
        self,
        result: InlineFetchResult | Sequence[Mapping[str, Any]] | None | BaseException,
    ) -> None:
        with self._lock:
            self._results.append(result)

    def _next(self) -> Any:
        with self._lock:
            if not self._results:
                raise AssertionError("ScriptedReportingAdapter has no queued source answer")
            result = self._results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def fetch_slice(self, request: ReportingSourceSliceRequestV1) -> Any:
        if not self.asynchronous:
            with self._lock:
                self.calls.append(request)
            if self._failures is not None:
                self._failures.hit_sync("fetch.before")
            result = self._next()
            if self._failures is not None:
                self._failures.hit_sync("fetch.after")
            return result

        async def fetch_async() -> Any:
            with self._lock:
                self.calls.append(request)
            if self._failures is not None:
                await self._failures.hit("fetch.before")
            result = self._next()
            if self._failures is not None:
                await self._failures.hit("fetch.after")
            return result

        return fetch_async()


async def run_reporting_adapter_conformance(
    adapter: ReportingAdapter,
    request: ReportingSourceSliceRequestV1,
    *,
    clock: Callable[[], datetime] | None = None,
    staging: ReportingStagingStore | None = None,
    seals: ReportingSealStore | None = None,
) -> SourceBatchManifestV1:
    """Wrap one minimal adapter and run the SDK's full replay conformance.

    The adapter needs only ``capabilities`` and ``fetch_slice``.  This helper
    uses fresh memory stores by default. Supply retained memory stores or
    freshly constructed PostgreSQL stores to test recovery with the same
    request; create their schemas first. Stores and the adapter are borrowed.
    The same clock drives both execution timestamps and conformance deadlines.
    """
    staging = staging if staging is not None else InMemoryStagingStore()
    seals = seals if seals is not None else InMemorySealStore()
    test_clock = clock or DeterministicReportingClock(request.period.source_read_cutoff_at)
    executor = InlineReportingSource(
        capabilities=adapter.capabilities,
        fetch=adapter.fetch_slice,
        staging=staging,
        seals=seals,
        clock=test_clock,
    )
    return await run_reporting_source_replay_conformance(
        executor=executor,
        request=request,
        object_reader=staging,
        clock=test_clock,
    )
