"""Tests for adcp.testing.SellerTestClient."""

from __future__ import annotations

from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
)
from adcp.decisioning.types import AdcpError
from adcp.testing import AdcpErrorPayload, SellerTestClient, ToolInvokeResult


class _SuccessPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        supported_billing=("operator",),
    )
    accounts = SingletonAccounts(account_id="test")

    def get_products(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"products": [{"id": "p1", "name": "Banner"}]}

    def create_media_buy(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"media_buy_id": "mb_1", "status": "active"}

    def update_media_buy(self, mid: Any, p: Any, ctx: Any) -> dict[str, Any]:
        return {"media_buy_id": mid, "status": "active"}

    def sync_creatives(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"creatives": []}

    def get_media_buy_delivery(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"media_buy_deliveries": []}

    def get_media_buys(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"media_buys": []}

    def list_creative_formats(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"creative_formats": []}

    def list_creatives(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"creatives": []}

    def provide_performance_feedback(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"acknowledged": True}


class _ErrorPlatform(_SuccessPlatform):
    def get_products(self, req: Any, ctx: Any) -> dict[str, Any]:
        raise AdcpError(
            "PRODUCT_NOT_FOUND",
            message="no products available",
            recovery="terminal",
            field="brief",
        )


# ---- ToolInvokeResult ----


def test_invoke_result_passed_true_when_no_error() -> None:
    result = ToolInvokeResult(data={"products": []}, adcp_error=None, structured_content={})
    assert result.passed is True


def test_invoke_result_passed_false_when_error_present() -> None:
    err = AdcpErrorPayload(code="NOT_FOUND", message="gone")
    result = ToolInvokeResult(data=None, adcp_error=err, structured_content={})
    assert result.passed is False


# ---- AdcpErrorPayload ----


def test_adcp_error_payload_required_fields() -> None:
    err = AdcpErrorPayload(code="INVALID_REQUEST", message="bad param")
    assert err.code == "INVALID_REQUEST"
    assert err.message == "bad param"
    assert err.recovery is None
    assert err.field is None


def test_adcp_error_payload_all_fields() -> None:
    err = AdcpErrorPayload(
        code="BUDGET_TOO_LOW",
        message="below floor",
        recovery="correctable",
        field="total_budget",
        suggestion="Increase to $500",
        retry_after=None,
        details={"min": 500},
    )
    assert err.recovery == "correctable"
    assert err.field == "total_budget"
    assert err.suggestion == "Increase to $500"
    assert err.details == {"min": 500}


# ---- SellerTestClient.invoke ----


_GET_PRODUCTS_PAYLOAD = {"buying_mode": "brief"}


async def test_invoke_success_passed_true() -> None:
    client = SellerTestClient(_SuccessPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.passed
    assert result.adcp_error is None


async def test_invoke_success_data_populated() -> None:
    client = SellerTestClient(_SuccessPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.data is not None
    assert "products" in result.data


async def test_invoke_error_passed_false() -> None:
    client = SellerTestClient(_ErrorPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert not result.passed


async def test_invoke_error_code_extracted() -> None:
    client = SellerTestClient(_ErrorPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.adcp_error is not None
    assert result.adcp_error.code == "PRODUCT_NOT_FOUND"


async def test_invoke_error_message_extracted() -> None:
    client = SellerTestClient(_ErrorPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.adcp_error is not None
    assert result.adcp_error.message == "no products available"


async def test_invoke_error_recovery_extracted() -> None:
    client = SellerTestClient(_ErrorPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.adcp_error is not None
    assert result.adcp_error.recovery == "terminal"


async def test_invoke_error_field_extracted() -> None:
    client = SellerTestClient(_ErrorPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.adcp_error is not None
    assert result.adcp_error.field == "brief"


async def test_invoke_error_data_is_none() -> None:
    client = SellerTestClient(_ErrorPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.data is None


async def test_invoke_none_payload_uses_empty_dict() -> None:
    # None payload is coerced to {} — verify invoke() doesn't raise TypeError.
    client = SellerTestClient(_SuccessPlatform())
    # None → {} means the tool receives no args; Pydantic validation runs
    # normally. We just confirm no exception is raised from the harness itself.
    result = await client.invoke("get_products", None)
    assert isinstance(result, ToolInvokeResult)


async def test_invoke_mcp_lazily_initialized() -> None:
    client = SellerTestClient(_SuccessPlatform())
    assert client._mcp is None
    await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert client._mcp is not None


async def test_invoke_mcp_reuses_instance_across_calls() -> None:
    client = SellerTestClient(_SuccessPlatform())
    await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    mcp_first = client._mcp
    await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert client._mcp is mcp_first


async def test_invoke_structured_content_populated() -> None:
    client = SellerTestClient(_SuccessPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert isinstance(result.structured_content, dict)


# ---- Public import path ----


def test_public_import_from_adcp_testing() -> None:
    import adcp.testing as pkg

    assert pkg.SellerTestClient is SellerTestClient
    assert pkg.ToolInvokeResult is ToolInvokeResult
    assert pkg.AdcpErrorPayload is AdcpErrorPayload
