from __future__ import annotations

"""A2A protocol adapter using the official a2a-sdk 1.0 client."""

import logging
import time
from typing import Any
from uuid import uuid4

import httpx
from a2a import types as pb
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value

from adcp import _idempotency
from adcp.exceptions import (
    ADCPAuthenticationError,
    ADCPConnectionError,
    ADCPTimeoutError,
    IdempotencyConflictError,
    IdempotencyExpiredError,
)
from adcp.protocols._adcp_errors import validate_adcp_error
from adcp.protocols.base import ProtocolAdapter
from adcp.signing.autosign import current_operation as _signing_operation
from adcp.types.core import AgentConfig, DebugInfo, TaskResult, TaskStatus
from adcp.validation.client_hooks import (
    validate_incoming_response,
    validate_outgoing_request,
)
from adcp.validation.schema_validator import SchemaValidationError, format_issues

logger = logging.getLogger(__name__)


def _part_data_dict(part: pb.Part) -> dict[str, Any] | None:
    """Return the dict payload of a Part if it carries a ``data`` oneof, else None."""
    if part.WhichOneof("content") != "data":
        return None
    value: Any = MessageToDict(part.data)
    if not isinstance(value, dict):
        return None
    return value


def _part_text(part: pb.Part) -> str | None:
    """Return the text payload of a Part if it carries a ``text`` oneof, else None."""
    if part.WhichOneof("content") != "text":
        return None
    return part.text


def _make_data_part(data: dict[str, Any]) -> pb.Part:
    """Build a Part carrying a ``data`` oneof from a plain dict."""
    value = Value()
    ParseDict(data, value)
    return pb.Part(data=value)


def _make_text_part(text: str) -> pb.Part:
    """Build a Part carrying a ``text`` oneof."""
    return pb.Part(text=text)


def _task_to_redacted_dict(task: pb.Task) -> dict[str, Any]:
    """Convert a Task proto to a debug-safe dict (camelCase JSON form)."""
    return MessageToDict(task)


def _filter_card_to_version(card: pb.AgentCard, version: str) -> pb.AgentCard:
    """Return a shallow copy of ``card`` whose ``supported_interfaces``
    is restricted to entries with ``protocol_version == version``.

    Non-matching entries are dropped; all other card fields are
    preserved. The resulting card is what we pass to ``ClientFactory``
    when the user wants to pin a specific A2A wire version.
    """
    clone = pb.AgentCard()
    clone.CopyFrom(card)
    keep = [iface for iface in card.supported_interfaces if iface.protocol_version == version]
    del clone.supported_interfaces[:]
    clone.supported_interfaces.extend(keep)
    return clone


class A2AAdapter(ProtocolAdapter):
    """Adapter for A2A protocol using the official a2a-sdk 1.0 client."""

    # A2A task states in which the server is still expecting more from
    # the buyer on the same task (input-required, auth-required, and
    # in-flight states). While the adapter holds a task_id in one of
    # these states, the next outbound Message must echo it back so the
    # server resumes the same task rather than orphaning it and starting
    # a new one. Everything else — completed/failed/canceled/rejected
    # (terminal) and the defensive unknown state — clears the retained
    # task_id so subsequent calls start a fresh task. The frozenset
    # holds protobuf enum int values so a rename upstream is a load-time
    # error, not a silent behavior change.
    _NONTERMINAL_TASK_STATES: frozenset[int] = frozenset(
        {
            pb.TaskState.TASK_STATE_SUBMITTED,
            pb.TaskState.TASK_STATE_WORKING,
            pb.TaskState.TASK_STATE_INPUT_REQUIRED,
            pb.TaskState.TASK_STATE_AUTH_REQUIRED,
        }
    )

    def __init__(
        self,
        agent_config: AgentConfig,
        force_a2a_version: str | None = None,
    ):
        """Initialize A2A adapter with official A2A client.

        ``force_a2a_version`` pins the A2A wire version by filtering the
        peer's advertised ``supported_interfaces`` to only entries whose
        ``protocol_version`` matches. Intended for tests or for forcing
        a 0.3-speaking path against a dual-advertising peer. Raises
        :class:`ADCPConnectionError` on first use if no advertised
        interface matches. ``None`` lets the SDK's ``ClientFactory``
        pick the most capable transport the peer supports.
        """
        super().__init__(agent_config)
        self._httpx_client: httpx.AsyncClient | None = None
        self._a2a_client: Client | None = None
        self._cached_agent_card: pb.AgentCard | None = None
        self._force_a2a_version = force_a2a_version
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

    @property
    def a2a_protocol_versions(self) -> list[str] | None:
        """Sorted list of A2A ``protocol_version`` strings the peer advertises.

        Populated after the first call (or any operation that fetches
        the ``AgentCard`` — :meth:`list_tools`, :meth:`get_agent_info`,
        or an ``_call_a2a_tool`` invocation). Returns ``None`` before
        the card has been fetched so callers can distinguish "not yet
        known" from "peer advertises nothing" (empty list).

        Example::

            client = ADCPClient(a2a_config)
            await client.adapter.get_agent_info()
            print(client.a2a_protocol_versions)  # ['0.3', '1.0']
        """
        if self._cached_agent_card is None:
            return None
        return sorted(
            {iface.protocol_version for iface in self._cached_agent_card.supported_interfaces}
        )

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

            if self.agent_config.extra_headers:
                headers.update(self.agent_config.extra_headers)

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

    async def _get_a2a_client(self) -> Client:
        """Get or create the A2A client.

        Uses :class:`~a2a.client.ClientFactory` to build a transport-negotiated
        :class:`~a2a.client.Client` against the resolved
        :class:`~a2a.types.AgentCard`. The shared ``httpx.AsyncClient`` is
        passed into the :class:`~a2a.client.ClientConfig` so the signing
        request hook and connection pool are reused across every outbound
        send.
        """
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

            # Build a non-streaming client that reuses our httpx pool.
            # Streaming is disabled: the ADCP adapter surface is one
            # request in, one task out — streaming would require an
            # async iterator API that does not match the SDK contract.
            self._cached_agent_card = agent_card
            client_card = agent_card
            if self._force_a2a_version is not None:
                # Filter the advertised interfaces to the pinned version
                # before handing the card to ClientFactory; the factory
                # picks a transport from whatever remains. Raising here
                # is nicer than a cryptic "no transport available" deep
                # in the SDK.
                client_card = _filter_card_to_version(agent_card, self._force_a2a_version)
                if not client_card.supported_interfaces:
                    raise ADCPConnectionError(
                        f"Peer does not advertise A2A protocol_version="
                        f"{self._force_a2a_version!r}; advertised versions: "
                        f"{sorted({i.protocol_version for i in agent_card.supported_interfaces})}",
                        agent_id=self.agent_config.id,
                        agent_uri=self.agent_config.agent_uri,
                    )
            factory = ClientFactory(ClientConfig(httpx_client=httpx_client, streaming=False))
            self._a2a_client = factory.create(client_card)
            logger.debug(f"Created A2A client for agent {self.agent_config.id}")

        return self._a2a_client

    async def _send_and_aggregate(
        self, client: Client, request: pb.SendMessageRequest
    ) -> pb.StreamResponse:
        """Send a non-streaming request and return the terminal StreamResponse.

        The 1.0 :meth:`~a2a.client.Client.send_message` is an async
        generator that yields :class:`StreamResponse` events — with
        ``streaming=False`` it yields a single event carrying the final
        task. Pulls that event out so the ADCP adapter can stay
        request/response. Raises :class:`RuntimeError` if the generator
        yields nothing (should not happen: the SDK raises before
        yielding zero events).
        """
        last: pb.StreamResponse | None = None
        stream = client.send_message(request)
        async for event in stream:
            last = event
        if last is None:
            raise RuntimeError("A2A client yielded no response events")
        return last

    async def close(self) -> None:
        """Close the HTTP client and clean up resources."""
        if self._httpx_client is not None:
            logger.debug(f"Closing A2A adapter client for agent {self.agent_config.id}")
            # Close the A2A client first so it can drain any transport
            # state (grpc channel, streaming iterator) before we tear
            # down the shared httpx pool underneath it.
            if self._a2a_client is not None:
                try:
                    await self._a2a_client.close()
                except Exception:  # noqa: BLE001
                    logger.debug("A2A client close raised; ignoring", exc_info=True)
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
        # Apply per-instance envelope enrichment (e.g. adcp_version pin).
        params = self._enrich_outgoing_params(params)

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
            # Explicit skill invocation (deterministic): a single DataPart
            # carrying ``{"skill": tool_name, "parameters": params}``.
            parts = [_make_data_part({"skill": tool_name, "parameters": params})]
        else:
            # Natural language invocation (flexible): agent interprets
            # intent from text.
            parts = [_make_text_part(self._format_tool_request(tool_name, params))]

        message = pb.Message(
            message_id=message_id,
            role=pb.Role.ROLE_USER,
            parts=parts,
            context_id=self._context_id or "",
            task_id=self._active_task_id or "",
        )

        request = pb.SendMessageRequest(message=message)

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
            # Non-streaming send returns a single StreamResponse envelope.
            stream_event = await self._send_and_aggregate(a2a_client, request)

            payload_kind = stream_event.WhichOneof("payload")
            if payload_kind == "task":
                result_task = stream_event.task

                if self.agent_config.debug and start_time:
                    duration_ms = (time.time() - start_time) * 1000
                    debug_info = DebugInfo(
                        request=debug_request,
                        response=_idempotency.deep_redact(
                            {"result": _task_to_redacted_dict(result_task)}
                        ),
                        duration_ms=duration_ms,
                    )

                # Compute next-turn state from the response but do NOT
                # commit yet — _process_task_response and the idempotency
                # check below can raise, and leaving the adapter advanced
                # after an exception would orphan the legitimate in-flight
                # task on the next retry. Commit only after both succeed.
                next_context_id = result_task.context_id or None
                if result_task.status.state in self._NONTERMINAL_TASK_STATES:
                    next_active_task_id: str | None = result_task.id
                else:
                    # Terminal states (completed/failed/canceled/rejected)
                    # clear the retained task_id — subsequent calls start
                    # a new task under the same context. The defensive
                    # unspecified state falls here too; warn so operators
                    # notice if a server starts emitting it.
                    next_active_task_id = None
                    if result_task.status.state == pb.TaskState.TASK_STATE_UNSPECIFIED:
                        logger.warning(
                            "A2A agent %s returned TASK_STATE_UNSPECIFIED for "
                            "task_id=%s; clearing active_task_id and "
                            "starting a fresh task on next call",
                            self.agent_config.id,
                            result_task.id,
                        )

                task_result = self._process_task_response(result_task, debug_info)
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

            if payload_kind == "message":
                # Message response (shouldn't happen for send_message with
                # skill invocation, but surface a graceful fallback).
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
                error=f"Invalid response from A2A client (payload={payload_kind!r})",
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

    def _process_task_response(
        self, task: pb.Task, debug_info: DebugInfo | None
    ) -> TaskResult[Any]:
        """Process a Task response from A2A into our TaskResult format."""
        task_state = task.status.state

        if task_state == pb.TaskState.TASK_STATE_COMPLETED:
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
        elif task_state == pb.TaskState.TASK_STATE_FAILED:
            # Per transport-errors.mdx §A2A Binding: a failed task carries
            # an ``adcp_error`` DataPart alongside the human-readable
            # TextPart. The structured envelope lands on
            # ``TaskResult.adcp_error`` for programmatic branching; the
            # text stays on ``error`` for humans. When the seller omits
            # the TextPart, fall back to the structured envelope's
            # ``message`` / ``code`` so adopters don't see the
            # ``"Task failed"`` placeholder mask a real diagnostic.
            text_msg = self._extract_text_from_task(task)
            adcp_error = self._extract_adcp_error_from_task(task)
            if text_msg:
                error_msg: str | None = text_msg
            elif adcp_error:
                error_msg = adcp_error.get("message") or adcp_error.get("code")
            else:
                error_msg = "Task failed"
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=error_msg,
                adcp_error=adcp_error,
                success=False,
                debug_info=debug_info,
            )
        else:
            # Handle all interim states (submitted, working, input-required, etc.).
            # Metadata ``status`` stays in the 0.3-style lowercase spec form
            # (``working``, ``input-required``) so downstream consumers don't
            # need to learn the TaskState_ prefix.
            state_name = pb.TaskState.Name(task_state)
            if state_name.startswith("TASK_STATE_"):
                status_str = state_name[len("TASK_STATE_") :].lower().replace("_", "-")
            else:
                status_str = state_name.lower()
            return TaskResult[Any](
                status=TaskStatus.SUBMITTED,
                data=None,  # Interim responses may not have structured AdCP content
                message=self._extract_text_from_task(task),
                success=True,
                metadata={
                    "task_id": task.id,
                    "context_id": task.context_id,
                    "status": status_str,
                },
                debug_info=debug_info,
            )

    def _format_tool_request(self, tool_name: str, params: dict[str, Any]) -> str:
        """Format tool request as natural language for A2A."""
        import json

        return f"Execute tool: {tool_name}\nParameters: {json.dumps(params, indent=2)}"

    def _extract_result_from_task(self, task: pb.Task) -> Any:
        """
        Extract result data from A2A Task following canonical format.

        Per A2A response spec:
        - Responses MUST include at least one DataPart (``data`` oneof)
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

        data_parts = [
            d for d in (_part_data_dict(p) for p in target_artifact.parts) if d is not None
        ]

        if not data_parts:
            logger.warning("A2A Task missing required DataPart (data oneof)")
            return {}

        # Use last DataPart as authoritative (handles streaming scenarios within an artifact)
        data = data_parts[-1]

        # Some A2A implementations (e.g., ADK) wrap the response in {"response": {...}}
        # Unwrap it to get the actual AdCP payload if present
        if isinstance(data, dict) and "response" in data:
            return data["response"]

        return data

    def _extract_adcp_error_from_task(self, task: pb.Task) -> dict[str, Any] | None:
        """Extract a spec-shaped ``adcp_error`` DataPart from a failed task.

        Per transport-errors.mdx §A2A Binding the failed task's artifact
        carries a DataPart wrapping ``{"adcp_error": {...}}``. Returns the
        validated envelope or ``None`` if no spec-shaped payload is present
        (spec-permitted: graceful sellers MAY omit the structured envelope,
        in which case adopters fall back to ``TaskResult.error``).
        """
        data = self._extract_result_from_task(task)
        if not isinstance(data, dict):
            return None
        return validate_adcp_error(data.get("adcp_error"))

    def _extract_text_from_task(self, task: pb.Task) -> str | None:
        """Extract human-readable message from TextPart if present."""
        if not task.artifacts:
            return None

        # Use last artifact (most recent in streaming scenarios)
        target_artifact = task.artifacts[-1]

        for part in target_artifact.parts:
            text = _part_text(part)
            if text is not None:
                return text

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

    async def list_transformers(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List creative transformers."""
        return await self._call_a2a_tool("list_transformers", params)

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
        # Ensure the A2A client (and cached agent card) is initialized.
        await self._get_a2a_client()

        if self._cached_agent_card is None:
            raise RuntimeError("Agent card cache was not populated by _get_a2a_client")
        agent_card: pb.AgentCard = self._cached_agent_card

        tool_names = [skill.name for skill in agent_card.skills if skill.name]
        logger.info(f"Found {len(tool_names)} tools from A2A agent {self.agent_config.id}")
        return tool_names

    async def get_agent_info(self) -> dict[str, Any]:
        """
        Get agent information including AdCP extension metadata from A2A agent card.

        Fetches the agent card via :class:`~a2a.client.A2ACardResolver` and
        extracts:

        - Basic agent info (name, description, version)
        - AdCP extension (extensions.adcp.adcp_version, extensions.adcp.protocols_supported)
        - Available skills/tools

        Returns:
            Dictionary with agent metadata
        """
        await self._get_a2a_client()

        logger.debug(f"Fetching A2A agent info for {self.agent_config.id}")

        if self._cached_agent_card is None:
            raise RuntimeError("Agent card cache was not populated by _get_a2a_client")
        agent_card: pb.AgentCard = self._cached_agent_card

        info: dict[str, Any] = {
            "name": agent_card.name,
            "description": agent_card.description,
            "version": agent_card.version,
            "protocol": "a2a",
            # A2A wire versions the peer advertises. Our server emits
            # both "0.3" and "1.0" so clients of either era interoperate;
            # this field lets buyers confirm what a given peer speaks.
            "a2a_protocol_versions": sorted(
                {iface.protocol_version for iface in agent_card.supported_interfaces}
            ),
        }

        tool_names = [skill.name for skill in agent_card.skills if skill.name]
        if tool_names:
            info["tools"] = tool_names

        # The 1.0 proto :class:`AgentCard` has no ``extensions`` map.
        # Sellers advertising AdCP capabilities must surface them via
        # ``skills`` entries or a follow-up
        # ``get_adcp_capabilities`` call rather than an out-of-band
        # extensions dict (which the 0.3 Pydantic card accepted).

        logger.info(f"Retrieved agent info for {self.agent_config.id}")
        return info

    # ========================================================================
    # V3 Protocol Methods - Protocol Discovery
    # ========================================================================

    async def get_adcp_capabilities(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get AdCP capabilities from the agent."""
        return await self._call_a2a_tool("get_adcp_capabilities", params)

    async def get_task_status(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get task status from the agent."""
        return await self._call_a2a_tool("get_task_status", params)

    async def list_tasks(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List tasks from the agent."""
        return await self._call_a2a_tool("list_tasks", params)

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

    async def validate_input(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Validate creative input."""
        return await self._call_a2a_tool("validate_input", params)

    async def verify_brand_claim(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Verify a brand claim."""
        return await self._call_a2a_tool("verify_brand_claim", params)

    async def verify_brand_claims(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Verify brand claims."""
        return await self._call_a2a_tool("verify_brand_claims", params)

    # ========================================================================
    # V3 Protocol Methods - Compliance
    # ========================================================================

    async def comply_test_controller(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Compliance test controller (sandbox only)."""
        return await self._call_a2a_tool("comply_test_controller", params)
