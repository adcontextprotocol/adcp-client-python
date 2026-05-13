"""Tests for ``serve(on_startup=..., on_shutdown=...)`` (issue #709).

Covers the ``transport="both"`` path — the only transport that honors
the hooks today — and the boot-time guard that prevents silent
mis-wiring on single-transport paths.
"""

from __future__ import annotations

import pytest

starlette = pytest.importorskip("starlette")

from starlette.testclient import TestClient

from adcp.server import ADCPHandler, ToolContext
from adcp.server.responses import capabilities_response
from adcp.server.serve import _build_mcp_and_a2a_app


class _Handler(ADCPHandler[ToolContext]):
    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"])


def _build_app(*, on_startup=None, on_shutdown=None):
    return _build_mcp_and_a2a_app(
        _Handler(),
        name="lifespan-test",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
    )


# ----- Happy path -------------------------------------------------------


def test_on_startup_fires_after_framework_startup() -> None:
    """``on_startup`` hooks run after both inner MCP and A2A
    lifespans have entered — the FastMCP session-manager task group
    is already initialized when user hooks run, so user code can
    safely use it."""
    events: list[str] = []

    async def hook_a() -> None:
        events.append("a")

    async def hook_b() -> None:
        events.append("b")

    app = _build_app(on_startup=[hook_a, hook_b])
    with TestClient(app) as client:
        # Sanity: requests still work — confirms framework lifespans
        # also ran successfully.
        resp = client.get("/.well-known/agent.json")
        assert resp.status_code == 200

    assert events == ["a", "b"]


def test_on_shutdown_fires_in_order_after_yield() -> None:
    """``on_shutdown`` hooks run when the parent lifespan exits — in
    order, and before the inner framework lifespans tear down."""
    events: list[str] = []

    async def hook_a() -> None:
        events.append("a")

    async def hook_b() -> None:
        events.append("b")

    app = _build_app(on_shutdown=[hook_a, hook_b])
    with TestClient(app):
        assert events == []  # not fired during request phase
    assert events == ["a", "b"]


def test_startup_and_shutdown_ordering_around_yield() -> None:
    """Combined: startup hooks fire before the yield (during boot);
    shutdown hooks fire after the yield (during teardown). Inside
    the ``with`` block, only startup events are visible."""
    events: list[str] = []

    async def startup() -> None:
        events.append("startup")

    async def shutdown() -> None:
        events.append("shutdown")

    app = _build_app(on_startup=[startup], on_shutdown=[shutdown])
    with TestClient(app):
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]


# ----- Failure modes ----------------------------------------------------


def test_startup_hook_failure_aborts_boot() -> None:
    """A startup hook raising propagates out of the parent lifespan
    — TestClient surfaces it on context entry (either directly or
    wrapped in an ExceptionGroup from FastMCP's task-group), mirroring
    what uvicorn does in production (process exits). What we care
    about is that the original error message reaches the caller; the
    exact wrapping is an asyncio detail."""

    async def boom() -> None:
        raise RuntimeError("boot-time wiring broke")

    app = _build_app(on_startup=[boom])
    with pytest.raises(BaseException) as exc_info:
        with TestClient(app):
            pass
    # Walk the exception chain (including ExceptionGroup leaves) for
    # our marker. Whatever the framing, the cause must be visible.
    assert "boot-time wiring broke" in _flatten_exception_text(exc_info.value)


def _flatten_exception_text(exc: BaseException) -> str:
    """Collect ``str(exc)`` plus every cause / context / group leaf."""
    parts: list[str] = []
    seen: set[int] = set()

    def walk(e: BaseException | None) -> None:
        if e is None or id(e) in seen:
            return
        seen.add(id(e))
        parts.append(str(e))
        walk(e.__cause__)
        walk(e.__context__)
        for sub in getattr(e, "exceptions", ()) or ():
            walk(sub)

    walk(exc)
    return "\n".join(parts)


def test_shutdown_hooks_all_attempted_when_one_raises() -> None:
    """Adopters wiring multiple cleanup hooks (DB close, scheduler
    stop, queue drain) want all of them attempted on a best-effort
    basis — a failure in one must not abort the rest. The first
    error re-raises so Starlette surfaces it; later errors land in
    logs.

    Verifies BOTH halves of the contract:
    1. every hook ran (via ``events`` list)
    2. the first error actually propagated out (caught here and
       inspected for the marker message)
    """
    events: list[str] = []

    async def first_ok() -> None:
        events.append("first")

    async def middle_raises() -> None:
        events.append("middle")
        raise RuntimeError("scheduler stop failed")

    async def last_ok() -> None:
        events.append("last")

    app = _build_app(on_shutdown=[first_ok, middle_raises, last_ok])
    raised: BaseException | None = None
    try:
        with TestClient(app):
            pass
    except BaseException as exc:
        raised = exc

    assert events == ["first", "middle", "last"]
    assert raised is not None, (
        "TestClient context exit should have surfaced the first " "shutdown hook error"
    )
    assert "scheduler stop failed" in _flatten_exception_text(raised)


# ----- Boot-time validation --------------------------------------------


def test_on_startup_rejected_on_single_transport_paths() -> None:
    """Lifespan hooks ship only for ``transport='both'`` today.
    Passing them with another transport raises ValueError at boot
    rather than silently dropping the hooks at runtime."""
    from adcp.server import serve

    async def hook() -> None:
        pass

    for transport in ("streamable-http", "sse", "a2a", "stdio"):
        with pytest.raises(ValueError, match="transport='both'"):
            serve(_Handler(), transport=transport, on_startup=[hook])
        with pytest.raises(ValueError, match="transport='both'"):
            serve(_Handler(), transport=transport, on_shutdown=[hook])


def test_no_hooks_is_a_no_op() -> None:
    """``on_startup=None`` / ``on_shutdown=None`` (the defaults) must
    not change anything observable about the unified app's lifespan
    composition. Belt-and-suspenders regression guard."""
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/.well-known/agent.json")
        assert resp.status_code == 200


# ----- ServeConfig surface ----------------------------------------------


def test_serveconfig_passes_hooks_through() -> None:
    """``ServeConfig(on_startup=..., on_shutdown=...)`` propagates
    into ``_serve_mcp_and_a2a`` via the config-unwrap branch of
    ``serve()``. Verify by calling ``serve()`` with a single-transport
    bundle and confirming the boot-time guard fires — that proves the
    field actually reached the dispatch path rather than being silently
    swallowed during config extraction."""
    from adcp.server import serve
    from adcp.server.serve import ServeConfig

    async def hook() -> None:
        pass

    cfg = ServeConfig(transport="streamable-http", on_startup=[hook])
    with pytest.raises(ValueError, match="transport='both'"):
        serve(_Handler(), config=cfg)
