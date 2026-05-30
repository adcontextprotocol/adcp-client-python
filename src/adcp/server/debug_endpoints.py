"""ASGI middleware exposing opt-in debug endpoints.

The endpoint is the storyboard runner's window into the seller's
anti-façade traffic counters — an empty dict means the platform never
called its upstream ad server, which is the textbook façade-adapter
failure mode AdCP's anti-façade contract is designed to catch.

The middleware composes outside the MCP / A2A apps so a GET to
``/_debug/traffic`` and ``/_debug/sessions`` short-circuit before either
inner app sees them.
Other paths pass through unchanged.

Production deployments stay closed: the middleware is only wired when
``serve(enable_debug_endpoints=True)`` is set. When unset, no route is
mounted and debug requests fall through to the inner app, which returns
a normal 404.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class DebugTrafficMiddleware:
    """ASGI middleware that handles ``GET /_debug/traffic`` and sessions.

    :param app: The downstream ASGI app.
    :param traffic_source: A zero-arg callable returning the current
        per-method count snapshot. Typically ``mock_ad_server.get_traffic``.
        Called fresh on every request — no caching, since the whole
        point is real-time visibility into upstream calls.
    :param session_count_source: A zero-arg callable returning the current
        MCP session snapshot for ``GET /_debug/sessions``. Typically
        ``lambda: get_mcp_session_stats(mcp).as_dict()``.
    :param debug_validate_request: Optional callable that receives the
        request headers and returns whether the debug route may respond.
    :param debug_public: When True, debug routes are reachable without
        ``debug_validate_request``. Keep False for network-reachable
        deployments.

    Other request methods (POST, PUT, etc.) and other paths fall
    through to ``app`` unchanged. HEAD on a debug endpoint is
    served as a 200 with no body (Starlette / uvicorn convention for
    this style of read-only endpoint).
    """

    def __init__(
        self,
        app: Any,
        *,
        traffic_source: Callable[[], dict[str, int]] | None = None,
        session_count_source: Callable[[], dict[str, Any]] | None = None,
        debug_validate_request: Callable[[dict[str, str]], bool] | None = None,
        debug_public: bool = False,
    ) -> None:
        self._app = app
        self._traffic_source = traffic_source
        self._session_count_source = session_count_source
        self._debug_validate_request = debug_validate_request
        self._debug_public = debug_public

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path")
        if path == "/_debug/traffic":
            await self._handle_debug_route(
                scope,
                receive,
                send,
                source=self._traffic_source,
            )
            return
        if path == "/_debug/sessions":
            await self._handle_debug_route(
                scope,
                receive,
                send,
                source=self._session_count_source,
            )
            return

        await self._app(scope, receive, send)

    async def _handle_debug_route(
        self,
        scope: Any,
        receive: Any,
        send: Any,
        *,
        source: Callable[[], dict[str, Any]] | None,
    ) -> None:
        method = scope.get("method", "GET").upper()
        if source is None:
            await _send_json(send, status=404, body=None)
            return
        if not self._debug_public:
            headers = _headers_from_scope(scope)
            if self._debug_validate_request is None or not self._debug_validate_request(headers):
                await _send_json(send, status=404, body=None)
                return
        if method not in {"GET", "HEAD"}:
            # Method-not-allowed — still own the route so a buggy
            # POST doesn't fall through to MCP / A2A and produce a
            # confusing transport-level error. This runs after auth so
            # unauthenticated probes don't learn the route exists.
            await _send_json(send, status=405, body=None, extra_headers=[(b"allow", b"GET, HEAD")])
            return

        snapshot = source()
        # Defensive copy — the caller's snapshot might already be a
        # fresh dict, but if an adopter wires a custom source that
        # returns its internal dict, we don't want json.dumps to
        # observe a concurrent mutation mid-serialize.
        body = None if method == "HEAD" else dict(snapshot)
        await _send_json(send, status=200, body=body)


def _headers_from_scope(scope: Any) -> dict[str, str]:
    """Decode ASGI headers into a lower-case mapping for debug auth hooks."""
    headers: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers", []):
        key = raw_key.decode("latin-1").lower()
        # Last value wins: common reverse-proxy setups append trusted
        # headers after client-supplied values.
        headers[key] = raw_value.decode("latin-1")
    return headers


async def _send_json(
    send: Any,
    *,
    status: int,
    body: dict[str, Any] | None,
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
