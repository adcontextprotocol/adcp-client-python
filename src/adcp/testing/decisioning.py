"""Test helpers for the v6 DecisioningPlatform framework.

Three adopter-facing helpers that close gaps surfaced by the salesagent
v3.12 → 4.x migration:

* :func:`make_request_context` — build a
  :class:`adcp.decisioning.RequestContext` for unit tests with sane
  defaults. The dataclass has a dozen fields with factory defaults; this
  helper documents what tests should reach for so adopters don't guess
  whether ``state`` / ``resolve`` / ``now`` factory defaults are safe.

* :func:`build_asgi_app` — build a Starlette ASGI app from a
  :class:`adcp.decisioning.DecisioningPlatform` without binding a port.
  Useful for in-process integration tests via ``httpx.AsyncClient``,
  ``starlette.testclient.TestClient``, or direct ASGI invocation. The
  default ``auto_emit_completion_webhooks=False`` skips the F12 boot
  gate that otherwise refuses to start a sales platform without a
  webhook sender wired.

* :func:`build_test_client` — async context manager that combines
  :func:`build_asgi_app`, ``asgi_lifespan.LifespanManager``, and
  ``httpx.AsyncClient`` into a single ``async with`` block. Requires
  ``asgi-lifespan`` (included in ``adcp[dev]``).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from adcp.decisioning.context import RequestContext
from adcp.decisioning.types import Account

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence
    from datetime import datetime

    import httpx

    from adcp.decisioning import (
        AuthInfo,
        BuyerAgent,
        DecisioningPlatform,
        ResourceResolver,
        StateReader,
    )


def make_request_context(
    *,
    account: Account[Any] | str | None = None,
    auth_info: AuthInfo | None = None,
    auth_principal: str | None = None,
    buyer_agent: BuyerAgent | None = None,
    now: datetime | None = None,
    state: StateReader | None = None,
    resolve: ResourceResolver | None = None,
    request_id: str | None = None,
    tenant_id: str | None = None,
    caller_identity: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RequestContext[Any]:
    """Build a :class:`RequestContext` for unit tests.

    All parameters are optional. The defaults are stable test contract:
    an empty ``Account(id="test-account")``, no auth, framework-default
    ``state`` / ``resolve`` (the v6.0 stub readers), ``now`` set to wall
    clock, and an empty metadata dict.

    Pass ``account=`` as either an :class:`Account` instance (full
    control) or a string (shorthand for ``Account(id=<string>)``) — the
    common test case.

    :param account: Resolved account. ``None`` → ``Account(id="test-account")``.
        ``str`` → ``Account(id=<string>)``.
    :param auth_info: Verified principal info. ``None`` for unauthenticated
        / ``'derived'`` test fixtures.
    :param auth_principal: Convenience field for tests that read
        ``ctx.auth_principal`` without constructing an ``AuthInfo``.
    :param buyer_agent: Resolved commercial buyer agent. ``None`` for
        tests not exercising the registry path.
    :param now: Request timestamp. ``None`` → wall clock at construction.
    :param state: Workflow-state reader. ``None`` → framework default
        (v6.0 stub returning empty values).
    :param resolve: Async resource resolver. ``None`` → framework default
        (v6.0 stub raising ``NotImplementedError``).
    :param request_id: Inherited from :class:`adcp.server.ToolContext`.
    :param tenant_id: Inherited from :class:`adcp.server.ToolContext`.
    :param caller_identity: Inherited from :class:`adcp.server.ToolContext`.
        The framework's idempotency middleware reads this; tests that
        exercise idempotency paths should set it explicitly.
    :param metadata: Inherited from :class:`adcp.server.ToolContext`.
        ``None`` → empty dict.

    :returns: A populated :class:`RequestContext[Any]`.
    """
    resolved_account: Account[Any]
    if account is None:
        resolved_account = Account(id="test-account")
    elif isinstance(account, str):
        resolved_account = Account(id=account)
    else:
        resolved_account = account

    kwargs: dict[str, Any] = {"account": resolved_account}
    if auth_info is not None:
        kwargs["auth_info"] = auth_info
    if auth_principal is not None:
        kwargs["auth_principal"] = auth_principal
    if buyer_agent is not None:
        kwargs["buyer_agent"] = buyer_agent
    if now is not None:
        kwargs["now"] = now
    if state is not None:
        kwargs["state"] = state
    if resolve is not None:
        kwargs["resolve"] = resolve
    if request_id is not None:
        kwargs["request_id"] = request_id
    if tenant_id is not None:
        kwargs["tenant_id"] = tenant_id
    if caller_identity is not None:
        kwargs["caller_identity"] = caller_identity
    if metadata is not None:
        kwargs["metadata"] = metadata

    return RequestContext(**kwargs)


def build_asgi_app(
    platform: DecisioningPlatform,
    *,
    name: str | None = None,
    advertise_all: bool = False,
    auto_emit_completion_webhooks: bool = False,
    allowed_hosts: Sequence[str] | None = None,
    **factory_kwargs: Any,
) -> Any:
    """Build a Starlette ASGI app for in-process integration tests.

    Composes :func:`adcp.decisioning.create_adcp_server_from_platform`
    with :func:`adcp.server.create_mcp_server` and returns the
    streamable-HTTP ASGI app — the same surface
    :func:`adcp.decisioning.serve` would mount, minus the network bind.

    Defaults differ from the production :func:`serve` wrapper in one
    place: ``auto_emit_completion_webhooks`` is ``False`` so tests don't
    need to wire a :class:`adcp.webhook_sender.WebhookSender` just to
    instantiate a sales platform. Override to ``True`` if your test
    explicitly exercises the F12 auto-emit path.

    :param platform: The :class:`DecisioningPlatform` instance under
        test.
    :param name: Server name on the AdCP capabilities envelope. Defaults
        to ``type(platform).__name__``.
    :param advertise_all: Forwarded to
        :func:`create_adcp_server_from_platform` and
        :func:`create_mcp_server`. Default ``False`` (override-detection
        filter on; matches :func:`serve`).
    :param auto_emit_completion_webhooks: Forwarded to
        :func:`create_adcp_server_from_platform`. Default ``False`` for
        test ergonomics — production :func:`serve` defaults to ``True``.
    :param allowed_hosts: Host header values the MCP transport-security
        layer will accept. ``None`` → FastMCP's loopback-only default
        (``localhost``, ``127.0.0.1``, ``[::1]``). Pass the hostname
        embedded in your ``base_url`` when using a non-loopback test
        address (e.g. ``["test"]`` for ``base_url="http://test"``).
        :func:`build_test_client` sets this automatically.
    :param factory_kwargs: Forwarded to
        :func:`create_adcp_server_from_platform` (executor, registry,
        webhook_sender, etc.).

    :returns: A Starlette ASGI application. Usable with
        ``starlette.testclient.TestClient``,
        ``httpx.AsyncClient(app=app, ...)``, or any ASGI test harness.
    """
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.server.serve import create_mcp_server

    handler, _executor, _registry = create_adcp_server_from_platform(
        platform,
        advertise_all=advertise_all,
        auto_emit_completion_webhooks=auto_emit_completion_webhooks,
        **factory_kwargs,
    )
    server_name = name or type(platform).__name__
    mcp = create_mcp_server(
        handler,
        name=server_name,
        advertise_all=advertise_all,
        allowed_hosts=allowed_hosts,
    )
    return mcp.streamable_http_app()


@asynccontextmanager
async def build_test_client(
    platform: DecisioningPlatform,
    *,
    base_url: str = "http://test",
    name: str | None = None,
    advertise_all: bool = False,
    auto_emit_completion_webhooks: bool = False,
    follow_redirects: bool = True,
    headers: Mapping[str, str] | None = None,
    **factory_kwargs: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async context manager yielding an ``httpx.AsyncClient`` wired against
    the platform's ASGI app via ``httpx.ASGITransport`` + ``LifespanManager``.

    Collapses the four-line boilerplate that every in-process integration test
    previously needed — ``build_asgi_app`` + ``LifespanManager`` +
    ``httpx.AsyncClient`` — into a single ``async with`` block::

        async with build_test_client(platform) as client:
            resp = await client.post("/mcp/", json=...)

    The context manager starts the ASGI lifespan on entry and shuts down both
    the client and the lifespan manager on exit. ``build_test_client(...)``
    itself is an ``AbstractAsyncContextManager[httpx.AsyncClient]``; the
    yielded object is a plain ``httpx.AsyncClient``.

    Requires ``asgi-lifespan`` (included in ``adcp[dev]``). Raises
    :class:`ImportError` with an actionable message if it is not installed.

    :param platform: The :class:`DecisioningPlatform` instance under test.
    :param base_url: Base URL for all requests. Default ``"http://test"``.
        The hostname is extracted and added to the transport-security
        ``allowed_hosts`` list automatically — no manual wiring needed.
    :param name: Server name forwarded to :func:`build_asgi_app`.
    :param advertise_all: Forwarded to :func:`build_asgi_app`.
    :param auto_emit_completion_webhooks: Forwarded to :func:`build_asgi_app`.
    :param follow_redirects: Forwarded to ``httpx.AsyncClient``. Default
        ``True`` — FastMCP's streamable-HTTP endpoint can issue a 307
        redirect (``/mcp`` → ``/mcp/``) and callers shouldn't have to
        handle it manually.
    :param headers: Default headers attached to every request. Useful for
        auth tests: ``headers={"x-adcp-auth": "tok_..."}``. ``None`` →
        no default headers.
    :param factory_kwargs: Forwarded to
        :func:`create_adcp_server_from_platform` via :func:`build_asgi_app`
        (executor, registry, webhook_sender, etc.).
    """
    try:
        from asgi_lifespan import LifespanManager
    except ImportError as exc:
        raise ImportError(
            "asgi-lifespan is required for build_test_client. "
            "Install it with: pip install 'adcp[dev]'"
        ) from exc

    import httpx as _httpx

    hostname = urlparse(base_url).hostname or "localhost"
    # validate_capabilities_response_shape (called by create_adcp_server_from_platform)
    # uses asyncio.run(), which raises if a loop is already running. Run the sync
    # builder in a thread so it gets a clean loop.
    app = await asyncio.to_thread(
        build_asgi_app,
        platform,
        name=name,
        advertise_all=advertise_all,
        auto_emit_completion_webhooks=auto_emit_completion_webhooks,
        allowed_hosts=[hostname],
        **factory_kwargs,
    )
    async with LifespanManager(app):
        async with _httpx.AsyncClient(
            transport=_httpx.ASGITransport(app=app),
            base_url=base_url,
            headers=headers,
            follow_redirects=follow_redirects,
        ) as client:
            yield client


__all__ = ["build_asgi_app", "build_test_client", "make_request_context"]
