from __future__ import annotations

"""A2A protocol adapter using the official a2a-sdk client."""

import logging
import time
from typing import Any
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, A2AClient
from a2a.types import (
    DataPart,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskState,
    TextPart,
)

from adcp import _idempotency
from adcp.exceptions import (
    ADCPAuthenticationError,
    ADCPConnectionError,
    ADCPTimeoutError,
    IdempotencyConflictError,
    IdempotencyExpiredError,
)
from adcp.protocols.base import ProtocolAdapter
from adcp.signing.autosign import current_operation as _signing_operation
from adcp.types.core import AgentConfig, DebugInfo, TaskResult, TaskStatus
from adcp.validation.client_hooks import (
    validate_incoming_response,
    validate_outgoing_request,
)
from adcp.validation.schema_validator import SchemaValidationError, format_issues

logger = logging.getLogger(__name__)


class A2AAdapter(ProtocolAdapter):
    """Adapter for A2A protocol using official a2a-sdk client."""

    # A2A task states in which the server is still expecting more from
    # the buyer on the same task (input-required, auth-required, and
    # in-flight states). While the adapter holds a task_id in one of
    # these states, the next outbound Message must echo it back so the
    # server resumes the same task rather than orphaning it and starting
    # a new one. Everything else — completed/failed/canceled/rejected
    # (terminal) and the defensive unknown state — clears the retained
    # task_id so subsequent calls start a fresh task. Coupled directly
    # to the TaskState enum so a rename upstream is a type error, not a
    # silent behavior change.
    _NONTERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
        {
            TaskState.submitted,
            TaskState.working,
            TaskState.input_required,
            TaskState.auth_required,
        }
    )

    def __init__(self, agent_config: AgentConfig):
        """Initialize A2A adapter with official A2A client."""
        super().__init__(agent_config)
        self._httpx_client: httpx.AsyncClient | None = None
        self._a2a_client: A2AClient | None = None
        # A2A contextId for multi-turn conversations. First request sends
        # context_id=None → server mints one and returns it on Task.context_id;
        # we stash it here and echo it back on every subsequent send so the
        # server can scope state to the same session. Callers can seed this
        # via ADCPClient(context_id=...) to resume a session across process
        # restarts, or clear it via ADCPClient.reset_context() to start a
        # new conversation.
        self._context_id: str | None = None
        # A2A task_id retained across turns only while the prior task is
        # non-terminal (input-required, working, etc). On terminal states
        # this clears to None so the next call starts a new task under
        # the same context_id. Without this, resume of an input-required
        # task orphans the server-side in-flight task.
        self._active_task_id: str | None = None

    @property
    def context_id(self) -> str | None:
        """Current A2A conversation context_id, or None if not yet established.

        ``None`` means either (a) a fresh conversation where the server
        has not yet replied, or (b) the context was cleared via
        ``set_context_id(None)``. Callers that need to distinguish these
        must track their own state.

        Not thread-safe: the adapter mutates this on every response. For
        concurrent use, serialize calls on one adapter or construct one
        per conversation.
        """
        return self._context_id

    @property
    def active_task_id(self) -> str | None:
        """A2A task_id the next send must echo to resume the same task.

        Populated when the last response was non-terminal (e.g.
        ``input-required``, ``working``). Echoed on the next outbound
        message so the server continues the same task. Clears to None
        on terminal states (``completed``/``failed``/``canceled``/
        ``rejected``) — and defensively on ``unknown`` — so subsequent
        calls start a fresh task under the same context.
        """
        return self._active_task_id

    def set_context_id(self, context_id: str | None) -> None:
        """Set the A2A context_id for subsequent message sends.

        Pass ``None`` to clear — the server mints a fresh id on the next
        call — or a string to seed. Seeding is safe for *resume* (pass
        back an id the server previously returned). Seeding with a
        *self-generated* id is server-dependent: per the A2A spec,
        agents MAY accept or reject client-supplied ids, and some
        frameworks (notably ADK) rewrite the id into their own session
        format and return the rewritten value on the next response — at
        which point this adapter auto-adopts it.

        Also clears any retained ``active_task_id``: switching context
        always starts a fresh task under the new context.
        """
        self._context_id = context_id
        self._active_task_id = None

    def _restore_active_task_id(self, task_id: str) -> None:
        """Internal: rehydrate ``active_task_id`` from a persisted checkpoint.

        Separate from normal in-flight state updates so the checkpoint
        restore path is an explicit contract — a rename of the storage
        field fails loudly here instead of silently breaking resume.
        Intended for ``ADCPClient.from_checkpoint`` only.
        """
        self._active_task_id = task_id

    async def _get_httpx_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client with connection pooling."""
        if self._httpx_client is None:
            limits = httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0,
            )

            headers = {}
            if self.agent_config.auth_token:
                if self.agent_config.auth_type == "bearer":
                    headers["Authorization"] = f"Bearer {self.agent_config.auth_token}"
                else:
                    headers[self.agent_config.auth_header] = self.agent_config.auth_token

            # When ADCPClient installed a signing_request_hook, register it as
            # an httpx request event hook so RFC 9421 signature headers are
            # attached transparently to every outgoing request. The hook is
            # a bound method, so each call reads the owning client's live
            # state (signing config, cached capabilities) — it is *not* a
            # snapshot. Out-of-band calls (e.g. agent-card fetch) no-op
            # inside the hook because the autosign ContextVar isn't set.
            #
            # follow_redirects is forced off whenever signing is active: RFC
            # 9421 binds the signature to the original `@authority`, so a 302
            # would forward stale signature bytes to a new host. httpx's
            # current default is False already, but pinning it matches the
            # MCP factory's invariant and protects against future upstream
            # changes or a2a-sdk overrides.
            event_hooks: dict[str, list[Any]] = {}
            client_kwargs: dict[str, Any] = {
                "limits": limits,
                "headers": headers,
                "timeout": self.agent_config.timeout,
            }
            if self.signing_request_hook is not None:
                event_hooks["request"] = [self.signing_request_hook]
                client_kwargs["follow_redirects"] = False
            if event_hooks:
                client_kwargs["event_hooks"] = event_hooks

            self._httpx_client = httpx.AsyncClient(**client_kwargs)
            logger.debug(
                f"Created HTTP client with connection pooling for agent {self.agent_config.id}"
            )
        return self._httpx_client

    async def _get_a2a_client(self) -> A2AClient:
        """Get or create the A2A client."""
        if self._a2a_client is None:
            httpx_client = await self._get_httpx_client()

            # Use A2ACardResolver to fetch the agent card
            card_resolver = A2ACardResolver(
                httpx_client=httpx_client,
                base_url=self.agent_config.agent_uri,
            )

            try:
                agent_card = await card_resolver.get_agent_card()
                logger.debug(f"Fetched agent card for {self.agent_config.id}")
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                if status_code in (401, 403):
                    raise ADCPAuthenticationError(
                        f"Authentication failed: HTTP {status_code}",
                        agent_id=self.agent_config.id,
                        agent_uri=self.agent_config.agent_uri,
                    ) from e
                else:
                    raise ADCPConnectionError(
                        f"Failed to fetch agent card: HTTP {status_code}",
                        agent_id=self.agent_config.id,
                        agent_uri=self.agent_config.agent_uri,
                    ) from e
            except httpx.TimeoutException as e:
                raise ADCPTimeoutError(
                    f"Timeout fetching agent card: {e}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                    timeout=self.agent_config.timeout,
                ) from e
            except httpx.HTTPError as e:
                raise ADCPConnectionError(
                    f"Failed to fetch agent card: {e}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e

            self._a2a_client = A2AClient(
                httpx_client=httpx_client,
                agent_card=agent_card,
            )
            logger.debug(f"Created A2A client for agent {self.agent_config.id}")

        return self._a2a_client

    async def close(self) -> None:
        """Close the HTTP client and clean up resources."""
        if self._httpx_client is not None:
            logger.debug(f"Closing A2A adapter client for agent {self.agent_config.id}")
            await self._httpx_client.aclose()
            self._httpx_client = None
            self._a2a_client = None

    async def _call_a2a_tool(
        self, tool_name: str, params: dict[str, Any], use_explicit_skill: bool = True
    ) -> TaskResult[Any]:
        """
        Call a tool using A2A protocol via official a2a-sdk client.

        Args:
            tool_name: Name of the skill/tool to invoke
            params: Parameters to pass to the skill
            use_explicit_skill: If True, use explicit skill invocation (deterministic).
                               If False, use natural language (flexible).

        The default is explicit skill invocation for predictable, repeatable behavior.
        See: https://docs.adcontextprotocol.org/docs/protocols/a2a-guide
        """
        start_time = time.time() if self.agent_config.debug else None
        if _idempotency.is_mutating(tool_name) and self.idempotency_capability_check:
            await self.idempotency_capability_check()
        params, idempotency_key = _idempotency.inject_key(
            tool_name, params, client_token=self.idempotency_client_token
        )

        # Pre-send schema validation. Matches the MCP adapter: strict mode
        # surfaces as TaskStatus.FAILED so the SDK's unified failure model
        # is preserved; warn mode logs and continues; off short-circuits.
        try:
            validate_outgoing_request(tool_name, params, self.request_validation_mode)
        except SchemaValidationError as exc:
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=str(exc),
                success=False,
                idempotency_key=idempotency_key,
            )

        a2a_client = await self._get_a2a_client()

        # Build A2A message
        message_id = str(uuid4())

        if use_explicit_skill:
            # Explicit skill invocation (deterministic)
            # Use DataPart with skill name and parameters
            data_part = DataPart(
                data={
                    "skill": tool_name,
                    "parameters": params,
                }
            )
            message = Message(
                message_id=message_id,
                role=Role.user,
                parts=[Part(root=data_part)],
                context_id=self._context_id,
                task_id=self._active_task_id,
            )
        else:
            # Natural language invocation (flexible)
            # Agent interprets intent from text
            text_part = TextPart(text=self._format_tool_request(tool_name, params))
            message = Message(
                message_id=message_id,
                role=Role.user,
                parts=[Part(root=text_part)],
                context_id=self._context_id,
                task_id=self._active_task_id,
            )

        # Build request params
        params_obj = MessageSendParams(message=message)

        # Build request
        request = SendMessageRequest(
            id=str(uuid4()),
            params=params_obj,
        )

        debug_info = None
        debug_request: dict[str, Any] = {}
        if self.agent_config.debug:
            debug_request = {
                "method": "send_message",
                "message_id": message_id,
                "tool": tool_name,
                "params": _idempotency.redact_params(params),
            }

        # Stamp the AdCP operation name so the httpx request event hook
        # installed by ADCPClient for RFC 9421 auto-signing can look up the
        # right signing policy. Set only around send_message so out-of-band
        # httpx calls (the agent-card fetch above, or unrelated work on
        # sibling tasks) stay outside the signing scope.
        signing_token = _signing_operation.set(tool_name)
        try:
            # Use official A2A client
            sdk_response = await a2a_client.send_message(request)

            # SendMessageResponse is a RootModel union - unwrap it to get the actual response
            # (either JSONRPCSuccessResponse or JSONRPCErrorResponse)
            response = sdk_response.root if hasattr(sdk_response, "root") else sdk_response

            # Handle JSON-RPC error response
            if hasattr(response, "error"):
                error_msg = response.error.message if response.error.message else "Unknown error"
                if self.agent_config.debug and start_time:
                    duration_ms = (time.time() - start_time) * 1000
                    debug_info = DebugInfo(
                        request=debug_request,
                        response=_idempotency.deep_redact({"error": response.error.model_dump()}),
                        duration_ms=duration_ms,
                    )
                return TaskResult[Any](
                    status=TaskStatus.FAILED,
                    error=error_msg,
                    success=False,
                    debug_info=debug_info,
                    idempotency_key=idempotency_key,
                )

            # Handle success response
            if hasattr(response, "result"):
                result = response.result

                if self.agent_config.debug and start_time:
                    duration_ms = (time.time() - start_time) * 1000
                    debug_info = DebugInfo(
                        request=debug_request,
                        response=_idempotency.deep_redact({"result": result.model_dump()}),
                        duration_ms=duration_ms,
                    )

                # Result can be either Task or Message
                if isinstance(result, Task):
                    # Compute next-turn state from the response but do NOT
                    # commit yet — _process_task_response and the idempotency
                    # check below can raise, and leaving the adapter advanced
                    # after an exception would orphan the legitimate in-flight
                    # task on the next retry. Commit only after both succeed.
                    # Task.context_id is required by a2a-sdk, so no None-guard.
                    next_context_id = result.context_id
                    if result.status.state in self._NONTERMINAL_TASK_STATES:
                        next_active_task_id: str | None = result.id
                    else:
                        # Terminal states (completed/failed/canceled/rejected)
                        # clear the retained task_id — subsequent calls start
                        # a new task under the same context. The defensive
                        # unknown state falls here too (don't cling to an
                        # undefined task); warn so operators notice if a
                        # server starts emitting it.
                        next_active_task_id = None
                        if result.status.state == TaskState.unknown:
                            logger.warning(
                                "A2A agent %s returned TaskState.unknown for "
                                "task_id=%s; clearing active_task_id and "
                                "starting a fresh task on next call",
                                self.agent_config.id,
                                result.id,
                            )
                    task_result = self._process_task_response(result, debug_info)
                    _idempotency.raise_for_idempotency_error(
                        tool_name, task_result.data, self.agent_config.id
                    )
                    # All raise-sites have passed; commit next-turn state so
                    # the adapter reflects the response the caller is about
                    # to receive.
                    self._context_id = next_context_id
                    self._active_task_id = next_active_task_id
                    # Post-receive schema validation. Only runs when the task
                    # carries data (terminal completion); async interim states
                    # with ``data=None`` skip naturally. Strict mode flips the
                    # TaskResult to FAILED; warn mode logs and passes through.
                    # Runs after the state commit — a payload-schema failure
                    # doesn't invalidate the A2A envelope ids, and the next
                    # call in the same conversation should still target the
                    # right session.
                    if task_result.success and task_result.data is not None:
                        response_outcome = validate_incoming_response(
                            tool_name, task_result.data, self.response_validation_mode
                        )
                        if not response_outcome.valid and self.response_validation_mode == "strict":
                            task_result = TaskResult[Any](
                                status=TaskStatus.FAILED,
                                error=(
                                    f"Schema validation failed for {tool_name}: "
                                    f"{format_issues(response_outcome.issues)}"
                                ),
                                message=task_result.message,
                                success=False,
                                debug_info=task_result.debug_info,
                                idempotency_key=task_result.idempotency_key,
                            )
                    return _idempotency.annotate_result(task_result, idempotency_key)
                else:
                    # Message response (shouldn't happen for send_message, but handle it)
                    agent_id = self.agent_config.id
                    logger.warning(f"Received Message instead of Task from A2A agent {agent_id}")
                    return TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=None,
                        message="Received message response",
                        success=True,
                        debug_info=debug_info,
                    )

            # Shouldn't reach here
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error="Invalid response from A2A client",
                success=False,
                debug_info=debug_info,
                idempotency_key=idempotency_key,
            )

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response={"error": str(e), "status_code": status_code},
                    duration_ms=duration_ms,
                )

            if status_code in (401, 403):
                error_msg = f"Authentication failed: HTTP {status_code}"
            else:
                error_msg = f"HTTP {status_code} error: {e}"

            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=error_msg,
                success=False,
                debug_info=debug_info,
                idempotency_key=idempotency_key,
            )
        except httpx.TimeoutException as e:
            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response={"error": str(e)},
                    duration_ms=duration_ms,
                )
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=f"Timeout: {e}",
                success=False,
                debug_info=debug_info,
                idempotency_key=idempotency_key,
            )
        except (IdempotencyConflictError, IdempotencyExpiredError):
            # Propagate typed idempotency errors — callers MUST handle these
            # distinctly (mint fresh key / reconcile state). Other ADCPError
            # subclasses (connection, timeout, auth) continue to be converted
            # to TaskResult(failed) below, preserving the existing contract.
            raise
        except Exception as e:
            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response={"error": str(e)},
                    duration_ms=duration_ms,
                )
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=str(e),
                success=False,
                debug_info=debug_info,
                idempotency_key=idempotency_key,
            )
        finally:
            _signing_operation.reset(signing_token)

    def _process_task_response(self, task: Task, debug_info: DebugInfo | None) -> TaskResult[Any]:
        """Process a Task response from A2A into our TaskResult format."""
        task_state = task.status.state

        if task_state == TaskState.completed:
            # Extract the result from the artifacts array
            result_data = self._extract_result_from_task(task)

            # Check for task-level errors in the payload
            errors = result_data.get("errors", []) if isinstance(result_data, dict) else []
            has_errors = bool(errors)

            return TaskResult[Any](
                status=TaskStatus.COMPLETED,
                data=result_data,
                message=self._extract_text_from_task(task),
                success=not has_errors,
                metadata={
                    "task_id": task.id,
                    "context_id": task.context_id,
                },
                debug_info=debug_info,
            )
        elif task_state == TaskState.failed:
            # Protocol-level failure - extract error message from TextPart
            error_msg = self._extract_text_from_task(task) or "Task failed"
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=error_msg,
                success=False,
                debug_info=debug_info,
            )
        else:
            # Handle all interim states (submitted, working, input-required, etc.)
            return TaskResult[Any](
                status=TaskStatus.SUBMITTED,
                data=None,  # Interim responses may not have structured AdCP content
                message=self._extract_text_from_task(task),
                success=True,
                metadata={
                    "task_id": task.id,
                    "context_id": task.context_id,
                    "status": task_state,
                },
                debug_info=debug_info,
            )

    def _format_tool_request(self, tool_name: str, params: dict[str, Any]) -> str:
        """Format tool request as natural language for A2A."""
        import json

        return f"Execute tool: {tool_name}\nParameters: {json.dumps(params, indent=2)}"

    def _extract_result_from_task(self, task: Task) -> Any:
        """
        Extract result data from A2A Task following canonical format.

        Per A2A response spec:
        - Responses MUST include at least one DataPart (kind: "data")
        - When multiple DataParts exist in an artifact, the last one is authoritative
        - When multiple artifacts exist, use the last one (most recent in streaming)
        - DataParts contain structured AdCP payload
        """
        if not task.artifacts:
            logger.warning("A2A Task missing required artifacts array")
            return {}

        # Use last artifact (most recent in streaming scenarios)
        target_artifact = task.artifacts[-1]

        if not target_artifact.parts:
            logger.warning("A2A Task artifact has no parts")
            return {}

        # Find all DataParts (kind: "data")
        # Note: Parts are wrapped in a Part union type, access via .root
        from a2a.types import DataPart

        data_parts = [p.root for p in target_artifact.parts if isinstance(p.root, DataPart)]

        if not data_parts:
            logger.warning("A2A Task missing required DataPart (kind: 'data')")
            return {}

        # Use last DataPart as authoritative (handles streaming scenarios within an artifact)
        last_data_part = data_parts[-1]
        data = last_data_part.data

        # Some A2A implementations (e.g., ADK) wrap the response in {"response": {...}}
        # Unwrap it to get the actual AdCP payload if present
        if isinstance(data, dict) and "response" in data:
            return data["response"]

        return data

    def _extract_text_from_task(self, task: Task) -> str | None:
        """Extract human-readable message from TextPart if present."""
        if not task.artifacts:
            return None

        # Use last artifact (most recent in streaming scenarios)
        target_artifact = task.artifacts[-1]

        # Find TextPart (kind: "text")
        # Note: Parts are wrapped in a Part union type, access via .root
        for part in target_artifact.parts:
            if isinstance(part.root, TextPart):
                return part.root.text

        return None

    # ========================================================================
    # ADCP Protocol Methods
    # ========================================================================

    async def get_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get advertising products."""
        return await self._call_a2a_tool("get_products", params)

    async def list_creative_formats(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List supported creative formats."""
        return await self._call_a2a_tool("list_creative_formats", params)

    async def sync_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync creatives."""
        return await self._call_a2a_tool("sync_creatives", params)

    async def list_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List creatives."""
        return await self._call_a2a_tool("list_creatives", params)

    async def get_media_buy_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get media buy delivery."""
        return await self._call_a2a_tool("get_media_buy_delivery", params)

    async def get_media_buys(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get media buys with status, creative approval state, and optional delivery snapshots."""
        return await self._call_a2a_tool("get_media_buys", params)

    async def get_signals(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get signals."""
        return await self._call_a2a_tool("get_signals", params)

    async def activate_signal(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Activate signal."""
        return await self._call_a2a_tool("activate_signal", params)

    async def provide_performance_feedback(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Provide performance feedback."""
        return await self._call_a2a_tool("provide_performance_feedback", params)

    async def log_event(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Log event."""
        return await self._call_a2a_tool("log_event", params)

    async def sync_event_sources(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync event sources."""
        return await self._call_a2a_tool("sync_event_sources", params)

    async def sync_audiences(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync audiences."""
        return await self._call_a2a_tool("sync_audiences", params)

    async def sync_catalogs(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync catalogs."""
        return await self._call_a2a_tool("sync_catalogs", params)

    async def preview_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Generate preview URLs for a creative manifest."""
        return await self._call_a2a_tool("preview_creative", params)

    async def create_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create media buy."""
        return await self._call_a2a_tool("create_media_buy", params)

    async def update_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update media buy."""
        return await self._call_a2a_tool("update_media_buy", params)

    async def build_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Build creative."""
        return await self._call_a2a_tool("build_creative", params)

    async def get_creative_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get creative delivery."""
        return await self._call_a2a_tool("get_creative_delivery", params)

    async def list_accounts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List accounts."""
        return await self._call_a2a_tool("list_accounts", params)

    async def sync_accounts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync accounts."""
        return await self._call_a2a_tool("sync_accounts", params)

    async def get_account_financials(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get account financials."""
        return await self._call_a2a_tool("get_account_financials", params)

    async def report_usage(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Report account usage."""
        return await self._call_a2a_tool("report_usage", params)

    async def list_tools(self) -> list[str]:
        """
        List available tools from A2A agent.

        Uses A2A client which already fetched the agent card during initialization.
        """
        # Get the A2A client (which already fetched the agent card)
        a2a_client = await self._get_a2a_client()

        # Fetch the agent card using the official method
        try:
            agent_card = await a2a_client.get_card()

            # Extract skills from agent card
            tool_names = [skill.name for skill in agent_card.skills if skill.name]

            logger.info(f"Found {len(tool_names)} tools from A2A agent {self.agent_config.id}")
            return tool_names

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                logger.error(f"Authentication failed for A2A agent {self.agent_config.id}")
                raise ADCPAuthenticationError(
                    f"Authentication failed: HTTP {status_code}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e
            else:
                logger.error(f"HTTP {status_code} error fetching agent card: {e}")
                raise ADCPConnectionError(
                    f"Failed to fetch agent card: HTTP {status_code}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e
        except httpx.TimeoutException as e:
            logger.error(f"Timeout fetching agent card for {self.agent_config.id}")
            raise ADCPTimeoutError(
                f"Timeout fetching agent card: {e}",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
                timeout=self.agent_config.timeout,
            ) from e
        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching agent card: {e}")
            raise ADCPConnectionError(
                f"Failed to fetch agent card: {e}",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
            ) from e

    async def get_agent_info(self) -> dict[str, Any]:
        """
        Get agent information including AdCP extension metadata from A2A agent card.

        Uses A2A client's get_card() method to fetch the agent card and extracts:
        - Basic agent info (name, description, version)
        - AdCP extension (extensions.adcp.adcp_version, extensions.adcp.protocols_supported)
        - Available skills/tools

        Returns:
            Dictionary with agent metadata
        """
        # Get the A2A client (which already fetched the agent card)
        a2a_client = await self._get_a2a_client()

        logger.debug(f"Fetching A2A agent info for {self.agent_config.id}")

        try:
            agent_card = await a2a_client.get_card()

            # Extract basic info
            info: dict[str, Any] = {
                "name": agent_card.name,
                "description": agent_card.description,
                "version": agent_card.version,
                "protocol": "a2a",
            }

            # Extract skills/tools
            tool_names = [skill.name for skill in agent_card.skills if skill.name]
            if tool_names:
                info["tools"] = tool_names

            # Extract AdCP extension metadata
            # Note: AgentCard type doesn't include extensions in the SDK,
            # but it may be present at runtime
            extensions = getattr(agent_card, "extensions", None)
            if extensions:
                adcp_ext = extensions.get("adcp")
                if adcp_ext:
                    info["adcp_version"] = adcp_ext.get("adcp_version")
                    info["protocols_supported"] = adcp_ext.get("protocols_supported")

            logger.info(f"Retrieved agent info for {self.agent_config.id}")
            return info

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                raise ADCPAuthenticationError(
                    f"Authentication failed: HTTP {status_code}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e
            else:
                raise ADCPConnectionError(
                    f"Failed to fetch agent card: HTTP {status_code}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e
        except httpx.TimeoutException as e:
            raise ADCPTimeoutError(
                f"Timeout fetching agent card: {e}",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
                timeout=self.agent_config.timeout,
            ) from e
        except httpx.HTTPError as e:
            raise ADCPConnectionError(
                f"Failed to fetch agent card: {e}",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
            ) from e

    # ========================================================================
    # V3 Protocol Methods - Protocol Discovery
    # ========================================================================

    async def get_adcp_capabilities(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get AdCP capabilities from the agent."""
        return await self._call_a2a_tool("get_adcp_capabilities", params)

    # ========================================================================
    # V3 Protocol Methods - Content Standards
    # ========================================================================

    async def create_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create content standards configuration."""
        return await self._call_a2a_tool("create_content_standards", params)

    async def get_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get content standards configuration."""
        return await self._call_a2a_tool("get_content_standards", params)

    async def list_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List content standards configurations."""
        return await self._call_a2a_tool("list_content_standards", params)

    async def update_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update content standards configuration."""
        return await self._call_a2a_tool("update_content_standards", params)

    async def calibrate_content(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Calibrate content against standards."""
        return await self._call_a2a_tool("calibrate_content", params)

    async def validate_content_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Validate content delivery against standards."""
        return await self._call_a2a_tool("validate_content_delivery", params)

    async def get_media_buy_artifacts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get artifacts associated with a media buy."""
        return await self._call_a2a_tool("get_media_buy_artifacts", params)

    # ========================================================================
    # V3 Protocol Methods - Governance
    # ========================================================================

    async def get_creative_features(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Evaluate governance features for a creative."""
        return await self._call_a2a_tool("get_creative_features", params)

    async def sync_plans(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync campaign governance plans."""
        return await self._call_a2a_tool("sync_plans", params)

    async def check_governance(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Check an action against campaign governance."""
        return await self._call_a2a_tool("check_governance", params)

    async def report_plan_outcome(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Report the outcome of a governed action."""
        return await self._call_a2a_tool("report_plan_outcome", params)

    async def get_plan_audit_logs(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Retrieve governance audit logs for plans."""
        return await self._call_a2a_tool("get_plan_audit_logs", params)

    # ========================================================================
    # V3 Protocol Methods - Sponsored Intelligence
    # ========================================================================

    async def si_get_offering(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get sponsored intelligence offering."""
        return await self._call_a2a_tool("si_get_offering", params)

    async def si_initiate_session(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Initiate sponsored intelligence session."""
        return await self._call_a2a_tool("si_initiate_session", params)

    async def si_send_message(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Send message in sponsored intelligence session."""
        return await self._call_a2a_tool("si_send_message", params)

    async def si_terminate_session(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Terminate sponsored intelligence session."""
        return await self._call_a2a_tool("si_terminate_session", params)

    # ========================================================================
    # V3 Protocol Methods - Governance (Property Lists)
    # ========================================================================

    async def create_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create a property list for governance."""
        return await self._call_a2a_tool("create_property_list", params)

    async def get_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get a property list with optional resolution."""
        return await self._call_a2a_tool("get_property_list", params)

    async def list_property_lists(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List property lists."""
        return await self._call_a2a_tool("list_property_lists", params)

    async def update_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update a property list."""
        return await self._call_a2a_tool("update_property_list", params)

    async def delete_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Delete a property list."""
        return await self._call_a2a_tool("delete_property_list", params)

    # ========================================================================
    # V3 Protocol Methods - Governance (Collection Lists)
    # ========================================================================

    async def create_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create a collection list for governance."""
        return await self._call_a2a_tool("create_collection_list", params)

    async def get_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get a collection list with optional resolution."""
        return await self._call_a2a_tool("get_collection_list", params)

    async def list_collection_lists(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List collection lists."""
        return await self._call_a2a_tool("list_collection_lists", params)

    async def update_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update a collection list."""
        return await self._call_a2a_tool("update_collection_list", params)

    async def delete_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Delete a collection list."""
        return await self._call_a2a_tool("delete_collection_list", params)

    # ========================================================================
    # V3 Protocol Methods - Governance (Sync Governance)
    # ========================================================================

    async def sync_governance(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync governance agents attached to an account."""
        return await self._call_a2a_tool("sync_governance", params)

    # ========================================================================
    # V3 Protocol Methods - TMP
    # ========================================================================

    async def context_match(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Match ad context to buyer packages."""
        return await self._call_a2a_tool("context_match", params)

    async def identity_match(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Match user identity for package eligibility."""
        return await self._call_a2a_tool("identity_match", params)

    # ========================================================================
    # V3 Protocol Methods - Brand Rights
    # ========================================================================

    async def get_brand_identity(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get brand identity information."""
        return await self._call_a2a_tool("get_brand_identity", params)

    async def get_rights(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get available rights for licensing."""
        return await self._call_a2a_tool("get_rights", params)

    async def acquire_rights(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Acquire rights for brand content usage."""
        return await self._call_a2a_tool("acquire_rights", params)

    async def update_rights(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update terms of an existing rights acquisition."""
        return await self._call_a2a_tool("update_rights", params)

    # ========================================================================
    # V3 Protocol Methods - Compliance
    # ========================================================================

    async def comply_test_controller(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Compliance test controller (sandbox only)."""
        return await self._call_a2a_tool("comply_test_controller", params)
