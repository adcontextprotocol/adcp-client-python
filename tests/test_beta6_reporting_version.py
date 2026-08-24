"""AdCP 3.2 beta.6 reporting controls fail closed for older targets."""

from __future__ import annotations

from typing import Any

import pytest

from adcp import ADCPClient
from adcp._version import is_adcp_version_at_least
from adcp.compat.reporting_version import (
    assert_reporting_request_supported,
    beta6_reporting_request_issue,
)
from adcp.exceptions import ADCPFeatureUnsupportedError
from adcp.types import AgentConfig, Protocol


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("3.1", False),
        ("3.2-beta.5", False),
        ("3.2.0-beta.5", False),
        ("3.2-beta.6", True),
        ("3.2.0-beta.6", True),
        ("3.2-rc.1", True),
        ("3.2", True),
        ("3.3", True),
    ],
)
def test_beta6_semver_gate(version: str, supported: bool) -> None:
    assert is_adcp_version_at_least(version, "3.2.0-beta.6") is supported


@pytest.mark.parametrize(
    ("tool_name", "params", "field"),
    [
        ("get_media_buy_delivery", {"requested_metrics": ["impressions"]}, "requested_metrics"),
        (
            "get_media_buy_delivery",
            {"reporting_dimensions": {"format": {"limit": 10}}},
            "reporting_dimensions.format",
        ),
        (
            "get_media_buy_delivery",
            {"reporting_dimensions": {"geo": {"sort_direction": "desc"}}},
            "reporting_dimensions.geo.sort_direction",
        ),
        (
            "get_media_buy_delivery",
            {"reporting_dimensions": {"geo": {"sort_by": "viewed_seconds"}}},
            "reporting_dimensions.geo.sort_by",
        ),
        (
            "get_products",
            {"product": {"reporting_capabilities": {"required_metrics": ["time_based_views"]}}},
            "product.reporting_capabilities.required_metrics",
        ),
        (
            "create_media_buy",
            {"committed_metrics": [{"scope": "standard", "metric_id": "quartile_100"}]},
            "committed_metrics[0].metric_id",
        ),
        (
            "provide_performance_feedback",
            {"metrics": [{"scope": "vendor", "metric_id": "attention", "qualifier": "q1"}]},
            "metrics[0].qualifier",
        ),
    ],
)
def test_detects_beta6_reporting_features(
    tool_name: str, params: dict[str, Any], field: str
) -> None:
    issue = beta6_reporting_request_issue(tool_name, params)
    assert issue is not None
    assert issue.field == field


def test_legacy_reporting_request_is_allowed_for_beta5() -> None:
    assert_reporting_request_supported(
        "get_media_buy_delivery",
        {
            "adcp_version": "3.2-beta.5",
            "reporting_dimensions": {"geo": {"limit": 10, "sort_by": "impressions"}},
        },
    )


def test_beta6_reporting_request_is_allowed_for_ga() -> None:
    assert_reporting_request_supported(
        "get_media_buy_delivery",
        {"adcp_version": "3.2", "requested_metrics": ["impressions"]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [Protocol.MCP, Protocol.A2A])
async def test_client_rejects_beta6_reporting_before_transport(protocol: Protocol) -> None:
    client = ADCPClient(
        AgentConfig(
            id="legacy-seller",
            agent_uri="https://seller.example.com",
            protocol=protocol,
        ),
        server_version="3.2.0-beta.5",
    )

    with pytest.raises(
        ADCPFeatureUnsupportedError,
        match=r"requested delivery metrics at requested_metrics requires AdCP 3\.2-beta\.6",
    ):
        if protocol == Protocol.MCP:
            await client.adapter._call_mcp_tool(
                "get_media_buy_delivery", {"requested_metrics": ["impressions"]}
            )
        else:
            await client.adapter._call_a2a_tool(
                "get_media_buy_delivery", {"requested_metrics": ["impressions"]}
            )
