"""Adopter-facing ReliableReportingService composition and adapter tooling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from adcp.reporting.conformance import ReportingSourceConformanceError
from adcp.reporting.fixtures import (
    SNAPSHOT_OFFERING_ID,
    redacted_capabilities,
    redacted_snapshot_request,
)
from adcp.reporting.ledger import (
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingDefinitionBinding,
    ReportingScheduleSpec,
    ReportingStatusCaller,
)
from adcp.reporting.service import (
    ReliableReportingConfigurationError,
    ReliableReportingService,
    ReportingAccountContext,
)
from adcp.reporting.testing import (
    DeterministicReportingClock,
    ScriptedReportingAdapter,
    run_reporting_adapter_conformance,
)
from adcp.server import ADCPHandler
from adcp.server.mcp_tools import get_tools_for_handler
from adcp.types import GetMediaBuyDeliveryResponse
from adcp.validation.schema_loader import get_named_validator

NOW = datetime(2026, 11, 1, 3, 10, tzinfo=timezone.utc)
SCHEDULE = ReportingScheduleSpec(period_duration="PT1H", delivery_sla="PT10M", alignment="utc")
DEFINITION = ReportingDefinitionBinding(
    report_definition_uri="https://contracts.example.test/reporting/paid-media-daily-v1",
    report_definition_sha256="a" * 64,
    schema_version="1.0.0",
    schema_uri="https://contracts.example.test/reporting/paid-media-daily-v1/schema",
    schema_sha256="b" * 64,
)


def _generation_key(route: str) -> ReportingConfigurationGenerationKey:
    return ReportingConfigurationGenerationKey(
        account_id="account-redacted",
        delivery_config_id=f"{route}-delivery",
        delivery_config_version=1,
    )


def _configuration(
    route: str = "gam", *, account_id: str = "account-redacted"
) -> ReportingConfiguration:
    return ReportingConfiguration(
        delivery_config_id=f"{route}-delivery",
        delivery_config_version=1,
        account_id=account_id,
        report_definition_id="PAID_MEDIA_DAILY_V1",
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=SCHEDULE,
        required_finality="snapshot",
        activated_at=datetime(2026, 11, 1, 1, 20, tzinfo=timezone.utc),
        media_buy_ids=("media-buy-redacted",),
        definition=DEFINITION,
    )


def _capability_offering(route: str) -> dict[str, Any]:
    return {
        "offering_id": f"{route.upper()}_SNAPSHOT_V1",
        "feed_purpose": "analytics",
        "report_definition_id": "PAID_MEDIA_DAILY_V1",
        "report_definition_uri": DEFINITION.report_definition_uri,
        "report_definition_sha256": DEFINITION.report_definition_sha256,
        "reporting_profile": {
            "id": "paid_media_delivery",
            "version": DEFINITION.schema_version,
            "schema_uri": DEFINITION.schema_uri,
            "schema_sha256": DEFINITION.schema_sha256,
            "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
            "schema_ref_policy": "local_fragment_only",
            "grain": "one row per media buy per period",
            "primary_keys": ["media_buy_id"],
        },
        "schedule": {
            "period_duration": "PT1H",
            "delivery_sla": "PT10M",
            "alignment": "utc",
        },
        "supported_finality": ["snapshot"],
        "reconciliation_mode": "delivery_only",
    }


def _account_context(configuration: ReportingConfiguration) -> ReportingAccountContext:
    route = configuration.delivery_config_id.removesuffix("-delivery")
    capabilities = redacted_capabilities()
    return ReportingAccountContext(
        account_id=configuration.account_id,
        adapter=route,
        currency="USD",
        account_timezone=configuration.account_timezone,
        source_scope=capabilities.source_scope,
        snapshot_offering_id=SNAPSHOT_OFFERING_ID,
        requested_dimensions=("campaign_id",),
        capability_offering=_capability_offering(route),
        publication_namespace="reporting-source:fixture",
    )


def _rows(impressions: int) -> list[dict[str, Any]]:
    return [
        {
            "media_buy_id": "media-buy-redacted",
            "campaign_id": "campaign-redacted-1",
            "impressions": impressions,
            "spend": "1.25",
        }
    ]


async def test_service_routes_two_frozen_generations_and_resolves_each_once() -> None:
    clock = DeterministicReportingClock(NOW)
    resolutions: list[str] = []

    def resolve(configuration: ReportingConfiguration) -> ReportingAccountContext:
        resolutions.append(configuration.delivery_config_id)
        return _account_context(configuration)

    service = ReliableReportingService.memory(account_context=resolve, clock=clock)
    gam = ScriptedReportingAdapter(redacted_capabilities(), [_rows(10)])
    freewheel = ScriptedReportingAdapter(redacted_capabilities(), [_rows(20)])
    service.sources.register("gam", gam)
    service.sources.register("freewheel", freewheel)
    gam_config = _configuration("gam")
    freewheel_config = _configuration("freewheel")
    await service.configure(gam_config)
    await service.configure(freewheel_config)
    await service.configure(gam_config)  # the frozen generation is idempotent

    turn = await service.run_worker(now=NOW)

    assert resolutions == ["gam-delivery", "freewheel-delivery"]
    assert set(turn.configurations) == {
        _generation_key("gam"),
        _generation_key("freewheel"),
    }
    assert all(item.revisions_committed for item in turn.configurations.values())
    assert len(gam.calls) == len(freewheel.calls) == 1
    assert gam.calls[0].identity.delivery_config_id == "gam-delivery"
    assert freewheel.calls[0].identity.delivery_config_id == "freewheel-delivery"


async def test_service_serves_status_and_exact_revision_content() -> None:
    clock = DeterministicReportingClock(NOW)
    service = ReliableReportingService.memory(
        account_context=_account_context,
        caller_resolver=lambda _request, _context: ReportingStatusCaller(
            account_id="account-redacted", consumer_id="buyer-1"
        ),
        clock=clock,
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(17)]))
    await service.configure(_configuration())
    turn = await service.run_worker(now=NOW)
    revision_id = turn.configurations[_generation_key("gam")].revisions_committed[0]

    status = await service.get_reporting_status({"view": "periods"})
    assert status["revisions"][0]["reporting_revision_id"] == revision_id

    exact = await service.get_revision_content(
        {
            "reporting_revision_id": revision_id,
            "pagination": {"limit": 100},
            "context": {"trace": "kept"},
        }
    )
    parsed = GetMediaBuyDeliveryResponse.model_validate(exact)
    assert parsed.reporting_revision_binding.reporting_revision_id == revision_id
    assert exact["reporting_rows"][0]["impressions"] == 17
    assert exact["context"] == {"trace": "kept"}


async def test_install_preserves_handlers_and_intercepts_only_exact_reads() -> None:
    clock = DeterministicReportingClock(NOW)
    service = ReliableReportingService.memory(
        account_context=_account_context,
        caller_resolver=lambda _request, _context: ReportingStatusCaller(
            account_id="account-redacted", consumer_id="buyer-1"
        ),
        clock=clock,
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(7)]))
    await service.configure(_configuration())
    turn = await service.run_worker(now=NOW)
    revision_id = turn.configurations[_generation_key("gam")].revisions_committed[0]

    class Seller(ADCPHandler):
        async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
            return {"supported_protocols": ["media_buy"]}

        async def get_products(self, params: Any, context: Any = None) -> Any:
            return {"products": [{"product_id": "kept"}]}

        async def get_media_buy_delivery(self, params: Any, context: Any = None) -> Any:
            return {"legacy": True}

    seller = service.install(Seller())
    assert (await seller.get_products({}))["products"][0]["product_id"] == "kept"
    assert await seller.get_media_buy_delivery({}) == {"legacy": True}
    exact = await seller.get_media_buy_delivery({"reporting_revision_id": revision_id})
    assert exact["reporting_revision_binding"]["reporting_revision_id"] == revision_id
    capabilities = await seller.get_adcp_capabilities({})
    assert capabilities["media_buy"]["reporting_delivery"]["supported"] is True
    tools = {item["name"] for item in get_tools_for_handler(seller, _include_schemas=False)}
    assert {"get_products", "get_reporting_status", "get_media_buy_delivery"} <= tools


@pytest.mark.parametrize("consumer_status_enabled", [False, True])
async def test_capability_block_is_schema_valid_and_only_advertises_installed_tiers(
    consumer_status_enabled: bool,
) -> None:
    service = ReliableReportingService.memory(
        account_context=_account_context, consumer_status_enabled=consumer_status_enabled
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(1)]))
    await service.configure(_configuration())
    block = service.capability_block()

    assert block["managed_delivery"] is False
    assert block["reconciled_billing"] is False
    assert "receipt_task" not in block
    if consumer_status_enabled:
        assert block["consumer_status_task"] == "sync_reporting_status"
    else:
        assert "consumer_status_task" not in block
    validator = get_named_validator("core/reporting-delivery-capabilities.json")
    assert validator is not None
    assert list(validator.iter_errors(block)) == []


async def test_startup_rejects_impossible_component_combinations() -> None:
    class Worker:
        async def run_once(self) -> None:
            return None

    class Receipts:
        async def handle(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            return request

    with pytest.raises(ReliableReportingConfigurationError, match="attempt storage"):
        await ReliableReportingService.memory(
            account_context=_account_context, notification_worker=Worker()
        ).initialize()
    with pytest.raises(ReliableReportingConfigurationError, match="managed delivery"):
        await ReliableReportingService.memory(
            account_context=_account_context, receipt_handler=Receipts()
        ).initialize()
    with pytest.raises(ReliableReportingConfigurationError, match="reconciled billing"):
        await ReliableReportingService.memory(
            account_context=_account_context, reconciled_billing=True
        ).initialize()


async def test_background_worker_reports_an_error_and_recovers_on_the_next_turn() -> None:
    recovered = asyncio.Event()
    errors: list[tuple[str, str]] = []

    class FlakyWorker:
        calls = 0

        async def run_once(self) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary materializer failure")
            recovered.set()
            return "recovered"

    async def capture(component: str, error: BaseException) -> None:
        errors.append((component, str(error)))

    service = ReliableReportingService.memory(
        account_context=_account_context,
        materialization_worker=FlakyWorker(),
        worker_interval=timedelta(milliseconds=1),
        worker_error_handler=capture,
    )
    await service.start()
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
    finally:
        await service.close()

    assert errors == [("materialization", "temporary materializer failure")]


async def test_configuration_rejects_unregistered_routes_and_mutated_generations() -> None:
    service = ReliableReportingService.memory(account_context=_account_context)
    with pytest.raises(ReliableReportingConfigurationError, match="unregistered"):
        await service.configure(_configuration())

    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(1)]))
    configuration = _configuration()
    await service.configure(configuration)
    changed = ReportingConfiguration(
        **{
            **configuration.__dict__,
            "media_buy_ids": ("another-media-buy",),
        }
    )
    with pytest.raises(ReliableReportingConfigurationError, match="other facts"):
        await service.configure(changed)


def test_account_context_validates_currency_timezone_and_frozen_scope() -> None:
    context = _account_context(_configuration())
    with pytest.raises(TypeError):
        context.source_scope["provider"] = "changed"  # type: ignore[index]
    with pytest.raises(ReliableReportingConfigurationError, match="currency"):
        ReportingAccountContext(
            account_id="account-redacted",
            adapter="gam",
            currency="usd",
            source_scope={},
            snapshot_offering_id=SNAPSHOT_OFFERING_ID,
        )
    with pytest.raises(ReliableReportingConfigurationError, match="timezone"):
        ReportingAccountContext(
            account_id="account-redacted",
            adapter="gam",
            currency="USD",
            source_scope={},
            account_timezone="Mars/Olympus",
            snapshot_offering_id=SNAPSHOT_OFFERING_ID,
        )


async def test_adapter_conformance_covers_sync_async_and_failure_injection() -> None:
    request = redacted_snapshot_request()
    sync_adapter = ScriptedReportingAdapter(redacted_capabilities(), [_rows(3)])
    sync_manifest = await run_reporting_adapter_conformance(sync_adapter, request)
    assert sync_manifest.row_count == 1
    assert len(sync_adapter.calls) == 1  # the replay comes from the immutable seal

    async_adapter = ScriptedReportingAdapter(redacted_capabilities(), [_rows(4)], asynchronous=True)
    async_manifest = await run_reporting_adapter_conformance(async_adapter, request)
    assert async_manifest.control_totals[0].value == "4"

    failing = ScriptedReportingAdapter(redacted_capabilities(), [RuntimeError("provider down")])
    with pytest.raises(ReportingSourceConformanceError) as captured:
        await run_reporting_adapter_conformance(failing, request)
    assert captured.value.code == "EXECUTION_FAILED"


def test_deterministic_clock_is_aware_and_monotonic() -> None:
    clock = DeterministicReportingClock(NOW)
    assert clock.advance(timedelta(minutes=5)) == NOW + timedelta(minutes=5)
    with pytest.raises(ValueError, match="cannot move backward"):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError, match="aware"):
        clock.set(datetime(2026, 1, 1))


@dataclass
class _CallerContext:
    account_id: str
    caller_identity: str


async def test_default_caller_requires_trusted_transport_identity() -> None:
    service = ReliableReportingService.memory(account_context=_account_context)
    caller = await service.caller_for(
        {}, _CallerContext(account_id="account-redacted", caller_identity="buyer-1")
    )
    assert caller == ReportingStatusCaller(account_id="account-redacted", consumer_id="buyer-1")
    with pytest.raises(ReliableReportingConfigurationError, match="authenticated"):
        await service.caller_for({}, SimpleNamespace(account_id="account-redacted"))
