"""The demo's mock policy must not weaken real governance scope conversion."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest

_EXAMPLES = str(Path(__file__).parent.parent / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

import seller_agent as seller  # noqa: E402

from adcp.exceptions import ADCPTaskError  # noqa: E402
from adcp.server.mcp_tools import create_tool_caller  # noqa: E402
from adcp.validation import ValidationHookConfig, validate_response  # noqa: E402


def legacy_request():
    return {
        "adcp_major_version": 3,
        "idempotency_key": "governance-storyboard-regression",
        "accounts": [
            {
                "account": {"account_id": "account-one"},
                "governance_agents": [
                    {
                        "url": "https://governance.example/mcp",
                        "authentication": {"schemes": ["Bearer"], "credentials": "x" * 40},
                        "categories": ["budget_authority", "brand_policy"],
                    }
                ],
            }
        ],
    }


def caller(*, demo_policy):
    return create_tool_caller(
        seller.DemoSeller(),
        "sync_governance",
        validation=ValidationHookConfig(requests="strict", responses="strict"),
        pre_validation_hook=(
            seller.DEMO_PRE_VALIDATION_HOOKS["sync_governance"] if demo_policy else None
        ),
    )


async def test_demo_explicitly_acknowledges_its_existing_accountwide_mock_policy(caplog):
    request = legacy_request()
    original = deepcopy(request)
    response = await caller(demo_policy=True)(request)
    assert request == original
    assert validate_response("sync_governance", response, version="3.0").valid
    assert response["accounts"][0]["governance_agents"] == [
        {"url": "https://governance.example/mcp"}
    ]
    assert "account-wide mock governance" in caplog.text
    assert "does not apply legacy category scope" in caplog.text
    assert "x" * 40 not in caplog.text
    assert "budget_authority" not in caplog.text


async def test_default_sdk_refuses_to_broaden_legacy_category_scope():
    with pytest.raises(ADCPTaskError) as caught:
        await caller(demo_policy=False)(legacy_request())
    assert caught.value.errors[0].code == "VERSION_UNSUPPORTED"


async def test_demo_does_not_accept_categories_on_the_current_contract():
    request = legacy_request()
    request.pop("adcp_major_version")
    request["adcp_version"] = "3.2"
    with pytest.raises(ADCPTaskError) as caught:
        await caller(demo_policy=True)(request)
    assert caught.value.errors[0].code == "VALIDATION_ERROR"


async def test_demo_does_not_collapse_multiple_legacy_governance_agents():
    request = legacy_request()
    agents = request["accounts"][0]["governance_agents"]
    agents.append({**deepcopy(agents[0]), "url": "https://other-governance.example/mcp"})
    original = deepcopy(request)
    with pytest.raises(ADCPTaskError):
        await caller(demo_policy=True)(request)
    assert request == original
