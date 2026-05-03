"""ASGI middleware exposing ``GET /_debug/traffic``.

The endpoint is the storyboard runner's window into the seller's
anti-façade traffic counters — an empty dict means the platform never
called its upstream ad server, which is the textbook façade-adapter
failure mode AdCP's anti-façade contract is designed to catch.

The middleware composes outside the MCP / A2A apps so a GET to
``/_debug/traffic`` short-circuits before either inner app sees it.
Other paths pass through unchanged.

Production deployments stay closed: the middleware is only wired when
``serve(enable_debug_endpoints=True)`` is set. When unset, no route is
mounted and a request to ``/_debug/traffic`` falls through to the
inner app, which returns a normal 404.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class DebugTrafficMiddleware:
    """ASGI middleware that handles ``GET /_debug/traffic``.

    :param app: The downstream ASGI app.
    :param traffic_source: A zero-arg callable returning the current
        per-method count snapshot. Typically ``mock_ad_server.get_traffic``.
        Called fresh on every request — no caching, since the whole
        point is real-time visibility into upstream calls.

    Other request methods (POST, PUT, etc.) and other paths fall
    through to ``app`` unchanged. HEAD on ``/_debug/traffic`` is
    served as a 200 with no body (Starlette / uvicorn convention for
    this style of read-only endpoint).
    """

    def __init__(
        self,
        app: Any,
        *,
        traffic_source: Callable[[], dict[str, int]],
    ) -> None:
        self._app = app
        self._traffic_source = traffic_source

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/_debug/traffic":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method not in {"GET", "HEAD"}:
            # Method-not-allowed — still own the route so a buggy
            # POST doesn't fall through to MCP / A2A and produce a
            # confusing transport-level error.
            await _send_json(send, status=405, body=None, extra_headers=[(b"allow", b"GET, HEAD")])
            return

        traffic = self._traffic_source()
        # Defensive copy — the caller's snapshot might already be a
        # fresh dict, but if an adopter wires a custom source that
        # returns its internal dict, we don't want json.dumps to
        # observe a concurrent mutation mid-serialize.
        body = None if method == "HEAD" else dict(traffic)
        await _send_json(send, status=200, body=body)


async def _send_json(
    send: Any,
    *,
    status: int,
    body: dict[str, int] | None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Write a JSON ASGI response (or empty body for 405 / HEAD)."""
    headers: list[tuple[bytes, bytes]] = []
    payload = b""
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(payload)).encode("ascii")))
    else:
        headers.append((b"content-length", b"0"))
    if extra_headers:
        headers.extend(extra_headers)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": payload})


__all__ = ["DebugTrafficMiddleware"]
