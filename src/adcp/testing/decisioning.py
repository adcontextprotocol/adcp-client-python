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

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from adcp.decisioning.context import RequestContext
from adcp.decisioning.types import Account
from adcp.validation.client_hooks import SERVER_DEFAULT_VALIDATION as DEFAULT_VALIDATION
from adcp.validation.client_hooks import ValidationHookConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence
    from datetime import datetime

    import httpx

    from adcp.decisioning import (
        AuthInfo,
        BuyerAgent,
        DecisioningPlatform,
        ResourceResolver,
        StateReader,
    )
    from adcp.server.auth import BearerTokenAuth
    from adcp.server.serve import ASGIMiddlewareEntry, ContextFactory, SkillMiddleware


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
    allowed_origins: Sequence[str] | None = None,
    auth: BearerTokenAuth | None = None,
    asgi_middleware: Sequence[ASGIMiddlewareEntry] | None = None,
    context_factory: ContextFactory | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    streaming_responses: bool = False,
    enable_dns_rebinding_protection: bool | None = None,
    max_request_size: int | None = None,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    discovery_base_url: str | None = None,
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None,
    **factory_kwargs: Any,
) -> Any:
    """Build a Starlette ASGI app for in-process integration tests.

    Returns the same middleware stack that :func:`adcp.decisioning.serve`
    mounts, minus the network bind. Pass any kwargs you would pass to
    :func:`adcp.decisioning.serve` (except the uvicorn-binding ones:
    ``port``, ``host``) and the result is drop-in compatible with
    ``httpx.ASGITransport``, ``starlette.testclient.TestClient``, or any
    ASGI test harness.

    The wrapping order mirrors production
    (``_run_mcp_http`` in ``adcp.server.serve``):

    1. ``auth`` innermost — body-peeks the JSON-RPC payload before the
       path normalizer reshapes it (discovery bypass).
    2. Path normalizer — strips trailing slashes so ``/mcp/`` → ``/mcp``
       without a 307 round-trip.
    3. Discovery wrapper (only when ``discovery_base_url`` is provided) —
       serves ``/.well-known/adcp-agents.json``.
    4. Size cap (``max_request_size``; ``None`` → 10 MB default).
    5. ``asgi_middleware`` outermost — CORS, tenant resolution, custom
       auth, etc.

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
    :param allowed_origins: CORS origin allowlist forwarded to
        :func:`create_mcp_server`. ``None`` → FastMCP default (no CORS).
    :param auth: Optional :class:`~adcp.server.auth.BearerTokenAuth`
        config applied to the MCP ASGI app. Drives
        :class:`~adcp.server.auth.BearerTokenAuthMiddleware`. ``None``
        → no bearer-token validation (unauthenticated).
    :param asgi_middleware: Optional ASGI middleware entries applied
        outermost — same semantics as :func:`serve`'s ``asgi_middleware``
        param. Use for CORS, request-id propagation, custom auth.
    :param context_factory: Optional factory that builds a
        :class:`~adcp.server.ToolContext` per tool call. Forwarded to
        :func:`create_mcp_server`. ``None`` → bare ``ToolContext()``.
    :param middleware: Optional sequence of
        :data:`~adcp.server.serve.SkillMiddleware` callables wrapping
        every tool dispatch. Forwarded to :func:`create_mcp_server`.
    :param streaming_responses: Forwarded to :func:`create_mcp_server`.
        Default ``False``.
    :param enable_dns_rebinding_protection: Forwarded to
        :func:`create_mcp_server`. ``None`` → FastMCP default.
    :param max_request_size: Request body size cap in bytes. ``None`` →
        the framework default (10 MB). ``0`` → disabled.
    :param validation: Schema validation config forwarded to
        :func:`create_mcp_server`. Defaults to
        :data:`~adcp.server.serve.DEFAULT_VALIDATION` (strict on both
        requests and responses) — matches production. Pass
        ``validation=None`` to disable.
    :param discovery_base_url: When provided, mounts the
        ``/.well-known/adcp-agents.json`` discovery endpoint using this
        as the advertised base URL (e.g. ``"http://test"``). ``None`` →
        discovery endpoint not mounted.
    :param pre_validation_hooks: Optional dict mapping AdCP tool name to
        a ``(tool_name, raw_args) -> raw_args`` callable. Forwarded to
        :func:`create_mcp_server`, identical semantics to
        :func:`adcp.decisioning.serve`'s ``pre_validation_hooks`` param.
        Use to install the same coercion hooks your production
        :func:`serve` call uses so in-process tests see the same
        validation surface as production. ``None`` → no hooks (default).
    :param factory_kwargs: Forwarded to
        :func:`create_adcp_server_from_platform`. Accepted keys:
        ``executor``, ``registry``, ``webhook_sender``,
        ``webhook_supervisor``, ``buyer_agent_registry``,
        ``config_store``, ``property_list_fetcher``, ``state_reader``,
        ``resource_resolver``.

    :returns: A Starlette ASGI application. Usable with
        ``starlette.testclient.TestClient``,
        ``httpx.AsyncClient(app=app, ...)``, or any ASGI test harness.
    """
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.server.serve import (
        _apply_asgi_middleware,
        _wrap_mcp_with_auth,
        _wrap_with_path_normalize,
        _wrap_with_size_limit,
        create_mcp_server,
    )

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
        allowed_origins=allowed_origins,
        context_factory=context_factory,
        middleware=middleware,
        streaming_responses=streaming_responses,
        enable_dns_rebinding_protection=enable_dns_rebinding_protection,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
    )
    # Mirror the wrapping chain from _run_mcp_http (adcp.server.serve).
    # auth must be innermost so its JSON-RPC body-peek runs before the
    # path normalizer reshapes scope["path"].
    app = mcp.streamable_http_app()
    app = _wrap_mcp_with_auth(app, auth)
    app = _wrap_with_path_normalize(app)
    if discovery_base_url is not None:
        from adcp.server.serve import _wrap_with_discovery

        app = _wrap_with_discovery(
            app,
            name=server_name,
            transports=["mcp"],
            base_url=discovery_base_url,
        )
    app = _wrap_with_size_limit(app, max_request_size)
    app = _apply_asgi_middleware(app, asgi_middleware)
    return app


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
    auth: BearerTokenAuth | None = None,
    allowed_origins: Sequence[str] | None = None,
    asgi_middleware: Sequence[ASGIMiddlewareEntry] | None = None,
    context_factory: ContextFactory | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    streaming_responses: bool = False,
    enable_dns_rebinding_protection: bool | None = None,
    max_request_size: int | None = None,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    discovery_base_url: str | None = None,
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None,
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

    ``allowed_hosts`` is derived automatically from ``base_url`` — the
    hostname is extracted and added to FastMCP's transport-security allowlist.
    Pass ``allowed_hosts`` to :func:`build_asgi_app` directly when you need
    custom control.

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
    :param auth: Forwarded to :func:`build_asgi_app`. ``None`` → no
        bearer-token validation.
    :param allowed_origins: CORS origin allowlist forwarded to
        :func:`build_asgi_app`. ``None`` → FastMCP default (no CORS).
    :param asgi_middleware: Forwarded to :func:`build_asgi_app`.
    :param context_factory: Forwarded to :func:`build_asgi_app`.
    :param middleware: Forwarded to :func:`build_asgi_app`.
    :param streaming_responses: Forwarded to :func:`build_asgi_app`.
    :param enable_dns_rebinding_protection: Forwarded to
        :func:`build_asgi_app`.
    :param max_request_size: Forwarded to :func:`build_asgi_app`.
    :param validation: Forwarded to :func:`build_asgi_app`. Defaults to
        :data:`~adcp.server.serve.DEFAULT_VALIDATION` (strict).
    :param discovery_base_url: Forwarded to :func:`build_asgi_app`.
        When ``None`` (default), the discovery endpoint is not mounted.
        Pass ``base_url`` here if your tests exercise
        ``/.well-known/adcp-agents.json``.
    :param pre_validation_hooks: Forwarded to :func:`build_asgi_app`.
        Install the same hooks your production :func:`serve` call uses
        so in-process tests see the same validation surface.
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
    # ``create_adcp_server_from_platform`` is a sync function whose
    # default ``validate_at_init=True`` path drives the async
    # capabilities handler through ``asyncio.run`` — incompatible with
    # the running event loop we're in here (#700). Pass
    # ``validate_at_init=False`` so the sync builder works in-loop;
    # the production ``serve()`` boot path still runs the full
    # validator. The other boot validators (``validate_platform``,
    # webhook signing, idempotency wiring) are sync-pure and always
    # run regardless.
    factory_kwargs.setdefault("validate_at_init", False)
    app = build_asgi_app(
        platform,
        name=name,
        advertise_all=advertise_all,
        auto_emit_completion_webhooks=auto_emit_completion_webhooks,
        allowed_hosts=[hostname],
        allowed_origins=allowed_origins,
        auth=auth,
        asgi_middleware=asgi_middleware,
        context_factory=context_factory,
        middleware=middleware,
        streaming_responses=streaming_responses,
        enable_dns_rebinding_protection=enable_dns_rebinding_protection,
        max_request_size=max_request_size,
        validation=validation,
        discovery_base_url=discovery_base_url,
        pre_validation_hooks=pre_validation_hooks,
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
