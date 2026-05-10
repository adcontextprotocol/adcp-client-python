"""Adopter pattern: DecisioningPlatform.create_media_buy with all three return paths.

The SalesResult[T] alias expands to:
  Awaitable[T] | T | TaskHandoff[T] | Awaitable[TaskHandoff[T]]

Verifies that all three adoption branches (sync value, async value, TaskHandoff) are
accepted by mypy --strict with zero type: ignore lines.
"""
from __future__ import annotations

from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SingletonAccounts,
)
from adcp.decisioning.specialisms.sales import SalesPlatform
from adcp.decisioning.types import SalesResult
from adcp.types import (
    CreateMediaBuyRequest,
    CreateMediaBuySuccessResponse,
    GetProductsRequest,
    GetProductsResponse,
    MediaBuyStatus,
)


class SyncSeller(DecisioningPlatform, SalesPlatform[dict[str, Any]]):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        channels=["display"],
        pricing_models=["cpm"],
    )
    accounts = SingletonAccounts(account_id="sync-seller")

    def get_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext[dict[str, Any]],
    ) -> GetProductsResponse:
        return GetProductsResponse(products=[])

    def create_media_buy(
        self,
        req: CreateMediaBuyRequest,
        ctx: RequestContext[dict[str, Any]],
    ) -> SalesResult[CreateMediaBuySuccessResponse]:
        return CreateMediaBuySuccessResponse(
            media_buy_id="mb_sync_1",
            status=MediaBuyStatus.active,
            packages=[],
        )


class TaskHandoffSeller(DecisioningPlatform, SalesPlatform[dict[str, Any]]):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-guaranteed"],
        channels=["display"],
        pricing_models=["cpd"],
    )
    accounts = SingletonAccounts(account_id="handoff-seller")

    def get_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext[dict[str, Any]],
    ) -> GetProductsResponse:
        return GetProductsResponse(products=[])

    def create_media_buy(
        self,
        req: CreateMediaBuyRequest,
        ctx: RequestContext[dict[str, Any]],
    ) -> SalesResult[CreateMediaBuySuccessResponse]:
        return ctx.handoff_to_task(self._approve)

    async def _approve(self, task_ctx: Any) -> CreateMediaBuySuccessResponse:
        return CreateMediaBuySuccessResponse(
            media_buy_id="mb_handoff_1",
            status=MediaBuyStatus.pending_start,
            packages=[],
        )


class AsyncSeller(DecisioningPlatform, SalesPlatform[dict[str, Any]]):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        channels=["display"],
        pricing_models=["cpm"],
    )
    accounts = SingletonAccounts(account_id="async-seller")

    def get_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext[dict[str, Any]],
    ) -> GetProductsResponse:
        return GetProductsResponse(products=[])

    async def create_media_buy(
        self,
        req: CreateMediaBuyRequest,
        ctx: RequestContext[dict[str, Any]],
    ) -> CreateMediaBuySuccessResponse:
        return CreateMediaBuySuccessResponse(
            media_buy_id="mb_async_1",
            status=MediaBuyStatus.active,
            packages=[],
        )
