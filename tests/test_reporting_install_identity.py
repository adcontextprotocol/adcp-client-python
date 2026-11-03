"""Reporting installations must retain each seller's own advertised tools."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.reporting.service import ReliableReportingService
from adcp.server import ADCPHandler
from adcp.server.mcp_tools import get_tools_for_handler, register_handler_tools


class Seller(ADCPHandler):
    def __init__(self, tool: str) -> None:
        super().__init__()
        self.tool = tool

    def advertised_tools_for_instance(self) -> set[str]:
        return {self.tool}

    async def get_products(self, params: Any, context: Any = None) -> Any:
        return {"products": [{"product_id": "first-seller"}]}

    async def get_media_buys(self, params: Any, context: Any = None) -> Any:
        return {"media_buys": [{"media_buy_id": "second-seller"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("same_service", [True, False])
async def test_install_preserves_distinct_seller_surfaces(same_service: bool) -> None:
    first_service = ReliableReportingService.memory(account_context=lambda *_: None)
    second_service = (
        first_service
        if same_service
        else ReliableReportingService.memory(account_context=lambda *_: None)
    )
    first = first_service.install(Seller("get_products"))
    first_type = type(first)
    assert first_service.install(first) is first
    assert type(first) is first_type

    second = second_service.install(Seller("get_media_buys"))
    second_type = type(second)
    assert second_type.__name__ != first_type.__name__
    assert second_service.install(second) is second
    assert type(second) is second_type

    first_tools = {item["name"] for item in get_tools_for_handler(first, _include_schemas=False)}
    second_tools = {item["name"] for item in get_tools_for_handler(second, _include_schemas=False)}
    assert "get_products" in first_tools and "get_media_buys" not in first_tools
    assert "get_media_buys" in second_tools and "get_products" not in second_tools
    assert await first.get_products({}) == {"products": [{"product_id": "first-seller"}]}
    assert await second.get_media_buys({}) == {"media_buys": [{"media_buy_id": "second-seller"}]}

    # New installation names must not weaken the public registry's conflict guard.
    registered = first_type.advertised_tools
    register_handler_tools(first_type.__name__, registered)
    with pytest.raises(ValueError, match="called twice with different tool sets"):
        register_handler_tools(first_type.__name__, registered | {"get_media_buys"})
    assert {
        item["name"] for item in get_tools_for_handler(first, _include_schemas=False)
    } == first_tools
