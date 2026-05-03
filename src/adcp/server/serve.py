"""One-liner server for ADCP handlers (MCP or A2A).

Stand up an ADCP-compliant server with a single function call:

    from adcp.server import ADCPHandler, serve
    from adcp.server.responses import capabilities_response

    class MyAgent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return capabilities_response(["media_buy"])

    # MCP (default)
    serve(MyAgent())

    # A2A
    serve(MyAgent(), transport="a2a")
"""

from __future__ import annotations

import logging
import os
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

logger = logging.getLogger("adcp.server")

from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import (
    _HANDLER_TOOLS,
    create_tool_caller,
    get_tools_for_handler,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from a2a.server.tasks.push_notification_config_store import (
        PushNotificationConfigStore,
    )
    from a2a.server.tasks.task_store import TaskStore

    from adcp.server.a2a_server import MessageParser
    from adcp.server.test_controller import TestControllerStore
    from adcp.validation.client_hooks import ValidationHookConfig


@dataclass(frozen=True)
class RequestMetadata:
    """Per-request metadata passed to :class:`ContextFactory`.

    Populated by the SDK before invoking the factory. Stable across the
    MCP and A2A transports — factories written against this shape work
    on both sides. Additional fields may be added in minor releases;
    factories should keep accepting ``RequestMetadata`` and pluck the
    fields they need by name, not by positional unpacking.

    :param tool_name: The AdCP operation being invoked (e.g.
        ``"get_products"``, ``"create_media_buy"``). Useful for
        tool-level audit logging and feature flagging.
    :param transport: ``"mcp"`` or ``"a2a"`` — the wire protocol
        currently dispatching this call. Agents that expose both can
        use this to branch on transport-specific behavior.
    :param request_id: The transport-assigned request id when one
        exists (A2A populates this from the task id; MCP leaves it
        ``None`` at the SDK layer today).
    """

    tool_name: str
    transport: Literal["mcp", "a2a"]
    request_id: str | None = None


SkillMiddleware = Callable[
    [str, dict[str, Any], ToolContext, Callable[[], Awaitable[Any]]],
    Awaitable[Any],
]
"""Middleware that wraps skill dispatch on both the MCP and A2A
transports — the audit / activity-feed / rate-limiter / tracing hook.
Composition semantics are identical across transports (shared
composer); middleware written against one transport works unchanged
on the other.

Signature (conceptually a Protocol; declared as a ``Callable`` alias so
it's importable and consistent with ``ContextFactory``)::

    async def middleware(
        skill_name: str,
        params: dict[str, Any],
        context: ToolContext,
        call_next: Callable[[], Awaitable[Any]],
    ) -> Any:
        ...

Middleware wraps ``call_next()`` — call it (possibly more than once to
implement retry, or never to short-circuit) to invoke the rest of the
chain plus the underlying handler. Anything the middleware returns
becomes the dispatch result the A2A transport serialises back to the
client, so middleware can short-circuit (skip the handler entirely) or
transform the result on the return side.

Middleware observes both success and failure — catch exceptions around
``call_next()`` to implement audit-on-failure or retry-classifier hooks.
Middleware re-raising propagates to the executor's normal error path
(application ``ADCPError`` → failed task w/ ``adcp_error`` DataPart;
other exceptions → opaque failed task per the spec's error-sanitisation
rule). **Swallowing an exception and returning a substitute result is
allowed but almost always wrong** — in particular, swallowing
``ADCPError`` subclasses (``IdempotencyConflictError``,
``ADCPTaskError``) serves a fake success for a failed mutation, which
double-bills / double-allocates in production.

``params`` is the parsed request dict passed to every middleware in
the chain and to the handler. Middleware cannot mutate what the next
layer sees by mutating ``params`` — transforms happen on the return
side only, by modifying the value returned from ``call_next()``.

Multiple middlewares compose outermost-first, matching Starlette/ASGI
semantics — if you pass ``middleware=[Audit(), RateLimit(), Metrics()]``,
the runtime order is::

    Audit.__call__ →  RateLimit.__call__ →  Metrics.__call__ →  handler

**Put audit outermost.** Middleware that short-circuits (rate limiter,
feature-flag gate) never calls ``call_next()``, so anything deeper in
the chain never sees the request. If your audit middleware sits
*after* the rate limiter, rejected calls disappear from the audit
trail — often the most interesting events for security review.

``call_next()`` runs in the same asyncio task as the middleware that
invoked it, so ``ContextVar`` values set before the call are visible
to downstream middleware and the handler. Don't ``asyncio.create_task``
your way around this unless you need the isolation.

**Security — middleware is a data processor for the full skill payload.**
``params`` is decoded business content (buyer briefs, budgets, brand
references, proposal text, PII in message parts). ``context`` carries
``caller_identity``, ``tenant_id``, and anything your ``context_factory``
populates. Installing a third-party middleware (observability vendor,
SaaS audit pipeline, external tracing) hands that vendor the complete
skill payload surface — treat it as a data processor under your
GDPR/CCPA controller-processor relationships and review the blast
radius before wiring vendors here.

**Security — do not format ``params`` or ``context.caller_identity``
into exception messages.** Middleware-raised exceptions pass through
``logger.exception`` in the executor (server-side trace with the raw
message) before the executor's sanitisation kicks in for the client
response. Exception text ends up in operator logs verbatim; keep it
opaque.

**Security — short-circuit caches MUST include principal + tenant in
the cache key.** A middleware that caches on ``skill_name + params``
alone and returns a cached result without calling ``call_next()``
will serve principal A's data to principal B on a matching-params
call. Key on ``(skill_name, params, context.caller_identity,
context.tenant_id)``.

Example — audit logging with exception capture::

    from adcp.server import SkillMiddleware, ToolContext

    async def audit_middleware(
        skill_name: str,
        params: dict[str, Any],
        context: ToolContext,
        call_next: Callable[[], Awaitable[Any]],
    ) -> Any:
        started_at = time.monotonic()
        try:
            result = await call_next()
        except Exception as exc:
            # Keep exception text opaque — this ends up in server logs.
            audit_log.failure(
                skill_name, context.caller_identity, type(exc).__name__
            )
            raise
        audit_log.success(
            skill_name,
            context.caller_identity,
            elapsed_ms=(time.monotonic() - started_at) * 1000,
        )
        return result

    create_a2a_server(MyAgent(), middleware=[audit_middleware])

The same middleware list also composes on the MCP side — pass it to
``create_mcp_server(middleware=...)`` or the transport-agnostic
``serve(middleware=...)``.
"""


def _log_advertised_tools(
    *,
    transport: Literal["mcp", "a2a"],
    handler: ADCPHandler[Any],
    advertise_all: bool,
    registered: list[str],
) -> None:
    """Log which tools the server just advertised, plus the delta vs the
    full spec surface the handler class could have supported.

    Operators occasionally rename a handler method and silently drop it
    from ``tools/list`` — discovering that during incident review is
    the wrong time. Emitting the advertised set and the unadvertised
    delta at startup turns a silent gap into a searchable log line.

    Registered at ``INFO`` because operators routinely tail this; the
    delta at ``DEBUG`` because it's noisy on fully-implemented handlers.

    Also fires a one-time ``UserWarning`` at boot when the handler
    class introduces a new specialism (a custom subclass that's not in
    the framework's tool registry and doesn't declare
    ``advertised_tools``) but ``advertise_all`` is False — closes the
    silent over-advertisement gap where adopters see the full
    ``ADCPHandler`` tool surface inherited via MRO when they meant to
    declare a focused subset.
    """
    registered_set = set(registered)
    full_defs = get_tools_for_handler(handler, advertise_all=True)
    full_names = {t["name"] for t in full_defs}
    unadvertised = sorted(full_names - registered_set)

    logger.info(
        "%s server advertising %d of %d tools%s",
        transport,
        len(registered_set),
        len(full_names),
        " (advertise_all=True)" if advertise_all else "",
    )
    if unadvertised and not advertise_all:
        logger.debug("%s server unadvertised tools: %s", transport, ", ".join(unadvertised))

    # Stacklevel walks: warnings.warn → _warn_if_unregistered_subclass →
    # _log_advertised_tools → operator's call site. The MCP path adds one
    # extra frame (_register_handler_tools); A2A calls _log_advertised_tools
    # directly from create_a2a_server.
    caller_stacklevel = 4 if transport == "mcp" else 3
    _warn_if_unregistered_subclass(
        handler, advertise_all=advertise_all, stacklevel=caller_stacklevel
    )


#: Bases whose tool set is broad-by-design — when an adopter subclass
#: lands on one of these via MRO without registering its own
#: ``advertised_tools``, the result is over-advertisement (the broad
#: base's full set inherited unintentionally). Naming the rule rather
#: than checking ``base.__name__ != "ADCPHandler"`` inline so future
#: broad bases (a hypothetical ``UniversalHandler``) get added to one
#: place — and a reviewer's first question becomes "is this base
#: broad-by-design?" not "what's special about ADCPHandler?".
_BROAD_SURFACE_BASES: frozenset[str] = frozenset({"ADCPHandler"})


def _warn_if_unregistered_subclass(
    handler: ADCPHandler[Any], *, advertise_all: bool, stacklevel: int = 4
) -> None:
    """Emit a one-time ``UserWarning`` when a custom handler base bypasses
    the tool-discovery registry.

    The trigger: the concrete handler class itself isn't in
    ``_HANDLER_TOOLS``, has no ``advertised_tools`` declaration of its
    own, and inherits its tool set from a broad-surface base (see
    :data:`_BROAD_SURFACE_BASES`) rather than a specialized base like
    ``GovernanceHandler``. That combination almost always means the
    adopter meant to declare a focused tool set but forgot to register
    it; the framework over-advertises by silently falling through to
    the broad base's full surface.

    Suppressed when ``advertise_all=True`` — that's the explicit "yes,
    advertise everything" opt-in.
    """
    if advertise_all:
        return
    cls = type(handler)
    if cls.__name__ in _HANDLER_TOOLS:
        return
    if "advertised_tools" in cls.__dict__:
        # Should already have been auto-registered via __init_subclass__,
        # but defensively skip the warning if the attribute exists.
        return
    # Walk MRO looking for a specialized (non-broad-surface) SDK base.
    # If one is found, the adopter is subclassing a focused base and
    # inheriting its tool set — that's the documented pattern, no
    # warning needed.
    has_specialized_parent = any(
        base.__name__ in _HANDLER_TOOLS and base.__name__ not in _BROAD_SURFACE_BASES
        for base in cls.__mro__
    )
    if has_specialized_parent:
        return
    # Default stacklevel=4 covers the MCP path (warn → this fn →
    # _log_advertised_tools → _register_handler_tools → caller). The A2A
    # path lacks _register_handler_tools and passes stacklevel=3.
    warnings.warn(
        f"Handler class {cls.__name__!r} subclasses ADCPHandler directly "
        f"but isn't registered in the framework's tool-discovery "
        f"registry. tools/list will inherit the full ADCPHandler tool "
        f"surface — this almost always means over-advertising for a "
        f"new specialism.\n\n"
        f"Pick one:\n"
        f"  (a) declare ``advertised_tools: set[str] = {{...}}`` on "
        f"{cls.__name__} (auto-registers via __init_subclass__)\n"
        f"  (b) call adcp.server.mcp_tools.register_handler_tools("
        f"{cls.__name__!r}, {{...}}) before serve()\n"
        f"  (c) pass advertise_all=True to serve() to acknowledge the "
        f"full advertisement\n\n"
        f"Decisioning-platform adopters: codegen via "
        f"`uv run python scripts/generate_decisioning_handler.py` "
        f"emits the declaration for you.",
        UserWarning,
        stacklevel=stacklevel,
    )


async def _dispatch_with_middleware(
    middleware: tuple[SkillMiddleware, ...] | Sequence[SkillMiddleware],
    skill_name: str,
    params: dict[str, Any],
    context: ToolContext,
    call_handler: Callable[[], Awaitable[Any]],
) -> Any:
    """Run ``call_handler`` wrapped in the supplied middleware chain.

    Shared by the MCP and A2A dispatch paths so composition semantics
    stay identical across transports — middleware porting between
    ``create_mcp_server(middleware=...)`` and
    ``create_a2a_server(middleware=...)`` needs zero changes.

    Outermost-first composition: the first entry in ``middleware`` sees
    every call *before* later entries and *before* the handler. No
    mutable indices, no loop-variable captures — a small recursive
    dispatcher reads the same with zero or ten middlewares.

    Middleware exceptions propagate to the caller unchanged; this
    function does no try/except so short-circuiting, transform, and
    exception-observation behaviors are owned by the transport-level
    executor, not the composer.
    """
    if not middleware:
        return await call_handler()

    async def _step(index: int) -> Any:
        if index >= len(middleware):
            return await call_handler()
        mw = middleware[index]

        async def call_next() -> Any:
            return await _step(index + 1)

        return await mw(skill_name, params, context, call_next)

    return await _step(0)


ContextFactory = Callable[[RequestMetadata], ToolContext]
"""Factory invoked per tool call to build a :class:`ToolContext`.

The SDK's server-side idempotency middleware reads
``ToolContext.caller_identity`` (and ``tenant_id`` for multi-tenant
scope) for cache keying, so factories wiring auth MUST populate
``caller_identity``. See :class:`~adcp.server.base.ToolContext` for
the full field contract.

The SDK deliberately does not know how your auth middleware surfaces
the authenticated principal — different downstreams use Starlette
``request.state``, ``contextvars.ContextVar``, thread-locals, etc.
The factory closes over whatever mechanism your middleware populates
and returns a ``ToolContext`` (or subclass).

Example using ``contextvars`` (recommended — middleware-agnostic)::

    from contextvars import ContextVar
    from adcp.server import RequestMetadata, ToolContext, create_mcp_server

    _principal: ContextVar[str | None] = ContextVar(
        "adcp_principal", default=None
    )
    _tenant: ContextVar[str | None] = ContextVar(
        "adcp_tenant", default=None
    )

    # Your HTTP middleware sets the ContextVars; tool calls read them.
    def build_context(meta: RequestMetadata) -> ToolContext:
        return ToolContext(
            request_id=meta.request_id,
            caller_identity=_principal.get(),
            tenant_id=_tenant.get(),
            metadata={"tool_name": meta.tool_name, "transport": meta.transport},
        )

    mcp = create_mcp_server(MyAgent(), context_factory=build_context)
"""


def serve(
    handler: ADCPHandler[Any] | Any,
    *,
    name: str = "adcp-agent",
    port: int | None = None,
    host: str | None = None,
    transport: str = "streamable-http",
    instructions: str | None = None,
    test_controller: TestControllerStore | None = None,
    context_factory: ContextFactory | None = None,
    task_store: TaskStore | None = None,
    push_config_store: PushNotificationConfigStore | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    asgi_middleware: Sequence[tuple[type, dict[str, Any]]] | None = None,
    message_parser: MessageParser | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    streaming_responses: bool = False,
    validation: ValidationHookConfig | None = None,
) -> None:
    """Start an MCP or A2A server from an ADCP handler or server builder.

    Accepts either an ``ADCPHandler`` instance or an ``ADCPServerBuilder``
    (from ``adcp_server()``). Builders are auto-converted via ``build_handler()``.

    This is the simplest way to run an ADCP agent. Set ``transport="a2a"``
    to serve over the A2A protocol instead of MCP, or ``transport="both"``
    to serve both protocols on the same port (MCP at ``/mcp``, A2A at
    ``/``).

    Args:
        handler: An ADCPHandler subclass instance with your tool implementations.
        name: Server name shown to clients / in the A2A agent card.
        port: Port to listen on. Defaults to PORT env var, then 3001.
        transport: ``"streamable-http"`` (default, MCP), ``"a2a"``, or
            ``"both"`` (one Starlette binary serving MCP at ``/mcp``
            and A2A at ``/``). Use ``"both"`` when you want adopters
            on either protocol to reach the same handler with shared
            ``context_factory`` + ``middleware`` wiring — JS hosts both
            on one Express app; this is the Python parity.
        instructions: Optional system instructions for the agent (MCP only).
        test_controller: Optional TestControllerStore instance for storyboard testing.
        context_factory: Optional factory that builds a :class:`ToolContext`
            per tool call — see :data:`ContextFactory`.
        task_store: Optional a2a-sdk ``TaskStore`` for durable A2A task
            persistence (A2A transport only). Defaults to ``InMemoryTaskStore``
            — tasks don't survive restart. See
            ``examples/a2a_db_tasks.py`` for the production pattern.
        push_config_store: Optional a2a-sdk ``PushNotificationConfigStore``
            for push-notif subscription persistence (A2A transport only).
            When unset, a2a-sdk surfaces the push-notif endpoints as
            ``UnsupportedOperationError`` — clients cannot register
            subscriptions at all. See ``examples/a2a_db_tasks.py`` for
            a durable reference implementation.
        middleware: Optional sequence of :data:`SkillMiddleware` callables
            wrapping every skill dispatch on both the MCP and A2A
            transports. Use for audit logging, activity-feed hooks,
            rate limiting, tracing. Composes outermost-first. See
            :data:`SkillMiddleware` for the signature and composition
            semantics.
        asgi_middleware: Optional sequence of ``(MiddlewareClass, kwargs)``
            tuples — Starlette-shape ASGI middleware applied to the
            outer HTTP app before uvicorn binds. Use for cross-cutting
            HTTP concerns the SDK does not own: tenant resolution
            (:class:`adcp.server.SubdomainTenantMiddleware`), CORS,
            request-id propagation, IP allowlists, custom auth.
            Composes outermost-first — the first entry sees every
            request before later entries. Each class is invoked as
            ``cls(app, **kwargs)``. Applied on every HTTP transport
            (``streamable-http``, ``a2a``, ``both``); ignored on
            ``stdio``.

            Middleware sees ``lifespan`` and ``websocket`` scopes in
            addition to ``http`` — guard non-HTTP scopes by passing
            them through unchanged (``if scope['type'] != 'http':
            await self.app(scope, receive, send); return``) so the
            framework's lifespan composition still runs.
        message_parser: Optional
            :data:`~adcp.server.a2a_server.MessageParser` callable for
            alternative A2A wire shapes (A2A transport only). The
            default parser handles ``DataPart(data={"skill": ...,
            "parameters": ...})`` plus a TextPart JSON fallback; supply
            this hook to accept JSON-RPC 2.0 message bodies or vendor-
            specific DataPart schemas. MCP does not use this kwarg
            (FastMCP owns the wire shape).
        advertise_all: When True, advertise every tool the handler type
            supports even if the subclass didn't override the method.
            Defaults to ``False`` — ``tools/list`` only shows tools the
            handler actually implements, which dramatically shrinks the
            advertised surface. Turn on for spec-compliance storyboards
            or when you want to signal ``not_supported`` on a specific
            tool to clients.
        max_request_size: Maximum request body size in bytes. Defaults
            to 10 MB. Set higher for sellers that legitimately transmit
            very large creative asset payloads; set lower for stricter
            public-facing deployments. Set to ``0`` to disable the cap
            entirely (not recommended — the cap is the only guard
            against adversarial payloads exhausting Pydantic validation
            CPU/memory). See :mod:`adcp.server._size_limit`.
        host: Network interface to bind to (MCP transports only). Defaults
            to the ``ADCP_HOST`` environment variable, then ``"0.0.0.0"``
            (all interfaces). Use ``"127.0.0.1"`` for local-only
            development. Container deployments (Fly.io, k8s, Cloud Run)
            require ``"0.0.0.0"`` so the process listens on the
            container's external interface.
        streaming_responses: When ``False`` (default), the streamable-http
            transport returns one ``application/json`` response per
            request. AdCP tools today don't emit progress events, and
            FastMCP's SSE-internal streaming default has an upstream bug
            that drops the ASGI response without completing — making the
            storyboard runner report ``overall_status: "unreachable"``.
            Set to ``True`` only if your tools genuinely emit progress
            notifications and your clients consume the SSE stream
            (MCP transports only). Note: the legacy ``transport="sse"``
            is a separate (deprecated) MCP transport, unrelated to this
            flag.
        validation: Optional :class:`ValidationHookConfig` enabling
            schema validation of every request and response against the
            bundled AdCP JSON schemas. ``requests="strict"`` raises
            ``VALIDATION_ERROR`` before the handler runs on a malformed
            payload; ``responses="strict"`` raises after the handler
            returns when the response shape drifts from spec. Sellers
            who want their server to enforce wire conformance pass
            ``ValidationHookConfig(requests="strict", responses="strict")``;
            the default ``None`` keeps validation off (zero overhead).
            Applies to both MCP and A2A transports.

    Security:
        This function does NOT configure authentication. In production,
        use a reverse proxy or middleware that validates credentials
        before forwarding to the endpoint. Without authentication,
        MCP exposes tools/list and A2A exposes /.well-known/agent.json,
        both of which reveal the agent's full capability surface.

    Example (MCP):
        from adcp.server import ADCPHandler, serve
        from adcp.server.responses import capabilities_response

        class MyAgent(ADCPHandler):
            async def get_adcp_capabilities(self, params, context=None):
                return capabilities_response(["media_buy"])

        serve(MyAgent(), name="my-agent")

    Example (A2A):
        serve(MyAgent(), name="my-agent", transport="a2a")

    With test controller:
        from adcp.server.test_controller import TestControllerStore

        class MyStore(TestControllerStore):
            async def force_account_status(self, account_id, status):
                ...

        serve(MyAgent(), name="my-agent", test_controller=MyStore())
    """
    # Accept ADCPServerBuilder from adcp_server() decorator pattern
    from adcp.server.builder import ADCPServerBuilder

    if isinstance(handler, ADCPServerBuilder):
        if not name or name == "adcp-agent":
            name = handler.name
        handler = handler.build_handler()

    if transport == "a2a":
        _serve_a2a(
            handler,
            name=name,
            port=port,
            test_controller=test_controller,
            context_factory=context_factory,
            task_store=task_store,
            push_config_store=push_config_store,
            middleware=middleware,
            asgi_middleware=asgi_middleware,
            message_parser=message_parser,
            advertise_all=advertise_all,
            max_request_size=max_request_size,
            validation=validation,
        )
    elif transport in ("streamable-http", "sse", "stdio"):
        _serve_mcp(
            handler,
            name=name,
            port=port,
            host=host,
            transport=transport,
            instructions=instructions,
            test_controller=test_controller,
            context_factory=context_factory,
            middleware=middleware,
            asgi_middleware=asgi_middleware,
            advertise_all=advertise_all,
            max_request_size=max_request_size,
            streaming_responses=streaming_responses,
            validation=validation,
        )
    elif transport == "both":
        _serve_mcp_and_a2a(
            handler,
            name=name,
            port=port,
            host=host,
            instructions=instructions,
            test_controller=test_controller,
            context_factory=context_factory,
            task_store=task_store,
            push_config_store=push_config_store,
            middleware=middleware,
            asgi_middleware=asgi_middleware,
            message_parser=message_parser,
            advertise_all=advertise_all,
            max_request_size=max_request_size,
            streaming_responses=streaming_responses,
            validation=validation,
        )
    else:
        valid = ", ".join(sorted(("a2a", "both", "streamable-http", "sse", "stdio")))
        raise ValueError(f"Unknown transport {transport!r}. Valid: {valid}")


def _apply_asgi_middleware(
    app: Any,
    asgi_middleware: Sequence[tuple[type, dict[str, Any]]] | None,
) -> Any:
    """Wrap ``app`` with operator-supplied Starlette-style ASGI middleware.

    Each entry is ``(MiddlewareClass, kwargs)`` and is invoked as
    ``cls(app, **kwargs)``. Composition is outermost-first — the first
    entry sees every request before later entries — so we wrap in
    reverse, matching :meth:`Starlette.add_middleware` semantics.

    No-op when the sequence is empty or ``None``.
    """
    if not asgi_middleware:
        return app
    for cls, kwargs in reversed(list(asgi_middleware)):
        app = cls(app, **kwargs)
    return app


def _wrap_with_path_normalize(app: Any) -> Any:
    """Wrap an ASGI app so trailing-slash variants of the same path
    route to the same handler instead of returning 307.

    The FastMCP streamable-http app mounts the JSON-RPC endpoint at
    ``/mcp`` (no trailing slash). Buyer libraries that POST to
    ``/mcp/`` get a 307 redirect, which:

    1. Costs an extra RTT per call (visible in the access log;
       Emma signals + AudioStack reports both noted this).
    2. Silently breaks buyer libs that don't follow redirects on POST
       (most HTTP clients don't, by default — POSTing to a redirect
       reverts to GET on the redirected URL, losing the body).

    Stripping a single trailing slash before dispatch is the standard
    fix; this middleware mutates ``scope["path"]`` and
    ``scope["raw_path"]`` in-place so downstream routing sees the
    canonical form. Only applies to non-root paths so we don't
    accidentally route ``/`` to ``''``.
    """

    async def _middleware(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in {"http", "websocket"}:
            path = scope.get("path", "")
            if len(path) > 1 and path.endswith("/"):
                # Mutate the scope's mutable copy — Starlette guarantees
                # a fresh dict per request so this doesn't leak across
                # connections.
                new_scope = dict(scope)
                new_scope["path"] = path.rstrip("/")
                raw_path = new_scope.get("raw_path")
                if isinstance(raw_path, bytes) and len(raw_path) > 1 and raw_path.endswith(b"/"):
                    new_scope["raw_path"] = raw_path.rstrip(b"/")
                scope = new_scope
        await app(scope, receive, send)

    return _middleware


def _wrap_with_size_limit(app: Any, max_request_size: int | None) -> Any:
    """Wrap an ASGI app with the request-body size cap.

    ``None`` = use the module default (10 MB). ``0`` = disable — skip
    the middleware entirely so sellers who genuinely need unlimited
    bodies can opt out. Any positive int overrides the default.

    Negative values raise ``ValueError`` — they have no meaningful
    interpretation and almost certainly indicate a typo (e.g. the
    author meant ``0`` for "disable" or a positive cap for "N bytes").
    Failing loudly at configure time beats a silent opt-out that only
    surfaces when an attacker finds it.
    """
    import logging

    from adcp.server._size_limit import (
        DEFAULT_MAX_REQUEST_BYTES,
        RequestSizeLimitMiddleware,
    )

    if max_request_size is not None and max_request_size < 0:
        raise ValueError(
            f"max_request_size must be >= 0 (got {max_request_size}). "
            "Use 0 to disable the cap entirely, or a positive int in bytes."
        )
    if max_request_size == 0:
        # Load-bearing warning — 0 disables the only Pydantic-validation
        # DoS guard. Operators should know, and a typo that lands on 0
        # should leave a breadcrumb in the startup log rather than
        # silently opt out.
        logging.getLogger("adcp.server").warning(
            "max_request_size=0 disables ASGI body cap; relying on upstream "
            "proxy or WAF to bound request size. This is a security-relevant "
            "configuration choice."
        )
        return app
    cap = max_request_size if max_request_size is not None else DEFAULT_MAX_REQUEST_BYTES
    return RequestSizeLimitMiddleware(app, max_bytes=cap)


def _bind_reusable_socket(host: str, port: int) -> Any:
    """Create a listening socket with SO_REUSEADDR set.

    Without ``SO_REUSEADDR``, rapid restarts (common during tests and
    storyboard runs) hit ``TIME_WAIT`` on the prior socket and the new
    process hangs on bind for up to 2×MSL (roughly a minute on macOS).
    Setting ``SO_REUSEADDR`` on the listening socket is the standard,
    portable fix on Linux and macOS; it is safe because listeners are
    unique by (addr, port) and the kernel still rejects a second live
    listener on the same tuple.

    On Windows ``SO_REUSEADDR`` has different semantics (it allows
    hijacking a live listener). FastMCP's streamable-http and uvicorn
    support Windows, so we guard with ``SO_EXCLUSIVEADDRUSE`` there —
    but since the ADCP server primarily targets Linux/macOS and the
    Windows path is rarely exercised, the guard is best-effort.

    EADDRINUSE collisions (port already bound by another process) are
    re-raised as ``OSError`` with a friendly remediation hint —
    every Emma backend test reported being lost in a raw ``[Errno 48]
    Address already in use`` with no pointer to the fix. The wrapped
    error tells adopters exactly what to do (set ``port=`` or
    ``ADCP_PORT``).
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            # Windows: prevent hijacking; don't set SO_REUSEADDR.
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
        sock.set_inheritable(True)
    except OSError as exc:
        sock.close()
        # EADDRINUSE on Linux/macOS = errno 98/48 (per platform). The
        # raw message is opaque ("[Errno 48] Address already in use"
        # — Emma reports flagged this as P1 friction). Project to a
        # remediation-bearing message that points adopters at the
        # ``port=`` / ``ADCP_PORT`` knobs.
        import errno

        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
            raise OSError(
                exc.errno,
                (
                    f"Port {port} on {host} is already in use — another process "
                    "is bound there (a stale dev server, a peer agent, or your "
                    "previous run). Pick a different port: pass ``port=<N>`` to "
                    "``adcp.decisioning.serve.serve(...)`` (or "
                    "``adcp.server.serve(...)``), or set the ``ADCP_PORT`` "
                    "environment variable. Default ADCP port is 3001 — common "
                    "alternates are 3011, 3021, 8080."
                ),
            ) from exc
        raise
    except Exception:
        sock.close()
        raise
    return sock


def _serve_mcp(
    handler: ADCPHandler[Any],
    *,
    name: str,
    port: int | None,
    host: str | None = None,
    transport: str,
    instructions: str | None,
    test_controller: TestControllerStore | None,
    context_factory: ContextFactory | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    asgi_middleware: Sequence[tuple[type, dict[str, Any]]] | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    streaming_responses: bool = False,
    validation: ValidationHookConfig | None = None,
) -> None:
    """Start an MCP server."""
    mcp = create_mcp_server(
        handler,
        name=name,
        port=port,
        host=host,
        instructions=instructions,
        include_test_controller=test_controller is not None,
        context_factory=context_factory,
        middleware=middleware,
        advertise_all=advertise_all,
        streaming_responses=streaming_responses,
        validation=validation,
    )

    if test_controller is not None:
        from adcp.server.test_controller import register_test_controller

        register_test_controller(mcp, test_controller, context_factory=context_factory)

    if transport in ("streamable-http", "sse"):
        _run_mcp_http(
            mcp,
            transport=transport,
            max_request_size=max_request_size,
            asgi_middleware=asgi_middleware,
        )
    else:
        # stdio — no listening socket, nothing to configure.
        mcp.run(transport=transport)


def _run_mcp_http(
    mcp: Any,
    *,
    transport: str,
    max_request_size: int | None = None,
    asgi_middleware: Sequence[tuple[type, dict[str, Any]]] | None = None,
) -> None:
    """Run FastMCP's HTTP transports with a pre-bound SO_REUSEADDR socket.

    FastMCP builds its own ``uvicorn.Server(config).serve()`` inside
    ``run_*_async`` and does not expose hooks to pass a pre-bound socket,
    so we reproduce the minimal setup here and hand uvicorn the socket
    directly via ``Server.serve([sock])``. This keeps the public surface
    (``serve()``) unchanged while fixing the readiness-flake on reruns.
    """
    import anyio
    import uvicorn

    host = getattr(mcp.settings, "host", "0.0.0.0")
    port = int(mcp.settings.port)
    log_level = getattr(mcp.settings, "log_level", "INFO").lower()

    if transport == "streamable-http":
        app = mcp.streamable_http_app()
    else:
        app = mcp.sse_app()

    app = _wrap_with_path_normalize(app)
    app = _wrap_with_size_limit(app, max_request_size)
    app = _apply_asgi_middleware(app, asgi_middleware)

    sock = _bind_reusable_socket(host, port)
    try:
        # One INFO line at the bind boundary so adopters know exactly
        # what URL the buyer should hit. uvicorn's default startup logs
        # are filtered/quieted in many configurations; this line is
        # framework-controlled and always lands. Emma signals/sales
        # backend tests both flagged silent-boot as P1 friction.
        mcp_path = "/mcp" if transport == "streamable-http" else "/sse"
        logger.info(
            "MCP listening on http://%s:%s%s (transport=%s)",
            host,
            port,
            mcp_path,
            transport,
        )
        config = uvicorn.Config(app, log_level=log_level)
        server = uvicorn.Server(config)

        async def _serve() -> None:
            await server.serve(sockets=[sock])

        anyio.run(_serve)
    finally:
        sock.close()


def _serve_a2a(
    handler: ADCPHandler[Any],
    *,
    name: str,
    port: int | None,
    test_controller: TestControllerStore | None,
    context_factory: ContextFactory | None = None,
    task_store: TaskStore | None = None,
    push_config_store: PushNotificationConfigStore | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    asgi_middleware: Sequence[tuple[type, dict[str, Any]]] | None = None,
    message_parser: MessageParser | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    validation: ValidationHookConfig | None = None,
) -> None:
    """Start an A2A server using uvicorn."""
    import uvicorn

    from adcp.server.a2a_server import create_a2a_server

    resolved_port = port or int(os.environ.get("PORT", "3001"))

    app = create_a2a_server(
        handler,
        name=name,
        port=resolved_port,
        test_controller=test_controller,
        context_factory=context_factory,
        task_store=task_store,
        push_config_store=push_config_store,
        middleware=middleware,
        message_parser=message_parser,
        advertise_all=advertise_all,
        validation=validation,
    )
    app = _wrap_with_size_limit(app, max_request_size)
    app = _apply_asgi_middleware(app, asgi_middleware)
    sock = _bind_reusable_socket("0.0.0.0", resolved_port)
    try:
        # Same bind-boundary INFO as the MCP path so A2A adopters
        # also see one framework-controlled line confirming the
        # listener is up.
        logger.info("A2A listening on http://0.0.0.0:%s/", resolved_port)
        config = uvicorn.Config(app)
        server = uvicorn.Server(config)
        import anyio

        async def _serve() -> None:
            await server.serve(sockets=[sock])

        anyio.run(_serve)
    finally:
        sock.close()


def _build_mcp_and_a2a_app(
    handler: ADCPHandler[Any],
    *,
    name: str,
    port: int,
    host: str,
    instructions: str | None,
    test_controller: TestControllerStore | None,
    context_factory: ContextFactory | None = None,
    task_store: TaskStore | None = None,
    push_config_store: PushNotificationConfigStore | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    message_parser: MessageParser | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    streaming_responses: bool = False,
    validation: ValidationHookConfig | None = None,
) -> Any:
    """Build the unified MCP+A2A ASGI app without starting a server.

    Split out from :func:`_serve_mcp_and_a2a` so tests can route
    requests through Starlette's ``TestClient`` against the same
    dispatcher production uses.

    Returns the size-limit-wrapped ASGI app. Wire to uvicorn /
    Starlette / your test harness as you would any other ASGI app.
    """
    import contextlib

    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Receive, Scope, Send

    from adcp.server.a2a_server import create_a2a_server

    # MCP app — FastMCP registers its streamable-http endpoint at
    # ``streamable_http_path`` (default ``/mcp``). The dispatcher
    # below preserves the full request path when routing to MCP, so
    # the inner Starlette router matches ``/mcp`` directly without
    # needing a Mount-based prefix strip.
    mcp = create_mcp_server(
        handler,
        name=name,
        port=port,
        host=host,
        instructions=instructions,
        include_test_controller=test_controller is not None,
        context_factory=context_factory,
        middleware=middleware,
        advertise_all=advertise_all,
        streaming_responses=streaming_responses,
        validation=validation,
    )
    if test_controller is not None:
        from adcp.server.test_controller import register_test_controller

        register_test_controller(mcp, test_controller, context_factory=context_factory)
    mcp_inner = mcp.streamable_http_app()
    # Wrap with the standard trailing-slash normalizer so ``/mcp/``
    # and ``/mcp`` resolve to the same FastMCP endpoint. Keep the
    # unwrapped ``mcp_inner`` reference so the lifespan composer
    # below can reach ``.router.lifespan_context``.
    mcp_app = _wrap_with_path_normalize(mcp_inner)

    # A2A app — built via the a2a-sdk wrapper. It mounts at the root
    # of its own app and handles ``/.well-known/agent.json``, ``/``,
    # and the message / push-notif endpoints.
    a2a_app = create_a2a_server(
        handler,
        name=name,
        port=port,
        test_controller=test_controller,
        context_factory=context_factory,
        task_store=task_store,
        push_config_store=push_config_store,
        middleware=middleware,
        message_parser=message_parser,
        advertise_all=advertise_all,
        validation=validation,
    )

    # Lifespan composition: FastMCP's session manager initializes a
    # task group on startup; a2a-sdk's stores have their own init.
    # Compose both inner lifespans on a parent Starlette; the
    # dispatcher routes ``lifespan`` scope events to the parent so
    # both initializers run before any request lands.
    @contextlib.asynccontextmanager
    async def _composed_lifespan(_app):  # type: ignore[no-untyped-def]
        async with mcp_inner.router.lifespan_context(mcp_inner):
            async with a2a_app.router.lifespan_context(a2a_app):
                yield

    parent = Starlette(lifespan=_composed_lifespan)

    async def _dispatch(scope: Scope, receive: Receive, send: Send) -> None:
        """Path-based ASGI dispatcher.

        ``/mcp`` and ``/mcp/...`` route to the FastMCP streamable-http
        app with the full original path preserved (FastMCP's inner
        route is at ``/mcp``). Everything else goes to A2A. Lifespan
        events route to the parent Starlette which composes both
        inner lifespans.
        """
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                await mcp_app(scope, receive, send)
                return
            await a2a_app(scope, receive, send)
            return
        if scope["type"] == "lifespan":
            await parent(scope, receive, send)
            return
        # Websocket and other scopes: route to A2A by default. MCP
        # streamable-http doesn't use websockets; A2A doesn't either
        # in the default a2a-sdk shape, but if either grows that
        # surface the dispatcher needs an explicit branch.
        await a2a_app(scope, receive, send)

    app: ASGIApp = _dispatch
    return _wrap_with_size_limit(app, max_request_size)


def _serve_mcp_and_a2a(
    handler: ADCPHandler[Any],
    *,
    name: str,
    port: int | None,
    host: str | None = None,
    instructions: str | None,
    test_controller: TestControllerStore | None,
    context_factory: ContextFactory | None = None,
    task_store: TaskStore | None = None,
    push_config_store: PushNotificationConfigStore | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    asgi_middleware: Sequence[tuple[type, dict[str, Any]]] | None = None,
    message_parser: MessageParser | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    streaming_responses: bool = False,
    validation: ValidationHookConfig | None = None,
) -> None:
    """Serve MCP and A2A on a single port via path dispatch.

    JS sellers host both transports on one Express/Hono app; this is
    the Python parity. Build both apps independently with the same
    handler, ``context_factory``, ``middleware``, etc., then route
    by URL path: ``/mcp`` and ``/mcp/...`` go to the MCP streamable-http
    app, everything else (``/``, ``/.well-known/agent.json``,
    A2A push endpoints) goes to the A2A app.

    Both apps see the same ``ToolContext`` and middleware chain because
    they share the same ``handler`` instance — adopters writing audit
    or rate-limit middleware get one wiring point that applies to both
    transports automatically.
    """
    import anyio
    import uvicorn

    resolved_port = port or int(os.environ.get("PORT", "3001"))
    resolved_host = host or os.environ.get("ADCP_HOST", "0.0.0.0")
    log_level = "info"

    app = _build_mcp_and_a2a_app(
        handler,
        name=name,
        port=resolved_port,
        host=resolved_host,
        instructions=instructions,
        test_controller=test_controller,
        context_factory=context_factory,
        task_store=task_store,
        push_config_store=push_config_store,
        middleware=middleware,
        message_parser=message_parser,
        advertise_all=advertise_all,
        max_request_size=max_request_size,
        streaming_responses=streaming_responses,
        validation=validation,
    )
    app = _apply_asgi_middleware(app, asgi_middleware)

    sock = _bind_reusable_socket(resolved_host, resolved_port)
    try:
        logger.info(
            "MCP+A2A unified listening on http://%s:%s " "(MCP at /mcp, A2A at /)",
            resolved_host,
            resolved_port,
        )
        config = uvicorn.Config(app, log_level=log_level)
        server = uvicorn.Server(config)

        async def _serve() -> None:
            await server.serve(sockets=[sock])

        anyio.run(_serve)
    finally:
        sock.close()


def create_mcp_server(
    handler: ADCPHandler[Any],
    *,
    name: str = "adcp-agent",
    port: int | None = None,
    host: str | None = None,
    instructions: str | None = None,
    include_test_controller: bool = False,
    context_factory: ContextFactory | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    advertise_all: bool = False,
    streaming_responses: bool = False,
    validation: ValidationHookConfig | None = None,
) -> Any:
    """Create a FastMCP server from an ADCP handler without starting it.

    Use this when you need to customize the server before running it,
    or when you need to add extra non-ADCP tools.

    Args:
        handler: An ADCPHandler subclass instance.
        name: Server name.
        port: Port to listen on.
        instructions: Optional system instructions.
        include_test_controller: When False (default), skip registering
            ``comply_test_controller`` as a handler tool. Sellers who want
            compliance-testing support should pass ``test_controller=`` to
            :func:`serve`, which registers a store-backed implementation
            via :func:`register_test_controller` and sets this flag
            implicitly. Registering the handler stub unconditionally would
            advertise a tool the seller didn't opt into.
        context_factory: Optional callable invoked per tool call to build
            a :class:`ToolContext` from the incoming :class:`RequestMetadata`.
            **Wiring this is how the server-side idempotency middleware
            gets the caller identity and tenant it needs for per-principal
            scoping** — a factory that returns ``caller_identity=None``
            effectively disables idempotency dedup. Sellers wiring their
            own HTTP auth middleware pass this to inject the authenticated
            principal into ``ToolContext.caller_identity``. See
            :data:`ContextFactory` for the recommended contextvars
            pattern. When ``None``, handlers receive a bare
            ``ToolContext()`` (no caller identity, no tenant).
        middleware: Optional sequence of :data:`SkillMiddleware` callables
            wrapping every tool dispatch. Symmetric with A2A's
            ``create_a2a_server(middleware=...)`` — the same list works
            on both transports. Use for audit logging, rate limiting,
            tracing, activity-feed hooks. See :data:`SkillMiddleware`
            for signature and composition semantics.
        advertise_all: When True, advertise every tool the handler type
            supports — even those whose method is still the SDK's
            ``not_supported`` default. Defaults to ``False``, which
            shrinks ``tools/list`` to only the tools the handler
            actually implements (subclass overrode the method). See
            :func:`~adcp.server.get_tools_for_handler` for semantics;
            use ``True`` for spec-compliance storyboards or when you
            deliberately want to expose a ``not_supported`` tool.
        host: Network interface to bind to. Defaults to the ``ADCP_HOST``
            environment variable, then ``"0.0.0.0"`` (all interfaces).
            Use ``"127.0.0.1"`` for local-only development.
        streaming_responses: When ``False`` (default), the streamable-http
            transport returns one ``application/json`` response per
            request — the right shape for AdCP tools today (none of which
            emit progress events). The FastMCP SSE-internal streaming
            default also has an upstream bug that drops the ASGI response
            without completing, blocking the storyboard runner. Set to
            ``True`` only if your tools genuinely emit progress
            notifications and your clients consume the SSE stream.

    Returns:
        A configured FastMCP server instance. Call ``mcp.run()`` to start,
        or ``mcp.streamable_http_app()`` to get the Starlette ASGI app for
        mounting behind a reverse proxy / adding HTTP middleware.

    Authentication:
        The SDK does not enforce authentication itself. Two integration
        patterns work:

        1. **Reverse-proxy auth** (simplest): the proxy (nginx, Caddy,
           Envoy) validates credentials and forwards only authenticated
           requests. The SDK trusts the proxy's decision.

        2. **In-process HTTP middleware**: call
           ``mcp.streamable_http_app()`` to get the Starlette app, then
           ``app.add_middleware(YourAuthMiddleware)``. The middleware
           extracts auth state per request (token, tenant, principal)
           into ContextVars; ``context_factory`` reads those to build a
           typed ``ToolContext``. Tools in
           :data:`adcp.server.DISCOVERY_TOOLS` (``get_adcp_capabilities``)
           should bypass auth per AdCP spec. See
           ``examples/mcp_with_auth_middleware.py`` and
           ``docs/handler-authoring.md``.

    Example (basic):
        >>> mcp = create_mcp_server(MyAgent(), name="my-agent")
        >>> mcp.run(transport="streamable-http")

    Example (custom auth + typed context via contextvars):
        >>> from contextvars import ContextVar
        >>> from adcp.server import RequestMetadata, ToolContext, create_mcp_server
        >>>
        >>> _principal: ContextVar[str | None] = ContextVar("p", default=None)
        >>> _tenant: ContextVar[str | None] = ContextVar("t", default=None)
        >>>
        >>> def build_context(meta: RequestMetadata) -> ToolContext:
        ...     return ToolContext(
        ...         caller_identity=_principal.get(),
        ...         tenant_id=_tenant.get(),
        ...     )
        >>>
        >>> mcp = create_mcp_server(
        ...     MyAgent(), name="my-agent", context_factory=build_context
        ... )
        >>> app = mcp.streamable_http_app()
        >>> app.add_middleware(MyAuthMiddleware)  # sets the ContextVars
        >>> # run via uvicorn
    """
    from mcp.server.fastmcp import FastMCP

    resolved_port = port or int(os.environ.get("PORT", "3001"))
    resolved_host = host if host is not None else (os.environ.get("ADCP_HOST") or "0.0.0.0")
    mcp = FastMCP(name, instructions=instructions, port=resolved_port)
    mcp.settings.host = resolved_host
    if not streaming_responses:
        # FastMCP's SSE-internal default has an upstream bug; switching to
        # stateless JSON-response mode is also semantically correct for
        # AdCP tools, which return one complete envelope per request.
        mcp.settings.stateless_http = True
        mcp.settings.json_response = True
    _register_handler_tools(
        mcp,
        handler,
        include_test_controller=include_test_controller,
        context_factory=context_factory,
        middleware=middleware,
        advertise_all=advertise_all,
        validation=validation,
    )
    return mcp


def _register_handler_tools(
    mcp: Any,
    handler: ADCPHandler[Any],
    *,
    include_test_controller: bool = False,
    context_factory: ContextFactory | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    advertise_all: bool = False,
    validation: ValidationHookConfig | None = None,
) -> None:
    """Register all ADCP tools from a handler onto a FastMCP server."""
    # Freeze middleware ordering at registration time. Tuple both guards
    # against a mutable list being reshuffled mid-request and matches the
    # A2A executor's handling.
    middleware_tuple: tuple[SkillMiddleware, ...] = tuple(middleware or ())

    tool_defs = get_tools_for_handler(handler, advertise_all=advertise_all)
    registered: list[str] = []
    for tool_def in tool_defs:
        tool_name = tool_def["name"]
        # Gate comply_test_controller on explicit opt-in. The handler base
        # class has a not-supported stub; registering it as an MCP tool
        # would advertise compliance-testing the seller didn't declare.
        if tool_name == "comply_test_controller" and not include_test_controller:
            continue
        description = tool_def.get("description", "")
        input_schema = tool_def.get("inputSchema", {"type": "object", "properties": {}})
        output_schema = tool_def.get("outputSchema")
        caller = create_tool_caller(handler, tool_name, validation=validation)
        _register_tool(
            mcp,
            tool_name,
            description,
            input_schema,
            caller,
            context_factory=context_factory,
            middleware=middleware_tuple,
            output_schema=output_schema,
        )
        registered.append(tool_name)

    _log_advertised_tools(
        transport="mcp",
        handler=handler,
        advertise_all=advertise_all,
        registered=registered,
    )


def _register_tool(
    mcp: Any,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    caller: Callable[..., Any],
    *,
    context_factory: ContextFactory | None = None,
    middleware: tuple[SkillMiddleware, ...] = (),
    output_schema: dict[str, Any] | None = None,
) -> None:
    """Register a single ADCP tool on a FastMCP server.

    Creates a Tool with a permissive arg model that accepts any fields,
    then overrides the advertised schema with the Pydantic-generated one.
    This ensures MCP clients see the correct schema while the handler
    receives all parameters as a plain dict.
    """
    from mcp.server.fastmcp.tools import Tool
    from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
    from pydantic import ConfigDict

    from adcp.exceptions import ADCPError
    from adcp.server.translate import translate_error

    # Lazy import — decisioning is optional for non-platform handlers,
    # but when present its ``AdcpError`` carries structured ``details``
    # (caused_by, validation_errors) that ``translate_error`` now
    # understands. AudioStack Emma P0: pre-fix this exception class
    # propagated to FastMCP's default handler and ``details`` was lost.
    try:
        from adcp.decisioning.types import AdcpError as DecisioningAdcpError  # noqa: N813
    except Exception:
        DecisioningAdcpError = None  # type: ignore[assignment,misc]  # noqa: N806

    async def fn(**kwargs: Any) -> dict[str, Any]:
        # Caller identity: FastMCP does not expose an authenticated principal
        # at the SDK level (``Context.client_id`` is a session hint, not an
        # authenticated user). Sellers wire auth via HTTP middleware on
        # ``mcp.streamable_http_app()`` and pass ``context_factory`` to
        # ``create_mcp_server()`` — the factory reads a ``contextvars.ContextVar``
        # the middleware populates and returns a typed ``ToolContext``.
        # The A2A transport derives ``caller_identity`` from
        # ``ServerCallContext.user`` automatically.
        context: ToolContext | None = None
        if context_factory is not None:
            meta = RequestMetadata(tool_name=name, transport="mcp")
            context = context_factory(meta)
            if not isinstance(context, ToolContext):
                # Catch downstream factories that return a dict or other
                # shape early — otherwise the handler explodes deep inside
                # with an AttributeError on caller_identity.
                raise TypeError(
                    f"context_factory for tool {name!r} returned "
                    f"{type(context).__name__}, not a ToolContext instance"
                )

        async def _call_handler() -> Any:
            return await caller(kwargs, context=context)

        try:
            if middleware:
                # Middleware requires a concrete ToolContext to match the
                # declared SkillMiddleware signature; synthesise an empty
                # one when no factory is configured so the chain still
                # runs. Handler itself keeps receiving ``None`` semantics
                # via ``context`` closed over by _call_handler.
                mw_context = context if context is not None else ToolContext()
                result = await _dispatch_with_middleware(
                    middleware, name, kwargs, mw_context, _call_handler
                )
            else:
                result = await _call_handler()
        except ADCPError as exc:
            # Translate AdCP-typed exceptions (IdempotencyConflictError,
            # ADCPTaskError with a spec code, etc.) into a ToolError so FastMCP
            # surfaces ``is_error=true`` with the spec error code in the
            # message text. Clients per AdCP §transport-errors will extract
            # the code via either structuredContent.adcp_error (if populated)
            # or the text-fallback path.
            raise translate_error(exc, protocol="mcp") from exc
        except Exception as exc:
            # Decisioning ``AdcpError`` is NOT a subclass of
            # ``adcp.exceptions.ADCPError`` (different class hierarchy
            # — ``adcp.decisioning.types.AdcpError``). Without this
            # branch it propagated to FastMCP's default exception
            # handler and ``details`` was lost on the wire. AudioStack
            # Emma P0 confirmed pre-fix.
            if DecisioningAdcpError is not None and isinstance(exc, DecisioningAdcpError):
                # ``# type: ignore[arg-type]`` because mypy can't see
                # that ``translate_error`` accepts decisioning AdcpError
                # via the lazy-import branch.
                raise translate_error(exc, protocol="mcp") from exc  # type: ignore[arg-type]
            raise
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json", exclude_none=True)  # type: ignore[no-any-return]
        if isinstance(result, dict):
            return result
        return {"result": result}

    # Create tool from function (gives us proper fn_metadata scaffolding)
    tool = Tool.from_function(fn, name=name, description=description, structured_output=True)

    # Override the advertised schema with the Pydantic-generated one
    tool.parameters = input_schema

    # Override fn_metadata with a permissive model that passes through
    # all fields as individual kwargs (instead of wrapping in a "kwargs" field).
    # Keep the output_schema/output_model so structuredContent is populated.
    class _AdcpArgs(ArgModelBase):
        model_config = ConfigDict(extra="allow")

        def model_dump_one_level(self) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for field_name in self.__class__.model_fields:
                result[field_name] = getattr(self, field_name)
            if self.model_extra:
                result.update(self.model_extra)
            return result

    # Advertise the spec response schema on ``tools/list`` when one is
    # available. FastMCP serializes ``Tool.output_schema`` (which reads
    # ``fn_metadata.output_schema``) into the ``outputSchema`` field of
    # the ``tools/list`` response — matches the TS port. Falls back to
    # the auto-derived shape from the ``fn`` return annotation when no
    # spec schema is mapped (e.g. handler-only custom tools).
    effective_output_schema = (
        output_schema if output_schema is not None else tool.fn_metadata.output_schema
    )
    tool.fn_metadata = FuncMetadata(
        arg_model=_AdcpArgs,
        output_schema=effective_output_schema,
        output_model=tool.fn_metadata.output_model,
        wrap_output=False,
    )

    # FastMCP does not expose a public API for registering pre-built Tool
    # objects with custom schemas. This accesses internals; requires mcp>=1.23.
    mcp._tool_manager._tools[name] = tool
