"""Tests for the decorator-based server builder."""

from __future__ import annotations

import pytest

from adcp.server.builder import ADCPServerBuilder, adcp_server
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import capabilities_response, products_response


class TestADCPServerBuilder:
    """Tests for the builder pattern."""

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
