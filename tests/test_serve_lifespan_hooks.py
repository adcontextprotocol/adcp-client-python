"""Tests for ``serve(on_startup=..., on_shutdown=...)`` (issue #709).

Covers the ``transport="both"`` path — the only transport that honors
the hooks today — and the boot-time guard that prevents silent
mis-wiring on single-transport paths.
"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from unittest.mock import Mock

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


def test_paired_hooks_share_contextvar_tokens() -> None:
    value: ContextVar[str] = ContextVar("hook_value", default="initial")
    token: Token[str] | None = None
    events: list[str] = []

    async def startup() -> None:
        nonlocal token
        assert value.get() == "parent"
        token = value.set("started")

    async def shutdown() -> None:
        assert value.get() == "started"
        assert token is not None
        value.reset(token)
        events.append(value.get())

    parent_token = value.set("parent")
    try:
        with TestClient(_build_app(on_startup=[startup], on_shutdown=[shutdown])):
            assert value.get() == "parent"
        assert value.get() == "parent"
    finally:
        value.reset(parent_token)
    assert events == ["parent"]


@pytest.mark.parametrize("kind", ["cancel_scope", "task_group"])
def test_paired_hooks_can_enter_and_exit_an_anyio_scope(kind: str) -> None:
    import anyio

    scope = None
    events: list[str] = []

    async def startup() -> None:
        nonlocal scope
        if kind == "cancel_scope":
            scope = anyio.CancelScope()
            scope.__enter__()
        else:
            scope = anyio.create_task_group()
            await scope.__aenter__()
        events.append("entered")

    async def shutdown() -> None:
        assert scope is not None
        if kind == "cancel_scope":
            scope.__exit__(None, None, None)
        else:
            await scope.__aexit__(None, None, None)
        events.append("exited")

    with TestClient(_build_app(on_startup=[startup], on_shutdown=[shutdown])):
        pass
    assert events == ["entered", "exited"]


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


def test_later_startup_failure_closes_started_resources_once() -> None:
    events: list[str] = []

    async def start_resource() -> None:
        events.append("resource_started")

    async def fail_later_startup() -> None:
        events.append("later_startup")
        raise RuntimeError("later startup failed")

    async def close_resource() -> None:
        events.append("resource_closed")

    app = _build_app(
        on_startup=[start_resource, fail_later_startup],
        on_shutdown=[close_resource],
    )
    with pytest.raises(BaseException) as raised:
        with TestClient(app):
            pytest.fail("failed startup must not admit requests")

    assert "later startup failed" in _flatten_exception_text(raised.value)
    assert events == ["resource_started", "later_startup", "resource_closed"]


def test_startup_failure_remains_primary_after_all_cleanup_hooks(caplog) -> None:
    events: list[str] = []

    async def fail_startup() -> None:
        raise ValueError("primary startup failure")

    async def fail_cleanup() -> None:
        events.append("first_cleanup")
        raise RuntimeError("secret-provider-body")

    async def finish_cleanup() -> None:
        events.append("last_cleanup")

    app = _build_app(on_startup=[fail_startup], on_shutdown=[fail_cleanup, finish_cleanup])
    with pytest.raises(BaseException) as raised:
        with TestClient(app):
            pytest.fail("failed startup must not admit requests")

    assert "primary startup failure" in _flatten_exception_text(raised.value)
    assert events == ["first_cleanup", "last_cleanup"]
    assert "secret-provider-body" not in caplog.text


@pytest.mark.parametrize("startup_failure", ["cancelled", "error"])
async def test_startup_failure_settles_cleanup_despite_repeated_cancellation(
    startup_failure: str,
) -> None:
    events: list[str] = []
    later_started = asyncio.Event()
    fail_startup = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    incoming: asyncio.Queue[dict] = asyncio.Queue()
    outgoing: asyncio.Queue[dict] = asyncio.Queue()

    async def first() -> None:
        events.append("started")

    async def later() -> None:
        later_started.set()
        try:
            await fail_startup.wait()
        except asyncio.CancelledError:
            events.append("startup_cancelled")
            raise
        raise ValueError("primary startup failure")

    async def close() -> None:
        cleanup_started.set()
        await allow_cleanup.wait()
        events.append("closed")

    app = _build_app(on_startup=[first, later], on_shutdown=[close])
    task = asyncio.create_task(
        app(
            {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
            incoming.get,
            outgoing.put,
        )
    )
    try:
        await incoming.put({"type": "lifespan.startup"})
        await asyncio.wait_for(later_started.wait(), 5)
        if startup_failure == "cancelled":
            task.cancel()
        else:
            fail_startup.set()
        await asyncio.wait_for(cleanup_started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert "closed" not in events
    finally:
        allow_cleanup.set()
        with pytest.raises(BaseException) as raised:
            await asyncio.wait_for(task, 5)

    if startup_failure == "cancelled":
        assert isinstance(raised.value, asyncio.CancelledError)
        assert events == ["started", "startup_cancelled", "closed"]
    else:
        assert "primary startup failure" in _flatten_exception_text(raised.value)
        assert events == ["started", "closed"]
    assert (await outgoing.get())["type"] == "lifespan.startup.failed"


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


def test_secondary_shutdown_diagnostics_do_not_format_adopter_values(caplog) -> None:
    class SecretClosure:
        async def __call__(self) -> None:
            raise ValueError("secret-secondary-error")

        def __repr__(self) -> str:
            return "secret-callable-repr"

    async def first_error() -> None:
        raise RuntimeError("first cleanup failure")

    app = _build_app(on_shutdown=[first_error, SecretClosure()])
    with pytest.raises(BaseException) as raised:
        with TestClient(app):
            pass

    assert "first cleanup failure" in _flatten_exception_text(raised.value)
    assert "on_shutdown hook failed" in caplog.text
    assert "secret-secondary-error" not in caplog.text
    assert "secret-callable-repr" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records if record.name == "adcp.server")


async def test_cancelled_shutdown_hook_does_not_skip_later_callbacks() -> None:
    from adcp.server.serve import _user_lifespan_hooks

    events: list[str] = []

    async def cancel_first() -> None:
        events.append("cancelled")
        raise asyncio.CancelledError()

    async def close_second() -> None:
        events.append("closed")

    with pytest.raises(asyncio.CancelledError):
        async with _user_lifespan_hooks((), (cancel_first, close_second)):
            pass
    assert events == ["cancelled", "closed"]


async def test_anyio_cancelled_scope_still_settles_shutdown_hooks() -> None:
    import anyio

    from adcp.server.serve import _user_lifespan_hooks

    events: list[str] = []

    async def close() -> None:
        await anyio.sleep(0)
        events.append("closed")

    with anyio.CancelScope() as scope:
        scope.cancel()
        async with _user_lifespan_hooks((), (close,)):
            pass

    assert events == ["closed"]


async def test_hook_lifecycle_failure_does_not_leave_framework_waiting() -> None:
    import anyio

    crash = asyncio.Event()
    closed = asyncio.Event()
    group = None
    incoming: asyncio.Queue[dict] = asyncio.Queue()
    outgoing: asyncio.Queue[dict] = asyncio.Queue()

    async def worker() -> None:
        await crash.wait()
        raise RuntimeError("worker failed")

    async def start() -> None:
        nonlocal group
        group = anyio.create_task_group()
        await group.__aenter__()
        group.start_soon(worker)

    async def close() -> None:
        assert group is not None
        try:
            await group.__aexit__(None, None, None)
        finally:
            closed.set()

    app = _build_app(on_startup=[start], on_shutdown=[close])
    task = asyncio.create_task(
        app(
            {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
            incoming.get,
            outgoing.put,
        )
    )
    try:
        await incoming.put({"type": "lifespan.startup"})
        assert (await asyncio.wait_for(outgoing.get(), 5))["type"] == "lifespan.startup.complete"
        crash.set()
        with pytest.raises(BaseException) as raised:
            await asyncio.wait_for(task, 5)
        assert not isinstance(raised.value, asyncio.TimeoutError)
        assert closed.is_set()
        assert (await outgoing.get())["type"] == "lifespan.shutdown.failed"
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_body_failure_remains_primary_after_hook_lifecycle_failure() -> None:
    import anyio

    from adcp.server.serve import _user_lifespan_hooks

    crash = asyncio.Event()
    group = None

    async def worker() -> None:
        await crash.wait()
        raise RuntimeError("secondary worker failure")

    async def start() -> None:
        nonlocal group
        group = anyio.create_task_group()
        await group.__aenter__()
        group.start_soon(worker)

    async def close() -> None:
        assert group is not None
        await group.__aexit__(None, None, None)

    with pytest.raises(ValueError, match="primary body failure"):
        async with _user_lifespan_hooks((start,), (close,)):
            crash.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), 5)
            except asyncio.CancelledError:
                raise ValueError("primary body failure") from None


@pytest.mark.parametrize("cancel_body", [False, True])
async def test_repeated_cancellation_settles_cleanup_before_transport_teardown(
    monkeypatch, cancel_body: bool
) -> None:
    events: list[str] = []
    close_requested = asyncio.Event()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    worker: asyncio.Task[None] | None = None
    incoming: asyncio.Queue[dict] = asyncio.Queue()
    outgoing: asyncio.Queue[dict] = asyncio.Queue()

    async def work() -> None:
        await close_requested.wait()
        close_started.set()
        await allow_close.wait()
        events.append("worker_settled")

    async def start() -> None:
        nonlocal worker
        worker = asyncio.create_task(work())

    async def close_worker() -> None:
        close_requested.set()
        assert worker is not None
        await worker
        events.append("worker_joined")

    async def close_pool() -> None:
        assert worker is not None and worker.done() and not worker.cancelled()
        events.append("pool_closed")

    # Observe the real inner transports without replacing their lifecycle.
    def observe(inner, name):
        original = inner.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            try:
                async with original(app):
                    events.append(name + "_started")
                    try:
                        yield
                    finally:
                        events.append(name + "_closing")
            finally:
                events.append(name + "_closed")

        inner.router.lifespan_context = lifespan
        return inner

    serve_module = importlib.import_module("adcp.server.serve")
    a2a_module = importlib.import_module("adcp.server.a2a_server")
    original_mcp = serve_module.create_mcp_server
    original_a2a = a2a_module.create_a2a_server

    def create_mcp(*args, **kwargs):
        server = original_mcp(*args, **kwargs)
        original_app = server.streamable_http_app
        monkeypatch.setattr(server, "streamable_http_app", lambda: observe(original_app(), "mcp"))
        return server

    monkeypatch.setattr(serve_module, "create_mcp_server", create_mcp)
    monkeypatch.setattr(
        a2a_module, "create_a2a_server", lambda *a, **kw: observe(original_a2a(*a, **kw), "a2a")
    )
    app = _build_app(on_startup=[start], on_shutdown=[close_worker, close_pool])
    task = asyncio.create_task(
        app(
            {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
            incoming.get,
            outgoing.put,
        )
    )
    try:
        await incoming.put({"type": "lifespan.startup"})
        startup = await asyncio.wait_for(outgoing.get(), 5)
        assert startup["type"] == "lifespan.startup.complete"
        if cancel_body:
            task.cancel()
        else:
            await incoming.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(close_started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert worker is not None and not worker.done()
        assert events == ["mcp_started", "a2a_started"]
    finally:
        allow_close.set()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            pass

    assert task.cancelled()
    assert events[:5] == [
        "mcp_started",
        "a2a_started",
        "worker_settled",
        "worker_joined",
        "pool_closed",
    ]
    assert events[5:] == ["a2a_closing", "a2a_closed", "mcp_closing", "mcp_closed"]
    shutdown = await outgoing.get()
    assert shutdown["type"] == "lifespan.shutdown.failed"


def test_synchronous_serve_owns_the_hook_event_loop(monkeypatch) -> None:
    import uvicorn

    from adcp.server import serve

    events: list[str] = []
    hook_loops = []

    async def start() -> None:
        hook_loops.append(asyncio.get_running_loop())
        events.append("start")

    async def close() -> None:
        hook_loops.append(asyncio.get_running_loop())
        await asyncio.sleep(0)
        events.append("close")

    async def drive_lifespan(server, sockets) -> None:
        messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
        results = []

        async def receive():
            return next(messages)

        async def send(message):
            results.append(message["type"])

        await server.config.app(
            {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send
        )
        assert results == ["lifespan.startup.complete", "lifespan.shutdown.complete"]

    sock = Mock()
    module = importlib.import_module("adcp.server.serve")
    monkeypatch.setattr(module, "_bind_reusable_socket", lambda *args: sock)
    monkeypatch.setattr(uvicorn.Server, "serve", drive_lifespan)
    serve(_Handler(), transport="both", on_startup=[start], on_shutdown=[close])
    assert events == ["start", "close"]
    assert hook_loops[0] is hook_loops[1]
    assert hook_loops[0].is_closed()
    sock.close.assert_called_once()


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
