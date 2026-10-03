"""Core reporting uses the ordinary decisioning dispatcher and its auth boundary."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest

from adcp.decisioning import Account, AdcpError, AuthInfo, LazyPlatformRouter
from adcp.decisioning.serve import create_adcp_server_from_platform
from adcp.reporting.fixtures import redacted_capabilities
from adcp.reporting.service import ReliableReportingConfigurationError, ReliableReportingService
from adcp.reporting.testing import ScriptedReportingAdapter
from adcp.server.auth import current_principal
from adcp.server.base import ToolContext
from adcp.server.mcp_tools import get_tools_for_handler
from adcp.signing import VerifiedSigner
from adcp.types import (
    GetAdcpCapabilitiesRequest,
    GetMediaBuyDeliveryRequest,
    GetProductsRequest,
    GetReportingStatusRequest,
    SyncReportingStatusRequest,
)
from adcp.validation.schema_loader import get_named_validator
from tests.test_lazy_platform_router import _SyncSalesPlatform
from tests.test_reliable_reporting_service import (
    NOW,
    _account_context,
    _capability_offering,
    _configuration,
    _rows,
)
from tests.test_reporting_ledger import _statement
from tests.test_validate_idempotency_wiring import _caps_with_idempotency

ACCOUNT = "account-redacted"
REF = {"account_id": ACCOUNT}


def _tool_context(principal: str, *, signed: bool) -> ToolContext:
    # The decisioning serve() context factory supplies this transport metadata.
    current_principal.set(None if signed else principal)
    if signed:
        auth_info = AuthInfo.from_verified_signer(
            VerifiedSigner(
                key_id=f"{principal}#key-1",
                alg="ed25519",
                label="sig1",
                verified_at=NOW.timestamp(),
                agent_url=principal,
            )
        )
        return ToolContext(metadata={"adcp.auth_info": auth_info})
    return ToolContext(metadata={"adcp.auth_info": {"principal": principal}})


class _AuthorizedAccounts:
    resolution = "explicit"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def resolve(self, ref: Any, auth_info: Any = None) -> Account[Any]:
        principal = auth_info.principal if auth_info is not None else None
        account_id = (ref or {}).get("account_id")
        self.calls.append((account_id, principal))
        if account_id != ACCOUNT or principal not in {
            "buyer-1",
            "buyer-2",
            "https://buyer-1.example.test",
            "https://buyer-2.example.test",
        }:
            raise AdcpError("UNAUTHORIZED", message="account denied", recovery="terminal")
        return Account(id=ACCOUNT, metadata={"tenant_id": "tenant-a"})


class _SalesPlatform(_SyncSalesPlatform):
    def __init__(self) -> None:
        super().__init__("reporting")
        self.accounts = _AuthorizedAccounts()
        self.delivery_threads: list[int] = []

    def get_media_buy_delivery(self, req: Any, ctx: Any) -> dict[str, Any]:
        self.delivery_threads.append(threading.get_ident())
        return super().get_media_buy_delivery(req, ctx)


@pytest.mark.parametrize("lazy", [False, True])
@pytest.mark.parametrize("signed", [False, True], ids=["bearer", "signed-request"])
async def test_install_dispatches_reporting_with_account_auth_and_preserves_delivery(
    lazy: bool,
    signed: bool,
) -> None:
    buyer_1 = "https://buyer-1.example.test" if signed else "buyer-1"
    buyer_2 = "https://buyer-2.example.test" if signed else "buyer-2"

    def context_for(principal: str) -> ToolContext:
        return _tool_context(principal, signed=signed)

    service = ReliableReportingService.memory(
        account_context=_account_context, clock=lambda: NOW, consumer_status_enabled=True
    )
    service.sources.register(
        "gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(10), _rows(10)])
    )
    await service.configure(replace(_configuration(), consumer_id=buyer_1))
    await service.configure(replace(_configuration(), consumer_id=buyer_2))
    child = _SalesPlatform()
    creations: list[str] = []

    def factory(tenant: str) -> _SalesPlatform:
        creations.append(tenant)
        return child

    base = (
        LazyPlatformRouter(
            accounts=child.accounts, factory=factory, capabilities=child.capabilities
        )
        if lazy
        else child
    )
    installed = service.install(base)
    assert service.install(installed) is installed
    with pytest.raises(ReliableReportingConfigurationError, match="already has another"):
        ReliableReportingService.memory(account_context=_account_context).install(installed)
    handler, executor, _ = create_adcp_server_from_platform(installed, validate_at_init=False)
    await service.start()
    token = current_principal.set(None)
    try:
        tools = {item["name"] for item in get_tools_for_handler(handler, _include_schemas=False)}
        assert {"get_reporting_status", "sync_reporting_status", "get_media_buy_delivery"} <= tools
        assert "sync_reporting_receipts" not in tools
        capabilities = await handler.get_adcp_capabilities(GetAdcpCapabilitiesRequest())
        assert capabilities["media_buy"]["reporting_delivery"] == service.capability_block()

        statement = _statement(
            delivery_config_id="gam-delivery",
            report_definition_id="PAID_MEDIA_DAILY_V1",
            period={
                "start": "2026-11-01T02:00:00Z",
                "end": "2026-11-01T03:00:00Z",
                "source_timezone": "UTC",
            },
            status_as_of="2026-11-01T03:10:00Z",
        )
        sync = SyncReportingStatusRequest.model_validate(
            {"account": REF, "idempotency_key": "reporting-status-0001", "statuses": [statement]}
        )
        result = await handler.sync_reporting_status(sync, context_for(buyer_1))
        assert result["results"][0]["result"] == "recorded"
        own = await service.store.list_consumer_statuses(account_id=ACCOUNT, consumer_id=buyer_1)
        assert len(own) == 1
        status_request = GetReportingStatusRequest.model_validate(
            {"account": REF, "view": "periods"}
        )
        other = await handler.get_reporting_status(status_request, context_for(buyer_2))
        assert other["consumer_statuses"] == []
        result = await handler.sync_reporting_status(sync, context_for(buyer_2))
        assert result["results"][0]["result"] == "recorded"
        assert (
            len(await service.store.list_consumer_statuses(account_id=ACCOUNT, consumer_id=buyer_2))
            == 1
        )

        turn = await service.run_worker()
        assert not turn.configuration_errors
        (revision_id,) = turn.configurations[
            replace(_configuration(), consumer_id=buyer_2).generation_key
        ].revisions_committed
        (foreign_revision_id,) = turn.configurations[
            replace(_configuration(), consumer_id=buyer_1).generation_key
        ].revisions_committed
        assert revision_id != foreign_revision_id
        exact = GetMediaBuyDeliveryRequest.model_validate(
            {"account": REF, "reporting_revision_id": revision_id}
        )
        content = await handler.get_media_buy_delivery(exact, context_for(buyer_2))
        assert content["reporting_rows"] == _rows(10)
        # Reporting doesn't instantiate lazy tenants or fall through to their
        # aggregate provider, which might return a different reporting scope.
        assert creations == []
        assert child.delivery_threads == []

        for method, request in (
            (handler.get_reporting_status, status_request),
            (handler.get_media_buy_delivery, exact),
            (handler.sync_reporting_status, sync),
        ):
            with pytest.raises(AdcpError) as error:
                await method(request, context_for("denied-buyer"))
            assert error.value.code == "UNAUTHORIZED"
        with pytest.raises(AdcpError) as error:
            await handler.get_media_buy_delivery(
                exact.model_copy(update={"account": {"account_id": "other-account"}}),
                context_for(buyer_1),
            )
        assert error.value.code == "UNAUTHORIZED"

        aggregate = await handler.get_media_buy_delivery(
            GetMediaBuyDeliveryRequest.model_validate({"account": REF}), context_for(buyer_1)
        )
        assert aggregate == {"media_buy_deliveries": []}
        assert child.delivery_threads and child.delivery_threads[0] != threading.get_ident()
        products = await handler.get_products(
            GetProductsRequest.model_validate(
                {"account": REF, "buying_mode": "brief", "brief": "A campaign"}
            ),
            context_for(buyer_1),
        )
        assert products["products"][0]["product_id"] == "prod-reporting"
        assert creations == (["tenant-a"] if lazy else [])
        assert all(principal is not None for _, principal in child.accounts.calls)
    finally:
        current_principal.reset(token)
        await service.close()
        executor.shutdown(wait=True)


async def test_first_boot_capabilities_do_not_require_account_generation_or_tenant() -> None:
    service = ReliableReportingService.memory(account_context=_account_context)
    offering = _capability_offering("gam")
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(redacted_capabilities(), []),
        capability_offerings=[offering],
    )
    offering["schedule"]["delivery_sla"] = "P10D"  # registration is a frozen declaration
    router = LazyPlatformRouter(
        accounts=_AuthorizedAccounts(),
        factory=lambda _: pytest.fail("discovery must not instantiate a tenant"),
        capabilities=_SalesPlatform.capabilities,
    )
    handler, executor, _ = create_adcp_server_from_platform(
        service.install(router), validate_at_init=False
    )
    try:
        declared = service.capability_block()
        assert declared["offerings"]
        before = await handler.get_adcp_capabilities(GetAdcpCapabilitiesRequest())
        assert before["media_buy"]["reporting_delivery"] == declared
        await service.start()
        assert await service.store.list_all_configurations() == ()
        response = await handler.get_adcp_capabilities(GetAdcpCapabilitiesRequest())
        block = response["media_buy"]["reporting_delivery"]
        assert block["offerings"][0]["schedule"]["delivery_sla"] == "PT10M"
        validator = get_named_validator("core/reporting-delivery-capabilities.json")
        assert validator is not None
        validator.validate(block)
        await service.configure(_configuration())
        assert service.capability_block() == block
    finally:
        await service.close()
        executor.shutdown(wait=True)
    assert service.capability_block() == declared
    after = await handler.get_adcp_capabilities(GetAdcpCapabilitiesRequest())
    assert after["media_buy"]["reporting_delivery"] == declared


def test_installed_platform_still_fails_idempotency_boot_guard() -> None:
    platform = _SalesPlatform()
    platform.capabilities = _caps_with_idempotency(supported=True)
    service = ReliableReportingService.memory(account_context=_account_context)
    with ThreadPoolExecutor(max_workers=2) as executor:
        with pytest.raises(AdcpError, match="[Ii]dempotency"):
            create_adcp_server_from_platform(
                service.install(platform),
                executor=executor,
                timed_sync_get_products_limit=1,
                validate_at_init=False,
            )


def test_uninstalled_platform_does_not_advertise_reporting_tasks() -> None:
    handler, executor, _ = create_adcp_server_from_platform(
        _SalesPlatform(), validate_at_init=False
    )
    try:
        tools = {item["name"] for item in get_tools_for_handler(handler, _include_schemas=False)}
        assert (
            not {"get_reporting_status", "sync_reporting_status", "sync_reporting_receipts"} & tools
        )
    finally:
        executor.shutdown(wait=True)
