"""A real optional reporting status task for the SDK MCP/A2A handler surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from adcp._version import is_adcp_version_at_least, resolve_adcp_version
from adcp.exceptions import ConfigurationError
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.server.base import ADCPHandler, ToolContext
from adcp.types import GetReportingStatusRequest

ReportingStatusCallerResolver = Callable[
    [dict[str, Any], ToolContext | None], Awaitable[ReportingStatusCaller]
]


class ReportingStatusNotificationHandler(ADCPHandler[ToolContext]):
    """Compose with adopter task overrides; mounts only the authoritative read.

    ``resolve_caller`` resolves the requested account using trusted transport
    authentication and account authorization. Request consumer/principal fields
    are never identity. Registration and other reporting tasks remain explicit
    adopter overrides, and do not appear merely because this handler exists.
    """

    def __init__(
        self,
        status: ReportingStatusHandler,
        *,
        resolve_caller: ReportingStatusCallerResolver,
        adcp_version: str | None = None,
    ) -> None:
        super().__init__()
        self.reporting_status_handler = status
        self._resolve_status_caller = resolve_caller
        self._adcp_version = resolve_adcp_version(adcp_version)
        if not is_adcp_version_at_least(self._adcp_version, "3.2-rc.3"):
            raise ConfigurationError(
                "reporting mounts require a supported AdCP 3.2 reporting contract; "
                "use 3.2-rc.3 for retained walks or omit the pin for the packaged default"
            )

    def get_adcp_version(self) -> str:
        """Public per-mount protocol pin, shared by schema and rendering."""
        return self._adcp_version

    async def get_reporting_status(
        self, params: GetReportingStatusRequest | dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        request = (
            dict(params)
            if isinstance(params, dict)
            else params.model_dump(mode="json", exclude_unset=True)
        )
        request["adcp_version"] = (
            context.resolved_adcp_version
            if context is not None and context.resolved_adcp_version is not None
            else request.get("adcp_version") or self.get_adcp_version()
        )
        caller = await self._resolve_status_caller(request, context)
        return await self.reporting_status_handler.handle(request, caller=caller)
