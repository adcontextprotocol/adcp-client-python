"""A2A server support for ADCP handlers.

Bridges ADCPHandler to the a2a-sdk server framework so the same handler
can be served over both MCP and A2A transports.

    from adcp.server import ADCPHandler, serve
    serve(MyHandler(), name="my-agent", transport="a2a")

.. note::
    Function signatures here use ``ADCPHandler[Any]`` rather than a
    propagated ``TContext`` TypeVar. This module dispatches by tool
    name and never reads typed fields off the context, so ``Any`` is
    both correct and keeps the call sites tidy — downstream code that
    needs typed context (their own handler subclass) keeps the TypeVar
    all the way to dispatch via :class:`ADCPHandler`. See the matching
    note in :mod:`adcp.server.mcp_tools`.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from a2a import types as pb
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from starlette.applications import Starlette

from adcp.exceptions import ADCPError
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.spec_compat import PreValidationHooks

# Decisioning-layer ``AdcpError`` (from ``adcp.decisioning.types``) is the
# wire-shaped structured error platform methods raise. It is NOT a subclass
# of :class:`adcp.exceptions.ADCPError`; the executor must catch both so
# storyboards graded against decisioning adopters see the same structured
# envelope as MCP. Lazy import — ``adcp.decisioning`` pulls in the
# decisioning graph, which the A2A server module shouldn't load at import
# time. When the import fails (decisioning extra not installed), only the
# client-side ``ADCPError`` path is active.
try:
    from adcp.decisioning.types import AdcpError as _DecisioningAdcpError
except Exception:  # pragma: no cover - decisioning is an optional dep surface
    _DECISIONING_ADCP_ERROR_TYPES: tuple[type[BaseException], ...] = ()
else:
    _DECISIONING_ADCP_ERROR_TYPES = (_DecisioningAdcpError,)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from a2a.server.tasks.push_notification_config_store import (
        PushNotificationConfigStore,
    )
    from a2a.server.tasks.task_store import TaskStore

    from adcp.server.auth import BearerTokenAuth
    from adcp.server.serve import ContextFactory, SkillMiddleware

from collections.abc import Awaitable, Callable  # noqa: E402

from adcp.validation.client_hooks import (  # noqa: E402
    SERVER_DEFAULT_VALIDATION,
    ValidationHookConfig,
)

MessageParser = Callable[[RequestContext], tuple[str | None, dict[str, Any]]]
"""Callable that extracts ``(skill_name, params)`` from an incoming
A2A :class:`RequestContext`.

The default parser handles a DataPart (``data`` oneof) carrying
``{"skill": ..., "parameters": ...}`` plus a TextPart JSON fallback.
Override this hook to accept alternative wire shapes — JSON-RPC 2.0
message bodies, vendor-specific DataPart schemas, or text-only skill
encodings. Return ``(None, {})`` to signal "no parseable skill"; the
executor will emit an error Task for the client.

Pair with :meth:`ADCPAgentExecutor._default_parse_request` when you
want to accept a custom shape *in addition to* the built-in shapes —
call the default as a fallback after your own parser returns
``(None, {})``.
"""

PublicUrlResolver = Callable[[Any], str | Awaitable[str]]
"""Per-request public URL resolver for the A2A agent card.

Called once per GET ``/.well-known/agent-card.json`` (and the 0.3
alias ``/.well-known/agent.json``) to derive the base URL embedded in
``supportedInterfaces`` entries.  Receives the Starlette
:class:`~starlette.requests.Request` and must return an absolute URL
string.  Both sync and async callables are accepted.

Typical use — multi-tenant subdomain routing::

    from starlette.requests import Request

    def agent_card_url(request: Request) -> str:
        host = request.headers.get("host", "localhost")
        return f"https://{host}/"

    serve(handler, transport="a2a", public_url=agent_card_url)

Async resolvers work the same way::

    from starlette.requests import Request

    async def agent_card_url(request: Request) -> str:
        host = request.headers.get("host", "localhost")
        return f"https://{host}/"

**Trust boundary:** the callable owns all header-trust decisions.
Do not read ``X-Forwarded-Host`` unless your proxy layer is confirmed
to strip that header on ingress — on a directly internet-facing
deployment, those headers are attacker-controlled.  The ``host``
header is set by the TLS-terminating proxy and is safe to use.

Returned URLs must be ``https://`` for non-loopback hosts.  Returning
``http://`` for a non-loopback hostname causes the per-request handler
to return HTTP 500 without echoing the bad URL to the client.
"""


from adcp.server.mcp_tools import create_tool_caller, get_tools_for_handler
from adcp.server.test_controller import TestControllerStore, _handle_test_controller

logger = logging.getLogger(__name__)


def _part_data_dict(part: pb.Part) -> dict[str, Any] | None:
    """Return the dict payload of a Part if it carries a ``data`` oneof, else None."""
    if part.WhichOneof("content") != "data":
        return None
    value = MessageToDict(part.data)
    return value


def _part_text(part: pb.Part) -> str | None:
    """Return the text payload of a Part if it carries a ``text`` oneof, else None."""
    if part.WhichOneof("content") != "text":
        return None
    return part.text


def _normalize_a2a_parameters(params: Any) -> dict[str, Any]:
    """Normalize protobuf Struct quirks in parsed A2A parameters."""
    if not isinstance(params, dict):
        return {}
    normalized = dict(params)
    major = normalized.get("adcp_major_version")
    # Protobuf Struct stores all JSON numbers as doubles, so MessageToDict
    # turns ``3`` into ``3.0``. Restore the integer wire field before the
    # shared AdCP version resolver sees it.
    if isinstance(major, float) and major.is_integer():
        normalized["adcp_major_version"] = int(major)
    return normalized


def _make_data_part(data: dict[str, Any]) -> pb.Part:
    """Build a Part carrying a ``data`` oneof from a plain dict."""
    value = Value()
    ParseDict(data, value)
    return pb.Part(data=value)


def _make_text_part(text: str) -> pb.Part:
    """Build a Part carrying a ``text`` oneof."""
    return pb.Part(text=text)


class ADCPAgentExecutor(AgentExecutor):
    """Bridges ADCPHandler methods to the a2a-sdk AgentExecutor interface.

    Incoming A2A messages are parsed to extract the ADCP skill name and
    parameters, dispatched to the matching handler method, and the result
    is published back as A2A Task events.

    Expects the explicit skill invocation format used by A2AAdapter:
        Part(data={"skill": "get_products", "parameters": {...}})
    """

    def __init__(
        self,
        handler: ADCPHandler[Any],
        test_controller: TestControllerStore | None = None,
        *,
        context_factory: ContextFactory | None = None,
        middleware: Sequence[SkillMiddleware] | None = None,
        message_parser: MessageParser | None = None,
        advertise_all: bool = False,
        validation: ValidationHookConfig | None = SERVER_DEFAULT_VALIDATION,
        pre_validation_hooks: PreValidationHooks | None = None,
        test_controller_account_resolver: Any | None = None,
    ) -> None:
        self._handler = handler
        self._context_factory = context_factory
        self._test_controller_account_resolver = test_controller_account_resolver
        # Store as a tuple so the executor can't be mutated from underneath
        # at runtime (a flaky test or a handler reaching self._middleware
        # can't corrupt the dispatch chain). Tuple ordering = runtime
        # ordering; first entry wraps outermost (see ``SkillMiddleware``
        # docstring for the composition semantics).
        self._middleware: tuple[SkillMiddleware, ...] = tuple(middleware or ())
        # Seller-supplied parser for non-default wire shapes (JSON-RPC,
        # bare TextPart with different skill layout, etc.). Falls back
        # to the built-in parser when None.
        self._message_parser: MessageParser | None = message_parser
        self._tool_callers: dict[str, Any] = {}

        # Build tool callers for all tools this handler supports.
        # Skip comply_test_controller unless the seller passed a
        # TestControllerStore; otherwise we would advertise a skill
        # backed only by the handler's not-supported stub.
        tool_defs = get_tools_for_handler(handler, advertise_all=advertise_all)
        for tool_def in tool_defs:
            name = tool_def["name"]
            if name == "comply_test_controller" and test_controller is None:
                continue
            hook = (pre_validation_hooks or {}).get(name)
            self._tool_callers[name] = create_tool_caller(
                handler,
                name,
                validation=validation,
                pre_validation_hook=hook,
                default_unnegotiated_adcp_version=None,
            )

        if test_controller is not None:
            self._register_test_controller(test_controller)

    @property
    def supported_skills(self) -> list[str]:
        """List of skill names this executor can handle."""
        return list(self._tool_callers.keys())

    def _register_test_controller(self, store: TestControllerStore) -> None:
        """Register comply_test_controller as a callable skill.

        Threads the ToolContext that the A2A executor built for this
        dispatch into the store so header-driven test state (populated
        by ``context_factory`` from ``ServerCallContext.user`` /
        message-metadata headers) composes with the storyboard-driven
        ``comply_test_controller`` skill. See #227.
        """

        resolver = self._test_controller_account_resolver

        async def _call_test_controller(
            params: dict[str, Any], context: ToolContext | None = None
        ) -> Any:
            return await _handle_test_controller(
                store,
                params,
                context=context,
                account_resolver=resolver,
            )

        self._tool_callers["comply_test_controller"] = _call_test_controller

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Execute an ADCP skill from an incoming A2A message."""
        skill_name, params = self._parse_request(context)

        if skill_name is None:
            await self._send_error(event_queue, context, "No skill specified in message")
            return

        if skill_name not in self._tool_callers:
            await self._send_error(event_queue, context, f"Unknown skill: {skill_name}")
            return

        tool_context = self._build_tool_context(skill_name, context)
        # Catch both the client-side :class:`ADCPError` (raised by
        # framework helpers like ``IdempotencyConflictError``) AND the
        # decisioning-layer :class:`AdcpError` (raised by platform methods
        # adopters write against the decisioning graph). They are
        # disjoint hierarchies; both project onto the same structured
        # ``adcp_error`` envelope per transport-errors.mdx §A2A Binding.
        structured_error_types: tuple[type[BaseException], ...] = (
            ADCPError,
            *_DECISIONING_ADCP_ERROR_TYPES,
        )
        try:
            result = await self._dispatch_with_middleware(skill_name, params, tool_context)
            # ``params`` carries the parsed wire request including any
            # ``context`` extension. Both success and error paths thread
            # it through to the result builder so the context-passthrough
            # contract holds across the dispatch outcome.
            await self._send_result(event_queue, context, skill_name, result, params)
        except structured_error_types as exc:
            # Application-layer AdCP error. Emit a failed task with the
            # adcp_error in a DataPart per transport-errors.mdx §A2A
            # Binding, plus a human-readable text part. The JSON-RPC
            # channel is reserved for transport-level errors (auth
            # rejected, rate-limited pre-dispatch).
            logger.info("AdCP application error for skill %s: %s", skill_name, exc)
            await self._send_adcp_error(event_queue, context, exc, params)
        except Exception:
            logger.exception("Error executing skill %s", skill_name)
            await self._send_error(event_queue, context, f"Skill execution failed: {skill_name}")

    async def _dispatch_with_middleware(
        self,
        skill_name: str,
        params: dict[str, Any],
        tool_context: ToolContext,
    ) -> Any:
        """Run the handler wrapped in the configured middleware chain.

        Delegates to :func:`adcp.server.serve._dispatch_with_middleware`
        so the composition semantics stay identical between transports —
        middleware that works with ``create_a2a_server(middleware=...)``
        works unchanged with ``create_mcp_server(middleware=...)``.

        Middleware exceptions propagate to the executor's normal error
        handling path in ``execute()``; this method does no try/except
        so short-circuiting, transform, and exception-observation all
        work the same way they do for the underlying handler.
        """
        from adcp.server.serve import _dispatch_with_middleware

        async def _call_handler() -> Any:
            return await self._tool_callers[skill_name](params, tool_context)

        return await _dispatch_with_middleware(
            self._middleware, skill_name, params, tool_context, _call_handler
        )

    def _build_tool_context(self, skill_name: str, request: RequestContext) -> ToolContext:
        """Build the :class:`ToolContext` handed to the skill dispatcher.

        When ``context_factory`` is configured, call it with a
        :class:`RequestMetadata` describing this A2A invocation; overlay the
        transport-derived ``caller_identity`` / ``request_id`` afterwards
        **only when the factory left them unset**, so factories that already
        know the principal (e.g. from a ContextVar the seller's auth layer
        populated) aren't clobbered.

        When no factory is configured, fall back to the A2A-only path that
        derives ``caller_identity`` from ``ServerCallContext.user`` —
        preserving behavior for sellers who haven't adopted
        ``context_factory=`` yet.
        """
        if self._context_factory is None:
            return _tool_context_from_request(request)

        from adcp.server.serve import RequestMetadata

        meta = RequestMetadata(
            tool_name=skill_name,
            transport="a2a",
            request_id=request.task_id,
        )
        ctx = self._context_factory(meta)
        if not isinstance(ctx, ToolContext):
            raise TypeError(
                f"context_factory for skill {skill_name!r} returned "
                f"{type(ctx).__name__}, not a ToolContext instance"
            )
        # Fill in transport-derived fields the factory didn't set. This
        # preserves the pre-factory A2A security invariant: if the seller
        # didn't explicitly populate caller_identity in their factory,
        # fall through to ServerCallContext.user (verified by the a2a-sdk
        # auth middleware) rather than silently sending None.
        if ctx.caller_identity is None:
            fallback = _tool_context_from_request(request)
            ctx.caller_identity = fallback.caller_identity
        if ctx.request_id is None:
            ctx.request_id = request.task_id
        return ctx

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """ADCP operations are synchronous; cancellation sets state to canceled."""
        event = _make_task(
            context,
            state=pb.TaskState.TASK_STATE_CANCELED,
            message="Task canceled",
        )
        await event_queue.enqueue_event(event)

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    def _parse_request(self, context: RequestContext) -> tuple[str | None, dict[str, Any]]:
        """Extract skill name and parameters from the A2A message.

        Dispatches to the caller-supplied :data:`MessageParser` when the
        executor was constructed with ``message_parser=``; otherwise
        falls through to :meth:`_default_parse_request`, which supports
        the standard shapes (DataPart with explicit skill + TextPart
        JSON fallback).
        """
        if self._message_parser is not None:
            return self._message_parser(context)
        return self._default_parse_request(context)

    def _default_parse_request(self, context: RequestContext) -> tuple[str | None, dict[str, Any]]:
        """Built-in parser. Supports two formats:

        1. Explicit skill invocation via a DataPart:
           ``Part(data={"skill": "get_products", "parameters": {...}})``
        2. Natural language fallback via TextPart (best-effort parse)

        Exposed as a module-level method so custom parsers can compose
        it — e.g. "try my JSON-RPC parser first, fall through to the
        default for legacy clients".
        """
        msg = context.message
        if msg is None or not msg.parts:
            return None, {}

        # Try DataPart first (explicit skill invocation)
        for part in msg.parts:
            data = _part_data_dict(part)
            if data is None:
                continue
            skill = data.get("skill")
            params = data.get("parameters", {})
            if skill:
                return str(skill), _normalize_a2a_parameters(params)

        # Fallback: try to parse TextPart as JSON
        for part in msg.parts:
            text = _part_text(part)
            if text is None:
                continue
            parsed = self._parse_text_request(text)
            if parsed[0] is not None:
                return parsed

        return None, {}

    def _parse_text_request(self, text: str) -> tuple[str | None, dict[str, Any]]:
        """Best-effort parse of a text request for skill + params."""
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "skill" in data:
                return str(data["skill"]), _normalize_a2a_parameters(data.get("parameters", {}))
        except (json.JSONDecodeError, TypeError):
            pass
        return None, {}

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    async def _send_result(
        self,
        event_queue: EventQueue,
        context: RequestContext,
        skill_name: str,
        result: Any,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Publish a completed task with the skill result.

        When ``params`` is supplied and carries a wire ``context`` field,
        echo it onto the result DataPart per the AdCP context-passthrough
        contract. This mirrors the MCP success path's
        :func:`adcp.server.helpers.inject_context` call in
        :mod:`adcp.server.mcp_tools` and keeps the error path's echo
        (see :meth:`_send_adcp_error`) symmetric on A2A.
        """
        # Normalize result to a JSON-safe dict
        if hasattr(result, "model_dump"):
            data = result.model_dump(mode="json", exclude_none=True)
        elif not isinstance(result, dict):
            data = {"result": result}
        else:
            data = result

        if params is not None and isinstance(data, dict):
            from adcp.server.helpers import inject_context

            inject_context(params, data)

        task = _make_task(
            context,
            state=pb.TaskState.TASK_STATE_COMPLETED,
            data=data,
            message=f"Completed {skill_name}",
        )
        await event_queue.enqueue_event(task)

    async def _send_error(
        self,
        event_queue: EventQueue,
        context: RequestContext,
        error_msg: str,
    ) -> None:
        """Publish a failed task."""
        task = _make_task(
            context,
            state=pb.TaskState.TASK_STATE_FAILED,
            message=error_msg,
        )
        await event_queue.enqueue_event(task)

    async def _send_adcp_error(
        self,
        event_queue: EventQueue,
        context: RequestContext,
        exc: Any,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Publish a failed task carrying an AdCP ``adcp_error`` payload.

        Follows transport-errors.mdx §A2A Binding: failed task with artifact
        containing a ``DataPart`` keyed under ``adcp_error`` plus a terse
        ``TextPart`` for human/LLM consumption.

        The structured envelope carries the full spec shape — ``code``,
        ``message``, ``recovery``, ``field``, ``suggestion``,
        ``retry_after``, ``details`` — populated when the raised
        exception supplies them, omitted when ``None``. Field extraction
        is shared with the MCP path via
        :func:`adcp.server.translate._extract_structured_fields`, so
        both transports project off the same source-of-truth shape.

        When ``params`` is supplied and carries a wire ``context`` field,
        that field is echoed alongside ``adcp_error`` in the DataPart —
        symmetric with the success path's
        :func:`adcp.server.helpers.inject_context` call. Without this
        echo, error responses violate the AdCP context-passthrough
        contract and buyers lose correlation IDs across the
        raise-AdcpError boundary.
        """
        # Lazy import — ``translate.py`` pulls in heavier server deps
        # (mcp.types) which the A2A module doesn't otherwise need.
        from adcp.server.helpers import inject_context
        from adcp.server.translate import _extract_structured_fields

        code, message, recovery, field, suggestion, details, _errors = _extract_structured_fields(
            exc
        )

        adcp_error: dict[str, Any] = {
            "code": code,
            "message": message,
            "recovery": recovery,
        }
        if field is not None:
            adcp_error["field"] = field
        if suggestion is not None:
            adcp_error["suggestion"] = suggestion
        # ``retry_after`` lives on decisioning AdcpError; project when present.
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            adcp_error["retry_after"] = retry_after
        if details:
            adcp_error["details"] = dict(details)

        data: dict[str, Any] = {"adcp_error": adcp_error}
        if params is not None:
            inject_context(params, data)

        task = _make_task(
            context,
            state=pb.TaskState.TASK_STATE_FAILED,
            data=data,
            message=message,
        )
        await event_queue.enqueue_event(task)


# ------------------------------------------------------------------
# Request context helpers
# ------------------------------------------------------------------


def _tool_context_from_request(request: RequestContext) -> ToolContext:
    """Derive a :class:`ToolContext` from an A2A :class:`RequestContext`.

    Extracts the authenticated principal from ``request.call_context.user``
    when present. Unauthenticated / anonymous requests get a bare
    ``ToolContext`` — server middleware that requires a principal (e.g. the
    idempotency store's per-principal scoping) falls through to its
    no-principal default rather than collapsing everyone into a shared
    namespace.

    Security invariant: ``ServerCallContext`` is populated by the seller's
    server-side auth middleware from verified transport material (bearer
    token, mTLS cert, OAuth identity). A malicious client cannot flip
    ``is_authenticated`` or set ``user_name`` from the message payload.
    The ``is_authenticated and user_name`` gate below relies on this
    invariant — do not relax it.

    PII note: the ``user_name`` string becomes ``caller_identity``, which
    the idempotency middleware logs prefix-truncated at DEBUG. If your auth
    layer sets ``user_name`` to an email address, treat idempotency debug
    logs as containing PII. Prefer opaque principal IDs.
    """
    ctx = ToolContext(request_id=request.task_id)
    call_context = getattr(request, "call_context", None)
    user = getattr(call_context, "user", None)
    if user is not None:
        is_auth = getattr(user, "is_authenticated", False)
        user_name = getattr(user, "user_name", "") or ""
        if is_auth and user_name:
            ctx.caller_identity = user_name
    return ctx


# ------------------------------------------------------------------
# Task factory
# ------------------------------------------------------------------


def _make_task(
    context: RequestContext,
    *,
    state: int,
    data: dict[str, Any] | None = None,
    message: str | None = None,
) -> pb.Task:
    """Build an a2a Task event from context and result data."""
    parts: list[pb.Part] = []
    if data is not None:
        parts.append(_make_data_part(data))
    if message:
        parts.append(_make_text_part(message))

    artifacts: list[pb.Artifact] = []
    if parts:
        artifacts.append(
            pb.Artifact(
                artifact_id=str(uuid4()),
                parts=parts,
            )
        )

    return pb.Task(
        id=context.task_id or str(uuid4()),
        context_id=context.context_id or str(uuid4()),
        status=pb.TaskStatus(state=state),  # type: ignore[arg-type]
        artifacts=artifacts,
    )


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


_BEARER_HTTP_SCHEME_ID = "bearerAuth"
_API_KEY_SCHEME_ID = "adcpAuth"


def _build_security_for_auth(
    auth: BearerTokenAuth | None,
) -> tuple[dict[str, pb.SecurityScheme], list[pb.SecurityRequirement]]:
    """Translate a :class:`BearerTokenAuth` config into A2A agent-card
    security primitives.

    a2a-sdk's client auth interceptor (``a2a.client.auth.interceptor``)
    skips credential attachment when the agent card publishes neither
    ``security_schemes`` nor ``security_requirements`` — buyers built on
    a2a-sdk silently send unauthenticated requests against an
    auth-protected seller and see a 401 they have no obvious way to fix.
    Publishing a scheme that matches the A2A leg's actual carrier closes
    that loop without requiring the seller to hand-roll an agent card.

    Returns ``({}, [])`` when ``auth`` is ``None`` so unauthenticated
    agents continue to publish no security envelope (preserving the
    pre-auth public-discovery shape).

    Maps the resolved A2A header / prefix config to the right OpenAPI-
    flavored scheme. The cut is on ``bearer_prefix_required`` alone, not
    on header name — RFC 7235 reserves ``Authorization`` for
    ``<scheme> <credentials>`` and ``__post_init__`` already rejects the
    misuse combo of ``Authorization`` + ``bearer_prefix_required=False``:

    * Bearer prefix required → :class:`HTTPAuthSecurityScheme` with
      ``scheme="bearer"`` (RFC 6750, scheme id ``"bearerAuth"``). This
      is what a2a-sdk's interceptor knows how to attach credentials for
      out of the box.
    * No bearer prefix (raw-token custom header) →
      :class:`APIKeySecurityScheme` (``in: header``, scheme id
      ``"adcpAuth"``). Buyers reading the card see the right shape and
      know to attach a raw token to the named header.

    The single :class:`SecurityRequirement` references the scheme by id
    with no scope list — bearer / api-key flows have no scope semantics
    in OpenAPI 3.x. ``bearer_format`` is intentionally omitted: tokens
    are validator-defined and the field is purely descriptive (a2a-sdk's
    interceptor doesn't read it).
    """
    if auth is None:
        return {}, []

    header_name = auth.resolved_a2a_header_name()
    bearer_prefix = auth.resolved_a2a_bearer_prefix_required()

    if bearer_prefix:
        scheme_id = _BEARER_HTTP_SCHEME_ID
        scheme = pb.SecurityScheme(
            http_auth_security_scheme=pb.HTTPAuthSecurityScheme(scheme="bearer"),
        )
    else:
        scheme_id = _API_KEY_SCHEME_ID
        scheme = pb.SecurityScheme(
            api_key_security_scheme=pb.APIKeySecurityScheme(
                location="header",
                name=header_name,
            ),
        )

    requirement = pb.SecurityRequirement(
        schemes={scheme_id: pb.StringList(list=[])},
    )
    return {scheme_id: scheme}, [requirement]


def _build_agent_card(
    handler: ADCPHandler[Any],
    *,
    name: str,
    port: int,
    description: str | None = None,
    version: str = "1.0.0",
    extra_skills: list[pb.AgentSkill] | None = None,
    advertise_all: bool = False,
    push_notifications_supported: bool = False,
    auth: BearerTokenAuth | None = None,
    public_url: str | None = None,
) -> pb.AgentCard:
    """Build an A2A AgentCard from an ADCPHandler's tool definitions.

    ``comply_test_controller`` is excluded from the card skills list unless
    the caller supplied it via ``extra_skills`` (which is how
    :func:`create_a2a_server` opts in when a ``TestControllerStore`` is
    wired). Extra skills are deduped by id so advertising the test
    controller never produces two entries.

    Honors the same ``advertise_all`` semantic as
    :func:`~adcp.server.get_tools_for_handler` so the published agent
    card reflects what the executor will actually dispatch.

    The card advertises both the 0.3 and 1.0 protocol bindings via
    ``supported_interfaces`` so ``enable_v0_3_compat`` clients and native
    1.0 clients see the transport they expect on
    ``/.well-known/agent-card.json``.
    """
    tool_defs = get_tools_for_handler(handler, advertise_all=advertise_all)
    extra_ids = {s.id for s in extra_skills} if extra_skills else set()

    skills = [
        pb.AgentSkill(
            id=td["name"],
            name=td["name"],
            description=td.get("description", td["name"]),
            tags=["adcp"],
        )
        for td in tool_defs
        if td["name"] != "comply_test_controller" and td["name"] not in extra_ids
    ]

    if extra_skills:
        skills.extend(extra_skills)

    url = (public_url.rstrip("/") + "/") if public_url else f"http://localhost:{port}/"

    security_schemes, security_requirements = _build_security_for_auth(auth)

    return pb.AgentCard(
        name=name,
        description=description or f"ADCP agent: {name}",
        version=version,
        security_schemes=security_schemes,
        security_requirements=security_requirements,
        # Ordering is load-bearing: a2a-sdk's v0.3 compat converter
        # (``a2a.compat.v0_3.conversions.to_compat_agent_card``) sets
        # ``primary_interface = compat_interfaces[0]``, so the entry it
        # picks for the top-level 0.3 ``url`` / ``preferredTransport`` /
        # ``protocolVersion`` back-fill is whichever 0.3 interface it
        # sees first. Keep 0.3 at index 0. 1.0 clients don't iterate
        # positionally — they filter by ``protocol_version`` — so
        # listing 1.0 second has no negotiation cost.
        supported_interfaces=[
            pb.AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="0.3"),
            pb.AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0"),
        ],
        skills=skills,
        # Advertise ``push_notifications`` only when the server actually
        # has a store wired. The a2a-sdk request handler gates every
        # push-notif op on this capability flag, and advertising it
        # without a store just means clients hit
        # ``UnsupportedOperationError`` after a successful capability
        # probe — a worse UX than "capability says no, don't try".
        capabilities=pb.AgentCapabilities(
            streaming=False,
            push_notifications=push_notifications_supported,
        ),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
    )


def _validate_card_url(url: str) -> str:
    """Validate the URL returned by a :data:`PublicUrlResolver`.

    Raises ``ValueError`` when the value is not a valid absolute URL or
    uses ``http://`` for a non-loopback host.  The per-request card
    handler catches this and returns HTTP 500 without echoing the bad
    value to the client.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            f"public_url resolver returned {url!r} — "
            "must be an absolute URL with scheme and host."
        )
    hostname = parsed.hostname or ""
    is_loopback = hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".localhost")
    if parsed.scheme != "https" and not is_loopback:
        raise ValueError(
            f"public_url resolver returned {url!r} — "
            "scheme must be 'https' for non-loopback hosts."
        )
    return url


_CARD_PATHS: frozenset[str] = frozenset({"/.well-known/agent-card.json", "/.well-known/agent.json"})


class _PerRequestCardMiddleware:
    """ASGI middleware that serves agent-card endpoints per-request.

    Intercepts GET ``/.well-known/agent-card.json`` and
    ``/.well-known/agent.json``; all other scopes (including
    ``lifespan``) pass through unchanged.

    Installed via :meth:`starlette.applications.Starlette.add_middleware`
    so the wrapped object remains a Starlette app — its ``.router``
    stays reachable for lifespan composition in
    :func:`adcp.server.serve._serve_mcp_and_a2a`.
    """

    def __init__(
        self,
        app: Any,
        *,
        resolver: PublicUrlResolver,
        handler: ADCPHandler[Any],
        name: str,
        port: int,
        description: str | None,
        version: str,
        extra_skills: list[pb.AgentSkill] | None,
        advertise_all: bool,
        push_notifications_supported: bool,
        auth: BearerTokenAuth | None,
    ) -> None:
        self.app = app
        self.resolver = resolver
        self.handler = handler
        self.name = name
        self.port = port
        self.description = description
        self.version = version
        self.extra_skills = extra_skills
        self.advertise_all = advertise_all
        self.push_notifications_supported = push_notifications_supported
        self.auth = auth

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        import inspect

        from starlette.requests import Request
        from starlette.responses import JSONResponse

        if (
            scope.get("type") == "http"
            and scope.get("path") in _CARD_PATHS
            and scope.get("method") == "GET"
        ):
            request = Request(scope, receive)
            try:
                raw_url: str | Awaitable[str] = self.resolver(request)
                if inspect.isawaitable(raw_url):
                    raw_url = await raw_url
                assert isinstance(raw_url, str)
                url = _validate_card_url(raw_url)
            except Exception:
                logger.error("public_url resolver raised", exc_info=True)
                error_response: Any = JSONResponse(
                    {"error": "agent-card temporarily unavailable"}, status_code=500
                )
                await error_response(scope, receive, send)
                return
            card = _build_agent_card(
                self.handler,
                name=self.name,
                port=self.port,
                description=self.description,
                version=self.version,
                extra_skills=self.extra_skills,
                advertise_all=self.advertise_all,
                push_notifications_supported=self.push_notifications_supported,
                auth=self.auth,
                public_url=url,
            )
            from a2a.server.routes import agent_card_routes as _card_routes_mod

            agent_card_to_dict = _card_routes_mod.agent_card_to_dict  # type: ignore[attr-defined]
            card_response: Any = JSONResponse(agent_card_to_dict(card))
            await card_response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_a2a_server(
    handler: ADCPHandler[Any],
    *,
    name: str = "adcp-agent",
    port: int | None = None,
    description: str | None = None,
    version: str = "1.0.0",
    test_controller: TestControllerStore | None = None,
    test_controller_account_resolver: Any | None = None,
    context_factory: ContextFactory | None = None,
    task_store: TaskStore | None = None,
    push_config_store: PushNotificationConfigStore | None = None,
    middleware: Sequence[SkillMiddleware] | None = None,
    message_parser: MessageParser | None = None,
    advertise_all: bool = False,
    validation: ValidationHookConfig | None = SERVER_DEFAULT_VALIDATION,
    pre_validation_hooks: PreValidationHooks | None = None,
    context_builder: Any | None = None,
    auth: BearerTokenAuth | None = None,
    public_url: str | PublicUrlResolver | None = None,
) -> Any:
    """Create an A2A Starlette application from an ADCP handler.

    The returned app dual-serves the a2a-sdk 0.3 and 1.0 wire formats via
    ``create_jsonrpc_routes(enable_v0_3_compat=True)``. Existing 0.3
    clients keep getting lowercase ``"state": "completed"`` and
    ``"kind": "task"`` discriminators; native 1.0 clients get the new
    shape. Do not disable the compat flag.

    Args:
        handler: An ADCPHandler subclass instance.
        name: Agent name shown in the A2A agent card.
        port: Port number (used in the agent card URL).
        description: Agent description for the agent card.
        version: Agent version string.
        test_controller: Optional TestControllerStore for storyboard testing.
        context_factory: Optional callable invoked per skill call to build
            a :class:`ToolContext` from :class:`RequestMetadata`. Mirrors
            the MCP-side ``context_factory=`` on
            :func:`~adcp.server.create_mcp_server` so a single factory
            populates tenant/adapter fields on both transports. When
            unset, the executor falls back to deriving ``caller_identity``
            from ``ServerCallContext.user`` — preserving pre-factory
            behavior. See :data:`~adcp.server.ContextFactory` for the
            recommended contextvars pattern.
        task_store: Optional a2a-sdk :class:`~a2a.server.tasks.task_store.TaskStore`
            instance for persisting A2A task state. Defaults to
            :class:`~a2a.server.tasks.inmemory_task_store.InMemoryTaskStore`,
            which is single-process and non-durable — fine for demos and
            local development, but tasks vanish on restart and don't share
            across workers. Production agents pass a durable subclass
            (Postgres, Redis, etc.). See ``examples/a2a_db_tasks.py`` for
            a reference SQLite-backed implementation and
            ``docs/handler-authoring.md`` for the persistence caveats on
            the default store.
        push_config_store: Optional a2a-sdk
            :class:`~a2a.server.tasks.push_notification_config_store.PushNotificationConfigStore`
            instance for persisting push-notification configs that clients
            register via ``tasks/pushNotificationConfig/set``. **When
            unset, a2a-sdk surfaces push-notif endpoints as
            ``UnsupportedOperationError``** — clients cannot register
            subscriptions at all. Set this only when your agent is ready
            to accept push-notif subscriptions. See
            ``examples/a2a_db_tasks.py`` for a reference SQLite-backed
            implementation that pairs with the ``SqliteTaskStore`` there.

            Security note: unlike ``TaskStore``, a2a-sdk's
            ``PushNotificationConfigStore`` ABC does not pass a
            ``ServerCallContext`` to ``set_info`` / ``get_info`` /
            ``delete_info``. Scoping by principal has to happen out-of-band
            (via a ``ContextVar`` your auth middleware populates) or by
            composition with a tenant-scoped ``TaskStore`` — the reference
            impl shows the ContextVar pattern.
        middleware: Optional sequence of :data:`~adcp.server.SkillMiddleware`
            callables wrapping every A2A skill dispatch. Composes
            outermost-first (first entry sees the call before later
            entries and before the handler). Use for audit logging,
            activity-feed hooks, rate limiting, per-skill tracing. See
            :data:`~adcp.server.SkillMiddleware` for the signature,
            composition semantics, and the exception-capture pattern
            audit hooks need.
        message_parser: Optional :data:`MessageParser` for alternative
            wire shapes. The default parser handles a DataPart carrying
            ``{"skill": ..., "parameters": ...}`` plus a TextPart JSON
            fallback. Supply this to accept JSON-RPC 2.0 message bodies,
            vendor-specific DataPart schemas, or other layouts. The
            callable returns ``(skill_name, params)`` or ``(None, {})``
            for "no parseable skill"; see :data:`MessageParser` and
            :meth:`ADCPAgentExecutor._default_parse_request` for the
            built-in fallback shape to delegate to for legacy clients.
        advertise_all: When True, advertise every tool the handler type
            supports — including ones whose method is still the SDK's
            ``not_supported`` default. Defaults to ``False``, which
            reflects only overridden methods in the agent card's
            ``skills`` list and in the executor's tool-caller registry.
            Turn on for spec-compliance storyboards or when the agent
            deliberately wants clients to see a ``not_supported`` tool.
        validation: :class:`ValidationHookConfig` enabling schema
            validation of every request and response against the
            bundled AdCP JSON schemas. Defaults to
            :data:`~adcp.validation.client_hooks.SERVER_DEFAULT_VALIDATION`
            (strict on both sides). Pass
            ``ValidationHookConfig(responses="warn")`` to log+continue
            on response drift, or ``validation=None`` to disable
            validation entirely.
        auth: Optional :class:`~adcp.server.auth.BearerTokenAuth`
            config. When supplied, the agent card publishes a matching
            ``bearerAuth`` security scheme + requirement so a2a-sdk's
            client auth interceptor attaches credentials automatically.
            Note that ``create_a2a_server`` does **not** install the
            request-time middleware itself — auth gating is wired by
            :func:`adcp.server.serve` via :class:`A2ABearerAuthMiddleware`
            at the ASGI layer. Adopters calling ``create_a2a_server``
            directly must wrap the returned app with
            :class:`A2ABearerAuthMiddleware` themselves.
        public_url: Public base URL for the A2A agent card
            (``/.well-known/agent-card.json``). Accepts either a static
            string or a :data:`PublicUrlResolver` callable for per-request
            resolution.

            *Static string* — replaces ``http://localhost:{port}/`` in
            every ``supported_interfaces`` URL.  Falls back to the
            ``PUBLIC_URL`` environment variable when ``public_url`` is
            ``None``.  Correct for single-host or fixed-URL deployments.

            *Callable* — receives the Starlette
            :class:`~starlette.requests.Request` on each card fetch and
            must return an absolute ``https://`` URL.  Use this for
            multi-tenant subdomain deployments where each tenant has its
            own public host::

                def agent_card_url(request: Request) -> str:
                    host = request.headers.get("host", "localhost")
                    return f"https://{host}/"

                serve(handler, transport="a2a", public_url=agent_card_url)

            When a callable is supplied the a2a-sdk's static
            ``create_agent_card_routes`` is bypassed in favour of an
            ASGI-layer intercept that builds the card per-request.  The
            ``DefaultRequestHandler``'s internal ``GetAgentCard`` RPC
            path retains a ``localhost`` fallback card — buyers probing
            the well-known endpoint always receive the per-request card.

            The ``PUBLIC_URL`` env-var fallback applies only when
            ``public_url`` is ``None``; a callable takes priority.

    Returns:
        A Starlette app ready to be run with uvicorn.
    """
    resolved_port = port or int(os.environ.get("PORT", "3001"))
    # A callable resolver takes priority; env-var fallback only applies
    # when public_url is None (not callable).
    resolved_public_url: str | PublicUrlResolver | None = (
        public_url if public_url is not None else os.environ.get("PUBLIC_URL")
    )

    executor = ADCPAgentExecutor(
        handler,
        test_controller=test_controller,
        context_factory=context_factory,
        middleware=middleware,
        message_parser=message_parser,
        advertise_all=advertise_all,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
        test_controller_account_resolver=test_controller_account_resolver,
    )

    if task_store is None:
        task_store = InMemoryTaskStore()

    # ``enable_v0_3_compat=True`` is load-bearing: it makes the server
    # dual-serve 0.3 and 1.0 wire formats on the same endpoint so existing
    # 0.3 buyer clients keep working unchanged. Do not disable.
    #
    # ``context_builder`` is the a2a-sdk seam for customising the
    # :class:`ServerCallContext` each handler receives. We thread it
    # through verbatim when supplied — bearer-token auth is wired
    # separately via :class:`A2ABearerAuthMiddleware` at the ASGI
    # layer (see ``serve.py:_wrap_a2a_with_auth``) because the v0.3
    # compat adapter swallows builder-raised ``HTTPException``s. The
    # builder kwarg remains for adopters customising the
    # ``ServerCallContext`` shape (e.g. surfacing additional
    # ``state`` fields from the request).
    jsonrpc_kwargs: dict[str, Any] = {
        "rpc_url": "/",
        "enable_v0_3_compat": True,
    }
    if context_builder is not None:
        jsonrpc_kwargs["context_builder"] = context_builder

    _extra_skills = _test_controller_skills() if test_controller else None
    _push_supported = push_config_store is not None

    if callable(resolved_public_url):
        # Per-request path: build a localhost fallback card for
        # DefaultRequestHandler's internal GetAgentCard RPC (buyers probe
        # /.well-known/agent-card.json directly; the RPC fallback is rarely
        # used). The well-known endpoints are served by
        # _PerRequestCardMiddleware which builds a fresh card per GET.
        fallback_card = _build_agent_card(
            handler,
            name=name,
            port=resolved_port,
            description=description,
            version=version,
            extra_skills=_extra_skills,
            advertise_all=advertise_all,
            push_notifications_supported=_push_supported,
            auth=auth,
            public_url=None,
        )
        # DefaultRequestHandler stores push_config_store verbatim and
        # treats None as "push-notif unsupported". Passing None is the
        # correct default; sellers opt in by wiring a store.
        request_handler = DefaultRequestHandler(
            agent_executor=executor,
            task_store=task_store,
            agent_card=fallback_card,
            push_config_store=push_config_store,
        )
        jsonrpc_kwargs["request_handler"] = request_handler
        routes = list(create_jsonrpc_routes(**jsonrpc_kwargs))
        # Install the per-request card intercept via ``add_middleware``
        # so ``app`` stays a Starlette instance — the unified-transport
        # lifespan composer in ``serve._serve_mcp_and_a2a`` reaches
        # ``a2a_inner.router.lifespan_context`` on this object.
        app = Starlette(routes=routes)
        app.add_middleware(
            _PerRequestCardMiddleware,
            resolver=resolved_public_url,
            handler=handler,
            name=name,
            port=resolved_port,
            description=description,
            version=version,
            extra_skills=_extra_skills,
            advertise_all=advertise_all,
            push_notifications_supported=_push_supported,
            auth=auth,
        )
    else:
        # Static card path: existing behaviour — card built once at
        # server init and served unchanged on every card request.
        agent_card = _build_agent_card(
            handler,
            name=name,
            port=resolved_port,
            description=description,
            version=version,
            extra_skills=_extra_skills,
            advertise_all=advertise_all,
            push_notifications_supported=_push_supported,
            auth=auth,
            public_url=resolved_public_url,
        )
        # DefaultRequestHandler stores push_config_store verbatim and treats
        # None as "push-notif endpoints unsupported" (UnsupportedOperationError
        # on tasks/pushNotificationConfig/*). Passing None is the correct
        # default; sellers opt in by wiring a store.
        request_handler = DefaultRequestHandler(
            agent_executor=executor,
            task_store=task_store,
            agent_card=agent_card,
            push_config_store=push_config_store,
        )
        jsonrpc_kwargs["request_handler"] = request_handler
        routes = (
            list(create_agent_card_routes(agent_card=agent_card))
            # 0.3 alias: A2A 0.3 buyer SDKs probe /.well-known/agent.json
            # as a positive A2A signal. Same handler, no redirect round-trip.
            + list(
                create_agent_card_routes(agent_card=agent_card, card_url="/.well-known/agent.json")
            )
            + list(create_jsonrpc_routes(**jsonrpc_kwargs))
        )
        app = Starlette(routes=routes)

    # Startup log lives on the create_a2a_server path (symmetric with
    # MCP's _register_handler_tools). Moved out of
    # ADCPAgentExecutor.__init__ so per-test executor constructions
    # don't pollute caplog with repeated startup messages.
    from adcp.server.serve import _log_advertised_tools

    _log_advertised_tools(
        transport="a2a",
        handler=handler,
        advertise_all=advertise_all,
        registered=list(executor.supported_skills),
    )

    return app


def _test_controller_skills() -> list[pb.AgentSkill]:
    """Build A2A skill definition for comply_test_controller."""
    return [
        pb.AgentSkill(
            id="comply_test_controller",
            name="comply_test_controller",
            description="Compliance test controller. Sandbox only, not for production use.",
            tags=["adcp", "testing"],
        )
    ]
