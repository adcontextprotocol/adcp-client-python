"""ADCP-managed Streamable HTTP session controls.

The upstream MCP session manager owns the transport lifecycle. This
module keeps the SDK-specific safety knobs and observability wrapper in
one place so ``serve.py`` does not have to grow more private-FastMCP
plumbing at every call site.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import anyio
from anyio.abc import TaskStatus
from mcp.server.auth.middleware.bearer_auth import (
    AuthenticatedUser,
    AuthorizationContext,
    authorization_context,
)
from mcp.server.auth.provider import AccessToken
from mcp.server.runner import serve_loop
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER, StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import (
    StreamableHTTPSessionManager,
)
from mcp.types import INVALID_REQUEST, ErrorData, JSONRPCError
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from adcp.server.auth import REQUEST_SCOPE_DISCOVERY, _read_request_state_auth

logger = logging.getLogger("adcp.server")


@dataclass(frozen=True)
class _UnboundSession:
    """A pre-auth session that has not yet seen an authenticated caller."""


@dataclass(frozen=True)
class _ClaimedSession:
    """A session permanently bound to its first authenticated caller."""

    owner: AuthorizationContext


_UNBOUND_SESSION = _UnboundSession()


@dataclass(frozen=True)
class MCPSessionStats:
    """Snapshot of a Streamable HTTP session manager.

    Numeric age and idle values are seconds from the manager's local
    monotonic clock. They are intended for metrics/debug visibility, not
    wall-clock audit records.
    """

    active_sessions: int
    max_active_sessions: int | None
    total_sessions_created: int
    sessions_created_last_60s: int
    stateless: bool
    session_idle_timeout: float | None
    session_age_seconds: tuple[float, ...]
    session_idle_seconds: tuple[float, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "active_sessions": self.active_sessions,
            "max_active_sessions": self.max_active_sessions,
            "total_sessions_created": self.total_sessions_created,
            "sessions_created_last_60s": self.sessions_created_last_60s,
            "stateless": self.stateless,
            "session_idle_timeout": self.session_idle_timeout,
            "session_age_seconds": list(self.session_age_seconds),
            "session_idle_seconds": list(self.session_idle_seconds),
        }


class ADCPStreamableHTTPSessionManager(StreamableHTTPSessionManager):
    """Streamable HTTP manager with ADCP safety knobs.

    ``max_active_sessions`` is enforced under the same creation lock that
    guards upstream session creation, so concurrent one-shot clients
    cannot overshoot the configured cap.
    """

    def __init__(
        self,
        *args: Any,
        max_active_sessions: int | None = None,
        **kwargs: Any,
    ) -> None:
        if max_active_sessions is not None and (
            isinstance(max_active_sessions, bool)
            or not isinstance(max_active_sessions, int)
            or max_active_sessions <= 0
        ):
            raise ValueError(
                f"max_active_sessions must be a positive integer (got {max_active_sessions!r}); "
                "set None to disable the guard."
            )
        super().__init__(*args, **kwargs)
        self.max_active_sessions = max_active_sessions
        self._session_created_at: dict[str, float] = {}
        self._session_last_seen_at: dict[str, float] = {}
        self._session_creation_events: deque[float] = deque()
        self._total_sessions_created = 0
        self._session_bindings: dict[str, _UnboundSession | _ClaimedSession] = {}

    def session_stats(self) -> MCPSessionStats:
        """Return a point-in-time session snapshot."""
        now = time.monotonic()
        self._prune_tracking(now)
        active_ids = set(self._server_instances)
        ages = tuple(
            max(0.0, now - self._session_created_at[session_id])
            for session_id in sorted(active_ids)
            if session_id in self._session_created_at
        )
        idle = tuple(
            max(0.0, now - self._session_last_seen_at[session_id])
            for session_id in sorted(active_ids)
            if session_id in self._session_last_seen_at
        )
        return MCPSessionStats(
            active_sessions=len(active_ids),
            max_active_sessions=self.max_active_sessions,
            total_sessions_created=self._total_sessions_created,
            sessions_created_last_60s=len(self._session_creation_events),
            stateless=self.stateless,
            session_idle_timeout=self.session_idle_timeout,
            session_age_seconds=ages,
            session_idle_seconds=idle,
        )

    async def _handle_stateful_request(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Process a stateful request with ADCP max-session enforcement.

        This mirrors upstream MCP 2.x with the cap check inserted
        under ``_session_creation_lock`` and bookkeeping attached to the
        session create / cleanup points.
        """
        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)
        requestor = self._requestor(request, scope)

        if request_mcp_session_id is not None and request_mcp_session_id in self._server_instances:
            transport = self._server_instances[request_mcp_session_id]
            if not await self._authorize_existing_session(
                request_mcp_session_id,
                requestor,
                auth_middleware_ran=REQUEST_SCOPE_DISCOVERY in scope,
                anonymous_discovery=scope.get(REQUEST_SCOPE_DISCOVERY) is True,
            ):
                logger.warning(
                    "Rejecting request for session %s: session is unclaimed or credential does "
                    "not match its owner",
                    request_mcp_session_id[:64],
                )
                await self._send_session_not_found(scope, receive, send)
                return
            logger.debug("Session already exists, handling request directly")
            self._session_last_seen_at[request_mcp_session_id] = time.monotonic()
            if transport.idle_scope is not None and self.session_idle_timeout is not None:
                transport.idle_scope.deadline = anyio.current_time() + self.session_idle_timeout
            await transport.handle_request(scope, receive, send)
            if transport.is_terminated:
                self._server_instances.pop(request_mcp_session_id, None)
                self._session_owners.pop(request_mcp_session_id, None)
                self._session_bindings.pop(request_mcp_session_id, None)
                self._forget_session(request_mcp_session_id)
            return

        if request_mcp_session_id is None:
            logger.debug("Creating new transport")
            body = await request.body()
            if not _is_initialize_request(body):
                await self._send_missing_session_response(scope, receive, send)
                return
            receive = _replay_body_receive(body)
            async with self._session_creation_lock:
                if (
                    self.max_active_sessions is not None
                    and len(self._server_instances) >= self.max_active_sessions
                ):
                    await self._send_max_sessions_response(scope, receive, send)
                    return

                new_session_id = uuid4().hex
                http_transport = StreamableHTTPServerTransport(
                    mcp_session_id=new_session_id,
                    is_json_response_enabled=self.json_response,
                    event_store=self.event_store,
                    security_settings=self.security_settings,
                    retry_interval=self.retry_interval,
                )

                assert http_transport.mcp_session_id is not None
                if requestor is not None:
                    self._session_owners[http_transport.mcp_session_id] = requestor
                    binding: _UnboundSession | _ClaimedSession = _ClaimedSession(requestor)
                else:
                    binding = _UNBOUND_SESSION
                self._session_bindings[http_transport.mcp_session_id] = binding
                self._server_instances[http_transport.mcp_session_id] = http_transport
                self._remember_session(http_transport.mcp_session_id)
                logger.info("Created new MCP stateful transport")

                async def run_server(
                    *,
                    task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
                ) -> None:
                    async with http_transport.connect() as streams:
                        read_stream, write_stream = streams
                        task_status.started()
                        try:
                            idle_scope = anyio.CancelScope()
                            if self.session_idle_timeout is not None:
                                idle_scope.deadline = (
                                    anyio.current_time() + self.session_idle_timeout
                                )
                                http_transport.idle_scope = idle_scope

                            with idle_scope:
                                await serve_loop(
                                    self.app,
                                    read_stream,
                                    write_stream,
                                    lifespan_state=self._lifespan_state,
                                    session_id=http_transport.mcp_session_id,
                                )

                            if idle_scope.cancelled_caught:
                                assert http_transport.mcp_session_id is not None
                                logger.info("MCP stateful session idle timeout")
                                self._server_instances.pop(http_transport.mcp_session_id, None)
                                self._session_owners.pop(http_transport.mcp_session_id, None)
                                self._session_bindings.pop(http_transport.mcp_session_id, None)
                                self._forget_session(http_transport.mcp_session_id)
                                await http_transport.terminate()
                        except Exception:
                            logger.exception("MCP stateful session crashed")
                        finally:
                            if (
                                http_transport.mcp_session_id
                                and http_transport.mcp_session_id in self._server_instances
                                and not http_transport.is_terminated
                            ):
                                logger.info(
                                    "Cleaning up crashed MCP stateful session "
                                    "from active instances."
                                )
                                del self._server_instances[http_transport.mcp_session_id]
                                self._session_owners.pop(http_transport.mcp_session_id, None)
                                self._session_bindings.pop(http_transport.mcp_session_id, None)
                                self._forget_session(http_transport.mcp_session_id)

                task_group = getattr(self, "_task_group", None)
                if task_group is None:
                    raise RuntimeError("Task group is not initialized. Make sure to use run().")
                await task_group.start(run_server)
                await http_transport.handle_request(scope, receive, send)
        else:
            error_response = JSONRPCError(
                jsonrpc="2.0",
                id="server-error",
                error=ErrorData(
                    code=INVALID_REQUEST,
                    message="Session not found",
                ),
            )
            response = Response(
                content=error_response.model_dump_json(by_alias=True, exclude_none=True),
                status_code=HTTPStatus.NOT_FOUND,
                media_type="application/json",
            )
            await response(scope, receive, send)

    @staticmethod
    def _requestor(request: Request, scope: Scope) -> AuthorizationContext | None:
        """Project ADCP request state into MCP's standard principal shape."""
        user = scope.get("user")
        if isinstance(user, AuthenticatedUser):
            return authorization_context(user)

        triple = _read_request_state_auth(request)
        if triple is None:
            return None
        principal_identity, tenant_id, _metadata = triple
        if principal_identity is None:
            return None

        # The session manager needs a stable authorization context, not bearer
        # material.  Constructing MCP's transport-specific shape here keeps the
        # generic Starlette auth middleware independent of MCP internals.
        token = hashlib.sha256(f"{principal_identity}\0{tenant_id!r}".encode()).hexdigest()
        return authorization_context(
            AuthenticatedUser(
                AccessToken(
                    token=token,
                    client_id=principal_identity,
                    scopes=[],
                    subject=tenant_id,
                )
            )
        )

    async def _authorize_existing_session(
        self,
        session_id: str,
        requestor: AuthorizationContext | None,
        *,
        auth_middleware_ran: bool,
        anonymous_discovery: bool,
    ) -> bool:
        """Atomically authorize or first-claim a stateful MCP session."""
        async with self._session_creation_lock:
            binding = self._session_bindings.get(session_id)
            if isinstance(binding, _ClaimedSession):
                return requestor is not None and requestor == binding.owner
            if not isinstance(binding, _UnboundSession):
                return False
            if requestor is not None:
                claimed = _ClaimedSession(requestor)
                self._session_bindings[session_id] = claimed
                self._session_owners[session_id] = requestor
                return True
            # Auth-less servers retain their historical anonymous behavior.
            # Once BearerTokenAuthMiddleware is installed, however, only its
            # explicit discovery bypass may reuse an unbound session.
            return not auth_middleware_ran or anonymous_discovery

    async def _send_session_not_found(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        error_body = JSONRPCError(
            jsonrpc="2.0",
            id=None,
            error=ErrorData(code=INVALID_REQUEST, message="Session not found"),
        )
        response = Response(
            error_body.model_dump_json(by_alias=True, exclude_unset=True),
            status_code=HTTPStatus.NOT_FOUND,
            media_type="application/json",
        )
        await response(scope, receive, send)

    def _remember_session(self, session_id: str) -> None:
        now = time.monotonic()
        self._session_created_at[session_id] = now
        self._session_last_seen_at[session_id] = now
        self._session_creation_events.append(now)
        self._total_sessions_created += 1
        self._prune_tracking(now)

    def _forget_session(self, session_id: str) -> None:
        self._session_created_at.pop(session_id, None)
        self._session_last_seen_at.pop(session_id, None)

    def _prune_tracking(self, now: float) -> None:
        active_ids = set(self._server_instances)
        for session_id in set(self._session_created_at) - active_ids:
            self._forget_session(session_id)
        cutoff = now - 60.0
        while self._session_creation_events and self._session_creation_events[0] < cutoff:
            self._session_creation_events.popleft()

    async def _send_max_sessions_response(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        limit = self.max_active_sessions
        error_response = JSONRPCError(
            jsonrpc="2.0",
            id="server-error",
            error=ErrorData(
                code=INVALID_REQUEST,
                message=f"Too many active MCP sessions (limit {limit})",
            ),
        )
        response = Response(
            content=error_response.model_dump_json(by_alias=True, exclude_none=True),
            status_code=HTTPStatus.TOO_MANY_REQUESTS,
            media_type="application/json",
        )
        await response(scope, receive, send)

    async def _send_missing_session_response(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        error_response = JSONRPCError(
            jsonrpc="2.0",
            id="server-error",
            error=ErrorData(
                code=INVALID_REQUEST,
                message="Bad Request: Missing session ID",
            ),
        )
        response = Response(
            content=error_response.model_dump_json(by_alias=True, exclude_none=True),
            status_code=HTTPStatus.BAD_REQUEST,
            media_type="application/json",
        )
        await response(scope, receive, send)


def get_mcp_session_stats(mcp_or_manager: Any) -> MCPSessionStats:
    """Return session stats for a FastMCP server or session manager.

    The helper accepts either the object returned by
    :func:`adcp.server.create_mcp_server` or its ``_session_manager``.
    For non-ADCP managers, only the fields available from upstream MCP
    internals are populated.
    """
    manager = getattr(mcp_or_manager, "_session_manager", mcp_or_manager)
    if hasattr(manager, "session_stats"):
        stats = manager.session_stats()
        if isinstance(stats, MCPSessionStats):
            return stats

    server_instances = getattr(manager, "_server_instances", {}) or {}
    return MCPSessionStats(
        active_sessions=len(server_instances),
        max_active_sessions=getattr(manager, "max_active_sessions", None),
        total_sessions_created=0,
        sessions_created_last_60s=0,
        stateless=bool(getattr(manager, "stateless", False)),
        session_idle_timeout=getattr(manager, "session_idle_timeout", None),
        session_age_seconds=(),
        session_idle_seconds=(),
    )


def _is_initialize_request(body: bytes) -> bool:
    try:
        raw_message = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(raw_message, dict) and raw_message.get("method") == "initialize"


def _replay_body_receive(body: bytes) -> Receive:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


__all__ = [
    "ADCPStreamableHTTPSessionManager",
    "MCPSessionStats",
    "get_mcp_session_stats",
]
