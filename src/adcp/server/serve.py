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
import sys
import warnings
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

logger = logging.getLogger("adcp.server")

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import (
    _HANDLER_TOOLS,
    create_tool_caller,
    get_tools_for_handler,
)
from adcp.validation.client_hooks import (
    SERVER_DEFAULT_VALIDATION as DEFAULT_VALIDATION,
)
from adcp.validation.client_hooks import (
    ValidationHookConfig,
)

# Re-exported as ``adcp.server.serve.DEFAULT_VALIDATION`` for adopters who
# want a non-magic name when constructing their own
# ``ValidationHookConfig`` overrides. The canonical definition lives in
# :mod:`adcp.validation.client_hooks` so both the server-side and any
# future server-creation seam can share one constant without a circular
# import via this module.

if TYPE_CHECKING:
    from a2a.server.tasks.push_notification_config_store import (
        PushNotificationConfigStore,
    )
    from a2a.server.tasks.task_store import TaskStore

    from adcp.server.a2a_server import MessageParser, PublicUrlResolver
    from adcp.server.auth import BearerTokenAuth
    from adcp.server.test_controller import TestControllerStore


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
    :param request_context: The originating Starlette ``Request`` for
        HTTP-borne calls (MCP streamable-http, A2A). ``None`` for
        stdio MCP and any path that doesn't have a Request to thread.
        Use ``request_context.state`` to read per-request state set
        by ASGI middleware — this works in both stateless and stateful
        MCP modes, where the older :mod:`contextvars` pattern only
        works in stateless (the stateful session task is a separate
        async task and does not see middleware-set ContextVars).
        Typed as ``Any`` to keep this dataclass dependency-light;
        adopters can ``cast(Request, meta.request_context)``.
    """

    tool_name: str
    transport: Literal["mcp", "a2a"]
    request_id: str | None = None
    request_context: Any = None


LifespanHook = Callable[[], Awaitable[None]]
"""Zero-arg async callable invoked during server startup or shutdown.

Used by :func:`serve`'s ``on_startup`` and ``on_shutdown`` kwargs (and
the corresponding :class:`ServeConfig` fields). Startup hooks run after
the framework's own lifespan startup completes; shutdown hooks run
before the framework's own lifespan shutdown. A hook raising during
startup propagates as a Starlette ``lifespan.startup.failed`` event
which aborts the server boot.

Only honored on ``transport="both"`` today — single-transport paths
(``streamable-http``, ``sse``, ``a2a``, ``stdio``) raise ``ValueError``
when hooks are passed. See issue #709.
"""


@dataclass(frozen=True)
class ServeConfig:
    """Configuration bundle for :func:`serve`.

    Consolidates the 22 keyword arguments of :func:`serve` into a single
    named, IDE-friendly object.  Use either the bundled form or individual
    kwargs — not both::

        # Bundled (cleaner IDE signature, easy to share / reuse)
        serve(MyAgent(), config=ServeConfig(name="my-agent", transport="a2a"))

        # Individual kwargs (backwards-compatible, unchanged)
        serve(MyAgent(), name="my-agent", transport="a2a")

    When *config* is supplied, all field values come from it; any individual
    kwargs passed alongside are ignored.  To vary a single field from a
    shared base config use :func:`dataclasses.replace`::

        base = ServeConfig(name="my-agent", validation=strict)
        serve(handler, config=dataclasses.replace(base, transport="a2a"))

    **Transport-specific fields** — fields marked *(A2A only)* or
    *(MCP only)* are silently ignored by the other transport.  Setting
    cross-transport fields triggers a ``UserWarning`` at boot.
    """

    # --- Identity / networking ---
    name: str = "adcp-agent"
    port: int | None = None
    host: str | None = None
    transport: str = "streamable-http"

    # --- MCP only ---
    instructions: str | None = None
    streaming_responses: bool = False
    stateless_http: bool = False
    session_idle_timeout: float | None = 1800.0

    # --- A2A / both ---
    task_store: TaskStore | None = None
    push_config_store: PushNotificationConfigStore | None = None
    message_parser: MessageParser | None = None
    public_url: str | PublicUrlResolver | None = None

    # --- Shared infrastructure ---
    test_controller: TestControllerStore | None = None
    context_factory: ContextFactory | None = None
    middleware: Sequence[SkillMiddleware] | None = None
    asgi_middleware: Sequence[tuple[type, dict[str, Any]]] | None = None
    advertise_all: bool = False
    max_request_size: int | None = None
    validation: ValidationHookConfig | None = None
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None

    # --- Discovery manifest ---
    base_url: str | None = None
    specialisms: list[str] | None = None
    description: str | None = None

    # --- Lifespan hooks (transport="both" only today) ---
    on_startup: Sequence[LifespanHook] | None = None
    on_shutdown: Sequence[LifespanHook] | None = None

    # --- Debug endpoints ---
    enable_debug_endpoints: bool = False
    debug_traffic_source: Callable[[], dict[str, int]] | None = None

    def __post_init__(self) -> None:
        _a2a_only = ("task_store", "push_config_store", "message_parser", "public_url")
        # ``session_idle_timeout`` (default 1800.0) is excluded from
        # the warning list: the ``not in (None, False)`` heuristic
        # treats any non-falsy default as "set" and would fire
        # spuriously under transport='a2a'. ``stateless_http`` (default
        # False) and ``streaming_responses`` (default False) work
        # cleanly with the heuristic.
        _mcp_only = ("instructions", "streaming_responses", "stateless_http")
        if self.transport == "a2a":
            mcp_set = sorted(f for f in _mcp_only if getattr(self, f) not in (None, False))
            if mcp_set:
                warnings.warn(
                    f"ServeConfig sets MCP-only fields {mcp_set} but "
                    f"transport='a2a'. These fields will be ignored.",
                    UserWarning,
                    stacklevel=3,
                )
        elif self.transport not in ("both", "streamable-http", "sse", "stdio"):
            pass  # unknown transport — let serve() raise a clear error
        elif self.transport not in ("a2a", "both"):
            a2a_set = sorted(f for f in _a2a_only if getattr(self, f) is not None)
            if a2a_set:
                warnings.warn(
                    f"ServeConfig sets A2A-only fields {a2a_set} but "
                    f"transport={self.transport!r}. These fields will be ignored.",
                    UserWarning,
                    stacklevel=3,
                )


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


def _get_starlette_request_for_dispatch() -> Any:
    """Return the Starlette ``Request`` for the in-flight MCP tool call, if
    any — else ``None``.

    The MCP lowlevel server stashes the originating ``Request`` in a
    contextvar (``mcp.server.lowlevel.server.request_ctx``) for the
    duration of each dispatched request, in both stateless and stateful
    modes. The contextvar lives in the dispatch sub-task that the
    session task spawned (``tg.start_soon(_handle_message, ...)``), so
    the value reachable here is the originating request — not the
    session-creation request — even when the streamable-http transport
    holds a long-lived session task.

    Returns ``None`` when called outside an MCP dispatch (e.g. from the
    server-builder smoke tests, or from A2A's executor which has its
    own context channel via ``ServerCallContext``).
    """
    try:
        from mcp.server.lowlevel.server import request_ctx
    except ImportError:  # pragma: no cover — mcp pin guarantees this
        return None
    try:
        ctx = request_ctx.get()
    except LookupError:
        return None
    return getattr(ctx, "request", None)


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

ASGIMiddlewareEntry = tuple[Callable[..., Any], dict[str, Any]] | Callable[..., Any]
"""A single ASGI middleware entry for :func:`serve`'s ``asgi_middleware`` param.

Each entry is either:

- A ``(callable, kwargs)`` tuple — invoked as ``callable(app, **kwargs)``.
  Both plain class constructors and :func:`functools.partial` instances work
  as the first element.
- A bare callable factory ``f(app) -> app`` — invoked as ``factory(app)``.

Both forms can be mixed in the same list.
"""


def serve(
    handler: ADCPHandler[Any] | Any,
    *,
    config: ServeConfig | None = None,
    name: str = "adcp-agent",
    port: int | None = None,
    host: str | None = None,
    transport: str = "streamable-http",
    instructions: str | None = None,
    test_controller: TestControllerStore | None = None,
    test_controller_account_resolver: Any | None = None,
    context_factory: ContextFactory | None = None,
    task_store: TaskStore | None = None,
    push_config_store: PushNotificationConfigStore | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    asgi_middleware: Sequence[ASGIMiddlewareEntry] | None = None,
    message_parser: MessageParser | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    streaming_responses: bool = False,
    stateless_http: bool = False,
    session_idle_timeout: float | None = 1800.0,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None,
    enable_debug_endpoints: bool = False,
    debug_traffic_source: Callable[[], dict[str, int]] | None = None,
    base_url: str | None = None,
    specialisms: list[str] | None = None,
    description: str | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
    enable_dns_rebinding_protection: bool | None = None,
    auth: BearerTokenAuth | None = None,
    public_url: str | PublicUrlResolver | None = None,
    on_startup: Sequence[LifespanHook] | None = None,
    on_shutdown: Sequence[LifespanHook] | None = None,
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
        config: Optional :class:`ServeConfig` bundle.  When supplied, all
            field values come from it and any individual kwargs passed
            alongside are ignored.  Use ``dataclasses.replace(config, ...)``
            to vary a single field from a shared base config.
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
        asgi_middleware: Optional sequence of ASGI middleware entries
            applied to the outer HTTP app before uvicorn binds. Use for
            cross-cutting HTTP concerns the SDK does not own: tenant
            resolution (:class:`adcp.server.SubdomainTenantMiddleware`),
            CORS, request-id propagation, IP allowlists, custom auth.
            Composes outermost-first — the first entry sees every request
            before later entries. Applied on every HTTP transport
            (``streamable-http``, ``sse``, ``a2a``, ``both``); ignored
            on ``stdio``.

            Each entry is either a ``(MiddlewareClass, kwargs)`` tuple
            invoked as ``cls(app, **kwargs)``, or a callable factory
            ``f(app) -> app``. Both forms can appear in the same list.

            Middleware sees ``lifespan`` and ``websocket`` scopes in
            addition to ``http`` — guard non-HTTP scopes by passing
            them through unchanged (``if scope['type'] != 'http':
            await self.app(scope, receive, send); return``) so the
            framework's lifespan composition still runs.

            Example (tuple form)::

                from starlette.middleware.cors import CORSMiddleware
                serve(handler, asgi_middleware=[
                    (CORSMiddleware, {"allow_origins": ["*"]}),
                ])

            Example (callable factory form, e.g. with ``functools.partial``)::

                import functools
                from starlette.middleware.cors import CORSMiddleware
                serve(handler, asgi_middleware=[
                    functools.partial(CORSMiddleware, allow_origins=["*"]),
                ])
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
        stateless_http: When ``False`` (default), MCP keeps a per-client
            session alive across requests so subsequent ``tools/call``
            posts skip the transport-construction tax — meaningfully
            faster for chatty clients, and the only mode where
            ``StreamableHTTPSessionManager``'s idle-reap path actually
            runs. (Stateless mode in upstream MCP holds GET-SSE streams
            with no idle eviction, which is why production adopters
            saw connections accumulate.) The SDK threads the
            originating Starlette ``Request`` into
            ``RequestMetadata.request_context`` in both modes so
            ``context_factory`` can read auth off ``request.state``;
            the bundled :func:`auth_context_factory` already does
            this. Set ``True`` for stateless deployments — multi-replica
            without sticky LB on ``Mcp-Session-Id``, or where you
            cannot configure session affinity.
        session_idle_timeout: Idle reap deadline (seconds) for stateful
            sessions. Each request pushes the deadline forward; idle
            sessions are terminated and their per-session state freed.
            Defaults to 1800 (30 min); ``None`` disables reaping.
            Ignored when ``stateless_http=True``.
        enable_debug_endpoints: When ``True``, mount ``GET /_debug/traffic``
            on the outer HTTP app. Returns the JSON dict from
            ``debug_traffic_source()`` — typically wired to the
            seller's :class:`adcp.decisioning.MockAdServer.get_traffic`.
            Defaults to ``False`` so production deployments stay
            closed; reference / dev sellers turn it on. Ignored on
            stdio. The endpoint exposes per-method outbound call
            counts for storyboard runners' anti-façade assertions.
        debug_traffic_source: Zero-arg callable returning the
            per-method count snapshot for ``/_debug/traffic``. Required
            when ``enable_debug_endpoints=True``; otherwise ignored.
            Typically ``mock_ad_server.get_traffic``.
        base_url: Optional public origin URL for the binary, used to
            populate the ``url`` field of each entry in the
            ``/.well-known/adcp-agents.json`` discovery manifest.
            Adopters behind a TLS-terminating reverse proxy SHOULD set
            this (e.g. ``"https://sales.example.com"``). When ``None``
            the manifest URLs fall back to ``http://<bind-host>:<port>``,
            which is correct for local development but wrong for
            production.
        specialisms: Optional list of AdCP specialism tags surfaced in
            the discovery manifest (e.g. ``["sales-non-guaranteed"]``).
            See :data:`adcp.server.discovery` for the full list.
            Defaults to a placeholder when omitted — adopters who know
            their specialism SHOULD pass it.
        description: Optional human-readable description surfaced in
            the discovery manifest's per-agent ``description`` field.
        validation: :class:`ValidationHookConfig` enabling schema
            validation of every request and response against the
            bundled AdCP JSON schemas. ``requests="strict"`` raises
            ``VALIDATION_ERROR`` before the handler runs on a malformed
            payload; ``responses="strict"`` raises after the handler
            returns when the response shape drifts from spec.

            **Defaults to** :data:`DEFAULT_VALIDATION` (strict on both
            sides) — wire-conformance by default. This catches the
            class of bug that shipped the ``pricing_options``
            regression past Pydantic ``extra="allow"`` silently
            swallowing an unknown shape. Adopters mid-migration who
            need response drift to warn rather than fail pass
            ``ValidationHookConfig(responses="warn")``; adopters who
            want validation off entirely pass
            ``ValidationHookConfig(requests="off", responses="off")``
            or ``validation=None``. Applies to both MCP and A2A
            transports.

    Security:
        This function does NOT configure authentication. In production,
        use a reverse proxy or middleware that validates credentials
        before forwarding to the endpoint. Without authentication,
        MCP exposes tools/list and A2A exposes /.well-known/agent.json,
        both of which reveal the agent's full capability surface.
        auth: Optional :class:`~adcp.server.auth.BearerTokenAuth` config
            applied to MCP, A2A, and ``transport="both"`` legs from the
            same source of truth. Drives MCP's
            :class:`~adcp.server.auth.BearerTokenAuthMiddleware` and
            A2A's :class:`~adcp.server.auth.BearerTokenContextBuilder`.
            On A2A, ``/.well-known/agent-card.json`` stays publicly
            accessible per A2A spec §4.1 — the agent-card route is
            registered separately and never invokes the builder. On
            stdio, ``auth`` is ignored with a warning (no HTTP layer).
            For non-bearer schemes (mTLS, signed-request derivation),
            wire your own middleware via ``asgi_middleware=`` instead.
        public_url: Public base URL for the A2A agent card
            (``/.well-known/agent-card.json``).  Accepts a static string
            or a :data:`~adcp.server.a2a_server.PublicUrlResolver`
            callable for per-request resolution.

            *Static string* — replaces ``http://localhost:{port}/`` in
            ``supportedInterfaces``.  Falls back to the ``PUBLIC_URL``
            env var when ``None``.  Correct for single-host deployments.

            *Callable* — receives the Starlette ``Request`` per card
            fetch; must return an absolute ``https://`` URL.  Use for
            multi-tenant subdomain deployments where each tenant host
            needs its own card::

                def resolver(request):
                    host = request.headers.get("host", "localhost")
                    return f"https://{host}/"

                serve(handler, transport="a2a", public_url=resolver)

            Ignored for MCP transports.
        on_startup: Optional sequence of :data:`LifespanHook` zero-arg
            async callables fired after both inner MCP and A2A
            lifespans have initialized. Use for adopter background
            work that must run for the lifetime of the server —
            schedulers, queue consumers, cache warmers, connection
            pools. A hook raising aborts boot via
            ``lifespan.startup.failed``. **Today honored only on**
            ``transport="both"``; passing on any other transport
            raises :class:`ValueError` at boot rather than silently
            dropping the hook. See ``examples/scheduler_lifespan.py``.
        on_shutdown: Optional sequence of :data:`LifespanHook` zero-arg
            async callables fired before either inner lifespan tears
            down. Every hook runs on a best-effort basis even if an
            earlier one raised; the first failure re-raises so
            Starlette surfaces it, later failures land in
            ``logger.error``. Same ``transport="both"`` restriction
            as ``on_startup``.

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
    # When a ServeConfig bundle is provided, extract all fields from it.
    # Individual kwargs are ignored so that config= is the single source of
    # truth.  Callers who need to vary one field should use
    # dataclasses.replace(config, field=value) rather than mixing styles.
    if config is not None:
        name = config.name
        port = config.port
        host = config.host
        transport = config.transport
        instructions = config.instructions
        test_controller = config.test_controller
        context_factory = config.context_factory
        task_store = config.task_store
        push_config_store = config.push_config_store
        middleware = config.middleware
        asgi_middleware = config.asgi_middleware
        message_parser = config.message_parser
        advertise_all = config.advertise_all
        max_request_size = config.max_request_size
        streaming_responses = config.streaming_responses
        stateless_http = config.stateless_http
        session_idle_timeout = config.session_idle_timeout
        validation = config.validation
        pre_validation_hooks = config.pre_validation_hooks
        enable_debug_endpoints = config.enable_debug_endpoints
        debug_traffic_source = config.debug_traffic_source
        base_url = config.base_url
        specialisms = config.specialisms
        description = config.description
        public_url = config.public_url
        on_startup = config.on_startup
        on_shutdown = config.on_shutdown

    # Accept ADCPServerBuilder from adcp_server() decorator pattern
    from adcp.server.builder import ADCPServerBuilder

    if isinstance(handler, ADCPServerBuilder):
        if not name or name == "adcp-agent":
            name = handler.name
        handler = handler.build_handler()

    # Compose the debug-traffic endpoint as the outermost ASGI
    # middleware. Mounting it ahead of any seller-provided
    # ``asgi_middleware`` means a runner's ``GET /_debug/traffic``
    # short-circuits before tenant-resolution / auth middleware runs —
    # the endpoint is for storyboard runners, not authenticated
    # buyers, and should not require buyer credentials to reach.
    asgi_middleware = _prepend_debug_endpoint(
        asgi_middleware,
        enable_debug_endpoints=enable_debug_endpoints,
        debug_traffic_source=debug_traffic_source,
    )

    # Lifespan hooks ship today only for transport="both" because that's
    # the path with an SDK-owned parent Starlette where composition is
    # straightforward (see :func:`_build_mcp_and_a2a_app`). For single-
    # transport paths, FastMCP / a2a-sdk own their inner Starlette and
    # we would have to mutate vendor internals to weave hooks in. Fail
    # closed here so adopters get a clear error at boot instead of
    # silently dropped hooks at runtime.
    if (on_startup or on_shutdown) and transport != "both":
        raise ValueError(
            f"on_startup / on_shutdown hooks require transport='both', got "
            f"transport={transport!r}. The single-transport paths "
            "(streamable-http, sse, a2a, stdio) do not yet expose a "
            "composition point for user lifespan hooks. Either set "
            "transport='both' (see examples/scheduler_lifespan.py for the "
            "pattern) or hand-wire ASGI lifespan-scope middleware. "
            "Single-transport support is tracked as a follow-up to #709."
        )

    if transport == "a2a":
        _serve_a2a(
            handler,
            name=name,
            port=port,
            test_controller=test_controller,
            test_controller_account_resolver=test_controller_account_resolver,
            context_factory=context_factory,
            task_store=task_store,
            push_config_store=push_config_store,
            middleware=middleware,
            asgi_middleware=asgi_middleware,
            message_parser=message_parser,
            advertise_all=advertise_all,
            max_request_size=max_request_size,
            validation=validation,
            pre_validation_hooks=pre_validation_hooks,
            base_url=base_url,
            specialisms=specialisms,
            description=description,
            auth=auth,
            public_url=public_url,
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
            test_controller_account_resolver=test_controller_account_resolver,
            context_factory=context_factory,
            middleware=middleware,
            asgi_middleware=asgi_middleware,
            advertise_all=advertise_all,
            max_request_size=max_request_size,
            streaming_responses=streaming_responses,
            stateless_http=stateless_http,
            session_idle_timeout=session_idle_timeout,
            validation=validation,
            pre_validation_hooks=pre_validation_hooks,
            base_url=base_url,
            specialisms=specialisms,
            description=description,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            enable_dns_rebinding_protection=enable_dns_rebinding_protection,
            auth=auth,
        )
    elif transport == "both":
        _serve_mcp_and_a2a(
            handler,
            name=name,
            port=port,
            host=host,
            instructions=instructions,
            test_controller=test_controller,
            test_controller_account_resolver=test_controller_account_resolver,
            context_factory=context_factory,
            task_store=task_store,
            push_config_store=push_config_store,
            middleware=middleware,
            asgi_middleware=asgi_middleware,
            message_parser=message_parser,
            advertise_all=advertise_all,
            max_request_size=max_request_size,
            streaming_responses=streaming_responses,
            stateless_http=stateless_http,
            session_idle_timeout=session_idle_timeout,
            validation=validation,
            pre_validation_hooks=pre_validation_hooks,
            base_url=base_url,
            specialisms=specialisms,
            description=description,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            enable_dns_rebinding_protection=enable_dns_rebinding_protection,
            auth=auth,
            public_url=public_url,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
        )
    else:
        valid = ", ".join(sorted(("a2a", "both", "streamable-http", "sse", "stdio")))
        raise ValueError(f"Unknown transport {transport!r}. Valid: {valid}")


def _prepend_debug_endpoint(
    asgi_middleware: Sequence[ASGIMiddlewareEntry] | None,
    *,
    enable_debug_endpoints: bool,
    debug_traffic_source: Callable[[], dict[str, int]] | None,
) -> Sequence[ASGIMiddlewareEntry] | None:
    """Prepend :class:`DebugTrafficMiddleware` to the asgi_middleware
    sequence when debug endpoints are enabled.

    No-op when ``enable_debug_endpoints=False`` — the middleware isn't
    mounted, ``/_debug/traffic`` falls through to the inner app, and
    the inner app returns 404. Production-default closed posture.

    Raises ``ValueError`` when debug endpoints are enabled but no
    traffic source is supplied — silently mounting an endpoint that
    would error on every request is worse than a clear configuration
    error at boot.
    """
    if not enable_debug_endpoints:
        return asgi_middleware
    if debug_traffic_source is None:
        raise ValueError(
            "enable_debug_endpoints=True requires debug_traffic_source= "
            "(typically mock_ad_server.get_traffic). Without a source the "
            "/_debug/traffic endpoint has nothing to return."
        )
    from adcp.server.debug_endpoints import DebugTrafficMiddleware

    debug_entry = (
        DebugTrafficMiddleware,
        {"traffic_source": debug_traffic_source},
    )
    if asgi_middleware is None:
        return [debug_entry]
    return [debug_entry, *asgi_middleware]


def _apply_asgi_middleware(
    app: Any,
    asgi_middleware: Sequence[ASGIMiddlewareEntry] | None,
) -> Any:
    """Wrap ``app`` with operator-supplied Starlette-style ASGI middleware.

    Each entry is either ``(MiddlewareClass, kwargs)`` invoked as
    ``cls(app, **kwargs)``, or a callable factory ``f(app) -> app`` invoked
    as ``factory(app)``. Both forms can appear in the same list. Composition
    is outermost-first — the first entry sees every request before later
    entries — so we wrap in reverse, matching :meth:`Starlette.add_middleware`
    semantics.

    No-op when the sequence is empty or ``None``.
    """
    if not asgi_middleware:
        return app
    for entry in reversed(list(asgi_middleware)):
        if isinstance(entry, tuple):
            cls, kwargs = entry
            app = cls(app, **kwargs)
        else:
            app = entry(app)
    return app


def _wrap_mcp_with_auth(app: Any, auth: BearerTokenAuth | None) -> Any:
    """Wrap the FastMCP HTTP app with :class:`BearerTokenAuthMiddleware`.

    No-op when ``auth`` is ``None``. Expects a
    :class:`~adcp.server.auth.BearerTokenAuth` config; raises
    :class:`TypeError` for anything else so misconfiguration is loud at
    boot, not silent at runtime.

    The middleware is applied *innermost* so its body-peek for the
    JSON-RPC discovery bypass sees the payload before the path
    normalizer / discovery wrapper / operator's ``asgi_middleware``
    layer reshape the request.
    """
    if auth is None:
        return app
    from adcp.server.auth import BearerTokenAuth, BearerTokenAuthMiddleware

    if not isinstance(auth, BearerTokenAuth):
        raise TypeError(
            f"serve(auth=...) expects BearerTokenAuth, got {type(auth).__name__}. "
            "Import from adcp.server.auth.BearerTokenAuth."
        )

    # FastMCP's ``streamable_http_app()`` returns a Starlette instance;
    # ``add_middleware`` wraps the inner app in place and preserves
    # FastMCP's lifespan + routing without a parallel Starlette.
    #
    # Per #720, ``Authorization: Bearer`` is always accepted; any
    # legacy custom header configured on ``BearerTokenAuth`` is folded
    # in as an additive alias. The legacy single-knob path (the
    # ``BearerTokenAuthMiddleware`` deprecation warnings on
    # ``header_name=`` / ``bearer_prefix_required=``) is bypassed here
    # — the dataclass already absorbed those into the alias list.
    app.add_middleware(
        BearerTokenAuthMiddleware,
        validate_token=auth.validate_token,
        unauthenticated_response=auth.unauthenticated_response,
        legacy_header_aliases=auth.resolved_mcp_legacy_aliases(),
        legacy_aliases_bearer_prefix_required=(
            # If adopter configured legacy bearer-prefix via the old
            # kwargs, honor that on the alias path. New-shape adopters
            # set this directly on the dataclass.
            auth.resolved_mcp_bearer_prefix_required()
            if (
                auth.bearer_prefix_required is not None
                or auth.mcp_bearer_prefix_required is not None
            )
            else auth.legacy_aliases_bearer_prefix_required
        ),
    )
    return app


def _wrap_a2a_with_auth(app: Any, auth: BearerTokenAuth | None) -> Any:
    """Wrap an A2A Starlette app with :class:`A2ABearerAuthMiddleware`.

    No-op when ``auth`` is ``None``. Returns the original app
    untouched, so the A2A side falls back to a2a-sdk's default
    (unauthenticated, agent-card publicly accessible) without any
    middleware overhead.

    The middleware is wrapped at the ASGI layer (not via
    ``Starlette.add_middleware``) so it sees the request before
    a2a-sdk's JsonRpcDispatcher and v0.3 compat adapter — which
    catch every exception including ``HTTPException`` and convert
    them to JSON-RPC errors with HTTP 200. ASGI-layer wrapping
    returns proper HTTP 401 every time.

    Same type guard as :func:`_wrap_mcp_with_auth` — a misconfig
    that passes a dict / lambda / wrong type is loud at boot.

    Async validators are rejected at boot because the A2A leg's
    middleware path is sync (the MCP middleware awaits async
    validators transparently — A2A can't without restructuring
    a2a-sdk's dispatcher). Catching the misuse at ``serve()`` time
    instead of on the first request prevents production deployments
    from shipping with silently-failing auth.
    """
    if auth is None:
        return app
    import inspect as _inspect

    from adcp.server.auth import A2ABearerAuthMiddleware, BearerTokenAuth

    if not isinstance(auth, BearerTokenAuth):
        raise TypeError(
            f"serve(auth=...) expects BearerTokenAuth, got {type(auth).__name__}. "
            "Import from adcp.server.auth.BearerTokenAuth."
        )
    if _inspect.iscoroutinefunction(auth.validate_token):
        raise TypeError(
            "BearerTokenAuth.validate_token is async, which the A2A leg "
            "cannot call directly — a2a-sdk's middleware path is sync. "
            "Wrap your async validator with a sync bridge "
            "(e.g. `lambda t: anyio.from_thread.run(my_async_validate, t)`) "
            "before passing it to BearerTokenAuth, or use transport="
            "'streamable-http' (MCP middleware awaits async validators "
            "transparently)."
        )
    return A2ABearerAuthMiddleware(app, auth)


def _wrap_with_discovery(
    app: Any,
    *,
    name: str,
    transports: list[Literal["mcp", "a2a"]],
    base_url: str,
    description: str | None = None,
    specialisms: list[str] | None = None,
) -> Any:
    """Wrap an ASGI app to serve ``/.well-known/adcp-agents.json``.

    Intercepts the discovery path and serves the AdCP multi-agent
    topology manifest; every other request passes through unchanged.
    Sits outside the inner transport apps (FastMCP / a2a-sdk Starlette)
    so adding the route doesn't require monkey-patching either upstream.

    GET returns the manifest as JSON; non-GET methods at the discovery
    path 404 back to the inner app — letting the inner Starlette
    return its standard 405 / 404 keeps the well-known surface
    read-only without baking method-policy into this wrapper.
    """
    from adcp.server.discovery import (
        DISCOVERY_PATH,
        build_manifest,
    )

    async def _middleware(scope: Any, receive: Any, send: Any) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("path") == DISCOVERY_PATH
            and scope.get("method") == "GET"
        ):
            from starlette.responses import JSONResponse

            manifest = build_manifest(
                name=name,
                transports=transports,
                base_url=base_url,
                description=description,
                specialisms=specialisms,
            )
            response = JSONResponse(manifest)
            await response(scope, receive, send)
            return
        await app(scope, receive, send)

    return _middleware


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
    test_controller_account_resolver: Any | None = None,
    context_factory: ContextFactory | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    asgi_middleware: Sequence[ASGIMiddlewareEntry] | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    streaming_responses: bool = False,
    stateless_http: bool = False,
    session_idle_timeout: float | None = 1800.0,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None,
    base_url: str | None = None,
    specialisms: list[str] | None = None,
    description: str | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
    enable_dns_rebinding_protection: bool | None = None,
    auth: BearerTokenAuth | None = None,
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
        stateless_http=stateless_http,
        session_idle_timeout=session_idle_timeout,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        enable_dns_rebinding_protection=enable_dns_rebinding_protection,
    )

    if test_controller is not None:
        from adcp.server.test_controller import register_test_controller

        register_test_controller(
            mcp,
            test_controller,
            context_factory=context_factory,
            account_resolver=test_controller_account_resolver,
        )

    if transport in ("streamable-http", "sse"):
        _run_mcp_http(
            mcp,
            transport=transport,
            asgi_middleware=asgi_middleware,
            max_request_size=max_request_size,
            discovery_name=name,
            discovery_base_url=base_url,
            discovery_specialisms=specialisms,
            discovery_description=description,
            auth=auth,
        )
    else:
        # stdio — no listening socket, no HTTP layer to authenticate. Auth
        # over stdio doesn't apply (no Authorization header). Warn loudly
        # rather than silently ignore so adopters notice the misconfig.
        if auth is not None:
            logger.warning(
                "auth=BearerTokenAuth ignored on transport='stdio' — stdio "
                "has no HTTP layer for bearer-token validation. Wire your "
                "own out-of-band auth or use transport='streamable-http'."
            )
        if asgi_middleware:
            logger.warning(
                "asgi_middleware is ignored on transport='stdio'; " "ASGI middleware will not run"
            )
        mcp.run(transport=transport)


def _run_mcp_http(
    mcp: Any,
    *,
    transport: str,
    asgi_middleware: Sequence[ASGIMiddlewareEntry] | None = None,
    max_request_size: int | None = None,
    discovery_name: str = "adcp-agent",
    discovery_base_url: str | None = None,
    discovery_specialisms: list[str] | None = None,
    discovery_description: str | None = None,
    auth: BearerTokenAuth | None = None,
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

    from adcp.server.discovery import resolve_base_url

    resolved_base_url = resolve_base_url(host, port, discovery_base_url)

    # Auth wraps innermost so the spec-mandated MCP discovery bypass
    # (initialize / tools/list / get_adcp_capabilities) sees the
    # JSON-RPC body before the path-normalizer / discovery wrapper /
    # operator-supplied asgi_middleware get a turn.
    app = _wrap_mcp_with_auth(app, auth)
    app = _wrap_with_path_normalize(app)
    app = _wrap_with_discovery(
        app,
        name=discovery_name,
        transports=["mcp"],
        base_url=resolved_base_url,
        description=discovery_description,
        specialisms=discovery_specialisms,
    )
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
    test_controller_account_resolver: Any | None = None,
    context_factory: ContextFactory | None = None,
    task_store: TaskStore | None = None,
    push_config_store: PushNotificationConfigStore | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    asgi_middleware: Sequence[ASGIMiddlewareEntry] | None = None,
    message_parser: MessageParser | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None,
    base_url: str | None = None,
    specialisms: list[str] | None = None,
    description: str | None = None,
    auth: BearerTokenAuth | None = None,
    public_url: str | PublicUrlResolver | None = None,
) -> None:
    """Start an A2A server using uvicorn."""
    import uvicorn

    from adcp.server.a2a_server import create_a2a_server
    from adcp.server.discovery import resolve_base_url

    resolved_port = port or int(os.environ.get("PORT", "3001"))
    resolved_base_url = resolve_base_url("0.0.0.0", resolved_port, base_url)

    app = create_a2a_server(
        handler,
        name=name,
        port=resolved_port,
        test_controller=test_controller,
        test_controller_account_resolver=test_controller_account_resolver,
        context_factory=context_factory,
        task_store=task_store,
        push_config_store=push_config_store,
        middleware=middleware,
        message_parser=message_parser,
        advertise_all=advertise_all,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
        auth=auth,
        public_url=public_url,
    )
    # Auth wraps the A2A app innermost (closer to the inner Starlette
    # router than the discovery + size-limit + asgi_middleware
    # wrappers) so bad tokens 401 before the request hits any
    # operator-supplied layer.
    app = _wrap_a2a_with_auth(app, auth)
    app = _wrap_with_discovery(
        app,
        name=name,
        transports=["a2a"],
        base_url=resolved_base_url,
        description=description,
        specialisms=specialisms,
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
    test_controller_account_resolver: Any | None = None,
    context_factory: ContextFactory | None = None,
    task_store: TaskStore | None = None,
    push_config_store: PushNotificationConfigStore | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    message_parser: MessageParser | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    streaming_responses: bool = False,
    stateless_http: bool = False,
    session_idle_timeout: float | None = 1800.0,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None,
    base_url: str | None = None,
    specialisms: list[str] | None = None,
    description: str | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
    enable_dns_rebinding_protection: bool | None = None,
    auth: BearerTokenAuth | None = None,
    public_url: str | PublicUrlResolver | None = None,
    on_startup: Sequence[LifespanHook] | None = None,
    on_shutdown: Sequence[LifespanHook] | None = None,
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
        stateless_http=stateless_http,
        session_idle_timeout=session_idle_timeout,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        enable_dns_rebinding_protection=enable_dns_rebinding_protection,
    )
    if test_controller is not None:
        from adcp.server.test_controller import register_test_controller

        register_test_controller(
            mcp,
            test_controller,
            context_factory=context_factory,
            account_resolver=test_controller_account_resolver,
        )
    mcp_inner = mcp.streamable_http_app()
    # Auth wraps the FastMCP Starlette app *before* the path
    # normalizer / dispatcher capture references. Wiring auth after
    # ``mcp_app`` is captured by ``_dispatch`` would silently bypass
    # the middleware on the MCP leg — the closure would already point
    # at the unwrapped reference.
    #
    # Reassigning the return value (rather than relying on
    # ``add_middleware``'s in-place mutation) future-proofs the call
    # site: if a future refactor changes ``_wrap_mcp_with_auth`` to
    # return a fresh ASGI callable, this line keeps wiring auth
    # instead of silently dropping it.
    mcp_inner = _wrap_mcp_with_auth(mcp_inner, auth)
    # Wrap with the standard trailing-slash normalizer so ``/mcp/``
    # and ``/mcp`` resolve to the same FastMCP endpoint. Keep the
    # unwrapped ``mcp_inner`` reference so the lifespan composer
    # below can reach ``.router.lifespan_context``.
    mcp_app = _wrap_with_path_normalize(mcp_inner)

    # A2A app — built via the a2a-sdk wrapper. It mounts at the root
    # of its own app and handles ``/.well-known/agent.json``, ``/``,
    # and the message / push-notif endpoints.
    #
    # Keep the unwrapped ``a2a_inner`` reference so the lifespan
    # composer below can reach ``.router.lifespan_context``; wrap the
    # dispatch reference separately so requests flow through auth on
    # their way to the inner Starlette app.
    a2a_inner = create_a2a_server(
        handler,
        name=name,
        port=port,
        test_controller=test_controller,
        test_controller_account_resolver=test_controller_account_resolver,
        context_factory=context_factory,
        task_store=task_store,
        push_config_store=push_config_store,
        middleware=middleware,
        message_parser=message_parser,
        advertise_all=advertise_all,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
        auth=auth,
        public_url=public_url,
    )
    # Auth wraps both legs *before* ``_dispatch`` captures references —
    # otherwise the closure points at unwrapped apps and auth is
    # silently bypassed on whichever leg hadn't been wrapped yet. The
    # MCP wrap above used ``add_middleware`` so it mutates in place;
    # the A2A wrap returns a new ASGI callable layered on
    # ``a2a_inner``.
    a2a_app = _wrap_a2a_with_auth(a2a_inner, auth)

    # Lifespan composition: FastMCP's session manager initializes a
    # task group on startup; a2a-sdk's stores have their own init.
    # Compose both inner lifespans on a parent Starlette; the
    # dispatcher routes ``lifespan`` scope events to the parent so
    # both initializers run before any request lands.
    #
    # User-supplied ``on_startup`` / ``on_shutdown`` hooks run *inside*
    # both framework lifespans — startup hooks fire after MCP+A2A have
    # finished their own initialization, so user code can rely on the
    # framework being ready; shutdown hooks fire before the framework
    # tears down, so user code can still use framework state. Startup
    # hook exceptions abort boot via Starlette's
    # ``lifespan.startup.failed`` event; shutdown hooks run inside a
    # ``finally`` so a failing earlier hook does not block the rest.
    user_startup = tuple(on_startup or ())
    user_shutdown = tuple(on_shutdown or ())

    @contextlib.asynccontextmanager
    async def _composed_lifespan(_app):  # type: ignore[no-untyped-def]
        async with mcp_inner.router.lifespan_context(mcp_inner):
            async with a2a_inner.router.lifespan_context(a2a_inner):
                for hook in user_startup:
                    await hook()
                try:
                    yield
                finally:
                    # Run every shutdown hook even if an earlier one
                    # raised — adopters that wire multiple cleanup
                    # hooks (close DB pool, stop scheduler, drain
                    # queue) want all of them attempted on a
                    # best-effort basis. Re-raise the first failure
                    # so Starlette surfaces it; log later failures
                    # without ``exc_info`` so adopter closure state
                    # (DB DSNs, tokens stashed in hook captures)
                    # doesn't end up verbatim in shutdown logs that
                    # downstream aggregators attach locals to.
                    #
                    # Catch ``Exception`` only — ``CancelledError`` /
                    # ``KeyboardInterrupt`` / ``SystemExit`` are the
                    # exact signals uvicorn uses to drive shutdown,
                    # and we want them to propagate immediately
                    # rather than getting collected into ``first_error``.
                    first_error: Exception | None = None
                    for hook in user_shutdown:
                        try:
                            await hook()
                        except Exception as exc:  # noqa: BLE001
                            if first_error is None:
                                first_error = exc
                            else:
                                logger.error(
                                    "on_shutdown hook %r raised: %s "
                                    "(suppressed; earlier hook also "
                                    "raised)",
                                    getattr(hook, "__name__", hook),
                                    exc,
                                )
                    if first_error is not None:
                        # If we reached the ``finally`` because the
                        # body raised (framework lifespan teardown,
                        # request handler escaping), don't overwrite
                        # that propagation with our shutdown error —
                        # the operator wants to see the upstream
                        # cause, not a secondary cleanup failure.
                        # Log the shutdown error so it isn't lost,
                        # let the original exception keep propagating.
                        if sys.exc_info()[0] is None:
                            raise first_error
                        logger.error(
                            "on_shutdown hook raised during exception "
                            "unwinding: %s (suppressed; the upstream "
                            "exception takes precedence)",
                            first_error,
                        )

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
    from adcp.server.discovery import resolve_base_url

    resolved_base_url = resolve_base_url(host, port, base_url)
    app = _wrap_with_discovery(
        app,
        name=name,
        transports=["mcp", "a2a"],
        base_url=resolved_base_url,
        description=description,
        specialisms=specialisms,
    )
    return _wrap_with_size_limit(app, max_request_size)


def _serve_mcp_and_a2a(
    handler: ADCPHandler[Any],
    *,
    name: str,
    port: int | None,
    host: str | None = None,
    instructions: str | None,
    test_controller: TestControllerStore | None,
    test_controller_account_resolver: Any | None = None,
    context_factory: ContextFactory | None = None,
    task_store: TaskStore | None = None,
    push_config_store: PushNotificationConfigStore | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    asgi_middleware: Sequence[ASGIMiddlewareEntry] | None = None,
    message_parser: MessageParser | None = None,
    advertise_all: bool = False,
    max_request_size: int | None = None,
    streaming_responses: bool = False,
    stateless_http: bool = False,
    session_idle_timeout: float | None = 1800.0,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None,
    base_url: str | None = None,
    specialisms: list[str] | None = None,
    description: str | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
    enable_dns_rebinding_protection: bool | None = None,
    auth: BearerTokenAuth | None = None,
    public_url: str | PublicUrlResolver | None = None,
    on_startup: Sequence[LifespanHook] | None = None,
    on_shutdown: Sequence[LifespanHook] | None = None,
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
        test_controller_account_resolver=test_controller_account_resolver,
        context_factory=context_factory,
        task_store=task_store,
        push_config_store=push_config_store,
        middleware=middleware,
        message_parser=message_parser,
        advertise_all=advertise_all,
        max_request_size=max_request_size,
        streaming_responses=streaming_responses,
        stateless_http=stateless_http,
        session_idle_timeout=session_idle_timeout,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
        base_url=base_url,
        specialisms=specialisms,
        description=description,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        enable_dns_rebinding_protection=enable_dns_rebinding_protection,
        auth=auth,
        public_url=public_url,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
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


def _expand_allowed_hosts(hosts: Sequence[str]) -> list[str]:
    """Synthesize ``host:*`` siblings for bare hosts.

    FastMCP's :class:`TransportSecurityMiddleware` matches the request's
    ``Host`` header literally against the configured ``allowed_hosts``
    list. A bare host like ``acme.localhost`` matches a request without
    a port suffix; the same request from a browser hitting
    ``http://acme.localhost:3001`` carries ``Host: acme.localhost:3001``
    and is rejected with ``421 Misdirected Request``.

    Adopters had to register both ``acme.localhost`` and
    ``acme.localhost:*`` explicitly. This helper synthesizes the second
    form when the input has no ``:`` separator, mirroring the
    port-stripping done in ``InMemorySubdomainTenantRouter`` so the two
    surfaces stay symmetric. Hosts that already include ``:`` (already
    have an explicit port or wildcard) pass through unchanged.

    Idempotent: if the adopter passed both ``acme.localhost`` and
    ``acme.localhost:*``, the result still contains each only once.

    IPv6 literals (bracketed ``[::1]`` or raw ``::1``) contain ``:`` and
    pass through without synthesis — no malformed ``::1:*`` siblings.
    Adopters running on custom IPv6 hosts pass the explicit
    ``[::1]:*`` form themselves.
    """
    seen: set[str] = set()
    result: list[str] = []
    for host in hosts:
        if host not in seen:
            seen.add(host)
            result.append(host)
        if ":" not in host:
            wildcard = f"{host}:*"
            if wildcard not in seen:
                seen.add(wildcard)
                result.append(wildcard)
    return result


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
    stateless_http: bool = False,
    session_idle_timeout: float | None = 1800.0,
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
    enable_dns_rebinding_protection: bool | None = None,
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
        stateless_http: When ``False`` (default), MCP keeps a
            per-client session task alive across requests so subsequent
            ``tools/call`` posts skip the per-request transport-
            construction tax — meaningfully faster for chatty clients,
            and the only mode where ``StreamableHTTPSessionManager``'s
            idle-reap path actually runs. (Stateless mode in upstream
            MCP holds GET-SSE streams open with no idle eviction —
            connections accumulate.) The SDK threads the originating
            Starlette ``Request`` into
            ``RequestMetadata.request_context``; the bundled
            :func:`~adcp.server.auth_context_factory` reads auth off
            ``request.state`` and works in both stateless and stateful.
            Custom factories using :mod:`contextvars` set in ASGI
            middleware should migrate — those vars do NOT propagate
            from the HTTP request task to the stateful session's
            dispatch task. Multi-replica stateful deployments need
            sticky load balancing on ``Mcp-Session-Id``; set
            ``stateless_http=True`` only when affinity isn't possible.
            Do not memoize per-call state on ``mcp.Context`` or
            session-manager-scoped objects in stateful mode — that
            smears identity across calls.
        session_idle_timeout: Idle reap deadline (seconds) for stateful
            sessions. Each request pushes the deadline forward; idle
            sessions are terminated and their per-session state freed.
            Defaults to 1800 (30 minutes); set to ``None`` to disable
            reaping. Ignored when ``stateless_http=True``. Required
            because without it
            ``StreamableHTTPSessionManager._server_instances`` grows
            without bound for clients that disconnect without DELETE.

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
        # FastMCP's SSE-internal streaming default has an upstream bug
        # that drops the ASGI response without completing; AdCP tools
        # return one complete envelope per request anyway, so JSON
        # response mode is both safer and semantically correct.
        mcp.settings.json_response = True
    mcp.settings.stateless_http = stateless_http
    # FastMCP's TransportSecurityMiddleware enforces DNS-rebinding
    # protection: the default ``allowed_hosts`` accepts only loopback
    # patterns (``127.0.0.1:*``, ``localhost:*``, ``[::1]:*``). Adopters
    # serving multi-tenant subdomain hosts (``acme.example.com``,
    # ``acme.localhost``) extend the list or the transport returns
    # ``421 Misdirected Request`` and MCP discovery fails. Adopters
    # whose outer ASGI middleware already validates hosts against a
    # tenant table (e.g. :class:`SubdomainTenantMiddleware`) can set
    # ``enable_dns_rebinding_protection=False`` so the MCP-layer check
    # doesn't duplicate the upstream validation.
    #
    # ``_expand_allowed_hosts`` synthesizes the ``host:*`` sibling for
    # any bare host (no ``:``) so adopters who pass ``acme.localhost``
    # also cover requests on ``acme.localhost:3001``. Mirrors the port
    # stripping :class:`InMemorySubdomainTenantRouter` does at lookup
    # time so the two surfaces stay symmetric.
    if (
        enable_dns_rebinding_protection is not None
        or allowed_hosts is not None
        or allowed_origins is not None
    ):
        from mcp.server.transport_security import TransportSecuritySettings

        if mcp.settings.transport_security is None:
            mcp.settings.transport_security = TransportSecuritySettings()
        ts = mcp.settings.transport_security
        if enable_dns_rebinding_protection is not None:
            ts.enable_dns_rebinding_protection = enable_dns_rebinding_protection
        if allowed_hosts:
            ts.allowed_hosts = [
                *ts.allowed_hosts,
                *_expand_allowed_hosts(allowed_hosts),
            ]
        if allowed_origins:
            ts.allowed_origins = [*ts.allowed_origins, *allowed_origins]
    _register_handler_tools(
        mcp,
        handler,
        include_test_controller=include_test_controller,
        context_factory=context_factory,
        middleware=middleware,
        advertise_all=advertise_all,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
    )
    # Pre-create the StreamableHTTPSessionManager so we can pass
    # ``session_idle_timeout`` — FastMCP's settings don't expose it as of
    # mcp 1.27.x. ``streamable_http_app()`` lazy-creates the manager only
    # if ``_session_manager`` is ``None``, so populating it here is the
    # extension point. Reaches into FastMCP private attrs ``_mcp_server``,
    # ``_event_store``, ``_retry_interval`` to mirror upstream's own
    # constructor call — guarded by the ``mcp<2.0`` pin since v2 may
    # rename these.
    if session_idle_timeout is not None and session_idle_timeout <= 0:
        raise ValueError(
            f"session_idle_timeout must be positive (got {session_idle_timeout!r}); "
            "set None to disable reaping."
        )
    # Suppress the timeout in stateless mode — upstream raises
    # ``RuntimeError`` if both are set. Silent because ``stateless_http=True,
    # session_idle_timeout=1800.0`` is the default combination and would
    # warn on every server boot otherwise. Adopters who explicitly want a
    # timeout should set ``stateless_http=False``.
    idle_timeout = None if mcp.settings.stateless_http else session_idle_timeout
    mcp._session_manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server,
        event_store=mcp._event_store,
        retry_interval=mcp._retry_interval,
        json_response=mcp.settings.json_response,
        stateless=mcp.settings.stateless_http,
        security_settings=mcp.settings.transport_security,
        session_idle_timeout=idle_timeout,
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
    validation: ValidationHookConfig | None = DEFAULT_VALIDATION,
    pre_validation_hooks: dict[str, Callable[..., Any]] | None = None,
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
        hook = (pre_validation_hooks or {}).get(tool_name)
        caller = create_tool_caller(
            handler, tool_name, validation=validation, pre_validation_hook=hook
        )
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
    from mcp.types import CallToolResult
    from pydantic import ConfigDict

    from adcp.exceptions import ADCPError
    from adcp.server.translate import build_mcp_error_result

    # Lazy import — decisioning is optional for non-platform handlers,
    # but when present its ``AdcpError`` carries structured ``details``
    # (caused_by, validation_errors) that need to reach the wire.
    try:
        from adcp.decisioning.types import AdcpError as DecisioningAdcpError  # noqa: N813
    except Exception:
        DecisioningAdcpError = None  # type: ignore[assignment,misc]  # noqa: N806

    async def fn(**kwargs: Any) -> dict[str, Any]:
        # Caller identity: FastMCP does not expose an authenticated principal
        # at the SDK level (``Context.client_id`` is a session hint, not an
        # authenticated user). Sellers wire auth via HTTP middleware on
        # ``mcp.streamable_http_app()`` and pass ``context_factory`` to
        # ``create_mcp_server()``. ``RequestMetadata.request_context`` carries
        # the originating Starlette ``Request`` so the factory can read
        # ``request.state.*`` set by middleware — this works in both
        # stateless and stateful streamable-http modes, where the older
        # ``contextvars.ContextVar`` pattern only works in stateless (the
        # stateful session task is a separate async task than the HTTP
        # request task and does not see middleware-set ContextVars).
        # The A2A transport derives ``caller_identity`` from
        # ``ServerCallContext.user`` automatically.
        context: ToolContext | None = None
        if context_factory is not None:
            request_context = _get_starlette_request_for_dispatch()
            meta = RequestMetadata(
                tool_name=name,
                transport="mcp",
                request_context=request_context,
            )
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
            # AdCP-typed exceptions (IdempotencyConflictError, ADCPTaskError
            # with a spec code, etc.) project to a CallToolResult with
            # ``isError=True`` AND ``structuredContent.adcp_error`` populated
            # — matching transport-errors.mdx §MCP Binding. Returning the
            # result directly bypasses FastMCP's ``_make_error_result`` path
            # which strips ``structuredContent`` from error envelopes. The
            # ``-> dict[str, Any]`` annotation drives FastMCP's output_schema
            # derivation; the actual return type is broader (CallToolResult
            # is a valid return per the lowlevel handler's contract).
            #
            # ``kwargs`` is the raw request dict — passing it lets the
            # builder echo the request's ``context`` extension into the
            # error envelope, symmetric with the success path's
            # ``inject_context`` call (mcp_tools.py).
            return build_mcp_error_result(exc, params=kwargs)  # type: ignore[return-value]
        except Exception as exc:
            # Decisioning ``AdcpError`` is NOT a subclass of
            # ``adcp.exceptions.ADCPError`` (different class hierarchy
            # — ``adcp.decisioning.types.AdcpError``). Catch it explicitly
            # and project the same structured envelope.
            if DecisioningAdcpError is not None and isinstance(exc, DecisioningAdcpError):
                return build_mcp_error_result(exc, params=kwargs)  # type: ignore[return-value]
            raise
        # Pre-built CallToolResult (error envelope from build_mcp_error_result)
        # passes through FastMCP's convert_result and the lowlevel handler
        # without re-validation against the success-path output_model — the
        # custom FuncMetadata subclass below handles the bypass.
        if isinstance(result, CallToolResult):
            return result  # type: ignore[return-value]
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

    class _AdcpFuncMetadata(FuncMetadata):
        """FuncMetadata that skips success-path output validation for error
        ``CallToolResult`` returns.

        FastMCP's stock ``convert_result`` validates ``result.structuredContent``
        against the success-path ``output_model`` whenever the tool returns a
        ``CallToolResult`` — but when the framework projects an ``AdcpError``
        as ``{"adcp_error": {...}}``, that payload doesn't conform to the
        success schema. Skip validation for ``isError=True`` envelopes; success
        envelopes still validate normally.
        """

        def convert_result(self, result: Any) -> Any:
            if isinstance(result, CallToolResult) and result.isError:
                return result
            return super().convert_result(result)

    # Advertise the spec response schema on ``tools/list`` when one is
    # available. FastMCP serializes ``Tool.output_schema`` (which reads
    # ``fn_metadata.output_schema``) into the ``outputSchema`` field of
    # the ``tools/list`` response — matches the TS port. Falls back to
    # the auto-derived shape from the ``fn`` return annotation when no
    # spec schema is mapped (e.g. handler-only custom tools).
    effective_output_schema = (
        output_schema if output_schema is not None else tool.fn_metadata.output_schema
    )
    tool.fn_metadata = _AdcpFuncMetadata(
        arg_model=_AdcpArgs,
        output_schema=effective_output_schema,
        output_model=tool.fn_metadata.output_model,
        wrap_output=False,
    )

    # FastMCP does not expose a public API for registering pre-built Tool
    # objects with custom schemas. This accesses internals; requires mcp>=1.23.
    mcp._tool_manager._tools[name] = tool
