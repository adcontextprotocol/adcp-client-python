"""SellerTestClient — in-process MCP harness for AdCP seller unit tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adcp.decisioning import DecisioningPlatform
    from adcp.validation.client_hooks import ValidationHookConfig


@dataclass(frozen=True)
class AdcpErrorPayload:
    """Typed container for a wire ``adcp_error`` dict.

    Maps to the AdCP transport-errors spec §MCP Binding fields.
    """

    code: str
    message: str
    recovery: str | None = None
    field: str | None = None
    suggestion: str | None = None
    retry_after: int | None = None
    details: dict[str, Any] | None = None


@dataclass
class ToolInvokeResult:
    """Result of a :meth:`SellerTestClient.invoke` call."""

    data: dict[str, Any] | None
    adcp_error: AdcpErrorPayload | None
    raw: dict[str, Any]

    @property
    def passed(self) -> bool:
        """True when the tool returned a success envelope (no adcp_error)."""
        return self.adcp_error is None


class SellerTestClient:
    """In-process MCP test client for AdCP seller implementations.

    Wraps a :class:`~adcp.decisioning.DecisioningPlatform` with
    :meth:`invoke` — a one-call helper that handles tool dispatch and
    ``adcp_error`` extraction from ``structuredContent``, eliminating
    the boilerplate every adopter previously rewrote in each test file.

    Usage::

        @pytest.fixture
        def seller():
            return SellerTestClient(MySeller())

        async def test_buy_not_found(seller):
            result = await seller.invoke(
                "update_media_buy", {"media_buy_id": "missing", ...}
            )
            assert not result.passed
            assert result.adcp_error.code == "MEDIA_BUY_NOT_FOUND"

        async def test_get_products_success(seller):
            result = await seller.invoke("get_products", {})
            assert result.passed
            assert "products" in result.data

    The harness calls :meth:`mcp.call_tool` in-process — it exercises
    the full handler dispatch and error-translation stack without HTTP
    or SSE framing. For HTTP-level tests (auth middleware, CORS, size
    limits), use :func:`adcp.testing.build_test_client` directly.

    A2A transport is not yet supported (A2A is served via a separate
    ASGI app; tracked as a follow-up on #662).

    ``run_scenario()`` is not implemented — it requires bundled
    compliance scenario playbooks that are not yet available in this SDK.
    """

    def __init__(
        self,
        platform: DecisioningPlatform,
        *,
        name: str | None = None,
        validation: ValidationHookConfig | None = None,
    ) -> None:
        """
        Args:
            platform: The :class:`~adcp.decisioning.DecisioningPlatform`
                instance under test.
            name: Server name forwarded to :func:`~adcp.server.serve.create_mcp_server`.
                Defaults to ``type(platform).__name__``.
            validation: Schema validation config. ``None`` (default) disables
                validation so tests focus on handler behavior, not schema
                conformance. Pass
                :data:`~adcp.validation.client_hooks.SERVER_DEFAULT_VALIDATION`
                to match production behavior.
        """
        self._platform = platform
        self._name = name
        self._validation = validation
        self._mcp: Any | None = None
        self._mcp_lock = asyncio.Lock()

    def _build_mcp_sync(self) -> Any:
        from adcp.decisioning.serve import create_adcp_server_from_platform
        from adcp.server.serve import create_mcp_server

        handler, _executor, _registry = create_adcp_server_from_platform(
            self._platform,
            auto_emit_completion_webhooks=False,
        )
        return create_mcp_server(
            handler,
            name=self._name or type(self._platform).__name__,
            validation=self._validation,
        )

    async def _ensure_mcp(self) -> Any:
        async with self._mcp_lock:
            if self._mcp is None:
                # create_adcp_server_from_platform calls asyncio.run() internally
                # (via validate_capabilities_response_shape) — must run in a thread
                # to avoid "cannot be called from a running event loop".
                self._mcp = await asyncio.to_thread(self._build_mcp_sync)
        return self._mcp

    async def invoke(
        self,
        tool: str,
        payload: dict[str, Any] | None = None,
    ) -> ToolInvokeResult:
        """Invoke a tool and return the result with adcp_error extraction.

        Args:
            tool: AdCP tool name (e.g. ``"update_media_buy"``).
            payload: Arguments forwarded to the tool. ``None`` → empty dict.

        Returns:
            :class:`ToolInvokeResult` — check :attr:`~ToolInvokeResult.passed`
            for success/failure and :attr:`~ToolInvokeResult.adcp_error` for
            error details.
        """
        from mcp.types import CallToolResult

        mcp = await self._ensure_mcp()
        kwargs = payload or {}
        raw_result = await mcp.call_tool(tool, kwargs)

        # FastMCP.call_tool returns different shapes depending on outcome
        # (observed with adcp-registered tools via _AdcpFuncMetadata):
        # - Error (AdcpError raised by handler): CallToolResult with isError=True
        #   and structuredContent={"adcp_error": {...}}
        # - Success: 2-tuple of (list[ContentBlock], dict | None) where the
        #   second element is the structured output dict
        if isinstance(raw_result, CallToolResult):
            structured: dict[str, Any] = raw_result.structuredContent or {}
        elif isinstance(raw_result, (tuple, list)) and len(raw_result) == 2:
            success_dict: Any = raw_result[1]
            structured = dict(success_dict) if success_dict is not None else {}
        else:
            raise RuntimeError(
                f"FastMCP.call_tool returned unexpected shape {type(raw_result)!r}; "
                "harness needs updating for this MCP version"
            )

        raw_error = structured.get("adcp_error")

        adcp_error: AdcpErrorPayload | None = None
        if raw_error is not None:
            adcp_error = AdcpErrorPayload(
                code=raw_error.get("code", "UNKNOWN"),
                message=raw_error.get("message", ""),
                recovery=raw_error.get("recovery"),
                field=raw_error.get("field"),
                suggestion=raw_error.get("suggestion"),
                retry_after=raw_error.get("retry_after"),
                details=raw_error.get("details"),
            )

        data: dict[str, Any] | None = None
        if raw_error is None and structured:
            data = dict(structured)

        return ToolInvokeResult(data=data, adcp_error=adcp_error, raw=structured)


__all__ = ["AdcpErrorPayload", "SellerTestClient", "ToolInvokeResult"]
