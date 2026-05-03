"""Anti-façade traffic counters for adopter platforms.

AdCP's anti-façade contract (PR #3816 in the spec) catches platform
methods that return spec-valid envelopes without doing any underlying
work — ``sync_creatives`` returning ``[]``, ``create_media_buy``
fabricating an id without persisting it, ``get_media_buy_delivery``
returning empty arrays. Storyboard runners need a way to *prove* that
the seller actually called its upstream ad server / database / queue,
not just produced shapes.

This module gives adopters a small Protocol they wire into their
platform impl and a ``/_debug/traffic`` HTTP endpoint runners can poll.
The platform method calls ``mock_ad_server.record_call("creative.upload",
{...})`` before returning; the runner asserts ``traffic["creative.upload"]
> 0`` after a sync_creatives storyboard step.

It composes with framework-side inbound traffic recording (issue #347):
the mock ad server counts *outbound* (platform → upstream) calls, and
the framework middleware counts *inbound* (buyer → server) calls. Both
expose JSON over the same ``/_debug/traffic`` surface.

Production deployments stay closed: the endpoint is gated behind a
``serve()`` kwarg that defaults to ``False``. Reference / dev sellers
flip it on; production sellers leave it off and the endpoint returns
404.

Example
-------

::

    from adcp.decisioning.mock_ad_server import (
        InMemoryMockAdServer, MockAdServer,
    )

    class MyPlatform(DecisioningPlatform, SalesPlatform):
        def __init__(self, *, mock_ad_server: MockAdServer | None = None):
            self._mock = mock_ad_server

        async def sync_creatives(self, req, ctx):
            # ... call upstream ad server, persist, etc. ...
            if self._mock is not None:
                self._mock.record_call(
                    "creative.upload",
                    {"count": len(req.creatives)},
                )
            return SyncCreativesSuccessResponse(creatives=[...])

    serve(
        MyPlatform(mock_ad_server=InMemoryMockAdServer()),
        enable_debug_endpoints=True,
    )
"""

from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MockAdServer(Protocol):
    """Outbound-traffic recorder for adopter platform methods.

    Implementations count named upstream calls so storyboard runners
    can assert the platform actually did work (anti-façade contract).
    The Protocol is deliberately minimal — ``record_call`` /
    ``get_traffic`` / ``reset`` — so adopters can swap in a counter
    backed by Prometheus, statsd, or a real ad-server SDK without
    changing the platform's call sites.
    """

    def record_call(self, method: str, args: dict[str, Any]) -> None:
        """Increment the counter for ``method``.

        :param method: A dotted name identifying the upstream operation
            — e.g. ``"creative.upload"``, ``"media_buy.create"``,
            ``"delivery.read"``. Adopters pick the namespace; runners
            assert against whatever the platform records.
        :param args: A dict of call arguments for diagnostic / audit
            purposes. Implementations MAY ignore the dict (the default
            in-memory impl does — only the count matters for
            anti-façade assertions). Implementations that persist or
            log args MUST treat them as untrusted: they may carry
            buyer-supplied content.
        """
        ...

    def get_traffic(self) -> dict[str, int]:
        """Return a snapshot of current per-method counts.

        Return value is a fresh dict — callers may mutate it without
        affecting subsequent reads. Order is implementation-defined.
        """
        ...

    def reset(self) -> None:
        """Clear all counters. Test-isolation hook — storyboard runners
        call this between scenarios so previous-step calls don't leak
        into the next assertion."""
        ...


class InMemoryMockAdServer:
    """Default thread-safe :class:`MockAdServer` implementation.

    Uses a ``threading.Lock`` rather than an ``asyncio.Lock`` because
    the recorder is called from both async platform methods AND from
    sync platform methods (which the framework dispatches on a
    ``ThreadPoolExecutor``). A threading lock is correct in both
    contexts; an asyncio lock would deadlock when acquired from a
    sync method on a worker thread that has no running event loop.
    Contention is negligible — every operation is an in-memory dict
    increment, microseconds at most.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def record_call(self, method: str, args: dict[str, Any]) -> None:
        # ``args`` is intentionally ignored — the count is what matters
        # for anti-façade assertions, and persisting buyer-supplied
        # args in-memory across requests is a footgun (PII retention,
        # unbounded growth). Adopters who want full call records wire
        # their own MockAdServer impl.
        del args
        with self._lock:
            self._counts[method] = self._counts.get(method, 0) + 1

    def get_traffic(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


__all__ = [
    "InMemoryMockAdServer",
    "MockAdServer",
]
