"""A real optional reporting status task for the SDK MCP/A2A handler surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

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
        self, status: ReportingStatusHandler, *, resolve_caller: ReportingStatusCallerResolver
    ) -> None:
        super().__init__()
        self.reporting_status_handler = status
        self._resolve_status_caller = resolve_caller

    async def get_reporting_status(
        self, params: GetReportingStatusRequest | dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        request = (
            params
            if isinstance(params, dict)
            else params.model_dump(mode="json", exclude_unset=True)
        )
        caller = await self._resolve_status_caller(request, context)
        return await self.reporting_status_handler.handle(request, caller=caller)
