from __future__ import annotations

"""MCP protocol adapter using official Python MCP SDK."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

# ExceptionGroup and BaseExceptionGroup are available in Python 3.11+
# In 3.11+, they're built-in types. For 3.10, we need to handle their absence.
try:
    _ExceptionGroup: type[BaseException] | None = ExceptionGroup  # type: ignore[name-defined]
    _BaseExceptionGroup: type[BaseException] | None = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:
    # Python 3.10 - ExceptionGroup doesn't exist
    _ExceptionGroup = None
    _BaseExceptionGroup = None

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp import ClientSession
    from mcp.types import RequestParamsMeta

try:
    import anyio
    import httpx2 as _mcp_httpx
    from mcp import ClientSession as _ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import (
        MCP_SESSION_ID,
        StreamableHTTPTransport,
    )
    from mcp.shared._compat import resync_tracer
    from mcp.shared._context_streams import create_context_streams
    from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT
    from mcp.shared.message import SessionMessage

    MCP_AVAILABLE = True
    _MCP_HTTP_STATUS_ERROR_TYPES: tuple[type[BaseException], ...] = (_mcp_httpx.HTTPStatusError,)
except ImportError:
    MCP_AVAILABLE = False
    _MCP_HTTP_STATUS_ERROR_TYPES = ()

try:
    import httpx as _httpx
    from httpx import HTTPStatusError

    HTTPX_AVAILABLE = True
    _HTTP_STATUS_ERROR_TYPES: tuple[type[BaseException], ...] = (HTTPStatusError,)
except ImportError:
    HTTPX_AVAILABLE = False
    _HTTP_STATUS_ERROR_TYPES = ()
    _httpx = None  # type: ignore[assignment]

_ALL_HTTP_STATUS_ERROR_TYPES: tuple[type[BaseException], ...] = (
    *_HTTP_STATUS_ERROR_TYPES,
    *_MCP_HTTP_STATUS_ERROR_TYPES,
)

import json

from adcp import _idempotency
from adcp.exceptions import (
    ADCPConnectionError,
    ADCPTimeoutError,
    IdempotencyConflictError,
    IdempotencyExpiredError,
)
from adcp.observability import mcp_trace_meta
from adcp.protocols._adcp_errors import validate_adcp_error as _validate_adcp_error
from adcp.protocols.base import ProtocolAdapter
from adcp.signing.autosign import current_operation as _signing_operation
from adcp.task_options import mark_task_dispatched
from adcp.types.core import DebugInfo, TaskResult, TaskStatus
from adcp.validation.client_hooks import (
    validate_incoming_response,
    validate_outgoing_request,
)
from adcp.validation.schema_validator import SchemaValidationError, format_issues

# Spec-defined limits from docs/building/implementation/mcp-response-extraction.mdx
# and docs/building/implementation/transport-errors.mdx.
_MAX_TEXT_SIZE_BYTES = 1_048_576  # 1MB cap on text items before JSON.parse

MCPHttpxClientFactory = Callable[..., Any]
"""Factory returning an MCP-compatible async HTTP client.

The callable must accept ``headers=``, ``timeout=``, ``auth=``,
``follow_redirects=``, and ``trust_env=`` keyword arguments. The returned
client must expose ``follow_redirects``, ``trust_env``, and ``event_hooks``
like :class:`httpx2.AsyncClient` so the adapter can enforce its transport
security and compose RFC 9421 signing hooks.
"""


def _make_hardened_mcp_http_factory(
    request_hooks: Sequence[Callable[[Any], Awaitable[None]]] = (),
) -> Callable[..., Any]:
    """Build an MCP HTTP client factory with fail-closed network defaults."""

    def factory(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
        **extra: Any,
    ) -> Any:
        kwargs: dict[str, Any] = {
            **extra,
            # MCP adds session and protocol headers after client construction,
            # so constructor-time headers are not a reliable sensitivity test.
            # Redirects also require a fresh SSRF decision for every target.
            "follow_redirects": False,
            "trust_env": False,
        }
        if request_hooks:
            kwargs["event_hooks"] = {"request": list(request_hooks)}
        kwargs["timeout"] = _coerce_mcp_timeout(timeout)
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return _mcp_httpx.AsyncClient(**kwargs)

    return factory


def _coerce_mcp_timeout(timeout: Any) -> Any:
    if timeout is None:
        return _mcp_httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
    if isinstance(timeout, _mcp_httpx.Timeout):
        return timeout
    connect = getattr(timeout, "connect", None)
    read = getattr(timeout, "read", None)
    write = getattr(timeout, "write", None)
    pool = getattr(timeout, "pool", None)
    if all(value is not None for value in (connect, read, write, pool)):
        return _mcp_httpx.Timeout(connect=connect, read=read, write=write, pool=pool)
    return timeout


def _make_signing_http_factory(
    hook: Callable[[Any], Awaitable[None]],
) -> Callable[..., Any]:
    """Build an MCP HTTP client factory that installs a signing request hook.

    MCP SDK v2 uses ``httpx2`` internally, but the signing hook only relies
    on the request's method, URL, headers, and body attributes shared with
    ``httpx``. Redirects stay disabled because an RFC 9421 signature binds
    the original ``@authority``.
    """

    return _make_hardened_mcp_http_factory((hook,))


def _make_custom_mcp_http_factory(
    custom_factory: MCPHttpxClientFactory,
    request_hooks: Sequence[Callable[[Any], Awaitable[None]]] = (),
) -> MCPHttpxClientFactory:
    """Wrap an adopter factory with mandatory MCP transport invariants."""

    def factory(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
        **extra: Any,
    ) -> Any:
        kwargs: dict[str, Any] = {
            **extra,
            "follow_redirects": False,
            "trust_env": False,
            "timeout": _coerce_mcp_timeout(timeout),
        }
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        client = custom_factory(**kwargs)

        if getattr(client, "trust_env", None) is not False:
            raise ValueError("httpx_client_factory must return a client with trust_env=False")
        if getattr(client, "follow_redirects", None) is not False:
            raise ValueError(
                "httpx_client_factory must return a client with follow_redirects=False"
            )
        if not callable(getattr(client, "sse", None)):
            raise TypeError(
                "httpx_client_factory must return an MCP-compatible client exposing sse(); "
                "MCP SDK v2 uses httpx2, so a plain httpx.AsyncClient is not compatible"
            )

        if request_hooks:
            event_hooks = getattr(client, "event_hooks", None)
            if not isinstance(event_hooks, dict):
                raise TypeError(
                    "httpx_client_factory must return a client exposing an event_hooks dict"
                )
            installed_hooks = event_hooks.setdefault("request", [])
            for hook in request_hooks:
                if hook not in installed_hooks:
                    installed_hooks.append(hook)
        return client

    return factory


@asynccontextmanager
async def streamablehttp_client(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: Any = None,
    httpx_client_factory: Callable[..., Any] | None = None,
    auth: Any = None,
    terminate_on_close: bool = True,
) -> Any:
    """Compatibility wrapper matching the MCP SDK v1 streamable client shape.

    MCP SDK v2's convenience client accepts a pre-built ``httpx2`` client and
    yields only ``(read, write)``. ADCP exposes the current MCP session id, so
    this mirrors the v2 transport setup while returning ``get_session_id`` as
    the third tuple item used by older ADCP code.
    """

    if httpx_client_factory is None:
        httpx_client_factory = _make_hardened_mcp_http_factory()

    transport = StreamableHTTPTransport(url)
    client = httpx_client_factory(headers=headers, timeout=timeout, auth=auth)

    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(client)

        read_stream_writer, read_stream = create_context_streams[SessionMessage | Exception](0)
        write_stream, write_stream_reader = create_context_streams[SessionMessage](0)

        async with (
            read_stream_writer,
            read_stream,
            write_stream,
            write_stream_reader,
            anyio.create_task_group() as tg,
        ):

            def start_get_stream() -> None:
                tg.start_soon(transport.handle_get_stream, client, read_stream_writer)

            tg.start_soon(
                transport.post_writer,
                client,
                write_stream_reader,
                read_stream_writer,
                write_stream,
                start_get_stream,
                tg,
            )

            try:
                yield read_stream, write_stream, lambda: transport.session_id
            finally:
                if transport.session_id and terminate_on_close:
                    await transport.terminate_session(client)
                tg.cancel_scope.cancel()
        await resync_tracer()


def _text_of(item: Any) -> str | None:
    """Return the text payload of an MCP content item, or None if not a text item."""
    if isinstance(item, dict):
        if item.get("type") != "text":
            return None
        text = item.get("text")
    else:
        if getattr(item, "type", None) != "text":
            return None
        text = getattr(item, "text", None)
    return text if isinstance(text, str) and text else None


def _result_is_error(result: Any) -> bool:
    return bool(getattr(result, "isError", getattr(result, "is_error", False)))


def _result_structured_content(result: Any) -> Any:
    return getattr(result, "structuredContent", getattr(result, "structured_content", None))


def extract_adcp_success(result: Any) -> dict[str, Any] | None:
    """Extract AdCP success response data from an MCP tool result.

    Implements the normative algorithm from AdCP spec §MCP Response Extraction
    (docs/building/implementation/mcp-response-extraction.mdx):

    1. If ``isError`` is truthy, return ``None`` — error extraction is a
       separate path.
    2. ``structuredContent`` — if present and a non-array object that is NOT
       an ``adcp_error``-only payload, return it.
    3. Text fallback — iterate ``content[]`` in order; for each ``type='text'``
       item within the 1MB size limit, ``json.loads`` and return the result
       if it is a non-array object that is NOT ``adcp_error``-only.
    4. No structured data found — return ``None``.
    """
    if _result_is_error(result):
        return None

    sc = _result_structured_content(result)
    if isinstance(sc, dict) and not (len(sc) == 1 and "adcp_error" in sc):
        return sc

    for item in getattr(result, "content", None) or []:
        text = _text_of(item)
        if text is None or len(text) > _MAX_TEXT_SIZE_BYTES:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and not (len(parsed) == 1 and "adcp_error" in parsed):
            return parsed
    return None


def extract_adcp_error(result: Any) -> dict[str, Any] | None:
    """Extract and validate an AdCP ``adcp_error`` object from an MCP result.

    Implements AdCP spec §Client Detection Order (MCP paths 1 + 5) from
    docs/building/implementation/transport-errors.mdx. Only applies when
    ``isError`` is truthy. Returns a validated error object or ``None``.
    """
    if not _result_is_error(result):
        return None

    sc = _result_structured_content(result)
    if isinstance(sc, dict):
        validated = _validate_adcp_error(sc.get("adcp_error"))
        if validated is not None:
            return validated

    for item in getattr(result, "content", None) or []:
        text = _text_of(item)
        # Apply the same 1MB pre-parse cap as the success path to prevent a
        # malicious server returning ``isError=true`` plus a giant payload from
        # forcing a multi-MB json.loads into memory before the 4KB validation
        # would reject it.
        if text is None or len(text) > _MAX_TEXT_SIZE_BYTES:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            validated = _validate_adcp_error(parsed.get("adcp_error"))
            if validated is not None:
                return validated
    return None


class MCPAdapter(ProtocolAdapter):
    """Adapter for MCP protocol using official Python MCP SDK."""

    def __init__(
        self,
        *args: Any,
        httpx_client_factory: MCPHttpxClientFactory | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        if not MCP_AVAILABLE:
            raise ImportError(
                "MCP SDK not installed. Install with: pip install mcp (requires Python 3.10+)"
            )
        self._session: Any = None
        self._exit_stack: Any = None
        self._connected_url: str | None = None
        self._get_session_id: Callable[[], str | None] | None = None
        self._httpx_client_factory = httpx_client_factory
        # True when the session was injected by ADCPClient.from_mcp_client().
        # Caller owns the lifecycle — close() is a no-op on injected adapters.
        self._session_is_injected: bool = False

    def _inject_session(self, session: ClientSession) -> None:
        """Pre-wire a caller-owned session, bypassing URL-based connection.

        Used by ADCPClient.from_mcp_client(). Once injected, _get_session()
        returns it immediately and close() is a no-op (caller owns lifecycle).
        """
        self._session = session
        self._session_is_injected = True

    def _http_headers(self) -> dict[str, str]:
        """Return transport headers for MCP HTTP requests."""
        headers: dict[str, str] = {}
        if self.agent_config.auth_token:
            # Support custom auth headers and types
            if self.agent_config.auth_type == "bearer":
                headers[self.agent_config.auth_header] = f"Bearer {self.agent_config.auth_token}"
            else:
                headers[self.agent_config.auth_header] = self.agent_config.auth_token

        if self.agent_config.extra_headers:
            headers.update(self.agent_config.extra_headers)
        return headers

    def _urls_to_try(self) -> list[str]:
        """Return MCP endpoint candidates, preserving the configured URL first."""
        uri = self.agent_config.agent_uri
        base = uri.rstrip("/")
        urls_to_try = [uri]
        if base.endswith("/mcp"):
            # User pointed at the MCP endpoint; also try the other slash form.
            urls_to_try.append(f"{base}/" if not uri.endswith("/") else base)
        else:
            urls_to_try.extend([f"{base}/mcp", f"{base}/mcp/"])
        return urls_to_try

    def _streamable_http_client_factory(self) -> MCPHttpxClientFactory:
        """Return the HTTP client factory used for MCP HTTP requests."""
        request_hooks = tuple(
            hook
            for hook in (self.tracing_request_hook, self.signing_request_hook)
            if hook is not None
        )
        if self._httpx_client_factory is not None:
            return _make_custom_mcp_http_factory(
                self._httpx_client_factory,
                request_hooks,
            )
        return _make_hardened_mcp_http_factory(request_hooks)

    def current_mcp_session_id(self) -> str | None:
        """Return the current SDK-managed MCP Streamable HTTP session id."""
        return self._get_session_id() if self._get_session_id is not None else None

    async def _cleanup_failed_connection(self, context: str) -> None:
        """
        Clean up resources after a failed connection attempt.

        This method handles cleanup without raising exceptions to avoid
        masking the original connection error.

        Args:
            context: Description of the context for logging (e.g., "during connection attempt")
        """
        if self._exit_stack is not None:
            old_stack = self._exit_stack
            self._exit_stack = None
            self._session = None
            self._connected_url = None
            self._get_session_id = None
            try:
                await old_stack.aclose()
            except BaseException as cleanup_error:
                # Handle all cleanup errors including ExceptionGroup
                # Re-raise KeyboardInterrupt and SystemExit immediately
                if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
                    raise

                if isinstance(cleanup_error, asyncio.CancelledError):
                    logger.debug(f"MCP session cleanup cancelled {context}")
                    return

                # Handle ExceptionGroup/BaseExceptionGroup from task group failures (Python 3.11+)
                # ExceptionGroup: for Exception subclasses (e.g., HTTPStatusError)
                # BaseExceptionGroup: for BaseException subclasses (e.g., CancelledError)
                # We need both because CancelledError is a BaseException, not an Exception
                is_exception_group = (
                    _ExceptionGroup is not None and isinstance(cleanup_error, _ExceptionGroup)
                ) or (
                    _BaseExceptionGroup is not None
                    and isinstance(cleanup_error, _BaseExceptionGroup)
                )

                if is_exception_group:
                    # Check if all exceptions in the group are CancelledError
                    # If so, treat the entire group as a cancellation
                    all_cancelled = all(
                        isinstance(exc, asyncio.CancelledError)
                        for exc in cleanup_error.exceptions  # type: ignore[attr-defined]
                    )
                    if all_cancelled:
                        logger.debug(f"MCP session cleanup cancelled {context}")
                        return

                    # Mixed group: skip CancelledErrors and log real errors
                    exceptions = cleanup_error.exceptions  # type: ignore[attr-defined]
                    cancelled_errors = [
                        exc for exc in exceptions if isinstance(exc, asyncio.CancelledError)
                    ]
                    cancelled_count = len(cancelled_errors)
                    if cancelled_count > 0:
                        logger.debug(
                            f"Skipping {cancelled_count} CancelledError(s) "
                            f"in mixed exception group {context}"
                        )

                    # Log each non-cancelled exception individually
                    for exc in exceptions:
                        if not isinstance(exc, asyncio.CancelledError):
                            self._log_cleanup_error(exc, context)
                else:
                    self._log_cleanup_error(cleanup_error, context)

    def _log_cleanup_error(self, exc: BaseException, context: str) -> None:
        """Log a cleanup error without raising."""
        # Check for known cleanup error patterns from httpx/anyio
        exc_str = str(exc).lower()

        # Common cleanup errors that are expected when connection fails
        is_known_cleanup_error = (
            isinstance(exc, RuntimeError)
            and ("cancel scope" in exc_str or "async context" in exc_str)
        ) or (
            # HTTP errors during cleanup (if httpx is available)
            HTTPX_AVAILABLE
            and isinstance(exc, _ALL_HTTP_STATUS_ERROR_TYPES)
        )

        if is_known_cleanup_error:
            # Expected cleanup errors - log at debug level without stack trace
            logger.debug(f"Ignoring expected cleanup error {context}: {exc}")
        else:
            # Truly unexpected cleanup errors - log at warning with full context
            logger.warning(f"Unexpected error during cleanup {context}: {exc}", exc_info=True)

    async def _get_session(self) -> ClientSession:
        """
        Get or create MCP client session with URL fallback handling.

        Raises:
            ADCPConnectionError: If connection to agent fails
        """
        if self._session is not None:
            return self._session  # type: ignore[no-any-return]

        logger.debug(f"Creating MCP session for agent {self.agent_config.id}")

        # Parse the agent URI to determine transport type
        parsed = urlparse(self.agent_config.agent_uri)

        # Use SSE transport for HTTP/HTTPS endpoints
        if parsed.scheme in ("http", "https"):
            self._exit_stack = AsyncExitStack()

            # Try the user's exact URL first, then the alternate slash form, then
            # /mcp discovery paths. MCP servers disagree on whether their endpoint
            # is at /mcp or /mcp/ — try both rather than silently normalizing.
            headers = self._http_headers()
            urls_to_try = self._urls_to_try()

            # RFC 9421 auto-signing: if ADCPClient installed a signing request
            # hook, wire it into streamable_http via a custom httpx client
            # factory. SSE transport has no equivalent signing knob — warn the
            # user and fall through to unsigned SSE. Both HTTP transports use
            # SDK-owned factories with trust_env=False so auth headers are not
            # sent through ambient proxy settings.
            streamable_http_extra: dict[str, Any] = {
                "httpx_client_factory": self._streamable_http_client_factory()
            }
            if (
                self.signing_request_hook is not None
                and self.agent_config.mcp_transport != "streamable_http"
            ):
                logger.warning(
                    "RFC 9421 auto-signing is not supported on MCP SSE "
                    "transport for agent %s; use mcp_transport='streamable_http' "
                    "to sign outgoing requests.",
                    self.agent_config.id,
                )

            last_error = None
            for url in urls_to_try:
                try:
                    get_session_id: Callable[[], str | None] | None = None
                    # Choose transport based on configuration
                    if self.agent_config.mcp_transport == "streamable_http":
                        # Use streamable HTTP transport (newer, bidirectional)
                        read, write, get_session_id = await self._exit_stack.enter_async_context(
                            streamablehttp_client(
                                url,
                                headers=headers,
                                timeout=self.agent_config.timeout,
                                **streamable_http_extra,
                            )
                        )
                    else:
                        sse_request_hooks = (
                            (self.tracing_request_hook,)
                            if self.tracing_request_hook is not None
                            else ()
                        )
                        # Use SSE transport (legacy, but widely supported)
                        sse_http_factory = (
                            _make_custom_mcp_http_factory(
                                self._httpx_client_factory,
                                sse_request_hooks,
                            )
                            if self._httpx_client_factory is not None
                            else _make_hardened_mcp_http_factory(sse_request_hooks)
                        )
                        read, write = await self._exit_stack.enter_async_context(
                            sse_client(
                                url,
                                headers=headers,
                                httpx_client_factory=sse_http_factory,
                            )
                        )

                    self._session = await self._exit_stack.enter_async_context(
                        _ClientSession(read, write)
                    )

                    # Initialize the session
                    await self._session.initialize()
                    self._connected_url = url
                    if self.agent_config.mcp_transport == "streamable_http":
                        self._get_session_id = get_session_id

                    logger.info(
                        f"Connected to MCP agent {self.agent_config.id} at {url} "
                        f"using {self.agent_config.mcp_transport} transport"
                    )
                    if url != self.agent_config.agent_uri:
                        logger.info(
                            f"Note: Connected using fallback URL {url} "
                            f"(configured: {self.agent_config.agent_uri})"
                        )

                    return self._session  # type: ignore[no-any-return]
                except BaseException as e:
                    # Catch BaseException to handle CancelledError from failed initialization
                    # Re-raise KeyboardInterrupt and SystemExit immediately
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
                    last_error = e
                    # Clean up the exit stack on failure to avoid resource leaks
                    await self._cleanup_failed_connection("during connection attempt")

                    # A task-level deadline and caller cancellation both use
                    # CancelledError to abort discovery. Never reinterpret it
                    # as a failed URL probe or continue to a fallback URL.
                    if isinstance(e, asyncio.CancelledError):
                        raise

                    # If this isn't the last URL to try, create a new exit stack and continue
                    if url != urls_to_try[-1]:
                        logger.debug(f"Retrying with next URL after error: {last_error}")
                        self._exit_stack = AsyncExitStack()
                        continue
                    # If this was the last URL, raise the error
                    logger.error(
                        f"Failed to connect to MCP agent {self.agent_config.id} using "
                        f"{self.agent_config.mcp_transport} transport. "
                        f"Tried URLs: {', '.join(urls_to_try)}"
                    )

                    # Classify error type for better exception handling
                    error_str = str(last_error).lower()
                    if "401" in error_str or "403" in error_str or "unauthorized" in error_str:
                        from adcp.exceptions import ADCPAuthenticationError

                        raise ADCPAuthenticationError(
                            f"Authentication failed: {last_error}",
                            agent_id=self.agent_config.id,
                            agent_uri=self.agent_config.agent_uri,
                        ) from last_error
                    elif "timeout" in error_str:
                        raise ADCPTimeoutError(
                            f"Connection timeout: {last_error}",
                            agent_id=self.agent_config.id,
                            agent_uri=self.agent_config.agent_uri,
                            timeout=self.agent_config.timeout,
                        ) from last_error
                    else:
                        raise ADCPConnectionError(
                            f"Failed to connect: {last_error}",
                            agent_id=self.agent_config.id,
                            agent_uri=self.agent_config.agent_uri,
                        ) from last_error

            # This shouldn't be reached, but just in case
            raise RuntimeError(f"Failed to connect to MCP agent at {self.agent_config.agent_uri}")
        else:
            raise ValueError(f"Unsupported transport scheme: {parsed.scheme}")

    def _serialize_mcp_content(self, content: list[Any]) -> list[dict[str, Any]]:
        """
        Convert MCP SDK content objects to plain dicts.

        The MCP SDK returns Pydantic objects (TextContent, ImageContent, etc.)
        but the rest of the ADCP client expects protocol-agnostic dicts.
        This method handles the translation at the protocol boundary.

        Args:
            content: List of MCP content items (may be dicts or Pydantic objects)

        Returns:
            List of plain dicts representing the content
        """
        result = []
        for item in content:
            # Already a dict, pass through
            if isinstance(item, dict):
                result.append(item)
            # Pydantic v2 model with model_dump()
            elif hasattr(item, "model_dump"):
                result.append(item.model_dump())
            # Pydantic v1 model with dict()
            elif hasattr(item, "dict") and callable(item.dict):
                result.append(item.dict())
            # Fallback: try to access __dict__
            elif hasattr(item, "__dict__"):
                result.append(dict(item.__dict__))
            # Last resort: serialize as unknown type
            else:
                logger.warning(f"Unknown MCP content type: {type(item)}, serializing as string")
                result.append({"type": "unknown", "data": str(item)})
        return result

    async def _call_mcp_tool(self, tool_name: str, params: dict[str, Any]) -> TaskResult[Any]:
        """Call a tool using MCP protocol."""
        start_time = time.time() if self.agent_config.debug else None
        debug_info = None
        debug_request: dict[str, Any] = {}
        if _idempotency.is_mutating(tool_name) and self.idempotency_capability_check:
            await self.idempotency_capability_check()
        params, idempotency_key = _idempotency.inject_key(
            tool_name, params, client_token=self.idempotency_client_token
        )
        # Apply per-instance envelope enrichment (e.g. adcp_version pin).
        # Runs after idempotency injection so the enriched dict is the
        # one that's validated and sent.
        params = self._enrich_outgoing_params(params)

        try:
            # Pre-send schema validation — throws in strict, logs in warn,
            # skips in off. Runs before session setup so a drifted payload
            # doesn't even open a connection.
            try:
                validate_outgoing_request(tool_name, params, self.request_validation_mode)
            except SchemaValidationError as exc:
                return TaskResult[Any](
                    status=TaskStatus.FAILED,
                    error=str(exc),
                    success=False,
                    idempotency_key=idempotency_key,
                )

            # Streamable HTTP sends from a long-lived writer task whose
            # ContextVar snapshot predates this call. Fetch signing policy in
            # the caller task before enqueueing the message; the request hook
            # then reads the cache without recursively using this session.
            if tool_name != "get_adcp_capabilities" and self.signing_capability_check:
                await self.signing_capability_check()

            session = await self._get_session()

            if self.agent_config.debug:
                debug_request = {
                    "protocol": "MCP",
                    "tool": tool_name,
                    "params": _idempotency.redact_params(params),
                    "transport": self.agent_config.mcp_transport,
                }

            # Stamp the AdCP operation name so the httpx request event hook
            # installed by ADCPClient (when a SigningConfig is present) can
            # look up the seller's signing policy for this call. Scoped
            # tightly around call_tool so session.initialize() above and
            # other out-of-band traffic stay outside the signing scope.
            signing_token = _signing_operation.set(tool_name)
            try:
                # Call the tool using MCP client session
                mark_task_dispatched(
                    self.task_options_client_token,
                    tool_name,
                    mutating=_idempotency.is_mutating(tool_name),
                    idempotency_key=idempotency_key,
                )
                trace_meta = mcp_trace_meta()
                if trace_meta is None:
                    result = await session.call_tool(tool_name, params)
                else:
                    result = await session.call_tool(
                        tool_name,
                        params,
                        meta=cast("RequestParamsMeta", trace_meta),
                    )
            finally:
                _signing_operation.reset(signing_token)

            # Check if this is an error response
            is_error = _result_is_error(result)

            # Extract human-readable message from content
            message_text = None
            if hasattr(result, "content") and result.content:
                serialized_content = self._serialize_mcp_content(result.content)
                if isinstance(serialized_content, list):
                    for item in serialized_content:
                        is_text = isinstance(item, dict) and item.get("type") == "text"
                        if is_text and item.get("text"):
                            message_text = item["text"]
                            break

            # Handle error responses per transport-errors.mdx §Client Detection
            # Order. Extract the adcp_error object from structuredContent first,
            # then from text fallback — whichever is present.
            if is_error:
                adcp_error = extract_adcp_error(result)
                # Raise typed idempotency exceptions before building a generic
                # TaskResult(failed), so callers that catch them distinctly
                # don't lose the signal.
                if adcp_error and adcp_error.get("code") in (
                    "IDEMPOTENCY_CONFLICT",
                    "IDEMPOTENCY_EXPIRED",
                ):
                    from adcp.exceptions import classify_task_error

                    raise classify_task_error(
                        tool_name, [adcp_error], agent_id=self.agent_config.id
                    )
                # FastMCP-style is_error with plain-text content: text-match
                # fallback for the two idempotency codes.
                _idempotency.raise_for_idempotency_text(
                    tool_name, message_text, self.agent_config.id
                )
                error_message = (
                    (adcp_error.get("message") if adcp_error else None)
                    or message_text
                    or "Tool execution failed"
                )
                if self.agent_config.debug and start_time:
                    duration_ms = (time.time() - start_time) * 1000
                    debug_info = DebugInfo(
                        request=debug_request,
                        response={
                            "error": error_message,
                            "is_error": True,
                            "adcp_error": adcp_error,
                        },
                        duration_ms=duration_ms,
                    )
                return TaskResult[Any](
                    status=TaskStatus.FAILED,
                    error=error_message,
                    adcp_error=adcp_error,
                    success=False,
                    debug_info=debug_info,
                    idempotency_key=idempotency_key,
                )

            # Success extraction per mcp-response-extraction.mdx §Extraction
            # Algorithm: prefer structuredContent (MCP 2025-03-26+), fall back
            # to JSON-parsing content[].text for older servers (including the
            # AdCP reference training agent).
            data_to_return = extract_adcp_success(result)
            if data_to_return is None:
                raise ValueError(
                    f"MCP tool {tool_name} returned no structured AdCP data. "
                    f"Neither structuredContent nor content[].text yielded a "
                    f"parseable non-adcp_error JSON object. "
                    f"Got content: {result.content if hasattr(result, 'content') else 'none'}"
                )

            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response=_idempotency.deep_redact(
                        {
                            "data": data_to_return,
                            "message": message_text,
                            "is_error": False,
                        }
                    ),
                    duration_ms=duration_ms,
                )

            _idempotency.raise_for_idempotency_error(
                tool_name, data_to_return, self.agent_config.id
            )

            # Post-receive schema validation — catches field-name drift from
            # agents. Strict mode fails the task; warn mode logs and returns
            # the data unchanged; off short-circuits without invoking the
            # validator. Never raises — mirrors the existing contract where
            # response-side failures surface as TaskStatus.FAILED.
            response_outcome = validate_incoming_response(
                tool_name, data_to_return, self.response_validation_mode
            )
            if not response_outcome.valid and self.response_validation_mode == "strict":
                return TaskResult[Any](
                    status=TaskStatus.FAILED,
                    error=(
                        f"Schema validation failed for {tool_name}: "
                        f"{format_issues(response_outcome.issues)}"
                    ),
                    message=message_text,
                    success=False,
                    debug_info=debug_info,
                    idempotency_key=idempotency_key,
                )

            # Return both the structured data and the human-readable message
            task_result = TaskResult[Any](
                status=TaskStatus.COMPLETED,
                data=data_to_return,
                message=message_text,
                success=True,
                debug_info=debug_info,
            )
            return _idempotency.annotate_result(task_result, idempotency_key)

        except (IdempotencyConflictError, IdempotencyExpiredError):
            # Propagate typed idempotency errors — callers MUST handle these
            # distinctly (mint fresh key / reconcile state). Other ADCPError
            # subclasses (connection, timeout) continue to be converted to
            # TaskResult(failed) below, preserving the existing contract.
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

    # ========================================================================
    # ADCP Protocol Methods
    # ========================================================================

    async def get_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get advertising products."""
        return await self._call_mcp_tool("get_products", params)

    async def list_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        return await self._call_mcp_tool("list_products", params)

    async def request_proposals(self, params: dict[str, Any]) -> TaskResult[Any]:
        return await self._call_mcp_tool("request_proposals", params)

    async def refine_proposals(self, params: dict[str, Any]) -> TaskResult[Any]:
        return await self._call_mcp_tool("refine_proposals", params)

    async def decline_proposals(self, params: dict[str, Any]) -> TaskResult[Any]:
        return await self._call_mcp_tool("decline_proposals", params)

    async def buy_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        return await self._call_mcp_tool("buy_products", params)

    async def accept_proposal(self, params: dict[str, Any]) -> TaskResult[Any]:
        return await self._call_mcp_tool("accept_proposal", params)

    async def control_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        return await self._call_mcp_tool("control_media_buy", params)

    async def list_creative_formats(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List supported creative formats."""
        return await self._call_mcp_tool("list_creative_formats", params)

    async def sync_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync creatives."""
        return await self._call_mcp_tool("sync_creatives", params)

    async def list_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List creatives."""
        return await self._call_mcp_tool("list_creatives", params)

    async def get_media_buy_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get media buy delivery."""
        return await self._call_mcp_tool("get_media_buy_delivery", params)

    async def get_media_buys(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get media buys with status, creative approval state, and optional delivery snapshots."""
        return await self._call_mcp_tool("get_media_buys", params)

    async def get_signals(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get signals."""
        return await self._call_mcp_tool("get_signals", params)

    async def activate_signal(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Activate signal."""
        return await self._call_mcp_tool("activate_signal", params)

    async def provide_performance_feedback(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Provide performance feedback."""
        return await self._call_mcp_tool("provide_performance_feedback", params)

    async def log_event(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Log event."""
        return await self._call_mcp_tool("log_event", params)

    async def sync_event_sources(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync event sources."""
        return await self._call_mcp_tool("sync_event_sources", params)

    async def sync_audiences(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync audiences."""
        return await self._call_mcp_tool("sync_audiences", params)

    async def sync_catalogs(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync catalogs."""
        return await self._call_mcp_tool("sync_catalogs", params)

    async def preview_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Generate preview URLs for a creative manifest."""
        return await self._call_mcp_tool("preview_creative", params)

    async def create_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create media buy."""
        return await self._call_mcp_tool("create_media_buy", params)

    async def update_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update media buy."""
        return await self._call_mcp_tool("update_media_buy", params)

    async def build_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Build creative."""
        return await self._call_mcp_tool("build_creative", params)

    async def get_creative_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get creative delivery."""
        return await self._call_mcp_tool("get_creative_delivery", params)

    async def list_transformers(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List creative transformers."""
        return await self._call_mcp_tool("list_transformers", params)

    async def list_accounts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List accounts."""
        return await self._call_mcp_tool("list_accounts", params)

    async def sync_accounts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync accounts."""
        return await self._call_mcp_tool("sync_accounts", params)

    async def get_account_financials(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get account financials."""
        return await self._call_mcp_tool("get_account_financials", params)

    async def report_usage(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Report account usage."""
        return await self._call_mcp_tool("report_usage", params)

    async def list_tools(self) -> list[str]:
        """List available tools from MCP agent."""
        session = await self._get_session()
        result = await session.list_tools()
        return [tool.name for tool in result.tools]

    async def get_agent_info(self) -> dict[str, Any]:
        """
        Get agent information including AdCP extension metadata from MCP server.

        MCP servers may expose metadata through:
        - Server capabilities exposed during initialization
        - extensions.adcp in server info (if supported)
        - Tool list

        Returns:
            Dictionary with agent metadata
        """
        session = await self._get_session()

        # Extract basic MCP server info
        info: dict[str, Any] = {
            "name": getattr(session, "server_name", None),
            "version": getattr(session, "server_version", None),
            "protocol": "mcp",
        }

        # Get available tools
        try:
            tools_result = await session.list_tools()
            tool_names = [tool.name for tool in tools_result.tools]
            if tool_names:
                info["tools"] = tool_names
        except Exception as e:
            logger.warning(f"Failed to list tools for {self.agent_config.id}: {e}")

        # Try to extract AdCP extension metadata from server capabilities
        # MCP servers may expose this in their initialization response
        capabilities = getattr(session, "_server_capabilities", None)
        if capabilities is not None:
            if isinstance(capabilities, dict):
                extensions = capabilities.get("extensions", {})
                adcp_ext = extensions.get("adcp", {})
                if adcp_ext:
                    info["adcp_version"] = adcp_ext.get("adcp_version")
                    info["protocols_supported"] = adcp_ext.get("protocols_supported")

        logger.info(f"Retrieved agent info for {self.agent_config.id}")
        return info

    async def close(self) -> None:
        """Close the MCP session and clean up resources."""
        if self._session_is_injected:
            return  # caller owns lifecycle; never close an injected session
        await self._cleanup_failed_connection("during close")

    async def close_mcp_session(self, session_id: str | None = None) -> None:
        """Terminate a stateful Streamable HTTP MCP session by id."""
        if self._session_is_injected:
            raise RuntimeError(
                "close_mcp_session is unavailable for from_mcp_client() sessions; "
                "the caller owns the injected transport lifecycle."
            )
        if self.agent_config.mcp_transport != "streamable_http":
            raise TypeError(
                "close_mcp_session is only supported for MCP streamable_http transport; "
                f"got {self.agent_config.mcp_transport!r}."
            )
        if session_id is None:
            session_id = self.current_mcp_session_id()
            if session_id is None:
                raise ValueError(
                    "No active MCP session id is available; pass session_id explicitly "
                    "or call after this client has initialized a Streamable HTTP session."
                )
        if not session_id or any(ch in session_id for ch in ("\r", "\n", "\x00")):
            raise ValueError("session_id must be a non-empty MCP session id header value")
        if not HTTPX_AVAILABLE:
            raise ImportError("httpx is required to close MCP Streamable HTTP sessions")

        headers = self._http_headers()
        headers[MCP_SESSION_ID] = session_id
        redirect_error_type: Any
        if self._httpx_client_factory is None:
            timeout = _httpx.Timeout(self.agent_config.timeout)
            event_hooks: dict[str, list[Any]] = {}
            if self.signing_request_hook is not None:
                event_hooks["request"] = [self.signing_request_hook]

            def client_factory(**kwargs: Any) -> Any:
                return _httpx.AsyncClient(
                    **kwargs,
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    event_hooks=event_hooks,
                )

            redirect_error_type = HTTPStatusError
        else:
            client_factory = self._streamable_http_client_factory()
            redirect_error_type = _mcp_httpx.HTTPStatusError
        close_client_kwargs: dict[str, Any] = {"headers": headers}
        if self._httpx_client_factory is not None:
            close_client_kwargs["timeout"] = self.agent_config.timeout
        urls_to_try = (
            [self._connected_url] if self._connected_url is not None else self._urls_to_try()
        )

        last_error: BaseException | None = None
        for url in urls_to_try:
            try:
                async with client_factory(**close_client_kwargs) as client:
                    response = await client.delete(url)
                if response.is_redirect:
                    location = response.headers.get("location")
                    suffix = f" to {location}" if location else ""
                    raise redirect_error_type(
                        f"Unexpected redirect while closing MCP session{suffix}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()

                current_session_id = self._get_session_id() if self._get_session_id else None
                if current_session_id == session_id:
                    await self._cleanup_failed_connection("after explicit MCP session close")
                return
            except _ALL_HTTP_STATUS_ERROR_TYPES as exc:
                last_error = exc
                # Keep fallback behavior symmetrical with session initialization:
                # a 404/405 on one candidate usually means "try the slash variant".
                exc_response = getattr(exc, "response", None)
                status_code = getattr(exc_response, "status_code", None)
                if status_code in (404, 405) and url != urls_to_try[-1]:
                    continue
                break
            except Exception as exc:
                last_error = exc
                if url != urls_to_try[-1]:
                    continue
                break

        raise ADCPConnectionError(
            f"Failed to close MCP session {session_id!r}: {last_error}",
            agent_id=self.agent_config.id,
            agent_uri=self.agent_config.agent_uri,
        ) from last_error

    # ========================================================================
    # V3 Protocol Methods - Protocol Discovery
    # ========================================================================

    async def get_adcp_capabilities(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get AdCP capabilities from the agent."""
        return await self._call_mcp_tool("get_adcp_capabilities", params)

    async def sync_agent_notification_configs(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Replace caller-scoped agent notification subscribers."""
        return await self._call_mcp_tool("sync_agent_notification_configs", params)

    async def get_task_status(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get task status from the agent."""
        return await self._call_mcp_tool("get_task_status", params)

    async def list_tasks(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List tasks from the agent."""
        return await self._call_mcp_tool("list_tasks", params)

    # ========================================================================
    # V3 Protocol Methods - Content Standards
    # ========================================================================

    async def create_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create content standards configuration."""
        return await self._call_mcp_tool("create_content_standards", params)

    async def get_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get content standards configuration."""
        return await self._call_mcp_tool("get_content_standards", params)

    async def list_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List content standards configurations."""
        return await self._call_mcp_tool("list_content_standards", params)

    async def update_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update content standards configuration."""
        return await self._call_mcp_tool("update_content_standards", params)

    async def calibrate_content(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Calibrate content against standards."""
        return await self._call_mcp_tool("calibrate_content", params)

    async def validate_content_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Validate content delivery against standards."""
        return await self._call_mcp_tool("validate_content_delivery", params)

    async def get_media_buy_artifacts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get artifacts associated with a media buy."""
        return await self._call_mcp_tool("get_media_buy_artifacts", params)

    # ========================================================================
    # V3 Protocol Methods - Governance
    # ========================================================================

    async def get_creative_features(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Evaluate governance features for a creative."""
        return await self._call_mcp_tool("get_creative_features", params)

    async def sync_plans(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync campaign governance plans."""
        return await self._call_mcp_tool("sync_plans", params)

    async def check_governance(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Check an action against campaign governance."""
        return await self._call_mcp_tool("check_governance", params)

    async def report_plan_outcome(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Report the outcome of a governed action."""
        return await self._call_mcp_tool("report_plan_outcome", params)

    async def report_plan_adjustment(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Report or review an adjustment to a governed outcome."""
        return await self._call_mcp_tool("report_plan_adjustment", params)

    async def get_plan_audit_logs(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Retrieve governance audit logs for plans."""
        return await self._call_mcp_tool("get_plan_audit_logs", params)

    # ========================================================================
    # V3 Protocol Methods - Sponsored Intelligence
    # ========================================================================

    async def si_get_offering(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get sponsored intelligence offering."""
        return await self._call_mcp_tool("si_get_offering", params)

    async def si_initiate_session(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Initiate sponsored intelligence session."""
        return await self._call_mcp_tool("si_initiate_session", params)

    async def si_send_message(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Send message in sponsored intelligence session."""
        return await self._call_mcp_tool("si_send_message", params)

    async def si_terminate_session(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Terminate sponsored intelligence session."""
        return await self._call_mcp_tool("si_terminate_session", params)

    # ========================================================================
    # V3 Protocol Methods - Governance (Property Lists)
    # ========================================================================

    async def create_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create a property list for governance."""
        return await self._call_mcp_tool("create_property_list", params)

    async def get_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get a property list with optional resolution."""
        return await self._call_mcp_tool("get_property_list", params)

    async def list_property_lists(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List property lists."""
        return await self._call_mcp_tool("list_property_lists", params)

    async def update_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update a property list."""
        return await self._call_mcp_tool("update_property_list", params)

    async def delete_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Delete a property list."""
        return await self._call_mcp_tool("delete_property_list", params)

    # ========================================================================
    # V3 Protocol Methods - Governance (Collection Lists)
    # ========================================================================

    async def create_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create a collection list for governance."""
        return await self._call_mcp_tool("create_collection_list", params)

    async def get_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get a collection list with optional resolution."""
        return await self._call_mcp_tool("get_collection_list", params)

    async def list_collection_lists(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List collection lists."""
        return await self._call_mcp_tool("list_collection_lists", params)

    async def update_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update a collection list."""
        return await self._call_mcp_tool("update_collection_list", params)

    async def delete_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Delete a collection list."""
        return await self._call_mcp_tool("delete_collection_list", params)

    # ========================================================================
    # V3 Protocol Methods - Governance (Sync Governance)
    # ========================================================================

    async def sync_governance(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync governance agents attached to an account."""
        return await self._call_mcp_tool("sync_governance", params)

    # ========================================================================
    # V3 Protocol Methods - TMP
    # ========================================================================

    async def context_match(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Match ad context to buyer packages."""
        return await self._call_mcp_tool("context_match", params)

    async def identity_match(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Match user identity for package eligibility."""
        return await self._call_mcp_tool("identity_match", params)

    # ========================================================================
    # V3 Protocol Methods - Brand Rights
    # ========================================================================

    async def get_brand_identity(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get brand identity information."""
        return await self._call_mcp_tool("get_brand_identity", params)

    async def get_rights(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get available rights for licensing."""
        return await self._call_mcp_tool("get_rights", params)

    async def acquire_rights(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Acquire rights for brand content usage."""
        return await self._call_mcp_tool("acquire_rights", params)

    async def update_rights(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update terms of an existing rights acquisition."""
        return await self._call_mcp_tool("update_rights", params)

    async def validate_input(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Validate creative input."""
        return await self._call_mcp_tool("validate_input", params)

    async def verify_brand_claim(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Verify a brand claim."""
        return await self._call_mcp_tool("verify_brand_claim", params)

    async def verify_brand_claims(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Verify brand claims."""
        return await self._call_mcp_tool("verify_brand_claims", params)

    # ========================================================================
    # V3 Protocol Methods - Compliance
    # ========================================================================

    async def comply_test_controller(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Compliance test controller (sandbox only)."""
        return await self._call_mcp_tool("comply_test_controller", params)
