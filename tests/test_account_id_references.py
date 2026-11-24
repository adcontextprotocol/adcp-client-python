"""AdCP 3.3: an account is its account_id (#1478).

Buyer side asserts the wire form the adapter receives; seller side drives the
decisioning handler and tool caller with real Pydantic validation.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter

from adcp import AccountNotFoundError, ADCPClient
from adcp.account_identity import account_key
from adcp.accounts import AccountRecord
from adcp.decisioning import (
    Account,
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    NaturalKeyAccounts,
    SyncAccountsResultRow,
)
from adcp.decisioning.capabilities import Account as AccountCapabilities
from adcp.decisioning.handler import PlatformHandler
from adcp.server import ToolContext
from adcp.types import (
    AccountReference,
    AgentConfig,
    CreateMediaBuyRequest,
    GetAdcpCapabilitiesResponse,
    GetProductsRequest,
    GetProductsResponse,
    ListAccountsRequest,
    SyncAccountsRequest,
    SyncCreativesRequest,
)
from adcp.types.core import TaskResult, TaskStatus
from tests.test_account_lifecycle import KEY, sync_result

REF = {"account_id": "seller-handle"}


def make_client(*, versions=("3.3-rc.3",), **kwargs):
    """A client whose seller declares ``versions`` (None: majors only, so 3.0)."""
    adcp_caps: dict = {"major_versions": [3], "idempotency": {"supported": False}}
    if versions is not None:
        adcp_caps["supported_versions"] = list(versions)
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp", protocol="mcp"),
        **{"account_policy": "auto", "raise_account_errors": True, **kwargs},
    )
    client._capabilities = GetAdcpCapabilitiesResponse.model_validate(
        {
            "adcp": adcp_caps,
            "supported_protocols": ["media_buy"],
            "account": {"supported_billing": ["agent"]},
        }
    )
    client._capabilities_fetched_at = time.monotonic()

    # Version negotiation runs inside the adapter (request_converter), so mock
    # the wire below it; the recorded params are what the seller would receive.
    async def wire(tool, params):
        if tool == "get_products":
            return TaskResult(status=TaskStatus.COMPLETED, data=GetProductsResponse(products=[]))
        if tool == "sync_accounts":
            body = sync_result().data.model_dump(mode="json", exclude_none=True)
            return TaskResult(status=TaskStatus.COMPLETED, data=body)
        return TaskResult(status=TaskStatus.COMPLETED, data={})

    client.adapter._call_mcp_tool_wire = AsyncMock(side_effect=wire)
    return client


def sent(client, tool):
    """Params of the last wire call for ``tool``."""
    calls = [c for c in client.adapter._call_mcp_tool_wire.call_args_list if c.args[0] == tool]
    return calls[-1].args[1]


async def provisioned(**kwargs):
    client = make_client(**kwargs)
    await client.accounts.ensure(KEY, billing="agent")
    return client


def _ref(value):
    return TypeAdapter(AccountReference).validate_python(value)


async def _call(client, task, **kwargs):
    """Issue ``task`` against KEY; returns the tool name."""
    account = _ref(KEY)
    if task == "get_products":
        await client.get_products(
            GetProductsRequest(buying_mode="wholesale", account=account), **kwargs
        )
    elif task == "sync_creatives":
        await client.sync_creatives(
            SyncCreativesRequest.model_construct(account=account, creatives=[]), **kwargs
        )
    else:
        await client.create_media_buy(
            CreateMediaBuyRequest.model_construct(account=account), **kwargs
        )
    return task


# -- buyer -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["off", "auto", "strict"])
@pytest.mark.parametrize("task", ["get_products", "sync_creatives", "create_media_buy"])
async def test_3_3_seller_gets_account_id_once_provisioned(task, policy):
    client = await provisioned(account_policy=policy)
    await _call(client, task)
    assert sent(client, task)["account"] == REF


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["get_products", "sync_creatives", "create_media_buy"])
async def test_3_2_seller_keeps_natural_key(task):
    client = await provisioned(versions=("3.2",))
    await _call(client, task)
    wire = sent(client, task)["account"]
    assert "account_id" not in wire
    assert wire["operator"] == KEY["operator"]


@pytest.mark.asyncio
async def test_natural_key_mode_sends_natural_key_to_3_3_seller():
    client = await provisioned(account_reference_mode="natural_key")
    await _call(client, "get_products")
    wire = sent(client, "get_products")["account"]
    assert "account_id" not in wire
    assert wire["operator"] == KEY["operator"]


@pytest.mark.asyncio
async def test_sync_accounts_and_list_accounts_keep_natural_key_at_3_3():
    client = await provisioned()
    request = SyncAccountsRequest.model_validate(
        {
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
            "accounts": [{**KEY, "billing": "agent"}],
        }
    )
    await client.sync_accounts(request)
    entry = sent(client, "sync_accounts")["accounts"][0]
    assert "account_id" not in entry
    assert entry["operator"] == KEY["operator"]
    await client.list_accounts(ListAccountsRequest(account=_ref(KEY)))
    assert "account_id" not in (sent(client, "list_accounts").get("account") or {})


@pytest.mark.asyncio
async def test_caller_request_is_not_mutated():
    client = await provisioned()
    request = GetProductsRequest(buying_mode="wholesale", account=_ref(KEY))
    before = deepcopy(request.model_dump(mode="json", exclude_none=True))
    await client.get_products(request)
    assert sent(client, "get_products")["account"] == REF
    assert request.model_dump(mode="json", exclude_none=True) == before


@pytest.mark.asyncio
async def test_not_found_on_id_reference_invalidates_registry_record():
    client = await provisioned()
    client.adapter._call_mcp_tool_wire = AsyncMock(
        return_value=TaskResult(
            status=TaskStatus.FAILED,
            success=False,
            adcp_error={"code": "ACCOUNT_NOT_FOUND", "message": "unknown"},
        )
    )
    with pytest.raises(AccountNotFoundError):
        await client.get_products(GetProductsRequest(buying_mode="wholesale", account=_ref(KEY)))
    assert sent(client, "get_products")["account"] == REF
    assert await client.accounts.get(KEY) is None


@pytest.mark.asyncio
async def test_record_without_account_id_keeps_natural_key():
    client = make_client()
    # An in-review request_account record has no seller handle yet.
    registry = client.accounts
    record = AccountRecord(account_id=None, status="pending_approval")
    original = registry.get

    async def get(reference):
        return record if account_key(reference) == account_key(KEY) else await original(reference)

    registry.get = get  # type: ignore[method-assign]
    await _call(client, "get_products", account_policy="off")
    wire = sent(client, "get_products")["account"]
    assert "account_id" not in wire
    assert wire["operator"] == KEY["operator"]


def test_account_reference_mode_validation_and_export():
    with pytest.raises(ValueError):
        ADCPClient(
            AgentConfig(id="s", agent_uri="https://s.example/mcp", protocol="mcp"),
            account_reference_mode="bogus",  # type: ignore[arg-type]
        )
    from adcp import AccountReferenceMode

    assert AccountReferenceMode is not None


# -- seller: capabilities ----------------------------------------------------


@pytest.fixture
def executor():
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


def _platform(account_caps, accounts=None, *, seen=None):
    from adcp.decisioning import SingletonAccounts

    class Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(account=account_caps)

        async def get_products(self, request, ctx):
            if seen is not None:
                seen.append(ctx.account.id)
            return GetProductsResponse(products=[])

    Seller.accounts = accounts or SingletonAccounts(account_id="seller")
    return Seller()


def _caps_caller(platform, executor, **kwargs):
    from adcp.server.mcp_tools import create_tool_caller

    handler = PlatformHandler(
        platform, executor=executor, registry=InMemoryTaskRegistry(), **kwargs
    )
    return create_tool_caller(
        handler,
        "get_adcp_capabilities",
        **{k: v for k, v in kwargs.items() if k in {"adcp_version", "supported_versions"}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_version", ["3.3-rc.3", None])
async def test_capabilities_declare_stable_account_id_to_3_3_callers(executor, caller_version):
    caller = _caps_caller(_platform(AccountCapabilities(supported_billing=["agent"])), executor)
    response = await caller({"adcp_version": caller_version} if caller_version else {})
    assert response["account"]["stable_account_id"] is True


@pytest.mark.asyncio
async def test_explicit_stable_account_id_false_is_kept(executor):
    account = AccountCapabilities(supported_billing=["agent"], stable_account_id=False)
    caller = _caps_caller(_platform(account), executor)
    response = await caller({"adcp_version": "3.3-rc.3"})
    assert response["account"]["stable_account_id"] is False


@pytest.mark.asyncio
async def test_3_2_caller_does_not_see_stable_account_id(executor):
    caller = _caps_caller(_platform(AccountCapabilities(supported_billing=["agent"])), executor)
    response = await caller({"adcp_version": "3.2"})
    assert "stable_account_id" not in response["account"]


@pytest.mark.asyncio
async def test_server_pinned_3_2_does_not_emit_stable_account_id(executor):
    caller = _caps_caller(
        _platform(AccountCapabilities(supported_billing=["agent"], stable_account_id=True)),
        executor,
        adcp_version="3.2",
        supported_versions=["3.2"],
    )
    response = await caller({})
    assert "stable_account_id" not in response["account"]


@pytest.mark.asyncio
async def test_no_account_block_adds_nothing(executor):
    from adcp.decisioning import SingletonAccounts

    class Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

    caller = _caps_caller(Seller(), executor)
    response = await caller({"adcp_version": "3.3-rc.3"})
    assert "account" not in response


# -- seller: NaturalKeyAccounts end to end -----------------------------------


def _auth(principal):
    from adcp.decisioning import ApiKeyCredential, AuthInfo

    return AuthInfo(
        kind="api_key",
        key_id=f"kid-{principal}",
        principal=principal,
        credential=ApiKeyCredential(kind="api_key", key_id=f"kid-{principal}"),
    )


def _ctx(principal):
    return ToolContext(metadata={"adcp.auth_info": _auth(principal)})


@pytest.mark.asyncio
async def test_provisioned_account_resolves_by_id_and_natural_key_per_principal(executor):
    by_key: dict = {}
    by_id: dict = {}

    def loader(key, auth):
        entry = by_key.get(account_key(key))
        return entry[0] if entry and entry[1] == auth.principal else None

    def loader_by_id(account_id, auth):
        entry = by_id.get(account_id)
        return entry[0] if entry and entry[1] == auth.principal else None

    def provision(request, ctx=None):
        key = request.accounts[0].model_dump(mode="json", exclude_none=True)
        key.pop("billing", None)
        account = Account(id="acc_1")
        by_key[account_key(key)] = (account, ctx.auth_info.principal)
        by_id[account.id] = (account, ctx.auth_info.principal)
        return [
            SyncAccountsResultRow(
                brand=key["brand"],
                operator=key["operator"],
                action="created",
                status="active",
                account_id=account.id,
            )
        ]

    seen: list = []
    store = NaturalKeyAccounts(loader, upsert_request=provision, loader_by_id=loader_by_id)
    handler = PlatformHandler(
        _platform(AccountCapabilities(supported_billing=["agent"]), store, seen=seen),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    synced = await handler.sync_accounts(
        SyncAccountsRequest.model_validate(
            {
                "idempotency_key": "00000000-0000-4000-8000-000000000001",
                "accounts": [{**KEY, "billing": "agent"}],
            }
        ),
        _ctx("buyer-a"),
    )
    assert synced is not None

    for ref in ({"account_id": "acc_1"}, KEY):
        await handler.get_products(
            GetProductsRequest(buying_mode="wholesale", account=_ref(ref)), _ctx("buyer-a")
        )
    assert seen == ["acc_1", "acc_1"]

    with pytest.raises(AdcpError) as error:
        await handler.get_products(
            GetProductsRequest(buying_mode="wholesale", account=_ref({"account_id": "acc_1"})),
            _ctx("buyer-b"),
        )
    assert error.value.code == "ACCOUNT_NOT_FOUND"
