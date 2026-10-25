"""Conversion losses log quietly on the seller and loudly on the buyer."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from importlib.resources import files
from typing import Any
from unittest.mock import AsyncMock

import pytest

from adcp import ADCPClient
from adcp.compat.versions import (
    CONVERSION_METADATA_KEY,
    ConversionLoss,
    convert_payload,
    log_conversion_summary,
)
from adcp.server import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import capabilities_response
from adcp.types import GetAdcpCapabilitiesResponse, GetProductsRequest
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus
from adcp.validation import validate_response

_LOGGER = "adcp.compat.versions"
_VECTORS = json.loads(
    files("adcp")
    .joinpath("_compliance/3.2.4/test-vectors/audience-evidence/vectors.json")
    .read_text()
)
_PRODUCT_LOSSES = (
    "audience_evidence",
    "included_signals/0/demographic_predicate",
    "included_signals/0/restricted_attributes",
)


def _product(index: int) -> dict[str, Any]:
    """A 3.2 product carrying the three fields ESA saw dropped for 3.1 buyers."""
    product = deepcopy(_VECTORS["product"])
    product["product_id"] = f"product_{index}"
    product["audience_evidence"] = [deepcopy(_VECTORS["evidence"])]
    product["included_signals"] = [
        {
            "signal_ref": {"scope": "product", "signal_id": "adults_25_34"},
            "name": "Adults 25-34",
            "value_type": "binary",
            "demographic_predicate": {"age": {"min": 25, "max": 34, "include_unknown": False}},
            "restricted_attributes": ["age"],
        }
    ]
    return product


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER and r.levelno == level]


async def test_seller_answering_an_older_buyer_logs_one_summary_not_a_warning_per_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = {
        "status": "completed",
        "cache_scope": "public",
        "products": [_product(index) for index in range(50)],
    }
    assert validate_response("get_products", response, version="3.2").valid

    class Seller(ADCPHandler):
        adcp_capabilities = {"media_buy": {"features": {"canonical_creatives": True}}}

        async def get_products(self, params: dict[str, Any], context: ToolContext):
            return deepcopy(response)

    context = ToolContext()
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    served = await create_tool_caller(Seller(), "get_products")(
        {"adcp_version": "3.1", "buying_mode": "brief", "brief": "video"}, context
    )

    assert validate_response("get_products", served, version="3.1").valid
    assert _records(caplog, logging.WARNING) == []
    debug = _records(caplog, logging.DEBUG)
    assert len(debug) == 150
    (summary,) = _records(caplog, logging.INFO)
    message = summary.getMessage()
    assert "get_products 3.2 -> 3.1 dropped 150 field(s)" in message
    assert message.endswith(
        "/products/*/audience_evidence, "
        "/products/*/included_signals/*/demographic_predicate, "
        "/products/*/included_signals/*/restricted_attributes"
    )
    # The metadata report keeps every exact, uncollapsed pointer.
    losses = context.metadata[CONVERSION_METADATA_KEY]["losses"]
    assert [loss["pointer"] for loss in losses] == [
        f"/products/{index}/{suffix}" for index in range(50) for suffix in _PRODUCT_LOSSES
    ]
    assert {(loss["source_version"], loss["target_version"]) for loss in losses} == {("3.2", "3.1")}


async def test_lossless_server_conversion_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Seller(ADCPHandler):
        adcp_capabilities = {"media_buy": {"features": {"canonical_creatives": True}}}

        async def get_products(self, params: dict[str, Any], context: ToolContext):
            return {"products": [], "status": "completed", "cache_scope": "public"}

    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    await create_tool_caller(Seller(), "get_products")(
        {"adcp_version": "3.1", "buying_mode": "brief", "brief": "video"}
    )
    assert [r for r in caplog.records if r.name == _LOGGER] == []


def test_summary_caps_the_distinct_pointers() -> None:
    losses = tuple(ConversionLoss(f"/field_{index}", "3.2", "3.1") for index in range(12))
    logger = logging.getLogger(_LOGGER)
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        log_conversion_summary("sync_accounts", losses, source_version="3.2", target_version="3.1")
        log_conversion_summary("sync_accounts", (), source_version="3.2", target_version="3.1")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    (record,) = records
    assert record.getMessage().endswith("/field_8, /field_9, ... (+2 more)")


def test_direct_conversion_defaults_to_a_warning_per_loss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    result = convert_payload(
        "get_products",
        {"status": "completed", "cache_scope": "public", "products": [_product(0)]},
        direction="sync",
        source_version="3.2",
        target_version="3.1",
    )
    assert len(result.losses) == 3
    assert len(_records(caplog, logging.WARNING)) == 3
    assert _records(caplog, logging.INFO) == []


async def test_buyer_side_conversion_keeps_a_warning_per_loss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp/", protocol=Protocol.MCP)
    )
    declaration = capabilities_response(
        ["media_buy"], adcp_version="3.1", supported_versions=["3.0", "3.1"]
    )
    declaration["adcp"]["idempotency"] = {"supported": False}
    client.fetch_capabilities = AsyncMock(
        return_value=GetAdcpCapabilitiesResponse.model_validate(declaration)
    )
    client.adapter._call_mcp_tool_wire = AsyncMock(
        return_value=TaskResult(
            status=TaskStatus.COMPLETED,
            success=True,
            data={
                "products": [],
                "status": "completed",
                "cache_scope": "public",
                "adcp_version": "3.1",
            },
        )
    )
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    result = await client.get_products(
        GetProductsRequest.model_validate(
            {
                "buying_mode": "brief",
                "brief": "video",
                "brand": {"domain": "example.com", "countries": ["US"]},
            }
        )
    )
    assert result.success, result.error
    (warning,) = _records(caplog, logging.WARNING)
    assert "dropped /brand/countries" in warning.getMessage()
    assert _records(caplog, logging.INFO) == []
    assert result.metadata[CONVERSION_METADATA_KEY]["losses"][0]["pointer"] == "/brand/countries"
