"""Account provisioning must not happen as a side effect of discovery."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter

from adcp import (
    AccountNotFoundError,
    AccountPaymentRequiredError,
    AccountRecord,
    AccountSetupRequiredError,
    ADCPClient,
    InMemoryAccountStorage,
)
from adcp.account_identity import account_key
from adcp.decisioning import (
    Account,
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    ExplicitAccounts,
    InMemoryTaskRegistry,
    NaturalKeyAccounts,
    SingletonAccounts,
    SyncAccountsResultRow,
)
from adcp.decisioning.capabilities import Account as AccountCapabilities
from adcp.decisioning.handler import PlatformHandler
from adcp.exceptions import classify_task_error
from adcp.server import ToolContext
from adcp.types import (
    AccountReference,
    AgentConfig,
    CreateMediaBuyRequest,
    GetAdcpCapabilitiesResponse,
    GetProductsRequest,
    GetProductsResponse,
    ListAccountsRequest,
    ListProductsRequest,
    SyncAccountsRequest,
)
from adcp.types.core import TaskResult, TaskStatus
from tests.conftest import validate_union

KEY = {
    "brand": {"domain": "acme.example", "countries": ["US", "GB"]},
    "operator": "buyer.example",
    "operator_unit": {"id": "seat-1", "name": "Agency"},
    "currency": "USD",
    "timezone": "UTC",
    "sandbox": False,
}


def make_client(*, seller="https://seller.example/mcp", storage=None, **account_caps):
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri=seller, protocol="mcp"), account_storage=storage
    )
    client._capabilities = GetAdcpCapabilitiesResponse.model_validate(
        {
            "adcp": {"major_versions": [3], "idempotency": {"supported": False}},
            "supported_protocols": ["media_buy"],
            "account": {"supported_billing": ["agent"], **account_caps},
        }
    )
    client._capabilities_fetched_at = time.monotonic()
    client.adapter.get_products = AsyncMock(
        return_value=TaskResult(status=TaskStatus.COMPLETED, data=GetProductsResponse(products=[]))
    )
    return client


def sync_result(*, status="active", action="created", **extras):
    from adcp.types import SyncAccountsResponse

    body = validate_union(
        SyncAccountsResponse,
        {
            "accounts": [
                {
                    **KEY,
                    "action": action,
                    "account_id": "seller-handle",
                    "status": status,
                    **extras,
                }
            ]
        },
    )
    return TaskResult(status=TaskStatus.COMPLETED, data=body)


def mock_sync(client, **kwargs):
    result = sync_result(**kwargs)
    client.adapter.sync_accounts = AsyncMock(return_value=result)
    client.adapter._parse_response = lambda raw, _type: raw


@pytest.mark.parametrize(
    "dimension,value",
    [
        ("currency", "EUR"),
        ("timezone", "Europe/London"),
        ("sandbox", True),
        ("operator", "other.example"),
        ("operator_unit", {"id": "seat-2"}),
        ("brand", {"domain": "acme.example", "countries": ["US"]}),
    ],
)
def test_complete_natural_key_distinguishes_accounts(dimension, value):
    assert account_key(KEY) != account_key({**KEY, dimension: value})


def test_mutable_metadata_does_not_change_identity():
    assert account_key(KEY) == account_key(
        {
            **KEY,
            "operator_unit": {"id": "seat-1", "name": "New name"},
            "brand": {"domain": "acme.example", "countries": ["GB", "US"], "industries": ["IAB1"]},
        }
    )


@pytest.mark.asyncio
async def test_auto_public_discovery_copies_request_and_drops_account_tokens():
    client = make_client()
    request = GetProductsRequest(
        account=KEY,
        buying_mode="wholesale",
        if_wholesale_feed_version="private-version",
        if_pricing_version="private-pricing",
    )
    await client.get_products(request)
    sent = client.adapter.get_products.call_args.args[0]
    assert "account" not in sent
    assert sent["brand"] == KEY["brand"]
    assert "if_wholesale_feed_version" not in sent
    assert "if_pricing_version" not in sent
    assert request.account is not None
    assert request.if_wholesale_feed_version == "private-version"


@pytest.mark.asyncio
async def test_compact_public_discovery_drops_private_version_and_passes_brand_key():
    client = make_client()
    client.adapter.list_products = AsyncMock(
        return_value=TaskResult(
            status=TaskStatus.COMPLETED,
            data={"products": []},
        )
    )
    client.adapter._parse_response = lambda raw, _type: raw
    request = ListProductsRequest(
        account=KEY, if_feed_version="private", if_pricing_version="pricing"
    )
    await client.list_products(request)
    sent = client.adapter.list_products.call_args.args[0]
    assert "account" not in sent
    assert "if_feed_version" not in sent
    assert "if_pricing_version" not in sent
    assert sent["brand"] == {"domain": "acme.example", "countries": ["GB", "US"]}


@pytest.mark.asyncio
async def test_strict_and_generic_calls_fail_before_transport():
    client = make_client()
    for invoke in (
        lambda: client.get_products(
            GetProductsRequest(buying_mode="wholesale", account=KEY), account_policy="strict"
        ),
        lambda: client.execute_task(
            "get_products",
            GetProductsRequest(buying_mode="wholesale", account=KEY),
            account_policy="strict",
        ),
    ):
        with pytest.raises(AccountNotFoundError) as exc:
            await invoke()
        assert exc.value.fault == "buyer_setup"
    client.adapter.get_products.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, KEY])
async def test_required_products_rejects_unprovisioned_accounts(key):
    client = make_client(required_for_products=True)
    with pytest.raises((AccountSetupRequiredError, AccountNotFoundError)):
        await client.get_products(GetProductsRequest(buying_mode="wholesale", account=key))
    client.adapter.get_products.assert_not_called()


@pytest.mark.asyncio
async def test_operator_auth_requires_explicit_id():
    client = make_client(require_operator_auth=True)
    await client.get_products(GetProductsRequest(buying_mode="wholesale", account=KEY))
    public = client.adapter.get_products.call_args.args[0]
    assert "account" not in public
    assert public["brand"] == KEY["brand"]
    with pytest.raises(AccountSetupRequiredError):
        await client.get_products(
            GetProductsRequest(buying_mode="wholesale", account=KEY), account_policy="strict"
        )
    with pytest.raises(AccountSetupRequiredError):
        await client.accounts.ensure(KEY, billing="agent")
    await client.get_products(
        GetProductsRequest(buying_mode="wholesale", account={"account_id": "external"})
    )
    assert client.adapter.get_products.call_args.args[0]["account"] == {"account_id": "external"}


@pytest.mark.asyncio
async def test_operator_auth_with_required_products_blocks_public_fallback():
    client = make_client(require_operator_auth=True, required_for_products=True)
    with pytest.raises(AccountSetupRequiredError):
        await client.get_products(GetProductsRequest(buying_mode="wholesale", account=KEY))
    client.adapter.get_products.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_is_memoized_durable_and_keeps_natural_reference():
    storage = InMemoryAccountStorage()
    client = make_client(storage=storage)
    mock_sync(client)
    records = await asyncio.gather(
        *[client.accounts.ensure(KEY, billing="agent", payment_terms="net_30") for _ in range(3)]
    )
    assert records[0].status == "active"
    assert records[0].account_id == "seller-handle"
    client.adapter.sync_accounts.assert_called_once()
    sent = client.adapter.sync_accounts.call_args.args[0]["accounts"][0]
    assert sent["payment_terms"] == "net_30"
    restarted = make_client(storage=storage)
    await restarted.get_products(
        GetProductsRequest(buying_mode="wholesale", account=KEY), account_policy="strict"
    )
    assert (
        restarted.adapter.get_products.call_args.args[0]["account"]["operator"] == "buyer.example"
    )
    other_seller = make_client(storage=storage, seller="https://other.example/mcp")
    with pytest.raises(AccountNotFoundError):
        await other_seller.get_products(
            GetProductsRequest(buying_mode="wholesale", account=KEY), account_policy="strict"
        )


@pytest.mark.asyncio
async def test_ensure_ignores_echoed_seller_assigned_timezone():
    client = make_client(timezone={"mode": "seller_fixed", "fixed_timezone": "UTC"})
    key = {name: value for name, value in KEY.items() if name != "timezone"}
    mock_sync(client)
    record = await client.accounts.ensure(key, billing="agent")
    assert record == AccountRecord("seller-handle", "active")


@pytest.mark.asyncio
async def test_direct_sync_learns_key_but_failed_and_dry_run_do_not():
    client = make_client()
    request = SyncAccountsRequest.model_validate(
        {
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
            "accounts": [{**KEY, "billing": "agent"}],
        }
    )
    mock_sync(client)
    await client.sync_accounts(request.model_copy(update={"dry_run": True}))
    assert await client.accounts.get(KEY) is None
    mock_sync(client, action="failed", errors=[{"code": "INVALID_REQUEST", "message": "bad terms"}])
    await client.sync_accounts(request)
    assert await client.accounts.get(KEY) is None
    mock_sync(client)
    await client.sync_accounts(request)
    assert (await client.accounts.get(KEY)).account_id == "seller-handle"


@pytest.mark.asyncio
async def test_sync_rows_match_identity_instead_of_response_order():
    from adcp.types import SyncAccountsResponse

    client = make_client()
    second = {**KEY, "operator_unit": {"id": "seat-2"}}
    request = SyncAccountsRequest.model_validate(
        {
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
            "accounts": [{**key, "billing": "agent"} for key in (KEY, second)],
        }
    )
    rows = [
        {**second, "action": "created", "account_id": "B", "status": "payment_required"},
        {**KEY, "action": "created", "account_id": "A", "status": "active"},
    ]
    body = validate_union(SyncAccountsResponse, {"accounts": rows})
    client.adapter.sync_accounts = AsyncMock(
        return_value=TaskResult(status=TaskStatus.COMPLETED, data=body)
    )
    client.adapter._parse_response = lambda raw, _type: raw
    await client.sync_accounts(request)
    assert await client.accounts.get(KEY) == AccountRecord("A", "active")
    assert await client.accounts.get(second) == AccountRecord("B", "payment_required")


@pytest.mark.asyncio
async def test_ambiguous_sync_identity_never_marks_an_account_provisioned():
    from adcp.types import SyncAccountsResponse

    client = make_client()
    second = {**KEY, "currency": "EUR"}
    request = SyncAccountsRequest.model_validate(
        {
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
            "accounts": [{**key, "billing": "agent"} for key in (KEY, second)],
        }
    )
    partial = {name: value for name, value in KEY.items() if name != "currency"}
    body = validate_union(
        SyncAccountsResponse,
        {"accounts": [{**partial, "action": "created", "account_id": "A", "status": "active"}]},
    )
    client.adapter.sync_accounts = AsyncMock(
        return_value=TaskResult(status=TaskStatus.COMPLETED, data=body)
    )
    client.adapter._parse_response = lambda raw, _type: raw
    await client.sync_accounts(request)
    assert await client.accounts.get(KEY) is None
    assert await client.accounts.get(second) is None


@pytest.mark.asyncio
async def test_failed_sync_missing_account_invalidates_its_key():
    client = make_client()
    mock_sync(client)
    await client.accounts.ensure(KEY, billing="agent")
    mock_sync(client, action="failed", errors=[{"code": "ACCOUNT_NOT_FOUND", "message": "gone"}])
    request = SyncAccountsRequest.model_validate(
        {
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
            "accounts": [{**KEY, "billing": "agent"}],
        }
    )
    await client.sync_accounts(request)
    assert await client.accounts.get(KEY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending_approval", "suspended", "rejected", "closed"])
async def test_inactive_product_discovery_uses_public_or_fails_strict(status):
    client = make_client()
    mock_sync(client, status=status)
    await client.accounts.ensure(KEY, billing="agent")
    request = GetProductsRequest(buying_mode="wholesale", account=KEY)
    with pytest.raises(AccountSetupRequiredError):
        await client.get_products(request, account_policy="strict")
    await client.get_products(request)
    assert "account" not in client.adapter.get_products.call_args.args[0]


@pytest.mark.asyncio
async def test_list_accounts_learns_complete_key():
    from adcp.types import ListAccountsResponse

    client = make_client(
        timezone={
            "mode": "account_fixed",
            "account_selection": "buyer_selected",
            "supported_timezones": ["UTC"],
        }
    )
    body = validate_union(
        ListAccountsResponse,
        {
            "accounts": [
                {
                    **KEY,
                    "account_id": "seller-handle",
                    "name": "Acme",
                    "status": "active",
                }
            ]
        },
    )
    client.adapter.list_accounts = AsyncMock(
        return_value=TaskResult(status=TaskStatus.COMPLETED, data=body)
    )
    client.adapter._parse_response = lambda raw, _type: raw
    await client.list_accounts(ListAccountsRequest(account=KEY))
    assert (await client.accounts.get(KEY)).status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timezone_caps",
    [
        None,
        {"mode": "seller_fixed", "fixed_timezone": "UTC"},
        {"mode": "account_fixed", "account_selection": "seller_assigned"},
    ],
)
async def test_cold_list_accounts_repairs_status_without_effective_timezone_key(timezone_caps):
    from adcp.types import ListAccountsResponse

    client = make_client(**({"timezone": timezone_caps} if timezone_caps else {}))
    caps = client.capabilities
    client._capabilities = None
    client.fetch_capabilities = AsyncMock(return_value=caps)
    key = {name: value for name, value in KEY.items() if name != "timezone"}
    await client.accounts._storage.save(
        client.agent_config.agent_uri,
        account_key(key),
        AccountRecord("seller-handle", "pending_approval"),
    )
    body = validate_union(
        ListAccountsResponse,
        {"accounts": [{**KEY, "account_id": "seller-handle", "name": "Acme", "status": "active"}]},
    )
    client.adapter.list_accounts = AsyncMock(
        return_value=TaskResult(status=TaskStatus.COMPLETED, data=body)
    )
    client.adapter._parse_response = lambda raw, _type: raw
    await client.list_accounts(ListAccountsRequest())
    assert await client.accounts.get(key) == AccountRecord("seller-handle", "active")
    await client.get_products(
        GetProductsRequest(buying_mode="wholesale", account=key), account_policy="strict"
    )
    assert client.adapter.get_products.call_args.args[0]["account"]["operator"] == key["operator"]


@pytest.mark.asyncio
async def test_account_errors_are_typed_and_invalidate_missing_keys():
    client = make_client()
    mock_sync(client)
    await client.accounts.ensure(KEY, billing="agent")
    client.adapter.get_products.return_value = TaskResult(
        status=TaskStatus.FAILED,
        success=False,
        adcp_error={"code": "ACCOUNT_NOT_FOUND", "message": "unknown"},
    )
    with pytest.raises(AccountNotFoundError):
        await client.get_products(GetProductsRequest(buying_mode="wholesale", account=KEY))
    assert await client.accounts.get(KEY) is None


@pytest.mark.parametrize(
    "code,exception",
    [
        ("ACCOUNT_NOT_FOUND", AccountNotFoundError),
        ("ACCOUNT_REQUIRED", AccountSetupRequiredError),
        ("ACCOUNT_SETUP_REQUIRED", AccountSetupRequiredError),
        ("ACCOUNT_PAYMENT_REQUIRED", AccountPaymentRequiredError),
    ],
)
def test_account_error_classification(code, exception):
    error = classify_task_error("get_products", [{"code": code, "message": "Buyer action needed"}])
    assert isinstance(error, exception)
    assert error.fault == "buyer_setup"
    assert not error.is_retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,exception",
    [
        ("pending_approval", AccountSetupRequiredError),
        ("payment_required", AccountPaymentRequiredError),
    ],
)
async def test_spend_requires_active_account(status, exception):
    client = make_client()
    mock_sync(client, status=status)
    await client.accounts.ensure(KEY, billing="agent")
    request = CreateMediaBuyRequest.model_construct(
        account=TypeAdapter(AccountReference).validate_python(KEY)
    )
    with pytest.raises(exception):
        await client.create_media_buy(request)


@pytest.fixture
def executor():
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


def make_handler(store, *, required=False, executor):
    class Seller(DecisioningPlatform):
        accounts = store
        capabilities = DecisioningCapabilities(
            account=AccountCapabilities(supported_billing=["agent"], required_for_products=required)
        )

        async def get_products(self, request, ctx):
            assert ctx.account.id == "" or ctx.account.id == "provisioned"
            return GetProductsResponse(products=[])

    return PlatformHandler(Seller(), executor=executor, registry=InMemoryTaskRegistry())


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [True, False])
async def test_explicit_accounts_support_public_discovery_or_account_required(executor, required):
    store = ExplicitAccounts(loader=lambda _: pytest.fail("Must not load an absent account"))
    handler = make_handler(store, required=required, executor=executor)
    if required:
        with pytest.raises(AdcpError) as error:
            await handler.get_products(
                GetProductsRequest(
                    buying_mode="wholesale",
                )
            )
        assert error.value.code == "ACCOUNT_REQUIRED"
    else:
        assert (
            await handler.get_products(
                GetProductsRequest(
                    buying_mode="wholesale",
                )
            )
        ).products == []


@pytest.mark.asyncio
async def test_natural_resolver_only_sees_synced_complete_keys(executor):
    persisted = {}
    provisioning_calls = []

    def loader(key, auth):
        return persisted.get(account_key(key))

    def provision(request, ctx=None):
        provisioning_calls.append(ctx)
        key = request.accounts[0].model_dump(mode="json", exclude_none=True)
        persisted[account_key(key)] = Account(id="provisioned")
        return [
            SyncAccountsResultRow(
                brand=key["brand"],
                operator=key["operator"],
                action="created",
                status="active",
                account_id="provisioned",
            )
        ]

    handler = make_handler(NaturalKeyAccounts(loader, upsert_request=provision), executor=executor)
    with pytest.raises(AdcpError) as error:
        await handler.get_products(GetProductsRequest(buying_mode="wholesale", account=KEY))
    assert error.value.code == "ACCOUNT_NOT_FOUND"
    assert persisted == {}
    assert provisioning_calls == []
    await handler.sync_accounts(
        SyncAccountsRequest.model_validate(
            {
                "idempotency_key": "00000000-0000-4000-8000-000000000001",
                "accounts": [{**KEY, "billing": "agent"}],
            }
        )
    )
    assert (
        await handler.get_products(GetProductsRequest(buying_mode="wholesale", account=KEY))
    ).products == []
    with pytest.raises(AdcpError):
        await handler.get_products(
            GetProductsRequest(buying_mode="wholesale", account={**KEY, "currency": "EUR"})
        )
    assert len(provisioning_calls) == 1


@pytest.mark.asyncio
async def test_task_aware_hook_is_optional_and_discovery_cannot_provision(executor):
    class Store(SingletonAccounts):
        def __init__(self):
            super().__init__("provisioned")
            self.contexts = []

        def resolve_for_task(self, ref, ctx):
            self.contexts.append(ctx)
            return Account(id="provisioned")

    store = Store()
    handler = make_handler(store, executor=executor)
    await handler.get_products(
        GetProductsRequest(
            buying_mode="wholesale",
        )
    )
    assert store.contexts[0].tool_name == "get_products"
    assert store.contexts[0].is_provisioning is False
    await handler._resolve_account(KEY, ToolContext(), tool_name="create_media_buy")
    assert store.contexts[1].is_provisioning is True
