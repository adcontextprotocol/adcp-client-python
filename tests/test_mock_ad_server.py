"""Tests for :mod:`adcp.decisioning.mock_ad_server` and the
``/_debug/traffic`` endpoint.

Covers the anti-façade contract surface (issue #383):

* Protocol compliance — ``InMemoryMockAdServer`` satisfies
  :class:`MockAdServer`.
* Counter semantics — ``record_call`` increments per-method,
  ``get_traffic`` returns a snapshot, ``reset`` clears.
* Thread safety — concurrent ``record_call`` invocations from many
  threads land the right total count.
* Wire endpoint — ``GET /_debug/traffic`` returns the recorder's
  snapshot when ``enable_debug_endpoints=True``; returns 404 when
  the flag is off.
* Smoke — record a few calls, hit the endpoint, observe the counts.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from adcp.decisioning.mock_ad_server import (
    InMemoryMockAdServer,
    MockAdServer,
)
from adcp.server.debug_endpoints import DebugTrafficMiddleware

# ---------------------------------------------------------------------------
# Protocol compliance + counter semantics
# ---------------------------------------------------------------------------


def test_in_memory_satisfies_protocol() -> None:
    """``InMemoryMockAdServer`` must satisfy the runtime-checkable
    :class:`MockAdServer` Protocol so adopters can pass either the
    default impl or their own without static-typing friction."""
    recorder = InMemoryMockAdServer()
    assert isinstance(recorder, MockAdServer)


def test_record_call_increments_per_method() -> None:
    recorder = InMemoryMockAdServer()
    recorder.record_call("creative.upload", {"count": 1})
    recorder.record_call("creative.upload", {"count": 2})
    recorder.record_call("media_buy.create", {"id": "mb_1"})
    traffic = recorder.get_traffic()
    assert traffic == {"creative.upload": 2, "media_buy.create": 1}


def test_get_traffic_returns_fresh_snapshot() -> None:
    """Mutating the returned dict must not affect subsequent reads —
    storyboard runners often pop counts off snapshots between
    assertions."""
    recorder = InMemoryMockAdServer()
    recorder.record_call("foo", {})
    snapshot_a = recorder.get_traffic()
    snapshot_a["foo"] = 9999
    snapshot_a["bar"] = 1
    snapshot_b = recorder.get_traffic()
    assert snapshot_b == {"foo": 1}


def test_reset_clears_counters() -> None:
    recorder = InMemoryMockAdServer()
    recorder.record_call("a", {})
    recorder.record_call("b", {})
    recorder.reset()
    assert recorder.get_traffic() == {}
    # Still functional after reset.
    recorder.record_call("a", {})
    assert recorder.get_traffic() == {"a": 1}


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_record_call_is_thread_safe() -> None:
    """Many threads calling ``record_call`` concurrently must land the
    exact total count — without a lock, the dict ``+= 1`` race drops
    increments and the test fails intermittently. We use 16 threads ×
    1000 calls so the race window is wide enough to surface a missing
    lock on every CI run."""
    recorder = InMemoryMockAdServer()
    n_threads = 16
    n_calls_per_thread = 1000

    def hammer() -> None:
        for _ in range(n_calls_per_thread):
            recorder.record_call("hot", {})

    threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert recorder.get_traffic() == {"hot": n_threads * n_calls_per_thread}


# ---------------------------------------------------------------------------
# Wire endpoint — DebugTrafficMiddleware
# ---------------------------------------------------------------------------


async def _passthrough_app(scope: Any, receive: Any, send: Any) -> None:
    """Inner app used as the next-hop downstream in middleware tests.

    Returns 404 with a marker body so test cases can distinguish
    "fell through to inner app" (expected when path != /_debug/traffic)
    from "middleware handled the request"."""
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"inner-fallthrough"})


async def _drive(
    app: Any,
    *,
    method: str = "GET",
    path: str = "/_debug/traffic",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Drive an ASGI app once with a synthetic HTTP request.

    Returns ``(status, headers, body)``. Headers are decoded to a
    plain ``dict[str, str]`` for easier assertion."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "headers": headers or [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("test", 1234),
        "http_version": "1.1",
    }

    received: dict[str, Any] = {"status": None, "headers": [], "body": b""}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
            received["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            received["body"] += message.get("body", b"")

    await app(scope, receive, send)
    headers = {k.decode("ascii"): v.decode("ascii") for k, v in received["headers"]}
    return received["status"], headers, received["body"]


@pytest.mark.asyncio
async def test_debug_endpoint_returns_traffic_json() -> None:
    recorder = InMemoryMockAdServer()
    recorder.record_call("creative.upload", {"count": 3})
    recorder.record_call("media_buy.create", {})
    app = DebugTrafficMiddleware(
        _passthrough_app, traffic_source=recorder.get_traffic, debug_public=True
    )

    status, headers, body = await _drive(app)
    assert status == 200
    assert headers["content-type"] == "application/json"
    payload = json.loads(body)
    assert payload == {"creative.upload": 1, "media_buy.create": 1}


@pytest.mark.asyncio
async def test_debug_endpoint_handles_head_with_empty_body() -> None:
    recorder = InMemoryMockAdServer()
    app = DebugTrafficMiddleware(
        _passthrough_app, traffic_source=recorder.get_traffic, debug_public=True
    )
    status, headers, body = await _drive(app, method="HEAD")
    assert status == 200
    assert body == b""
    assert headers["content-length"] == "0"


@pytest.mark.asyncio
async def test_debug_endpoint_rejects_post_with_405() -> None:
    """POST on ``/_debug/traffic`` is owned by the middleware (so it
    doesn't fall through to MCP / A2A and emit a confusing transport
    error). Returns 405 with an ``Allow`` header."""
    recorder = InMemoryMockAdServer()
    app = DebugTrafficMiddleware(
        _passthrough_app, traffic_source=recorder.get_traffic, debug_public=True
    )
    status, headers, _ = await _drive(app, method="POST")
    assert status == 405
    assert headers["allow"] == "GET, HEAD"


@pytest.mark.asyncio
async def test_debug_endpoint_hides_method_gate_until_authorized() -> None:
    app = DebugTrafficMiddleware(
        _passthrough_app,
        traffic_source=lambda: {"ok": 1},
        debug_validate_request=lambda headers: headers.get("x-debug-token") == "secret",
    )

    denied, denied_headers, _ = await _drive(app, method="POST")
    assert denied == 404
    assert "allow" not in denied_headers

    allowed, allowed_headers, _ = await _drive(
        app,
        method="POST",
        headers=[(b"x-debug-token", b"secret")],
    )
    assert allowed == 405
    assert allowed_headers["allow"] == "GET, HEAD"


@pytest.mark.asyncio
async def test_debug_sessions_endpoint_returns_session_json() -> None:
    app = DebugTrafficMiddleware(
        _passthrough_app,
        session_count_source=lambda: {
            "active_sessions": 2,
            "max_active_sessions": 5,
            "sessions_created_last_60s": 3,
        },
        debug_public=True,
    )

    status, headers, body = await _drive(app, path="/_debug/sessions")
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == {
        "active_sessions": 2,
        "max_active_sessions": 5,
        "sessions_created_last_60s": 3,
    }


@pytest.mark.asyncio
async def test_unconfigured_debug_endpoint_returns_404() -> None:
    app = DebugTrafficMiddleware(_passthrough_app, traffic_source=lambda: {}, debug_public=True)
    status, _, body = await _drive(app, path="/_debug/sessions")
    assert status == 404
    assert body == b""


@pytest.mark.asyncio
async def test_debug_endpoint_requires_validator_when_not_public() -> None:
    app = DebugTrafficMiddleware(
        _passthrough_app,
        traffic_source=lambda: {"ok": 1},
        debug_validate_request=lambda headers: headers.get("x-debug-token") == "secret",
    )

    denied, _, denied_body = await _drive(app)
    assert denied == 404
    assert denied_body == b""

    allowed, _, allowed_body = await _drive(
        app,
        headers=[(b"x-debug-token", b"secret")],
    )
    assert allowed == 200
    assert json.loads(allowed_body) == {"ok": 1}


@pytest.mark.asyncio
async def test_debug_endpoint_uses_last_duplicate_header_value() -> None:
    app = DebugTrafficMiddleware(
        _passthrough_app,
        traffic_source=lambda: {"ok": 1},
        debug_validate_request=lambda headers: headers.get("x-debug-token") == "secret",
    )

    status, _, body = await _drive(
        app,
        headers=[
            (b"x-debug-token", b"attacker"),
            (b"x-debug-token", b"secret"),
        ],
    )

    assert status == 200
    assert json.loads(body) == {"ok": 1}


@pytest.mark.asyncio
async def test_debug_endpoint_passes_through_other_paths() -> None:
    """Any path other than ``/_debug/traffic`` reaches the inner
    app unchanged — the middleware must not swallow normal traffic."""
    recorder = InMemoryMockAdServer()
    app = DebugTrafficMiddleware(
        _passthrough_app, traffic_source=recorder.get_traffic, debug_public=True
    )
    status, _, body = await _drive(app, path="/mcp")
    assert status == 404
    assert body == b"inner-fallthrough"


# ---------------------------------------------------------------------------
# Wire-up via serve()'s _prepend_debug_endpoint helper
# ---------------------------------------------------------------------------


def test_serve_skips_debug_when_disabled() -> None:
    """When ``enable_debug_endpoints=False``, no debug middleware is
    added to the asgi_middleware sequence."""
    from adcp.server.serve import _prepend_debug_endpoint

    result = _prepend_debug_endpoint(
        None,
        enable_debug_endpoints=False,
        debug_traffic_source=lambda: {"x": 1},
    )
    assert result is None


def test_serve_requires_debug_source_when_enabled() -> None:
    """``enable_debug_endpoints=True`` without a source must fail at
    boot — silently mounting an endpoint that errors on every request
    is worse than a clear configuration error."""
    from adcp.server.serve import _prepend_debug_endpoint

    with pytest.raises(ValueError, match="debug_traffic_source"):
        _prepend_debug_endpoint(
            None,
            enable_debug_endpoints=True,
            debug_traffic_source=None,
        )


def test_serve_accepts_session_source_without_traffic_source() -> None:
    from adcp.server.serve import _prepend_debug_endpoint

    result = _prepend_debug_endpoint(
        None,
        enable_debug_endpoints=True,
        debug_traffic_source=None,
        session_count_source=lambda: {"active_sessions": 0},
        debug_public=True,
    )
    assert result is not None
    assert result[0][1]["traffic_source"] is None
    assert result[0][1]["session_count_source"] is not None


def test_serve_requires_debug_auth_unless_public() -> None:
    from adcp.server.serve import _prepend_debug_endpoint

    with pytest.raises(ValueError, match="debug_validate_request"):
        _prepend_debug_endpoint(
            None,
            enable_debug_endpoints=True,
            debug_traffic_source=lambda: {},
        )


def test_serve_prepends_debug_middleware_when_enabled() -> None:
    """When enabled, the debug middleware lands at index 0 — so a
    runner's ``GET /_debug/traffic`` short-circuits before any
    seller-supplied middleware (auth, tenant resolution) runs."""
    from adcp.server.serve import _prepend_debug_endpoint

    class _DummyMiddleware:
        def __init__(self, app: Any, **_: Any) -> None:
            self.app = app

    seller_middleware = [(_DummyMiddleware, {})]
    result = _prepend_debug_endpoint(
        seller_middleware,
        enable_debug_endpoints=True,
        debug_traffic_source=lambda: {},
        session_count_source=lambda: {"active_sessions": 0},
        debug_public=True,
    )
    assert result is not None
    assert len(result) == 2
    assert result[0][0] is DebugTrafficMiddleware
    assert result[1][0] is _DummyMiddleware


# ---------------------------------------------------------------------------
# End-to-end smoke — record a few calls, hit the endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_record_then_observe_via_endpoint() -> None:
    """The whole cycle: simulate a few sync_creatives calls, then
    poll ``GET /_debug/traffic`` and assert the counts. This is the
    storyboard runner's exact use case."""
    recorder = InMemoryMockAdServer()
    app = DebugTrafficMiddleware(
        _passthrough_app, traffic_source=recorder.get_traffic, debug_public=True
    )

    # Simulate three sync_creatives calls and one create_media_buy.
    for _ in range(3):
        recorder.record_call("creative.upload", {"count": 1})
    recorder.record_call("media_buy.create", {"id": "mb_x"})

    status, _, body = await _drive(app)
    assert status == 200
    assert json.loads(body) == {"creative.upload": 3, "media_buy.create": 1}

    # Reset clears the snapshot the runner sees on the next poll.
    recorder.reset()
    _, _, body2 = await _drive(app)
    assert json.loads(body2) == {}
