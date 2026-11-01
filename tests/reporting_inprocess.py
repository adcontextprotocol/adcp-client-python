"""A buyer client bound to an installed in-process seller platform (test helper)."""

from __future__ import annotations

from typing import Any

from adcp import types as wire
from adcp.types.core import TaskResult, TaskStatus


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True, by_alias=True)
    return value


def _result(model: Any, value: Any) -> TaskResult[Any]:
    payload = _plain(value)
    try:
        data = model.model_validate(payload)
    except Exception:  # noqa: BLE001 - fixture sellers serve minimal buys
        data = payload
    return TaskResult.model_construct(status=TaskStatus.COMPLETED, data=data, success=True)


class InProcessBuyer:
    """The tasks ``client.reporting`` reads, dispatched to a platform under one caller."""

    def __init__(self, platform: Any, context: Any) -> None:
        self._platform = platform
        self._context = context
        self.fail: set[str] = set()

    async def _call(self, task: str, request: Any, model: Any) -> TaskResult[Any]:
        if task in self.fail:
            return TaskResult.model_construct(
                status=TaskStatus.FAILED, data=None, success=False, error=f"{task} unavailable"
            )
        params = request.model_dump(mode="json", exclude_none=True)
        return _result(model, await getattr(self._platform, task)(params, self._context()))

    async def get_principal(self, request: Any) -> TaskResult[Any]:
        return await self._call("get_principal", request, wire.GetPrincipalResponse)

    async def sync_principal(self, request: Any) -> TaskResult[Any]:
        return await self._call("sync_principal", request, wire.SyncPrincipalResponse)

    async def list_accounts(self, request: Any) -> TaskResult[Any]:
        return await self._call("list_accounts", request, wire.ListAccountsResponse)

    async def get_media_buys(self, request: Any) -> TaskResult[Any]:
        return await self._call("get_media_buys", request, wire.GetMediaBuysResponse)

    async def get_reporting_status(self, request: Any) -> TaskResult[Any]:
        return await self._call("get_reporting_status", request, wire.GetReportingStatusResponse)

    async def fetch_capabilities(self) -> Any:
        return _plain(await self._platform.get_adcp_capabilities({}, self._context()))
