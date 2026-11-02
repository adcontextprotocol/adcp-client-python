"""AdCP 3.3 experimental reads: ``list_proposals`` and ``list_collections``.

Both tasks are new in 3.3 with no older equivalent. A seller serves each only
when its capabilities declare it, never to a caller below 3.3, and the buyer
refuses to send either to a 3.2 peer.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from adcp import ADCPClient
from adcp.compat.versions import convert_payload
from adcp.decisioning import (
    CollectionCatalogPlatform,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    OpenOpportunitiesPlatform,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import LifecycleTool, MediaBuy, OpenOpportunities
from adcp.decisioning.dispatch import validate_platform
from adcp.decisioning.experimental_reads import refuse_caller_below_release
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.types import AdcpError
from adcp.exceptions import ADCPFeatureUnsupportedError, ADCPTaskError
from adcp.server import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.principal import _ACCOUNT_NOTIFICATION_TYPES, _MEDIA_BUY_NOTIFICATION_TYPES
from adcp.server.responses import capabilities_response
from adcp.types import ListCollectionsRequest, ListProposalsRequest
from adcp.types.core import AgentConfig, Protocol, TaskStatus
from adcp.validation import validate_response
from adcp.validation._response_envelope import uses_task_status_envelope
from adcp.webhooks import WebhookSender

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas" / "cache"
PIN = "3.3-beta.3"
AT = "2026-10-23T09:14:05Z"

PROPOSALS: dict[str, Any] = {"outcome": "listed", "proposals": [], "opportunities": []}
COLLECTIONS: dict[str, Any] = {
    "outcome": "listed",
    "collections": [],
    "collections_version": "cc_1",
    "cache_scope": "public",
}


class _Sales(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="reads-account")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "mb-1"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}


class _Reads(_Sales):
    """Implements both reads but declares neither."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, Any]] = []

    def list_proposals(self, req, ctx):
        self.seen.append(("list_proposals", ctx.account.id))
        return dict(PROPOSALS)

    def list_collections(self, req, ctx):
        self.seen.append(("list_collections", ctx.account.id))
        return dict(COLLECTIONS)


class _LifecycleDeclared(_Reads):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        media_buy=MediaBuy(lifecycle_tools=[LifecycleTool.list_proposals]),
        experimental_features=["media_buy.open_opportunities"],
    )


class _OpenOpportunitiesDeclared(_Reads):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        media_buy=MediaBuy(open_opportunities=OpenOpportunities(list_proposals=True)),
        experimental_features=["media_buy.open_opportunities"],
    )


class _CatalogDeclared(_Reads):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        experimental_features=["media_buy.collection_catalog", "media_buy.collection_cards"],
    )


def _handler(platform: DecisioningPlatform, executor: ThreadPoolExecutor) -> PlatformHandler:
    return PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())


def test_platforms_satisfy_the_new_protocols() -> None:
    assert isinstance(_Reads(), OpenOpportunitiesPlatform)
    assert isinstance(_Reads(), CollectionCatalogPlatform)
    assert not isinstance(_Sales(), OpenOpportunitiesPlatform)


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        (_Reads, set()),
        (_LifecycleDeclared, {"list_proposals"}),
        (_OpenOpportunitiesDeclared, {"list_proposals"}),
        (_CatalogDeclared, {"list_collections"}),
    ],
)
def test_advertised_only_when_declared_and_implemented(
    platform: type[DecisioningPlatform], expected: set[str]
) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        advertised = _handler(platform(), executor).get_advertised_tools()
    assert advertised & {"list_proposals", "list_collections"} == expected


@pytest.mark.parametrize(
    ("capabilities", "method"),
    [
        (
            DecisioningCapabilities(
                specialisms=["sales-non-guaranteed"],
                media_buy=MediaBuy(lifecycle_tools=[LifecycleTool.list_proposals]),
            ),
            "list_proposals",
        ),
        (
            DecisioningCapabilities(
                specialisms=["sales-non-guaranteed"],
                media_buy=MediaBuy(open_opportunities=OpenOpportunities(list_proposals=True)),
            ),
            "list_proposals",
        ),
        (
            DecisioningCapabilities(
                specialisms=["sales-non-guaranteed"],
                experimental_features=[
                    "media_buy.collection_catalog",
                    "media_buy.collection_cards",
                ],
            ),
            "list_collections",
        ),
    ],
)
def test_boot_fails_for_a_declaration_without_a_method(
    capabilities: DecisioningCapabilities, method: str
) -> None:
    class _Declared(_Sales):
        pass

    _Declared.capabilities = capabilities
    with pytest.raises(AdcpError) as caught:
        validate_platform(_Declared())
    missing = [item["method"] for item in caught.value.details["missing"]]
    assert missing.count(method) == 1


@pytest.mark.asyncio
async def test_platform_handler_dispatches_with_resolved_account() -> None:
    platform = _LifecycleDeclared()
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = _handler(platform, executor)
        response = await handler.list_proposals(
            ListProposalsRequest.model_validate({"account": {"account_id": "reads-account"}}),
            ToolContext(resolved_adcp_version=PIN),
        )
    assert response == PROPOSALS
    [(name, account_id)] = platform.seen
    assert name == "list_proposals"
    assert account_id.startswith("reads-account")


@pytest.mark.asyncio
async def test_list_collections_serves_without_an_account() -> None:
    platform = _CatalogDeclared()
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = _handler(platform, executor)
        response = await handler.list_collections(
            ListCollectionsRequest.model_validate({}), ToolContext(resolved_adcp_version=PIN)
        )
    assert response == COLLECTIONS
    assert [name for name, _ in platform.seen] == ["list_collections"]


@pytest.mark.parametrize("tool", ["list_proposals", "list_collections"])
@pytest.mark.parametrize("version", ["3.2", "3.2.7", "3.1", "not-a-version"])
def test_platform_handler_refuses_a_caller_below_3_3(tool: str, version: str) -> None:
    with pytest.raises(AdcpError) as caught:
        refuse_caller_below_release(tool, version)
    assert caught.value.code == "UNSUPPORTED_FEATURE"
    assert caught.value.details == {"task": tool, "adcp_version": version}


@pytest.mark.parametrize("version", [None, "3.3-beta.0", PIN, "3.3", "3.4"])
def test_platform_handler_serves_3_3_and_later(version: str | None) -> None:
    refuse_caller_below_release("list_collections", version)


@pytest.mark.parametrize("tool", ["list_proposals", "list_collections"])
def test_converter_has_no_older_equivalent(tool: str) -> None:
    with pytest.raises(ADCPFeatureUnsupportedError, match="unavailable in AdCP 3.2"):
        convert_payload(
            tool,
            {"account": {"account_id": "a"}},
            direction="request",
            source_version="3.2",
            target_version=PIN,
        )


class _Seller(ADCPHandler[Any]):
    async def list_proposals(self, params, context=None):
        return dict(PROPOSALS)

    async def list_collections(self, params, context=None):
        return dict(COLLECTIONS)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_proposals", "list_collections"])
async def test_server_refuses_a_3_2_caller_with_unsupported_feature(tool: str) -> None:
    caller = create_tool_caller(_Seller(), tool)
    with pytest.raises(ADCPTaskError) as caught:
        await caller({"adcp_version": "3.2", "account": {"account_id": "a"}})
    error = caught.value.errors[0]
    assert error.code == "UNSUPPORTED_FEATURE"
    assert error.details == {"task": tool, "adcp_version": "3.2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_proposals", "list_collections"])
async def test_server_serves_a_3_3_caller(tool: str) -> None:
    caller = create_tool_caller(_Seller(), tool)
    response = await caller({"adcp_version": PIN, "account": {"account_id": "a"}})
    assert response["status"] == "completed"
    assert validate_response(tool, response, version=PIN).valid


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [Protocol.MCP, Protocol.A2A])
@pytest.mark.parametrize("tool", ["list_proposals", "list_collections"])
async def test_client_refuses_a_3_2_peer_before_transport(protocol: Protocol, tool: str) -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example", protocol=protocol),
        server_version="3.2",
        adcp_version="3.2",
        account_policy="off",
    )
    wire = AsyncMock()
    if protocol is Protocol.MCP:
        client.adapter._call_mcp_tool_wire = wire
    else:
        client.adapter._call_a2a_tool_wire = wire
    if tool == "list_proposals":
        result = await client.list_proposals(
            ListProposalsRequest.model_validate({"account": {"account_id": "a"}})
        )
    else:
        result = await client.list_collections(ListCollectionsRequest.model_validate({}))
    assert result.status == TaskStatus.FAILED
    assert result.adcp_error["code"] == "UNSUPPORTED_FEATURE"
    assert result.adcp_error["details"]["wire_response_received"] is False
    wire.assert_not_called()


def test_a_3_2_capabilities_caller_never_sees_list_proposals_declared() -> None:
    declaration = capabilities_response(["media_buy"], adcp_version=PIN)
    declaration["adcp"]["idempotency"] = {"supported": False}
    declaration["media_buy"] = {
        "lifecycle_tools": ["list_products", "list_proposals"],
        "open_opportunities": {"list_proposals": True},
    }
    converted = convert_payload(
        "get_adcp_capabilities",
        declaration,
        direction="sync",
        source_version=PIN,
        target_version="3.2",
    )
    assert converted.payload["media_buy"] == {"lifecycle_tools": ["list_products"]}
    assert {loss.pointer for loss in converted.losses} == {
        "/media_buy/lifecycle_tools/1",
        "/media_buy/open_opportunities",
    }


@pytest.mark.parametrize("tool", ["list_proposals", "list_collections"])
def test_status_envelope_follows_each_prerelease_schema(tool: str) -> None:
    # beta.0 through beta.2 close the root without status; beta.3 composes the
    # protocol envelope, which requires it.
    for version in ("3.3-beta.0", "3.3-beta.1", "3.3-beta.2"):
        assert uses_task_status_envelope(tool, version) is False
    assert uses_task_status_envelope(tool, PIN) is True
    assert uses_task_status_envelope(tool, None) is True


@pytest.mark.parametrize("bundle", ["3.3.0-beta.1", "3.3.0-beta.2", "3.3.0-beta.3"])
def test_bundled_list_collections_examples_validate(bundle: str) -> None:
    schema = json.loads(
        (SCHEMAS / bundle / "media-buy" / "list-collections-response.json").read_text()
    )
    version = bundle.replace("3.3.0-", "3.3-")
    for example in schema["examples"]:
        outcome = validate_response("list_collections", example["data"], version=version)
        assert outcome.valid, outcome.issues


def test_proposal_created_preparation_binds_notification_id_to_proposal_id() -> None:
    payload = {
        "subscriber_id": "buyer-proposals",
        "account_id": "acc_1",
        "proposal_id": "prop_1",
        "opportunity_id": "opp_1",
        "later_reason": "event_package",
        "expires_at": AT,
    }
    prepared = WebhookSender.prepare_proposal_created(
        url="https://buyer.example/hook", payload=payload
    )
    body = json.loads(prepared.body)
    assert body["notification_type"] == "proposal.created"
    assert body["notification_id"] == "prop_1"
    assert body["idempotency_key"] == prepared.idempotency_key

    with pytest.raises(ValueError, match="notification_id must equal proposal_id"):
        WebhookSender.prepare_proposal_created(
            url="https://buyer.example/hook", payload={**payload, "notification_id": "other"}
        )
    with pytest.raises(ValueError, match="schema validation"):
        # Identifiers only: commercial terms or prose never ride on it.
        WebhookSender.prepare_proposal_created(
            url="https://buyer.example/hook", payload={**payload, "terms": "cheap"}
        )


def test_every_account_anchored_notification_type_requires_the_acknowledgment() -> None:
    """Drift guard: the agent-level surface classifies every enum value."""
    enum = json.loads((SCHEMAS / "3.3.0-beta.3" / "enums" / "notification-type.json").read_text())
    caller_anchored = {"capabilities.changed", "principal.changed", "account_request.changed"}
    assert set(enum["enum"]) - caller_anchored - _MEDIA_BUY_NOTIFICATION_TYPES == set(
        _ACCOUNT_NOTIFICATION_TYPES
    )
