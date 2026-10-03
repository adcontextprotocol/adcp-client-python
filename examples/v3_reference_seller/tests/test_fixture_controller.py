"""Controller account isolation and upstream-backed compact discovery."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import Account as AccountRow
from src.platform import V3ReferenceSeller
from src.test_controller import ReferenceFixtureController

from adcp.decisioning import Account, AdcpError, AuthInfo, RequestContext
from adcp.server import ToolContext
from adcp.server.test_controller import TestControllerError as ControllerError
from adcp.server.test_controller import _list_scenarios
from adcp.types import ListProductsRequest


def _controller(monkeypatch, existing=None):
    monkeypatch.setattr(
        "src.test_controller.current_tenant", lambda: SimpleNamespace(id="tenant_a")
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.begin.return_value = session
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=result)
    template = Account(
        id="bootstrap",
        mode="mock",
        metadata={
            "tenant_id": "tenant_a",
            "buyer_agent_id": "buyer_a",
            "mock_upstream_url": "http://127.0.0.1:4603",
            "network_code": "network_a",
            "advertiser_id": "advertiser_a",
        },
    )
    platform = SimpleNamespace(
        accounts=SimpleNamespace(resolve=AsyncMock(return_value=template)),
        _sessionmaker=MagicMock(return_value=session),
    )
    controller = ReferenceFixtureController(platform, mock_upstream_url="http://127.0.0.1:4603")
    context = ToolContext(
        metadata={"adcp.auth_info": AuthInfo(kind="bearer", principal="https://buyer.example")}
    )
    return controller, context, session


_FIXTURE = {"brand": {"domain": "brand.example"}, "operator": "operator.example", "sandbox": True}


def test_controller_advertises_only_real_account_seeding(monkeypatch):
    controller, _, _ = _controller(monkeypatch)
    assert set(_list_scenarios(controller)) == {"seed_account", "force_get_products_arm"}
    for url in [
        "https://127.0.0.1:4603",
        "http://ad-server.example",
        "http://127.0.0.1.evil.example",
    ]:
        with pytest.raises(ValueError, match="loopback"):
            ReferenceFixtureController(controller.platform, mock_upstream_url=url)


@pytest.mark.asyncio
async def test_seed_writes_actual_account_and_ignores_fixture_routing(monkeypatch):
    controller, context, session = _controller(monkeypatch)
    await controller.seed_account(
        "fixture_account",
        {
            **_FIXTURE,
            "ext": {
                "network_code": "foreign_network",
                "advertiser_id": "foreign_advertiser",
                "mock_upstream_url": "http://169.254.169.254",
            },
        },
        context=context,
    )
    row = session.add.call_args.args[0]
    assert isinstance(row, AccountRow)
    assert (row.tenant_id, row.buyer_agent_id, row.account_id, row.sandbox) == (
        "tenant_a",
        "buyer_a",
        "fixture_account",
        True,
    )
    assert row.ext["network_code"] == "network_a"
    assert row.ext["advertiser_id"] == "advertiser_a"
    assert "mock_upstream_url" not in row.ext


@pytest.mark.asyncio
async def test_seed_requires_authenticated_context_and_owned_account(monkeypatch):
    foreign = AccountRow(tenant_id="tenant_a", buyer_agent_id="buyer_b", account_id="foreign")
    controller, context, session = _controller(monkeypatch, foreign)
    with pytest.raises(ControllerError):
        await controller.seed_account("foreign", _FIXTURE, context=context)
    session.add.assert_not_called()
    assert foreign.ext is None
    with pytest.raises(ControllerError):
        await controller.seed_account("fixture", _FIXTURE, context=ToolContext())
    controller.platform.accounts.resolve.return_value.mode = "live"
    with pytest.raises(ControllerError):
        await controller.seed_account("fixture", _FIXTURE, context=context)


@pytest.mark.asyncio
async def test_rejection_is_one_shot_principal_account_and_tenant_scoped(monkeypatch):
    controller, context, _ = _controller(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("src.test_controller.monotonic", lambda: clock[0])
    await controller.force_get_products_arm(
        "rejected", reason="Minimum spend", account={}, context=context
    )
    account = controller.platform.accounts.resolve.return_value
    auth = context.metadata["adcp.auth_info"]
    other_auth = AuthInfo(kind="bearer", principal="https://other-buyer.example")
    assert controller.take_rejection(RequestContext(account=account, auth_info=other_auth)) is None
    assert (
        controller.take_rejection(
            RequestContext(account=Account(id="other", metadata=account.metadata), auth_info=auth)
        )
        is None
    )
    assert (
        controller.take_rejection(
            RequestContext(
                account=Account(id=account.id, metadata={"tenant_id": "tenant_b"}), auth_info=auth
            )
        )
        is None
    )
    ctx = RequestContext(account=account, auth_info=auth)
    assert controller.take_rejection(ctx) == {"status": "rejected", "reason": "Minimum spend"}
    assert controller.take_rejection(ctx) is None
    await controller.force_get_products_arm(
        "rejected", reason="Expired directive", account={}, context=context
    )
    clock[0] = 401.0
    assert controller.take_rejection(ctx) is None
    with pytest.raises(ControllerError):
        await controller.force_get_products_arm("submitted", task_id="fake-task", context=context)


@pytest.mark.asyncio
async def test_natural_fixture_identity_ignores_display_metadata_and_null_defaults(monkeypatch):
    from src.platform import _make_account_store

    monkeypatch.setattr("src.platform.current_tenant", lambda: SimpleNamespace(id="tenant_a"))
    row = AccountRow(
        id="actual_fixture",
        tenant_id="tenant_a",
        buyer_agent_id="buyer_a",
        account_id="fixture",
        status="active",
        sandbox=False,
        ext={
            "network_code": "network_a",
            "advertiser_id": "advertiser_a",
            "fixture_scope": {**_FIXTURE, "operator_unit": {"id": "seat"}},
        },
    )
    buyer_result, fixture_result = MagicMock(), MagicMock()
    buyer_result.scalar_one_or_none.return_value = SimpleNamespace(id="buyer_a")
    fixture_result.scalars.return_value.all.return_value = [row]
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[buyer_result, fixture_result])
    store = _make_account_store(
        MagicMock(return_value=session), mock_upstream_url="http://127.0.0.1:4603"
    )
    resolved = await store.resolve(
        {
            "brand": {"domain": "brand.example", "brand_id": None, "countries": None},
            "operator": "operator.example",
            "operator_unit": {"id": "seat", "name": "Display only"},
            "sandbox": False,
        },
        AuthInfo(kind="bearer", principal="https://buyer.example"),
    )
    assert resolved.id == "actual_fixture"
    assert session.execute.call_count == 2  # No unrelated onboarding fallback.


def _catalog_context(account_id="account_a"):
    return RequestContext(
        account=Account(
            id=account_id,
            mode="mock",
            metadata={
                "tenant_id": "tenant_a",
                "network_code": "network_a",
                "advertiser_id": "advertiser_a",
                "mock_upstream_url": "http://up.test",
            },
        )
    )


_PRODUCTS = [
    {
        "product_id": pid,
        "name": pid,
        "delivery_type": "guaranteed",
        "channel": "display",
        "format_ids": ["display_300x250"],
        "pricing": {"model": "cpm", "cpm": 5, "currency": "USD"},
    }
    for pid in ["product_a", "product_b"]
]


@pytest.mark.asyncio
async def test_compact_discovery_reads_upstream_and_outcome_target_is_inert():
    platform = V3ReferenceSeller(sessionmaker=MagicMock(), upstream_api_key="upstream-secret")
    with respx.mock() as mock:
        route = mock.get("http://up.test/v1/products").mock(
            return_value=httpx.Response(200, json={"products": _PRODUCTS})
        )
        req = ListProductsRequest.model_validate(
            {"fields": ["product_id", "name"], "max_results": 1}
        )
        first = await platform.list_products(req, _catalog_context())
        with_target = await platform.list_products(
            ListProductsRequest.model_validate(
                {
                    **req.model_dump(mode="json", exclude_none=True),
                    "criteria": {
                        "outcome_target": {
                            "goal": {"kind": "metric", "metric": "clicks"},
                            "volume": 10000,
                        }
                    },
                }
            ),
            _catalog_context(),
        )
        assert first.model_dump(exclude_unset=True) == with_target.model_dump(exclude_unset=True)
        assert first.products[0].model_dump(exclude_unset=True) == {
            "product_id": "product_a",
            "name": "product_a",
        }
        assert route.call_count == 2
        assert route.calls[0].request.headers["Authorization"] == "Bearer upstream-secret"
        assert route.calls[0].request.headers["X-Network-Code"] == "network_a"
        full = await platform.list_products(ListProductsRequest(), _catalog_context())
        assert full.products[0].pricing_options[0].fixed_price == 5
        assert full.products[0].format_options[0].format_kind == "image"
        second = await platform.list_products(
            req.model_copy(update={"cursor": first.next_cursor}), _catalog_context()
        )
        assert second.products[0].product_id == "product_b"
        assert second.next_cursor is None
        unchanged = await platform.list_products(
            req.model_copy(update={"if_feed_version": first.feed_version}), _catalog_context()
        )
        assert unchanged.outcome == "unchanged"
        with pytest.raises(AdcpError, match="Cursor"):
            await platform.list_products(
                req.model_copy(update={"cursor": first.next_cursor}), _catalog_context("account_b")
            )
        await platform.aclose_upstream_clients()
