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
  deprecated ``auto_emit_completion_webhooks`` option is forwarded for source
  compatibility but never synthesizes a webhook for an inline terminal result.

* :func:`build_test_client` — async context manager that combines
  :func:`build_asgi_app`, ``asgi_lifespan.LifespanManager``, and
  ``httpx.AsyncClient`` into a single ``async with`` block. Requires
  ``asgi-lifespan`` (included in ``adcp[dev]``).
"""

from __future__ import annotations

import warnings
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from adcp.decisioning.context import RequestContext
from adcp.decisioning.types import Account
from adcp.validation.client_hooks import SERVER_DEFAULT_VALIDATION as DEFAULT_VALIDATION
from adcp.validation.client_hooks import ValidationHookConfig

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
    from adcp.server.auth import BearerTokenAuth
    from adcp.server.helpers import ResponseEnhancer
    from adcp.server.serve import (
        ASGIMiddlewareEntry,
        ContextFactory,
        LifespanHook,
        SkillMiddleware,
    )
    from adcp.server.spec_compat import PreValidationHooks


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
    transport: Literal["mcp", "a2a", "both"] = "mcp",
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
    stateless_http: bool = False,
    session_idle_timeout: float | None = 1800.0,
    max_active_sessions: int | None = None,
    enable_dns_rebinding_protection: bool | None = None,
    max_request_size: int | None = None,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    discovery_base_url: str | None = None,
    pre_validation_hooks: PreValidationHooks | None = None,
    response_enhancer: ResponseEnhancer | None = None,
    on_startup: Sequence[LifespanHook] | None = None,
    on_shutdown: Sequence[LifespanHook] | None = None,
    **factory_kwargs: Any,
) -> Any:
    """Build a Starlette ASGI app for in-process integration tests.

    Returns the same middleware stack that :func:`adcp.decisioning.serve`
    mounts, minus the network bind. Pass any kwargs you would pass to
    :func:`adcp.decisioning.serve` (except the uvicorn-binding ones:
    ``port``, ``host``) and the result is drop-in compatible with
    ``httpx.ASGITransport``, ``starlette.testclient.TestClient``, or any
    ASGI test harness.

    ``transport="mcp"`` maps to production's
    ``transport="streamable-http"`` spelling. ``"a2a"`` and ``"both"``
    use the same app builders as their production counterparts.

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
    :param transport: In-process transport topology: ``"mcp"`` (the
        production ``"streamable-http"`` transport), ``"a2a"``, or
        ``"both"``. Defaults to ``"mcp"`` for backward compatibility.
    :param name: Server name on the AdCP capabilities envelope. Defaults
        to ``type(platform).__name__``.
    :param advertise_all: Forwarded to
        :func:`create_adcp_server_from_platform` and
        :func:`create_mcp_server`. Default ``False`` (override-detection
        filter on; matches :func:`serve`).
    :param auto_emit_completion_webhooks: Deprecated compatibility argument
        forwarded to :func:`create_adcp_server_from_platform`. Passing ``True``
        warns and does not synthesize a webhook for inline terminal results.
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
    :param stateless_http: Forwarded to :func:`create_mcp_server` for
        ``transport="mcp"`` and to the production composition path for
        ``transport="both"``. Ignored by ``transport="a2a"``.
    :param session_idle_timeout: Idle reap deadline for stateful MCP
        sessions. Defaults to 1800 seconds. Forwarded for ``"mcp"`` and
        ``"both"``; ignored by ``"a2a"``.
    :param max_active_sessions: Optional cap for active stateful MCP
        sessions. Forwarded for ``"mcp"`` and ``"both"``; ignored by
        ``"a2a"``.
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
        a ``(tool_name, raw_args) -> raw_args`` callable or ordered
        sequence. Forwarded to :func:`create_mcp_server`, identical semantics to
        :func:`adcp.decisioning.serve`'s ``pre_validation_hooks`` param.
        Use to install the same coercion hooks your production
        :func:`serve` call uses so in-process tests see the same
        validation surface as production. ``None`` → no hooks (default).
    :param response_enhancer: Optional server-wide
        :data:`~adcp.server.ResponseEnhancer` forwarded to
        :func:`create_mcp_server`, so in-process tests exercise the same
        enhancer wiring your production :func:`serve` call uses. ``None``
        → no enhancer (default).
    :param on_startup: Async zero-argument hooks run after the MCP and A2A
        framework lifespans start. Requires ``transport="both"``.
    :param on_shutdown: Async zero-argument hooks run before the MCP and A2A
        framework lifespans stop. Requires ``transport="both"``.
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
    if transport not in ("mcp", "a2a", "both"):
        raise ValueError(f"Unsupported transport {transport!r}; expected 'mcp', 'a2a', or 'both'.")
    if (on_startup or on_shutdown) and transport != "both":
        raise ValueError(
            "on_startup / on_shutdown hooks require transport='both', "
            f"got transport={transport!r}."
        )
    if transport == "a2a":
        ignored_session_settings = []
        if stateless_http:
            ignored_session_settings.append("stateless_http")
        if session_idle_timeout != 1800.0:
            ignored_session_settings.append("session_idle_timeout")
        if max_active_sessions is not None:
            ignored_session_settings.append("max_active_sessions")
        if ignored_session_settings:
            warnings.warn(
                "build_asgi_app sets MCP-only session fields "
                f"{sorted(ignored_session_settings)} but transport='a2a'. "
                "These fields will be ignored.",
                UserWarning,
                stacklevel=2,
            )

    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.server.serve import (
        _apply_asgi_middleware,
        _build_a2a_app,
        _build_mcp_and_a2a_app,
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
    if transport == "both":
        # TODO(#1047): expose A2A-only message_parser/public_url and task /
        # push stores explicitly once the public testing signature owns them.
        app = _build_mcp_and_a2a_app(
            handler,
            name=server_name,
            port=0,
            host="127.0.0.1",
            instructions=None,
            test_controller=None,
            context_factory=context_factory,
            middleware=middleware,
            advertise_all=advertise_all,
            max_request_size=max_request_size,
            streaming_responses=streaming_responses,
            stateless_http=stateless_http,
            session_idle_timeout=session_idle_timeout,
            max_active_sessions=max_active_sessions,
            validation=validation,
            pre_validation_hooks=pre_validation_hooks,
            response_enhancer=response_enhancer,
            base_url=discovery_base_url,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            enable_dns_rebinding_protection=enable_dns_rebinding_protection,
            auth=auth,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
            include_discovery=discovery_base_url is not None,
        )
        return _apply_asgi_middleware(app, asgi_middleware)
    if transport == "a2a":
        # TODO(#1047): expose A2A-only message_parser/public_url and task /
        # push stores explicitly once the public testing signature owns them.
        return _build_a2a_app(
            handler,
            name=server_name,
            port=0,
            test_controller=None,
            context_factory=context_factory,
            middleware=middleware,
            asgi_middleware=asgi_middleware,
            advertise_all=advertise_all,
            max_request_size=max_request_size,
            validation=validation,
            pre_validation_hooks=pre_validation_hooks,
            response_enhancer=response_enhancer,
            base_url=discovery_base_url,
            auth=auth,
            include_discovery=discovery_base_url is not None,
        )
    mcp = create_mcp_server(
        handler,
        name=server_name,
        advertise_all=advertise_all,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        context_factory=context_factory,
        middleware=middleware,
        streaming_responses=streaming_responses,
        stateless_http=stateless_http,
        session_idle_timeout=session_idle_timeout,
        max_active_sessions=max_active_sessions,
        enable_dns_rebinding_protection=enable_dns_rebinding_protection,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
        response_enhancer=response_enhancer,
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
    transport: Literal["mcp", "a2a", "both"] = "mcp",
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
    stateless_http: bool = False,
    session_idle_timeout: float | None = 1800.0,
    max_active_sessions: int | None = None,
    enable_dns_rebinding_protection: bool | None = None,
    max_request_size: int | None = None,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    discovery_base_url: str | None = None,
    pre_validation_hooks: PreValidationHooks | None = None,
    response_enhancer: ResponseEnhancer | None = None,
    on_startup: Sequence[LifespanHook] | None = None,
    on_shutdown: Sequence[LifespanHook] | None = None,
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
    :param transport: Forwarded to :func:`build_asgi_app`.
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
    :param stateless_http: Forwarded to :func:`build_asgi_app`.
    :param session_idle_timeout: Forwarded to :func:`build_asgi_app`.
    :param max_active_sessions: Forwarded to :func:`build_asgi_app`.
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
    :param response_enhancer: Forwarded to :func:`build_asgi_app`. Wire
        the same enhancer your production :func:`serve` call uses so
        in-process tests exercise the enhancer path.
    :param on_startup: Forwarded to :func:`build_asgi_app`. Requires
        ``transport="both"``.
    :param on_shutdown: Forwarded to :func:`build_asgi_app`. Requires
        ``transport="both"``.
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
    # the running event loop we're in here (#700). Force
    # ``validate_at_init=False`` regardless of what the adopter passed
    # — a test client is by definition inside a running loop, so a
    # caller asking for ``True`` would hit the exact bug this PR
    # fixes. Loud error is better than mysterious ``RuntimeError``.
    if factory_kwargs.get("validate_at_init") is True:
        raise ValueError(
            "build_test_client cannot validate capabilities at init — "
            "the test client runs inside the test's event loop, and "
            "the sync validator uses asyncio.run() which is "
            "incompatible with a running loop. Drop validate_at_init "
            "from factory_kwargs and `await "
            "validate_capabilities_response_shape_async(handler)` "
            "yourself if you need the boot-time check. See #700."
        )
    factory_kwargs["validate_at_init"] = False
    # The other boot validators (``validate_platform``, webhook
    # signing, idempotency wiring) are sync-pure and always run
    # regardless. Only the capabilities-shape validator is gated.
    app = build_asgi_app(
        platform,
        transport=transport,
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
        stateless_http=stateless_http,
        session_idle_timeout=session_idle_timeout,
        max_active_sessions=max_active_sessions,
        enable_dns_rebinding_protection=enable_dns_rebinding_protection,
        max_request_size=max_request_size,
        validation=validation,
        discovery_base_url=discovery_base_url,
        pre_validation_hooks=pre_validation_hooks,
        response_enhancer=response_enhancer,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
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
