"""The seller side of ``request_account`` (AdCP 3.3, experimental ``account.requests``).

An AccountStore with ``request_account`` serves the tool and advertises the
``account.requests`` block to 3.3 callers; ``list_requests`` serves the caller's
requests on ``list_accounts`` when the buyer asks for them.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter

from adcp.decisioning import (
    Account,
    AccountRequestsPage,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    ResolveContext,
)
from adcp.decisioning.capabilities import Account as AccountCapabilities
from adcp.decisioning.handler import PlatformHandler
from adcp.exceptions import ConfigurationError
from adcp.server.base import ToolContext
from adcp.types import (
    GetAdcpCapabilitiesResponse,
    ListAccountsRequest,
    ListAccountsResponse,
    RequestAccountRequest,
    RequestAccountResponse,
)
from adcp.types.core import TaskResult, TaskStatus

KEY = {"brand": {"domain": "acme.example"}, "operator": "agency.example"}


def _record(request_id: str, state: str, **fields: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "state": state,
        "requested": KEY,
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z",
        **fields,
    }


class _Store:
    """A store that opens accounts after review and lists the caller's requests."""

    def __init__(self) -> None:
        self.requests: dict[str, dict[str, Any]] = {}
        self.accounts: list[Account[Any]] = []
        self.list_requests_calls = 0
        self.seen_ctx: ResolveContext | None = None

    def resolve(self, ref: Any = None, auth_info: Any = None) -> Account[Any]:
        return Account(id="acc-1")

    def list(self, filter: dict[str, Any] | None = None, ctx: Any = None) -> list[Account[Any]]:
        return list(self.accounts)

    async def request_account(
        self, params: RequestAccountRequest, ctx: ResolveContext | None = None
    ) -> dict[str, Any]:
        self.seen_ctx = ctx
        for record in self.requests.values():
            if record["state"] in ("in_review", "resolved"):
                return record
        record = _record(
            f"req-{len(self.requests) + 1}",
            "in_review",
            pending_action={"waiting_on": "seller", "kind": "review", "message": "Checking."},
        )
        self.requests[record["request_id"]] = record
        return record

    async def list_requests(
        self, params: ListAccountsRequest, ctx: ResolveContext | None = None
    ) -> AccountRequestsPage:
        self.list_requests_calls += 1
        items = list(self.requests.values())
        if params.request_ids is not None:
            items = [item for item in items if item["request_id"] in params.request_ids]
        return AccountRequestsPage(items=items[:1], next_cursor="c1" if len(items) > 1 else None)

    def approve(self, request_id: str) -> None:
        self.requests[request_id].update(
            state="resolved",
            resolution="created",
            account_id="acc-1",
            resolved_at="2026-11-02T00:00:00Z",
        )
        self.accounts.append(Account(id="acc-1", name="Acme", status="active", revision=2))


def _caps(**account: Any) -> DecisioningCapabilities:
    return DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        account=AccountCapabilities.model_validate({"supported_billing": ["operator"], **account}),
    )


def _handler(store: Any, capabilities: DecisioningCapabilities | None = None) -> PlatformHandler:
    class _Platform(DecisioningPlatform):
        accounts = store

    _Platform.capabilities = capabilities or _caps()
    return PlatformHandler(
        _Platform(), executor=ThreadPoolExecutor(max_workers=1), registry=InMemoryTaskRegistry()
    )


def _ctx(version: str | None = "3.3-beta.3") -> ToolContext:
    return ToolContext(resolved_adcp_version=version)


@pytest.mark.asyncio
async def test_request_account_advertises_account_requests_to_33_callers_only() -> None:
    handler = _handler(
        _Store(),
        _caps(requests={"supported": True, "changeable_attributes": ["billing", "timezone"]}),
    )
    assert "request_account" in handler.advertised_tools_for_instance()
    caps = await handler.get_adcp_capabilities({}, _ctx())
    assert caps["account"]["requests"] == {
        "supported": True,
        "request_task": "request_account",
        "read_task": "list_accounts",
        "changeable_attributes": ["billing", "timezone"],
    }
    assert caps["account"]["supported_billing"] == ["operator"]
    assert "account.requests" in caps["experimental_features"]
    GetAdcpCapabilitiesResponse.model_validate(caps)
    older = await handler.get_adcp_capabilities({}, _ctx("3.2"))
    assert "requests" not in older["account"]
    assert "account.requests" not in (older.get("experimental_features") or [])


@pytest.mark.asyncio
async def test_without_the_hook_nothing_is_advertised_even_when_features_claim_it() -> None:
    class _Plain:
        def resolve(self, ref: Any = None, auth_info: Any = None) -> Account[Any]:
            return Account(id="acc-1")

        def list(self, filter: Any = None, ctx: Any = None) -> list[Account[Any]]:
            return []

    capabilities = _caps()
    capabilities.experimental_features = ["account.requests"]
    handler = _handler(_Plain(), capabilities)
    assert "request_account" not in handler.advertised_tools_for_instance()
    caps = await handler.get_adcp_capabilities({}, _ctx())
    assert "requests" not in caps["account"]
    assert "account.requests" not in (caps.get("experimental_features") or [])
    refused = await handler.request_account(
        RequestAccountRequest.model_validate({"idempotency_key": "k" * 16, **KEY}), _ctx()
    )
    assert "not supported" in str(refused).lower() or "NOT_SUPPORTED" in str(refused)


def test_configuration_is_refused_rather_than_invented() -> None:
    no_billing = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    with pytest.raises(ConfigurationError, match="supported_billing"):
        _handler(_Store(), no_billing)

    class _ListsOnly:
        def resolve(self, ref: Any = None, auth_info: Any = None) -> Account[Any]:
            return Account(id="acc-1")

        def list_requests(self, params: Any, ctx: Any = None) -> list[Any]:
            return []

    with pytest.raises(ConfigurationError, match="needs accounts.list"):
        _handler(_ListsOnly())

    class _NoHook:
        def resolve(self, ref: Any = None, auth_info: Any = None) -> Account[Any]:
            return Account(id="acc-1")

    with pytest.raises(ConfigurationError, match="no request_account hook"):
        _handler(_NoHook(), _caps(requests={"supported": True}))

    class _NoListRequests:
        def resolve(self, ref: Any = None, auth_info: Any = None) -> Account[Any]:
            return Account(id="acc-1")

        def list(self, filter: Any = None, ctx: Any = None) -> list[Account[Any]]:
            return []

        def request_account(self, params: Any, ctx: Any = None) -> dict[str, Any]:
            return _record("req-1", "in_review")

    with pytest.warns(UserWarning, match="list_requests"):
        _handler(_NoListRequests())


@pytest.mark.asyncio
async def test_request_account_serves_the_hooks_record() -> None:
    store = _Store()
    handler = _handler(store)
    response = await handler.request_account(
        RequestAccountRequest.model_validate({"idempotency_key": "k" * 16, **KEY}), _ctx()
    )
    assert response["status"] == "completed"
    assert response["request"]["state"] == "in_review"
    TypeAdapter(RequestAccountResponse).validate_python(response)
    assert store.seen_ctx is not None and store.seen_ctx.tool_name == "request_account"


@pytest.mark.asyncio
async def test_list_accounts_serves_requests_only_when_asked_and_only_to_33() -> None:
    store = _Store()
    handler = _handler(store)
    await handler.request_account(
        RequestAccountRequest.model_validate({"idempotency_key": "k" * 16, **KEY}), _ctx()
    )
    store.requests["req-9"] = _record("req-9", "rejected")
    plain = await handler.list_accounts(ListAccountsRequest(), _ctx())
    assert "account_requests" not in plain and store.list_requests_calls == 0
    asked = await handler.list_accounts(ListAccountsRequest(include_requests=True), _ctx())
    ListAccountsResponse.model_validate(asked)
    assert [item["request_id"] for item in asked["account_requests"]] == ["req-1"]
    assert asked["account_requests_pagination"] == {"has_more": True, "cursor": "c1"}
    by_id = await handler.list_accounts(ListAccountsRequest(request_ids=["req-9"]), _ctx())
    assert [item["request_id"] for item in by_id["account_requests"]] == ["req-9"]
    assert by_id["account_requests_pagination"] == {"has_more": False}
    older = await handler.list_accounts(ListAccountsRequest(include_requests=True), _ctx("3.2"))
    assert "account_requests" not in older and store.list_requests_calls == 2


@pytest.mark.asyncio
async def test_list_accounts_serves_revision_from_32() -> None:
    store = _Store()
    store.accounts.append(Account(id="acc-1", revision=4))
    handler = _handler(store)
    current = await handler.list_accounts(ListAccountsRequest(), _ctx("3.2"))
    assert current["accounts"][0]["revision"] == 4
    older = await handler.list_accounts(ListAccountsRequest(), _ctx("3.1"))
    assert "revision" not in older["accounts"][0]


@pytest.mark.asyncio
async def test_a_python_buyer_ensures_against_the_seller_and_refreshes_to_ready() -> None:
    from adcp import AccountPending, AccountReady
    from adcp.client import ADCPClient
    from adcp.types.core import AgentConfig

    store = _Store()
    handler = _handler(store)
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp", protocol="mcp"),
        account_policy="auto",
    )
    client._capabilities = GetAdcpCapabilitiesResponse.model_validate(
        await handler.get_adcp_capabilities({}, _ctx())
    )
    client._capabilities_fetched_at = time.monotonic()

    async def request_account(params: dict[str, Any]) -> TaskResult:
        request = RequestAccountRequest.model_validate(params)
        return TaskResult(
            status=TaskStatus.COMPLETED, data=await handler.request_account(request, _ctx())
        )

    async def list_accounts(params: dict[str, Any]) -> TaskResult:
        request = ListAccountsRequest.model_validate(params)
        return TaskResult(
            status=TaskStatus.COMPLETED, data=await handler.list_accounts(request, _ctx())
        )

    client.adapter.request_account = AsyncMock(side_effect=request_account)
    client.adapter.list_accounts = AsyncMock(side_effect=list_accounts)
    client.adapter.sync_accounts = AsyncMock()
    client.adapter._parse_response = lambda raw, _type: raw

    outcome = await client.accounts.ensure(KEY)
    assert isinstance(outcome, AccountPending) and outcome.request_id == "req-1"
    client.adapter.sync_accounts.assert_not_called()
    store.approve("req-1")
    refreshed = await client.accounts.refresh()
    assert [type(item) for item in refreshed] == [AccountReady]
    again = await client.accounts.ensure(KEY)
    assert isinstance(again, AccountReady) and again.account_id == "acc-1"
    assert len(store.requests) == 1
