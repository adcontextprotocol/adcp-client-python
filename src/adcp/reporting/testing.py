"""Deterministic test helpers for reporting source adapters.

Adapter authors can exercise the exact production wrapper without constructing
manifests or staged objects themselves.  The conformance runner executes the
same frozen slice twice and verifies replay identity plus every staged byte.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from adcp.reporting.conformance import run_reporting_source_replay_conformance
from adcp.reporting.inline_source import (
    InlineFetchResult,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.service import ReportingAdapter
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    ReportingSourceSliceRequestV1,
    SourceBatchManifestV1,
)

__all__ = [
    "DeterministicReportingClock",
    "ScriptedReportingAdapter",
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
    """

    def __init__(
        self,
        capabilities: ReportingSourceCapabilitiesV1,
        results: Sequence[
            InlineFetchResult | Sequence[Mapping[str, Any]] | None | BaseException
        ] = (),
        *,
        asynchronous: bool = False,
    ) -> None:
        self._capabilities = capabilities
        self._results = deque(results)
        self.asynchronous = asynchronous
        self.calls: list[ReportingSourceSliceRequestV1] = []

    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        return self._capabilities

    def queue(
        self,
        result: InlineFetchResult | Sequence[Mapping[str, Any]] | None | BaseException,
    ) -> None:
        self._results.append(result)

    def _next(self, request: ReportingSourceSliceRequestV1) -> Any:
        self.calls.append(request)
        if not self._results:
            raise AssertionError("ScriptedReportingAdapter has no queued source answer")
        result = self._results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def fetch_slice(self, request: ReportingSourceSliceRequestV1) -> Any:
        if not self.asynchronous:
            return self._next(request)

        async def fetch_async() -> Any:
            return self._next(request)

        return fetch_async()


async def run_reporting_adapter_conformance(
    adapter: ReportingAdapter,
    request: ReportingSourceSliceRequestV1,
    *,
    clock: DeterministicReportingClock | None = None,
) -> SourceBatchManifestV1:
    """Wrap one minimal adapter and run the SDK's full replay conformance.

    The adapter needs only ``capabilities`` and ``fetch_slice``.  This helper
    intentionally uses the in-memory staging and seal stores: it grades adapter
    semantics, while durable-store conformance remains a separate concern.
    """
    staging = InMemoryStagingStore()
    executor = InlineReportingSource(
        capabilities=adapter.capabilities,
        fetch=adapter.fetch_slice,
        staging=staging,
        seals=InMemorySealStore(),
        clock=clock or DeterministicReportingClock(request.period.source_read_cutoff_at),
    )
    return await run_reporting_source_replay_conformance(
        executor=executor,
        request=request,
        object_reader=staging,
    )
