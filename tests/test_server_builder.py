"""Tests for the decorator-based server builder."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.server.builder import HANDLER_TO_DOMAIN, ADCPServerBuilder, adcp_server
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import capabilities_response, products_response


class TestADCPServerBuilder:
    """Tests for the builder pattern."""

    def test_list_account_changes_uses_media_buy_protocol(self) -> None:
        assert HANDLER_TO_DOMAIN["list_account_changes"] == "media_buy"

    def test_basic_decorator_registration(self) -> None:
        server = adcp_server("test-seller")

        @server.get_products
        async def get_products(params, context=None):
            return products_response([])

        assert "get_products" in server._handlers

    def test_multiple_handlers(self) -> None:
        server = adcp_server("test-seller")

        @server.get_products
        async def get_products(params, context=None):
            return products_response([])

        @server.create_media_buy
        async def create_media_buy(params, context=None):
            return {}

        assert len(server._handlers) == 2

    def test_build_handler(self) -> None:
        server = adcp_server("test-seller")

        @server.get_products
        async def get_products(params, context=None):
            return products_response([{"product_id": "p1", "name": "Test"}])

        @server.get_adcp_capabilities
        async def caps(params, context=None):
            return capabilities_response(["media_buy"])

        handler = server.build_handler()
        assert hasattr(handler, "get_products")
        assert hasattr(handler, "get_adcp_capabilities")

    @pytest.mark.asyncio
    async def test_handler_calls_registered_function(self) -> None:
        server = adcp_server("test-seller")
        called = False

        @server.get_products
        async def get_products(params, context=None):
            nonlocal called
            called = True
            return products_response([])

        handler = server.build_handler()
        await handler.get_products({})
        assert called is True

    def test_detect_domains(self) -> None:
        server = adcp_server("test-seller")

        @server.get_products
        async def gp(params, context=None):
            return {}

        @server.get_signals
        async def gs(params, context=None):
            return {}

        domains = server._detect_domains()
        assert "media_buy" in domains
        assert "signals" in domains

    @pytest.mark.asyncio
    async def test_auto_capabilities(self) -> None:
        server = adcp_server("test-seller")

        @server.get_products
        async def gp(params, context=None):
            return products_response([])

        handler = server.build_handler()
        # Auto-generated capabilities should include media_buy
        result = await handler.get_adcp_capabilities({})
        assert "supported_protocols" in result
        assert "media_buy" in result["supported_protocols"]
        assert result["media_buy"]["features"]["canonical_creatives"] is True

    @pytest.mark.asyncio
    async def test_fresh_31_request_uses_framework_canonical_capability(self) -> None:
        server = adcp_server("test-seller")

        @server.get_products
        async def gp(params, context=None):
            return products_response([])

        handler = server.build_handler()
        caller = create_tool_caller(handler, "get_products")

        result = await caller(
            {
                "adcp_version": "3.1",
                "brief": "Q4 campaign",
                "promoted_offering": "Shoes",
                "buying_mode": "brief",
            }
        )

        assert result["products"] == []

    @pytest.mark.asyncio
    async def test_30_discovery_does_not_poison_later_31_request(self) -> None:
        server = adcp_server("test-seller")

        @server.get_products
        async def gp(params, context=None):
            return products_response([])

        handler = server.build_handler()
        capabilities = create_tool_caller(handler, "get_adcp_capabilities")
        get_products = create_tool_caller(handler, "get_products")

        legacy_caps = await capabilities({"adcp_version": "3.0"})
        assert "canonical_creatives" not in legacy_caps.get("media_buy", {}).get("features", {})

        result = await get_products(
            {
                "adcp_version": "3.1",
                "brief": "Q4 campaign",
                "promoted_offering": "Shoes",
                "buying_mode": "brief",
            }
        )

        assert result["products"] == []

    @pytest.mark.asyncio
    async def test_custom_capabilities_omission_keeps_framework_default(self) -> None:
        server = adcp_server("test-seller")

        @server.get_adcp_capabilities
        async def capabilities(params, context=None):
            return {"supported_protocols": ["media_buy"]}

        @server.get_products
        async def get_products(params, context=None):
            return products_response([])

        handler = server.build_handler()
        capabilities_call = create_tool_caller(handler, "get_adcp_capabilities")
        get_products_call = create_tool_caller(handler, "get_products")

        advertised = await capabilities_call({"adcp_version": "3.1"})
        assert advertised["media_buy"]["features"]["canonical_creatives"] is True

        result = await get_products_call(
            {
                "adcp_version": "3.1",
                "brief": "Q4 campaign",
                "promoted_offering": "Shoes",
                "buying_mode": "brief",
            }
        )
        assert result["products"] == []

    @pytest.mark.asyncio
    async def test_framework_respects_explicit_legacy_capability(self) -> None:
        server = adcp_server("test-seller")
        received: dict[str, Any] = {}

        @server.get_adcp_capabilities
        async def capabilities(params, context=None):
            return {
                "supported_protocols": ["media_buy"],
                "media_buy": {"features": {"canonical_creatives": False}},
            }

        @server.create_media_buy
        async def create_media_buy(params, context=None):
            received.update(params)
            return {"media_buy_id": "mb-1", "packages": params["packages"]}

        handler = server.build_handler()
        caller = create_tool_caller(handler, "get_adcp_capabilities")

        unversioned = await caller({})
        major_only = await caller({"adcp_major_version": 3})
        modern = await caller({"adcp_version": "3.1"})
        legacy = await caller({"adcp_version": "3.0"})

        assert unversioned["media_buy"]["features"]["canonical_creatives"] is False
        assert major_only["media_buy"]["features"]["canonical_creatives"] is False
        assert modern["media_buy"]["features"]["canonical_creatives"] is False
        assert "canonical_creatives" not in legacy.get("media_buy", {}).get("features", {})

        create = create_tool_caller(handler, "create_media_buy")
        await create(
            {
                "adcp_version": "3.1",
                "packages": [
                    {
                        "product_id": "p-1",
                        "format_ids": [
                            {
                                "agent_url": "https://seller.example/mcp",
                                "id": "display_300x250_image",
                            }
                        ],
                    }
                ],
            }
        )
        assert "format_ids" not in received["packages"][0]
        assert received["packages"][0]["format_option_refs"]

    def test_factory_function(self) -> None:
        server = adcp_server("my-seller", version="2.0.0")
        assert isinstance(server, ADCPServerBuilder)
        assert server.name == "my-seller"
        assert server.version == "2.0.0"

    def test_private_attr_raises(self) -> None:
        server = adcp_server("test")
        with pytest.raises(AttributeError):
            server._private_thing

    def test_importable_from_server_package(self) -> None:
        from adcp.server import ADCPServerBuilder, adcp_server

        assert callable(adcp_server)
        assert ADCPServerBuilder is not None

    def test_serve_accepts_builder(self) -> None:
        """serve() should accept ADCPServerBuilder and auto-convert."""

        server = adcp_server("test-seller")

        @server.get_products
        async def gp(params, context=None):
            return products_response([])

        # Verify build_handler works (serve() would call this internally)
        handler = server.build_handler()
        assert hasattr(handler, "get_products")

    def test_typo_raises(self) -> None:
        """Unknown task names should raise ValueError."""
        server = adcp_server("test")
        with pytest.raises(ValueError, match="not a known ADCP task"):

            @server.get_product  # typo - missing 's'
            async def handler(params, context=None):
                return {}

    @pytest.mark.parametrize(
        ("adopter_name", "wire_name", "params"),
        [
            ("build_creative_legacy", "build_creative", {"idempotency_key": "build-1"}),
            ("list_creative_formats_legacy", "list_creative_formats", {}),
            (
                "preview_creative_legacy",
                "preview_creative",
                {"request_type": "variant", "variant_id": "variant-1"},
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_legacy_decorator_dispatches_under_wire_tool_name(
        self,
        adopter_name: str,
        wire_name: str,
        params: dict[str, object],
    ) -> None:
        server = adcp_server("legacy-creative")
        calls: list[str] = []

        async def implementation(request, context=None):
            calls.append(adopter_name)
            return {}

        getattr(server, adopter_name)(implementation)
        handler = server.build_handler()

        assert hasattr(handler, adopter_name)
        await create_tool_caller(handler, wire_name)(params)
        assert calls == [adopter_name]

    @pytest.mark.parametrize(
        "wire_name",
        ["build_creative", "list_creative_formats", "preview_creative"],
    )
    def test_legacy_only_decorators_require_explicit_name(self, wire_name: str) -> None:
        server = adcp_server("legacy-creative")

        with pytest.raises(ValueError, match=f"{wire_name}_legacy"):
            getattr(server, wire_name)(lambda params, context=None: {})
