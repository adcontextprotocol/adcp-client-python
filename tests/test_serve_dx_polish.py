"""DX polish for adcp.server.serve — port-collision remediation +
trailing-slash path normalization.

Both surfaced as cross-cutting friction in 2 of 4 Emma backend tests:

* Default port=3001 collides silently. Raw ``OSError: [Errno 48]
  Address already in use`` deep in ``_bind_reusable_socket`` is what
  buyers see — no remediation hint, no breadcrumb to ``port=`` /
  ``ADCP_PORT``.
* MCP streamable-http mounts at ``/mcp`` (no trailing slash). Buyer
  libraries POSTing to ``/mcp/`` get a 307 redirect; libs that don't
  follow redirects on POST silently break (lose the body when the
  redirect rewrites POST → GET).

These tests exercise both fixes at the ASGI layer (path normalize)
and at the bind layer (port-collision projection).
"""

from __future__ import annotations

import errno
import socket
from typing import Any

import pytest

from adcp.server.serve import _bind_reusable_socket, _wrap_with_path_normalize

# ---- _bind_reusable_socket EADDRINUSE remediation ----


def test_bind_reusable_socket_friendly_eaddrinuse() -> None:
    """When the requested port is bound by another process, the raw
    ``OSError`` gets projected to a remediation-bearing message that
    points at ``port=`` / ``ADCP_PORT``. Without this fix, every Emma
    backend tester wasted ~10 minutes hunting a 30-second
    misconfiguration."""
    # Bind a holder socket on an ephemeral port to guarantee
    # EADDRINUSE on the second bind.
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    busy_port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError) as exc_info:
            _bind_reusable_socket("127.0.0.1", busy_port)
    finally:
        holder.close()

    msg = str(exc_info.value)
    # The remediation must cite both the port number AND a concrete
    # next step the adopter can take (port= or ADCP_PORT). Otherwise
    # we've just rewritten the error without making it actionable.
    assert str(busy_port) in msg
    assert "port=" in msg or "ADCP_PORT" in msg
    assert exc_info.value.errno == errno.EADDRINUSE


def test_bind_reusable_socket_other_oserror_passes_through() -> None:
    """Errors that aren't EADDRINUSE (permission denied, address not
    available, etc.) MUST pass through unchanged so adopters debugging
    a different problem don't get a misleading port-collision message
    pointing at the wrong fix."""
    # 0.0.0.0 is bindable; -1 isn't (negative port). The error class
    # depends on the platform — we assert pass-through behavior, NOT
    # a specific errno.
    with pytest.raises((OverflowError, OSError)) as exc_info:
        _bind_reusable_socket("127.0.0.1", -1)
    msg = str(exc_info.value)
    # Specifically MUST NOT carry the EADDRINUSE remediation phrase
    # — that would be a false flag pointing the adopter at the wrong
    # knob.
    assert "ADCP_PORT" not in msg
    assert "Pick a different port" not in msg


# ---- _wrap_with_path_normalize trailing-slash strip ----


class _CapturingApp:
    """Minimal ASGI app that records the path it was invoked with."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.raw_paths: list[Any] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.paths.append(scope.get("path", ""))
        self.raw_paths.append(scope.get("raw_path"))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


@pytest.mark.asyncio
async def test_path_normalize_strips_trailing_slash_on_mcp() -> None:
    """``/mcp/`` → ``/mcp`` BEFORE the inner app sees it. No 307,
    body preserved (the app handles the request inline)."""
    app = _CapturingApp()
    wrapped = _wrap_with_path_normalize(app)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "raw_path": b"/mcp/",
        "headers": [],
    }

    async def _recv() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def _send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await wrapped(scope, _recv, _send)
    assert app.paths == ["/mcp"]
    assert app.raw_paths == [b"/mcp"]
    # Inner app still runs (the wrapped middleware doesn't 307).
    assert any(m["type"] == "http.response.start" for m in sent)


@pytest.mark.asyncio
async def test_path_normalize_leaves_root_slash_alone() -> None:
    """The root path ``/`` MUST NOT be rewritten to ``''`` — that
    would route a health check to the empty string and cause 404s.
    The middleware only touches non-root trailing slashes."""
    app = _CapturingApp()
    wrapped = _wrap_with_path_normalize(app)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "headers": [],
    }

    async def _recv() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def _send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await wrapped(scope, _recv, _send)
    assert app.paths == ["/"]


@pytest.mark.asyncio
async def test_path_normalize_passes_through_path_without_trailing_slash() -> None:
    """``/mcp`` (no trailing slash) is the canonical form — the
    middleware must not touch it, just pass through unchanged."""
    app = _CapturingApp()
    wrapped = _wrap_with_path_normalize(app)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "headers": [],
    }

    async def _recv() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(msg: dict[str, Any]) -> None:
        pass

    await wrapped(scope, _recv, _send)
    assert app.paths == ["/mcp"]


@pytest.mark.asyncio
async def test_path_normalize_does_not_mutate_outer_scope() -> None:
    """The middleware copies the scope before mutating it — without
    this, ASGI's contract that adjacent middlewares get an unmodified
    scope is violated. Regression guard against a future refactor that
    edits scope in-place."""
    app = _CapturingApp()
    wrapped = _wrap_with_path_normalize(app)

    original_scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "raw_path": b"/mcp/",
        "headers": [],
    }
    snapshot = dict(original_scope)

    async def _recv() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(msg: dict[str, Any]) -> None:
        pass

    await wrapped(original_scope, _recv, _send)
    assert original_scope == snapshot, (
        "_wrap_with_path_normalize mutated its outer scope arg; "
        "subsequent middlewares would see the rewritten path"
    )
