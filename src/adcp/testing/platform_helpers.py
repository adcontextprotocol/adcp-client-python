"""Server-side test helpers for DecisioningPlatform unit tests.

These helpers exist so adopters writing platform-handler tests can:

1. Construct a :class:`~adcp.decisioning.context.RequestContext` in one
   line without guessing which factory defaults are safe — use
   :func:`make_request_context`.

2. Get a runnable ASGI app from a platform in one call — use
   :func:`build_asgi_app` to wire ``httpx.AsyncClient`` or
   ``starlette.testclient.TestClient`` against the server in-process.

These are the official test seams for platform-handler unit tests.
Do not call ``create_adcp_server_from_platform`` directly in tests —
:func:`build_asgi_app` sets test-appropriate defaults (no auto-emit
webhooks, single-threaded executor) that production ``serve()`` does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.platform import DecisioningPlatform
    from adcp.decisioning.types import Account

__all__ = ["make_request_context", "build_asgi_app"]


def make_request_context(
    account: Account[Any] | None = None,
    account_id: str = "test-account",
    **overrides: Any,
) -> RequestContext[Any]:
    """Build a :class:`~adcp.decisioning.context.RequestContext` for unit tests.

    Adopters testing through ``PlatformHandler`` typically only read
    ``ctx.account`` and sometimes ``ctx.request_id``. This helper fills
    in all factory defaults so callers don't have to know which ones are
    safe — the framework-owned stubs for ``state`` and ``resolve`` are
    wired in automatically.

    ``caller_identity`` is defaulted to the resolved account's ``id`` for
    test simplicity. The production dispatch path sets a composite opaque
    key (``<store>:<account_id>``); do not assert on the exact value in
    adopter tests. Override via ``**overrides`` when testing per-principal
    idempotency scoping.

    :param account: Pre-built :class:`~adcp.decisioning.types.Account`.
        When provided, ``account_id`` is ignored.
    :param account_id: Used to construct ``Account(id=account_id)`` when
        ``account`` is ``None``. Defaults to ``"test-account"``.
    :param overrides: Forwarded verbatim to
        :class:`~adcp.decisioning.context.RequestContext`. Any field on
        ``RequestContext`` or its parent :class:`~adcp.server.base.ToolContext`
        can be overridden here (e.g. ``request_id="req-123"``,
        ``auth_info=...``, ``caller_identity="custom-id"``).

    Example::

        from adcp.testing import make_request_context

        ctx = make_request_context(account_id="acme-corp")
        result = await platform.get_products(req, ctx)

    Example with a pre-built Account::

        from adcp.decisioning import Account
        from adcp.testing import make_request_context

        account = Account(id="acme", metadata={"adapter": my_adapter})
        ctx = make_request_context(account=account)
    """
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import Account as _Account

    resolved: Account[Any] = account if account is not None else _Account(id=account_id)
    if "caller_identity" not in overrides:
        overrides["caller_identity"] = resolved.id
    return RequestContext(account=resolved, **overrides)


def build_asgi_app(
    platform: DecisioningPlatform,
    *,
    name: str | None = None,
) -> Any:
    """Build an ASGI app from a :class:`~adcp.decisioning.platform.DecisioningPlatform`.

    Equivalent to calling ``serve(platform, name=name)`` but returns the
    ASGI callable instead of blocking. Wire it to ``httpx.AsyncClient``
    (via ``httpx.ASGITransport``) or ``starlette.testclient.TestClient``
    for in-process integration tests.

    Test-appropriate defaults applied automatically (both differ from
    production ``serve()``):

    - ``auto_emit_completion_webhooks=False`` — skips the F12 boot-time
      webhook-sender gate that fires for webhook-eligible specialisms
      (``create_media_buy``, ``activate_signal``, etc.). Tests that
      exercise webhook emission should wire a sender explicitly via the
      platform's own setup, not through this helper.
    - ``thread_pool_size=1`` — allocates a one-thread executor instead
      of the production ``min(32, cpu+4)`` default. Reduces OS-thread
      churn in test suites that call this helper per test case.

    :param platform: The :class:`~adcp.decisioning.platform.DecisioningPlatform`
        instance.
    :param name: Server name advertised on AdCP capabilities. Defaults to
        ``type(platform).__name__``, matching :func:`~adcp.decisioning.serve.serve`.

    :returns: ASGI callable (Starlette application). Compatible with
        ``httpx.AsyncClient(transport=httpx.ASGITransport(app=app), ...)``
        and ``starlette.testclient.TestClient(app)``.

    .. warning::
        ``build_asgi_app`` is **synchronous** and must be called from outside
        a running asyncio event loop (i.e., in a sync fixture or at module
        scope). Calling it from inside an ``async def`` test will raise
        ``RuntimeError: asyncio.run() cannot be called from a running event
        loop`` because the boot-time capabilities validator uses
        ``asyncio.run()`` internally.

        Pattern for async test suites::

            def test_my_platform():  # sync — build app here
                app = build_asgi_app(MyPlatform())

                async def _run():
                    async with LifespanManager(app):
                        async with httpx.AsyncClient(...) as client:
                            ...

                asyncio.run(_run())

        Or use a sync ``pytest.fixture`` to build the app once per test
        module and yield it into async tests.

    Example::

        import asyncio
        import httpx
        from asgi_lifespan import LifespanManager
        from adcp.testing import build_asgi_app

        def test_get_products():
            app = build_asgi_app(MyPlatform(), name="test-seller")

            async def _run():
                async with LifespanManager(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://localhost",
                        follow_redirects=True,  # /mcp/ → /mcp
                    ) as client:
                        resp = await client.post("/mcp/", json={...})
                        assert resp.status_code == 200

            asyncio.run(_run())
    """
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.server.serve import create_mcp_server

    handler, _, _ = create_adcp_server_from_platform(
        platform,
        thread_pool_size=1,
        auto_emit_completion_webhooks=False,
    )
    server_name = name or type(platform).__name__
    mcp = create_mcp_server(
        handler,
        name=server_name,
        # DNS rebinding protection rejects arbitrary Host headers from
        # httpx.ASGITransport test clients; disable it in test contexts.
        enable_dns_rebinding_protection=False,
    )
    return mcp.streamable_http_app()
