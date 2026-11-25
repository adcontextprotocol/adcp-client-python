"""Adopter-facing ReliableReportingService composition and adapter tooling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from adcp.reporting.conformance import ReportingSourceConformanceError
from adcp.reporting.fixtures import (
    OFFICIAL_OFFERING_ID,
    SNAPSHOT_OFFERING_ID,
    redacted_capabilities,
    redacted_snapshot_request,
)
from adcp.reporting.inline_source import InlineFetchResult
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingContractualCutoffPolicy,
    ReportingDefinitionBinding,
    ReportingProducer,
    ReportingScheduleSpec,
    ReportingSourceFinalPolicy,
    ReportingStatusCaller,
)
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.service import (
    ProducerFactory,
    ReliableReportingConfigurationError,
    ReliableReportingService,
    ReliableReportingState,
    ReportingAccountContext,
)
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    reporting_source_capabilities_sha256_v1,
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

# The snapshot fixture declares a thirty-minute availability lag.
NOW = datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)
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
        consumer_id="buyer-1",
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
        consumer_id="buyer-1",
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


def _account_binding(
    label: str,
) -> tuple[
    ReportingConfiguration,
    ReportingAccountContext,
    ReportingSourceCapabilitiesV1,
    dict[str, Any],
]:
    definition = replace(
        DEFINITION,
        report_definition_uri=f"https://contracts.example.test/reporting/{label}",
        report_definition_sha256=label * 64,
    )
    configuration = replace(_configuration(account_id=f"account-{label}"), definition=definition)
    raw = redacted_capabilities().model_dump(mode="json")
    raw["source_scope"]["source_account_id"] = configuration.account_id
    raw["capability_version"] = f"fixture-{label}"
    for offering in raw["offerings"]:
        offering["offering_id"] = f"{offering['offering_id']}_{label}"
        offering["source_timezone"] = "UTC"
        offering["product_ids"] = [configuration.report_definition_id]
        offering["contract"]["report_definition_uri"] = definition.report_definition_uri
        offering["contract"]["report_definition_sha256"] = definition.report_definition_sha256
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    capabilities = ReportingSourceCapabilitiesV1.model_validate(raw)
    public = _capability_offering("gam")
    public["offering_id"] = f"GAM_SNAPSHOT_{label}"
    public["report_definition_uri"] = definition.report_definition_uri
    public["report_definition_sha256"] = definition.report_definition_sha256
    context = replace(
        _account_context(configuration),
        source_scope=capabilities.source_scope,
        snapshot_offering_id=capabilities.offerings[0].offering_id,
        capability_offering=public,
        source_capabilities=capabilities,
    )
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    return configuration, context, capabilities, public


def _capabilities_with_digest(
    capabilities: ReportingSourceCapabilitiesV1, digest: str
) -> ReportingSourceCapabilitiesV1:
    raw = capabilities.model_dump(mode="json")
    for offering in raw["offerings"]:
        offering["contract"]["report_definition_sha256"] = digest
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    return ReportingSourceCapabilitiesV1.model_validate(raw)


def _rows(impressions: int) -> list[dict[str, Any]]:
    return [
        {
            "media_buy_id": "media-buy-redacted",
            "campaign_id": "campaign-redacted-1",
            "impressions": impressions,
            "spend": "1.25",
        }
    ]


def _finality_service(
    configuration: ReportingConfiguration,
    *,
    snapshot: bool,
    official: bool,
    close: bool = False,
    store: InMemoryReportingLedgerStore | None = None,
    producer_factory: ProducerFactory = ReportingProducer,
    results: tuple[InlineFetchResult | None, ...] = (),
    clock: DeterministicReportingClock | None = None,
    source_format: str | None = None,
    cadence: str | None = None,
    products: tuple[str, ...] = (),
    adapter_type: type[ScriptedReportingAdapter] = ScriptedReportingAdapter,
) -> ReliableReportingService:
    public = _capability_offering("gam")
    public["supported_finality"] = [configuration.required_finality]
    context = replace(
        _account_context(configuration),
        snapshot_offering_id=SNAPSHOT_OFFERING_ID if snapshot else None,
        official_offering_id=OFFICIAL_OFFERING_ID if official else None,
        capability_offering=public,
    )
    raw = redacted_capabilities().model_dump(mode="json")
    for offering in raw["offerings"]:
        offering.update(
            source_timezone="UTC", product_ids=products or [configuration.report_definition_id]
        )
        offering["windowing"]["minimum_window"] = "PT1H"
    if close:
        raw["offerings"][0].update(restatement_window="P1D", official_close_lag="P2D")
    if cadence is not None:
        raw["offerings"][0].update(restatement_window="P1D", restatement_cadence=cadence)
    raw["offerings"] = [
        offering for offering, enabled in zip(raw["offerings"], (snapshot, official)) if enabled
    ]
    if source_format:
        for offering in raw["offerings"]:
            offering["formats"] = [{"media_type": source_format, "compression": "none"}]
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    adapter = adapter_type(ReportingSourceCapabilitiesV1.model_validate(raw), results)
    service = ReliableReportingService(
        store=store or InMemoryReportingLedgerStore(),
        account_context=lambda _: context,
        producer_factory=producer_factory,
        clock=clock,
    )
    service.sources.register("gam", adapter, capability_offerings=[public])
    return service


async def test_official_configuration_requires_a_selected_finality_policy() -> None:
    configuration = replace(_configuration(), required_finality="official")
    public = {**_capability_offering("gam"), "supported_finality": ["official"]}
    context = replace(
        _account_context(configuration),
        snapshot_offering_id=None,
        official_offering_id=OFFICIAL_OFFERING_ID,
        capability_offering=public,
    )
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register(
        "gam", ScriptedReportingAdapter(redacted_capabilities(), []), capability_offerings=[public]
    )

    with pytest.raises(ReliableReportingConfigurationError, match="selected finality policy"):
        await service.configure(configuration)

    snapshot = await service.store.read_status_snapshot(
        caller=ReportingStatusCaller(configuration.account_id, configuration.consumer_id)
    )
    assert snapshot.configurations == ()


async def test_snapshot_close_requires_a_selected_finality_policy() -> None:
    configuration = _configuration()
    capabilities = redacted_capabilities().model_dump(mode="json")
    capabilities["offerings"][0].update(restatement_window="P1D", official_close_lag="P2D")
    capabilities["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(capabilities)
    service = ReliableReportingService.memory(account_context=_account_context)
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(ReportingSourceCapabilitiesV1.model_validate(capabilities), []),
    )

    with pytest.raises(ReliableReportingConfigurationError, match="selected finality policy"):
        await service.configure(configuration)


async def test_cutoff_policy_rejects_an_authoritative_only_source() -> None:
    configuration = replace(
        _configuration(),
        required_finality="official",
        definition=replace(
            DEFINITION,
            finality_policy=ReportingContractualCutoffPolicy("cutoff-v1", "P2D"),
        ),
    )
    public = {**_capability_offering("gam"), "supported_finality": ["official"]}
    context = replace(
        _account_context(configuration),
        snapshot_offering_id=None,
        official_offering_id=OFFICIAL_OFFERING_ID,
        capability_offering=public,
    )
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register(
        "gam", ScriptedReportingAdapter(redacted_capabilities(), []), capability_offerings=[public]
    )

    with pytest.raises(ReliableReportingConfigurationError, match="snapshot source offering"):
        await service.configure(configuration)


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


async def test_account_capabilities_bind_distinct_offerings_with_shared_source_storage() -> None:
    first, first_context, first_capabilities, first_public = _account_binding("c")
    second, second_context, second_capabilities, _ = _account_binding("d")
    second = replace(second, automated_recovery_window=timedelta(days=2), status_retention_days=100)
    contexts = {first.account_id: first_context, second.account_id: second_context}
    service = ReliableReportingService.memory(
        account_context=lambda configuration: contexts[configuration.account_id]
    )
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(first_capabilities, [_rows(10), _rows(20)]),
    )

    await service.configure(first)
    await service.configure(second)

    first_source = service._bindings[first.generation_key].producer._source
    second_source = service._bindings[second.generation_key].producer._source
    assert first_source.capabilities == first_capabilities
    assert second_source.capabilities == second_capabilities
    assert first_source._staging is second_source._staging
    assert first_source._seals is second_source._seals
    assert [
        item["offering_id"]
        for item in service.capability_block(account_id=first.account_id)["offerings"]
    ] == [first_public["offering_id"]]
    assert [
        item["offering_id"]
        for item in service.capability_block(account_id=second.account_id)["offerings"]
    ] == [second_context.capability_offering["offering_id"]]
    first_block = service.capability_block(account_id=first.account_id)
    assert first_block["automated_recovery_window_seconds"] == int(
        first.automated_recovery_window.total_seconds()
    )
    assert first_block["status_retention_days"] == first.status_retention_days
    assert service.capability_block() == {}


async def test_account_capability_rejects_a_conflicting_registered_offering_id() -> None:
    configuration, context, capabilities, public = _account_binding("c")
    conflicting = {**public, "report_definition_sha256": "e" * 64}
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(_capabilities_with_digest(capabilities, "e" * 64), []),
        capability_offerings=[conflicting],
    )

    with pytest.raises(ReliableReportingConfigurationError, match="conflicting declarations"):
        await service.configure(configuration)


async def test_account_capability_rejects_reusing_an_offering_id_for_another_generation() -> None:
    first, context, capabilities, public = _account_binding("c")
    revised_context = replace(
        context,
        capability_offering={**public, "standard_dimensions": ["country"]},
    )
    second = replace(
        first,
        delivery_config_version=2,
        account_binding_sha256=revised_context.binding_sha256(),
    )
    contexts = {1: context, 2: revised_context}
    service = ReliableReportingService.memory(
        account_context=lambda configuration: contexts[configuration.delivery_config_version]
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    await service.configure(first)

    with pytest.raises(ReliableReportingConfigurationError, match="conflicting declarations"):
        await service.configure(second)
    assert first.generation_key in service._pending_bindings
    assert second.generation_key not in service._pending_bindings


async def test_account_capabilities_keep_the_registered_source_authority() -> None:
    configuration, context, capabilities, public = _account_binding("c")

    class AuthorizedAdapter(ScriptedReportingAdapter):
        def __init__(self) -> None:
            super().__init__(capabilities, [])
            self.checked: list[ReportingConfigurationGenerationKey] = []

        def configuration_binding(self, item: ReportingConfiguration) -> None:
            self.checked.append(item.generation_key)
            return None

    adapter = AuthorizedAdapter()
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register("gam", adapter, capability_offerings=[public])
    await service.configure(configuration)

    source = service._bindings[configuration.generation_key].producer._source
    assert source.configuration_binding(configuration) is None
    assert adapter.checked == [configuration.generation_key]


async def test_account_capabilities_bind_a_selected_official_source() -> None:
    configuration, context, capabilities, public = _account_binding("c")
    configuration = replace(
        configuration,
        required_finality="official",
        schedule=replace(configuration.schedule, period_duration="P1D"),
        definition=replace(
            configuration.definition,
            finality_policy=ReportingSourceFinalPolicy("declared-close", "delivery.is_final"),
        ),
    )
    context = replace(
        context,
        snapshot_offering_id=None,
        official_offering_id=capabilities.offerings[1].offering_id,
        capability_offering={
            **public,
            "schedule": {**public["schedule"], "period_duration": "P1D"},
            "supported_finality": ["official"],
        },
    )
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(capabilities, []),
    )

    await service.configure(configuration)

    source = service._bindings[configuration.generation_key].producer._source
    assert source.capabilities.capabilities_sha256 == capabilities.capabilities_sha256
    assert configuration.generation_key in service._bindings


async def test_decisioning_capabilities_include_only_the_resolved_account_offering() -> None:
    from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform, SingletonAccounts
    from adcp.decisioning.capabilities import Account, SupportedProtocol
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.server import AccountAwareToolContext

    first, first_context, _first_source, first_public = _account_binding("a")
    second, second_context, _second_source, second_public = _account_binding("b")
    contexts = {first.account_id: first_context, second.account_id: second_context}
    service = ReliableReportingService.memory(
        account_context=lambda item: contexts[item.account_id]
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    await service.configure(first)
    await service.configure(second)

    class Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            supported_protocols=[SupportedProtocol.media_buy],
            account=Account(supported_billing=["agent"]),
        )
        accounts = SingletonAccounts(account_id=first.account_id)

    handler, executor, _registry = create_adcp_server_from_platform(
        service.install(Seller()), validate_at_init=False, auto_emit_completion_webhooks=False
    )
    try:
        response = await handler.get_adcp_capabilities(
            {}, AccountAwareToolContext(account_id=first.account_id)
        )
    finally:
        executor.shutdown(wait=True)
        await service.close()

    offering_ids = {
        item["offering_id"] for item in response["media_buy"]["reporting_delivery"]["offerings"]
    }
    assert first_public["offering_id"] in offering_ids
    assert second_public["offering_id"] not in offering_ids


@pytest.mark.parametrize(
    "mismatch",
    [
        "source_scope",
        "definition",
        "public_contract",
        "source_class",
        "format",
        "window",
        "requested_metric",
        "requested_dimension",
        "product",
        "source_timezone",
        "adapter_build",
        "checksum",
        "publication_namespace",
        "schedule",
        "period_timezone",
        "period_anchor",
        "managed_method",
        "reconciliation_mode",
        "unsupported_finality",
    ],
)
async def test_account_capability_mismatch_is_rejected_before_binding(mismatch: str) -> None:
    configuration, context, capabilities, public = _account_binding("c")
    if mismatch == "source_scope":
        context = replace(context, source_scope={"source_account_id": "another-account"})
    elif mismatch == "definition":
        context = replace(
            context, source_capabilities=_capabilities_with_digest(capabilities, "e" * 64)
        )
    elif mismatch == "public_contract":
        context = replace(
            context,
            capability_offering={**public, "report_definition_sha256": "e" * 64},
        )
    elif mismatch == "source_class":
        context = replace(context, snapshot_offering_id=capabilities.offerings[1].offering_id)
    elif mismatch == "requested_metric":
        context = replace(context, requested_metrics=("impressions", "clicks"))
    elif mismatch == "requested_dimension":
        context = replace(context, requested_dimensions=("missing_dimension",))
    elif mismatch == "publication_namespace":
        context = replace(context, publication_namespace="reporting-source:other")
    elif mismatch == "schedule":
        context = replace(
            context,
            capability_offering={
                **public,
                "schedule": {**public["schedule"], "delivery_sla": "PT15M"},
            },
        )
    elif mismatch in {"period_timezone", "period_anchor"}:
        field_name = "period_timezone" if mismatch == "period_timezone" else "period_anchor"
        field_value = "Europe/Berlin" if mismatch == "period_timezone" else "2026-11-01T00:00:00Z"
        context = replace(
            context,
            capability_offering={
                **public,
                "schedule": {**public["schedule"], field_name: field_value},
            },
        )
    elif mismatch == "managed_method":
        context = replace(
            context,
            capability_offering={
                **public,
                "method": {
                    "pattern": "file_transfer",
                    "transport": "https",
                    "orchestration": "producer_managed",
                    "destination_modes": ["existing"],
                },
            },
        )
    elif mismatch == "reconciliation_mode":
        context = replace(
            context, capability_offering={**public, "reconciliation_mode": "consumer_receipt"}
        )
    elif mismatch == "unsupported_finality":
        context = replace(
            context,
            capability_offering={**public, "supported_finality": ["snapshot", "official"]},
        )
    elif mismatch in {"format", "window", "source_timezone", "adapter_build", "product"}:
        raw = capabilities.model_dump(mode="json")
        if mismatch == "format":
            raw["offerings"][0]["formats"] = [
                {"media_type": "application/x-ndjson", "compression": "gzip"}
            ]
        elif mismatch == "window":
            raw["offerings"][0]["windowing"]["minimum_window"] = "P1D"
        elif mismatch == "source_timezone":
            raw["offerings"][0]["source_timezone"] = "America/New_York"
        elif mismatch == "product":
            raw["offerings"][0]["product_ids"] = ["another-product"]
        else:
            raw["adapter_build"]["adapter_version"] = "another-build"
        raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
        context = replace(
            context, source_capabilities=ReportingSourceCapabilitiesV1.model_validate(raw)
        )
    else:
        context = replace(
            context,
            source_capabilities=capabilities.model_copy(
                update={"capability_version": "edited-without-checksum"}
            ),
        )
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(capabilities, []),
        capability_offerings=[public],
    )
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())

    with pytest.raises(ReliableReportingConfigurationError):
        await service.configure(configuration)

    assert configuration.generation_key not in service._bindings
    assert configuration.generation_key not in service._pending_bindings


async def test_account_capabilities_recover_the_original_generation_contract() -> None:
    configuration, context, capabilities, public = _account_binding("c")
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()

    def service_for(resolved: ReportingAccountContext) -> ReliableReportingService:
        service = ReliableReportingService(store=store, account_context=lambda _: resolved)
        service.sources.register(
            "gam",
            ScriptedReportingAdapter(capabilities, []),
            capability_offerings=[public],
        )
        return service

    original = service_for(context)
    await original.configure(configuration)
    await original.initialize()

    recovered = service_for(context)
    await recovered.initialize()
    source = recovered._bindings[configuration.generation_key].producer._source
    assert source.capabilities.capabilities_sha256 == capabilities.capabilities_sha256

    changed = replace(
        context,
        source_capabilities=_capabilities_with_digest(capabilities, "e" * 64),
        capability_offering={**public, "report_definition_sha256": "e" * 64},
    )
    with pytest.raises(ReliableReportingConfigurationError):
        await service_for(changed).initialize()

    raw = capabilities.model_dump(mode="json")
    raw["offerings"][0]["fastest_safe_cadence"] = "PT48M"
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    changed_source = replace(
        context, source_capabilities=ReportingSourceCapabilitiesV1.model_validate(raw)
    )
    with pytest.raises(ReliableReportingConfigurationError):
        await service_for(changed_source).initialize()

    changed_public = replace(
        context, capability_offering={**public, "standard_dimensions": ["campaign_id"]}
    )
    with pytest.raises(ReliableReportingConfigurationError):
        await service_for(changed_public).initialize()
    assert await store.list_all_configurations() == (configuration,)


@pytest.mark.parametrize("window", [None, timedelta(0), timedelta(minutes=2)])
async def test_service_exposes_first_read_jitter_control(window: timedelta | None) -> None:
    service = ReliableReportingService.memory(
        account_context=_account_context, read_jitter_window=window
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(10)]))
    await service.configure(_configuration())
    assert service._bindings[_generation_key("gam")].producer._read_jitter_window == (
        timedelta(minutes=5) if window is None else window
    )


def test_service_refuses_negative_read_jitter_window() -> None:
    with pytest.raises(ReliableReportingConfigurationError, match="nonnegative"):
        ReliableReportingService.memory(
            account_context=_account_context, read_jitter_window=timedelta(seconds=-1)
        )


async def test_default_jitter_preserves_existing_custom_producer_factory_signature() -> None:
    from adcp.reporting.ledger import ReportingProducer

    def factory(*, source, offerings, store, object_reader, escalation, worker_id, clock):
        return ReportingProducer(
            source=source,
            offerings=offerings,
            store=store,
            object_reader=object_reader,
            escalation=escalation,
            worker_id=worker_id,
            clock=clock,
        )

    service = ReliableReportingService.memory(
        account_context=_account_context, producer_factory=factory
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(10)]))
    await service.configure(_configuration())
    assert service._bindings[_generation_key("gam")].producer._read_jitter_window == timedelta(
        minutes=5
    )


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
    capabilities = await seller.get_adcp_capabilities(
        {}, SimpleNamespace(account_id="account-redacted")
    )
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
    assert service.capability_block(account_id="account-redacted")["offerings"]
    await service.initialize()
    assert service.capability_block(account_id="account-redacted")["offerings"]
    await service.start()
    block = service.capability_block(account_id="account-redacted")

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
    await service.close()


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


async def test_worker_isolates_configuration_and_extension_errors_and_retries() -> None:
    configuration_error = LedgerConflictError("LEASE_LOST", "secret-store-body")
    errors: list[tuple[str, BaseException]] = []

    class FlakyLedger(InMemoryReportingLedgerStore):
        failed = False

        async def find_obligation(self, **kwargs: Any) -> Any:
            if kwargs["account_id"] == "account-a" and not self.failed:
                self.failed = True
                raise configuration_error
            return await super().find_obligation(**kwargs)

    class FlakyWorker:
        def __init__(self, name: str) -> None:
            self.name = name
            self.error = RuntimeError(f"secret-{name}-body")
            self.calls = 0

        async def run_once(self) -> str:
            self.calls += 1
            if self.calls == 1:
                raise self.error
            return self.name

    materializer = FlakyWorker("materialization")
    notifier = FlakyWorker("notification")
    service = ReliableReportingService(
        store=FlakyLedger(),
        account_context=_account_context,
        clock=lambda: NOW,
        materialization_worker=materializer,
        notification_worker=notifier,
        notification_attempt_store=object(),
        worker_error_handler=lambda component, error: errors.append((component, error)),
    )
    adapter = ScriptedReportingAdapter(redacted_capabilities(), [_rows(10), _rows(20)])
    service.sources.register("gam", adapter)
    failed_config = _configuration(account_id="account-a")
    healthy_config = _configuration(account_id="account-b")
    await service.configure(failed_config)
    await service.configure(healthy_config)
    try:
        first = await service.run_worker()
        assert first.configuration_errors == {failed_config.generation_key: configuration_error}
        assert set(first.configurations) == {healthy_config.generation_key}
        assert first.configurations[healthy_config.generation_key].revisions_committed
        assert first.extension_errors == {
            "materialization": materializer.error,
            "notification": notifier.error,
        }
        assert first.did_work
        assert errors == [
            ("configuration:account-a:gam-delivery@1", configuration_error),
            ("materialization", materializer.error),
            ("notification", notifier.error),
        ]
        assert service.ready
        assert service.failure is None

        recovered = await service.run_worker()
        assert not recovered.configuration_errors
        assert not recovered.extension_errors
        assert recovered.configurations[failed_config.generation_key].revisions_committed
        assert recovered.extension_results == ["materialization", "notification"]
        assert len(adapter.calls) == 2
        assert service.ready
    finally:
        await service.close()
    await service.wait()


async def test_background_worker_reports_an_error_and_recovers_on_the_next_turn() -> None:
    errors: list[tuple[str, BaseException]] = []
    failure = RuntimeError("temporary materializer failure")
    recovered = asyncio.Event()

    class FlakyWorker:
        calls = 0

        async def run_once(self) -> str:
            self.calls += 1
            if self.calls == 1:
                raise failure
            recovered.set()
            return "recovered"

    async def capture(component: str, error: BaseException) -> None:
        errors.append((component, error))

    worker = FlakyWorker()
    service = ReliableReportingService.memory(
        account_context=_account_context,
        materialization_worker=worker,
        worker_interval=timedelta(milliseconds=1),
        worker_error_handler=capture,
    )
    await service.start()
    try:
        await asyncio.wait_for(recovered.wait(), timeout=2)
        assert errors == [("materialization", failure)]
        assert worker.calls >= 2
        assert service.ready
        assert service.failure is None
    finally:
        await service.close()

    await service.wait()
    assert service.state is ReliableReportingState.CLOSED


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


@pytest.mark.parametrize("mismatch", ["definition", "schema", "profile"])
def test_public_offering_requires_matching_adapter_contract(mismatch: str) -> None:
    service = ReliableReportingService.memory(account_context=_account_context)
    offering = _capability_offering("gam")
    if mismatch == "definition":
        offering["report_definition_sha256"] = "c" * 64
    elif mismatch == "schema":
        offering["reporting_profile"]["schema_sha256"] = "c" * 64
    else:
        offering["reporting_profile"]["id"] = "other-profile"
    with pytest.raises(ReliableReportingConfigurationError, match="no matching snapshot"):
        service.sources.register(
            "gam",
            ScriptedReportingAdapter(redacted_capabilities(), []),
            capability_offerings=[offering],
        )
    assert service.sources.names == ()


async def test_public_offering_conflicts_are_rejected_and_identical_ids_are_deduplicated() -> None:
    service = ReliableReportingService.memory(account_context=_account_context)
    offering = _capability_offering("gam")
    for route in ("gam", "freewheel"):
        service.sources.register(
            route,
            ScriptedReportingAdapter(redacted_capabilities(), []),
            capability_offerings=[offering],
        )
    offering["schedule"]["delivery_sla"] = "P1D"
    with pytest.raises(ReliableReportingConfigurationError, match="conflicting declarations"):
        service.sources.register(
            "other",
            ScriptedReportingAdapter(redacted_capabilities(), []),
            capability_offerings=[offering],
        )
    try:
        await service.start()
        assert len(service.capability_block()["offerings"]) == 1
        assert service.capability_block()["offerings"][0]["schedule"]["delivery_sla"] == "PT10M"
    finally:
        await service.close()


async def test_configuration_must_honor_its_registered_public_contract() -> None:
    def resolve(configuration: ReportingConfiguration) -> ReportingAccountContext:
        offering = _capability_offering("gam")
        offering["schedule"]["delivery_sla"] = "P1D"
        return replace(_account_context(configuration), capability_offering=offering)

    service = ReliableReportingService.memory(account_context=resolve)
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(redacted_capabilities(), []),
        capability_offerings=[_capability_offering("gam")],
    )
    with pytest.raises(ReliableReportingConfigurationError, match="registered public offering"):
        await service.configure(_configuration())
    assert await service.store.list_all_configurations() == ()


@pytest.mark.parametrize("tier", ["managed", "reconciled"])
async def test_first_boot_offerings_require_their_installed_tier(tier: str) -> None:
    service = ReliableReportingService.memory(account_context=_account_context)
    offering = _capability_offering("gam")
    if tier == "managed":
        offering["method"] = {
            "pattern": "file_transfer",
            "transport": "https",
            "orchestration": "producer_managed",
            "destination_modes": ["existing"],
        }
    else:
        offering["reconciliation_mode"] = "consumer_receipt"
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(redacted_capabilities(), []),
        capability_offerings=[offering],
    )
    try:
        with pytest.raises(ReliableReportingConfigurationError, match="require"):
            await service.start()
        assert not service.ready
        assert service.capability_block()["offerings"]
    finally:
        await service.close()


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


@pytest.mark.parametrize("principal", [None, "other-buyer"])
async def test_default_caller_keeps_account_aware_context_after_account_resolution(
    principal: str | None,
) -> None:
    from adcp.server import AccountAwareToolContext
    from adcp.server.auth import current_principal
    from adcp.server.helpers import resolve_account_into_context

    account = SimpleNamespace(account_id="account-redacted")

    async def resolve(ref: Any) -> Any:
        assert ref == {"account_id": "account-redacted"}
        return account

    context = AccountAwareToolContext(caller_identity="buyer-1")
    request = {"account": {"account_id": "account-redacted"}, "consumer_id": "other-buyer"}
    assert await resolve_account_into_context(request, context, resolve) is None
    assert context.account is account
    service = ReliableReportingService.memory(account_context=_account_context)
    token = current_principal.set(principal)
    try:
        assert await service.caller_for(request, context) == ReportingStatusCaller(
            account_id="account-redacted", consumer_id="buyer-1"
        )
    finally:
        current_principal.reset(token)


async def test_decisioning_caller_uses_live_principal_not_account_cache_identity() -> None:
    from adcp.decisioning import Account, RequestContext
    from adcp.server.auth import current_principal

    service = ReliableReportingService.memory(account_context=_account_context)
    context = RequestContext(
        account=Account(id="account-redacted"),
        caller_identity="adopter.AccountStore:account-redacted",
        auth_principal="earlier-principal",
    )
    for principal in ("buyer-1", "buyer-2", None):
        token = current_principal.set(principal)
        try:
            # Caller-supplied identity fields must never choose the reader.
            request = {"account_id": "other-account", "consumer_id": "other-buyer"}
            if principal is None:
                with pytest.raises(ReliableReportingConfigurationError, match="authenticated"):
                    await service.caller_for(request, context)
            else:
                assert await service.caller_for(request, context) == ReportingStatusCaller(
                    account_id="account-redacted", consumer_id=principal
                )
        finally:
            current_principal.reset(token)


@pytest.mark.parametrize("with_packages", [False, True])
async def test_sdk_delivery_floats_publish_canonical_revision(with_packages: bool) -> None:
    response = GetMediaBuyDeliveryResponse.model_validate(
        {
            "reporting_period": {
                "start": "2026-11-01T02:00:00Z",
                "end": "2026-11-01T03:00:00Z",
            },
            "media_buy_deliveries": [
                {
                    "media_buy_id": "media-buy-redacted",
                    "status": "active",
                    "totals": {"impressions": 10.0, "spend": 1.25},
                    "by_package": (
                        [
                            {
                                "package_id": "pkg-1",
                                "pricing_model": "cpm",
                                "rate": 1.0,
                                "currency": "USD",
                                "impressions": 10.0,
                                "spend": 1.25,
                            }
                        ]
                        if with_packages
                        else []
                    ),
                }
            ],
        }
    )
    service = ReliableReportingService.memory(
        account_context=_account_context,
        caller_resolver=lambda *_: ReportingStatusCaller("account-redacted", "buyer-1"),
        clock=lambda: NOW,
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [response]))
    await service.configure(_configuration())
    try:
        turn = await service.run_worker()
        assert not turn.configuration_errors
        (revision_id,) = turn.configurations[_generation_key("gam")].revisions_committed
        content = await service.get_revision_content({"reporting_revision_id": revision_id})
        rows = content["reporting_rows"]
        assert rows[0]["spend"] == "1.25"
        assert rows[0]["impressions"] in (10, "10")
        from adcp.reporting.canonical_json import canonical_json_utf8_v1

        assert canonical_json_utf8_v1(rows)
    finally:
        await service.close()


async def test_unconfigured_service_declares_nothing_in_any_lifecycle_state():
    service = ReliableReportingService.memory(account_context=_account_context)
    assert service.capability_block() == {}
    await service.start()
    assert service.capability_block() == {}
    await service.close()
    assert service.capability_block() == {}


def test_shared_offering_refuses_two_snapshot_sources() -> None:
    public = {**_capability_offering("gam"), "supported_finality": ["snapshot", "official"]}
    raw = redacted_capabilities().model_dump(mode="json")
    raw["offerings"] = [raw["offerings"][0]]
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    first = ReportingSourceCapabilitiesV1.model_validate(raw)
    raw["offerings"][0]["offering_id"] = "SECOND_SNAPSHOT"
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    second = ReportingSourceCapabilitiesV1.model_validate(raw)
    service = ReliableReportingService.memory(account_context=_account_context)
    service.sources.register(
        "one", ScriptedReportingAdapter(first, []), capability_offerings=[public]
    )
    with pytest.raises(ReliableReportingConfigurationError):
        service.sources.register(
            "two", ScriptedReportingAdapter(second, []), capability_offerings=[public]
        )


def test_shared_offering_on_one_adapter_checks_both_source_slas() -> None:
    public = {**_capability_offering("gam"), "supported_finality": ["snapshot", "official"]}
    service = ReliableReportingService.memory(account_context=_account_context)
    service.sources.register(
        "gam", ScriptedReportingAdapter(redacted_capabilities(), []), capability_offerings=[public]
    )
    with pytest.raises(ReliableReportingConfigurationError, match="schedule"):
        service.capability_block()


@pytest.mark.parametrize("finality", ["snapshot", "official"])
async def test_shared_account_offering_recovery_selects_same_source(finality: str) -> None:
    configuration, context, capabilities, public = _account_binding("d")
    raw = capabilities.model_dump(mode="json")
    raw["offerings"][1]["windowing"]["minimum_window"] = "PT1H"
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    capabilities = ReportingSourceCapabilitiesV1.model_validate(raw)
    public = {
        **public,
        "offering_id": "SHARED_DAILY",
        "supported_finality": ["snapshot", "official"],
        "schedule": {**public["schedule"], "delivery_sla": "P7D"},
    }
    context = replace(
        context,
        source_capabilities=capabilities,
        capability_offering=public,
        official_offering_id=capabilities.offerings[1].offering_id,
    )
    configuration = replace(
        configuration,
        required_finality=finality,
        schedule=replace(configuration.schedule, delivery_sla="P7D"),
        definition=replace(
            configuration.definition,
            finality_policy=ReportingSourceFinalPolicy("policy", "is_final"),
        ),
        account_binding_sha256=context.binding_sha256(),
    )
    store = InMemoryReportingLedgerStore()

    def service() -> ReliableReportingService:
        result = ReliableReportingService(store=store, account_context=lambda _: context)
        result.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
        return result

    first = service()
    await first.configure(configuration)
    await first.initialize()
    expected = capabilities.offerings[0 if finality == "snapshot" else 1].offering_id
    assert (
        first._bindings[configuration.generation_key].producer._offerings.offering_for(finality)
        == expected
    )
    recovered = service()
    await recovered.initialize()
    assert (
        recovered._bindings[configuration.generation_key].producer._offerings.offering_for(finality)
        == expected
    )


@pytest.mark.parametrize("finality", ["snapshot", "official"])
async def test_shared_subscription_recovers_selected_registered_source(finality: str) -> None:
    from adcp.reporting.ledger.models import ReportingSubscriptionGeneration

    raw = redacted_capabilities().model_dump(mode="json")
    raw["offerings"][1]["windowing"]["minimum_window"] = "PT1H"
    public = {
        **_capability_offering("gam"),
        "supported_finality": ["snapshot", "official"],
        "schedule": {**_capability_offering("gam")["schedule"], "delivery_sla": "P7D"},
    }
    sources = []
    for offering in raw["offerings"]:
        payload = {**raw, "offerings": [offering]}
        payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
        sources.append(ReportingSourceCapabilitiesV1.model_validate(payload))
    config = replace(
        _configuration(),
        required_finality=finality,
        schedule=replace(SCHEDULE, delivery_sla="P7D"),
        definition=replace(
            DEFINITION, finality_policy=ReportingSourceFinalPolicy("policy", "is_final")
        ),
        subscription=ReportingSubscriptionGeneration("gam-delivery", public["offering_id"]),
        activated_at=None,
    )
    context = replace(
        _account_context(config),
        capability_offering=public,
        official_offering_id=OFFICIAL_OFFERING_ID,
    )
    store = InMemoryReportingLedgerStore()

    def service() -> ReliableReportingService:
        result = ReliableReportingService(store=store, account_context=lambda _: context)
        result.sources.register(
            "gam", ScriptedReportingAdapter(sources[0], []), capability_offerings=[public]
        )
        result.sources.register(
            "official", ScriptedReportingAdapter(sources[1], []), capability_offerings=[public]
        )
        return result

    first = service()
    await first.configure(config)
    await first.initialize()
    expected = SNAPSHOT_OFFERING_ID if finality == "snapshot" else OFFICIAL_OFFERING_ID
    assert (
        first._bindings[config.generation_key].producer._offerings.offering_for(finality)
        == expected
    )
    recovered = service()
    await recovered.initialize()
    assert (
        recovered._bindings[config.generation_key].producer._offerings.offering_for(finality)
        == expected
    )


@pytest.mark.parametrize("case", ["contract", "snapshot_sla", "official_sla"])
def test_shared_offering_rejects_incompatible_sources(case: str) -> None:
    public = {
        **_capability_offering("gam"),
        "supported_finality": ["snapshot", "official"],
        "schedule": {**_capability_offering("gam")["schedule"], "delivery_sla": "P7D"},
    }
    raw = redacted_capabilities().model_dump(mode="json")
    raw["offerings"][1]["windowing"]["minimum_window"] = "PT1H"
    if case == "contract":
        raw["offerings"][1]["contract"]["report_definition_sha256"] = "c" * 64
    else:
        raw["offerings"][0 if case == "snapshot_sla" else 1]["worst_case_availability_lag"] = "P8D"
    sources = []
    for offering in raw["offerings"]:
        payload = {**raw, "offerings": [offering]}
        payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
        sources.append(ReportingSourceCapabilitiesV1.model_validate(payload))
    service = ReliableReportingService.memory(account_context=_account_context)
    service.sources.register(
        "one", ScriptedReportingAdapter(sources[0], []), capability_offerings=[public]
    )
    with pytest.raises(ReliableReportingConfigurationError):
        service.sources.register(
            "two", ScriptedReportingAdapter(sources[1], []), capability_offerings=[public]
        )


async def test_subscription_account_binding_is_frozen_at_admission() -> None:
    from adcp.reporting.ledger.models import ReportingSubscriptionGeneration

    config, context, capabilities, public = _account_binding("d")
    config = replace(
        config,
        activated_at=None,
        account_binding_sha256=None,
        subscription=ReportingSubscriptionGeneration("gam-delivery", public["offering_id"]),
    )
    store = InMemoryReportingLedgerStore()
    service = ReliableReportingService(store=store, account_context=lambda _: context)
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    await service._install_subscription_generation(config, {})
    await service.initialize()
    (stored,) = await store.list_all_configurations()
    assert stored.account_binding_sha256 == context.binding_sha256()
    await service._install_subscription_generation(config, {})
    changed = replace(context, currency="EUR")
    restarted = ReliableReportingService(store=store, account_context=lambda _: changed)
    restarted.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    with pytest.raises(ReliableReportingConfigurationError, match="account binding"):
        await restarted.initialize()


def test_shared_account_offering_derives_both_source_ids() -> None:
    _, context, capabilities, public = _account_binding("d")
    public = {**public, "supported_finality": ["snapshot", "official"]}
    resolved = replace(
        context,
        capability_offering=public,
        snapshot_offering_id=None,
        official_offering_id=None,
    )
    assert resolved.snapshot_offering_id == capabilities.offerings[0].offering_id
    assert resolved.official_offering_id == capabilities.offerings[1].offering_id


@pytest.mark.parametrize("case", ["missing", "duplicate", "contract"])
def test_shared_account_offering_refuses_ambiguous_sources(case: str) -> None:
    _, context, capabilities, public = _account_binding("d")
    raw = capabilities.model_dump(mode="json")
    if case == "missing":
        raw["offerings"] = raw["offerings"][:1]
    elif case == "duplicate":
        duplicate = {**raw["offerings"][0], "offering_id": "OTHER_SNAPSHOT"}
        raw["offerings"].append(duplicate)
    else:
        raw["offerings"][1]["contract"]["report_definition_sha256"] = "e" * 64
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    with pytest.raises(ReliableReportingConfigurationError):
        replace(
            context,
            capability_offering={**public, "supported_finality": ["snapshot", "official"]},
            snapshot_offering_id=None,
            official_offering_id=None,
            source_capabilities=ReportingSourceCapabilitiesV1.model_validate(raw),
        )


async def test_direct_account_offering_preserves_snapshot_based_official_policy() -> None:
    configuration, context, capabilities, public = _account_binding("d")
    raw = capabilities.model_dump(mode="json")
    raw["offerings"][1]["windowing"]["minimum_window"] = "PT1H"
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    capabilities = ReportingSourceCapabilitiesV1.model_validate(raw)
    context = replace(
        context,
        source_capabilities=capabilities,
        official_offering_id=capabilities.offerings[1].offering_id,
        capability_offering={
            **public,
            "supported_finality": ["snapshot", "official"],
            "schedule": {**public["schedule"], "delivery_sla": "P7D"},
        },
    )
    configuration = replace(
        configuration,
        required_finality="official",
        schedule=replace(configuration.schedule, delivery_sla="P7D"),
        definition=replace(
            configuration.definition,
            finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT1H"),
        ),
        account_binding_sha256=context.binding_sha256(),
    )
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    await service.configure(configuration)


async def test_single_snapshot_offering_keeps_cutoff_finality() -> None:
    public = {**_capability_offering("gam"), "supported_finality": ["snapshot", "official"]}
    raw = redacted_capabilities().model_dump(mode="json")
    raw["offerings"] = raw["offerings"][:1]
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    context = replace(_account_context(_configuration()), capability_offering=public)
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(ReportingSourceCapabilitiesV1.model_validate(raw), []),
        capability_offerings=[public],
    )
    assert service.capability_block()["offerings"][0]["supported_finality"] == [
        "snapshot",
        "official",
    ]
    configuration = replace(
        _configuration(),
        required_finality="official",
        definition=replace(
            DEFINITION, finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT1H")
        ),
    )
    await service.configure(configuration)


async def test_subscription_rejects_a_different_advertised_account_offering() -> None:
    from adcp.reporting.ledger.models import ReportingSubscriptionGeneration

    config, context, capabilities, public = _account_binding("d")
    config = replace(
        config,
        activated_at=None,
        account_binding_sha256=None,
        subscription=ReportingSubscriptionGeneration("gam-delivery", "OTHER_OFFERING"),
    )
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    with pytest.raises(ReliableReportingConfigurationError, match="subscription offering"):
        await service._install_subscription_generation(config, {})


async def test_shared_subscription_close_refuses_unavailable_official_metric() -> None:
    from adcp.reporting.ledger.models import ReportingSubscriptionGeneration

    raw = redacted_capabilities().model_dump(mode="json")
    raw["offerings"][0].update(official_close_lag="PT0S", restatement_window="PT1H")
    raw["offerings"][1]["windowing"]["minimum_window"] = "PT1H"
    raw["offerings"][1]["metrics"] = [
        metric for metric in raw["offerings"][1]["metrics"] if metric["name"] != "spend"
    ]
    sources = []
    for offering in raw["offerings"]:
        offering["source_timezone"] = "UTC"
        offering["product_ids"] = ["PAID_MEDIA_DAILY_V1"]
        payload = {**raw, "offerings": [offering]}
        payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
        sources.append(ReportingSourceCapabilitiesV1.model_validate(payload))
    public = {
        **_capability_offering("gam"),
        "supported_finality": ["snapshot", "official"],
        "schedule": {**_capability_offering("gam")["schedule"], "delivery_sla": "P7D"},
    }
    config = replace(
        _configuration(),
        activated_at=None,
        schedule=replace(SCHEDULE, delivery_sla="P7D"),
        definition=replace(
            DEFINITION, finality_policy=ReportingSourceFinalPolicy("policy", "is_final")
        ),
        subscription=ReportingSubscriptionGeneration("gam-delivery", public["offering_id"]),
    )
    context = replace(_account_context(config), capability_offering=public)
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register(
        "gam", ScriptedReportingAdapter(sources[0], []), capability_offerings=[public]
    )
    service.sources.register(
        "official", ScriptedReportingAdapter(sources[1], []), capability_offerings=[public]
    )
    with pytest.raises(ReliableReportingConfigurationError, match="requested metrics"):
        await service.configure(config)


async def test_direct_snapshot_close_routes_its_named_authoritative_source() -> None:
    raw = redacted_capabilities().model_dump(mode="json")
    raw["offerings"][0].update(official_close_lag="PT0S", restatement_window="PT1H")
    raw["offerings"][1]["windowing"]["minimum_window"] = "PT1H"
    sources = []
    for offering in raw["offerings"]:
        offering["source_timezone"] = "UTC"
        offering["product_ids"] = ["PAID_MEDIA_DAILY_V1"]
        payload = {**raw, "offerings": [offering]}
        payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
        sources.append(ReportingSourceCapabilitiesV1.model_validate(payload))
    public = {
        **_capability_offering("gam"),
        "supported_finality": ["snapshot", "official"],
        "schedule": {**_capability_offering("gam")["schedule"], "delivery_sla": "P7D"},
    }
    config = replace(
        _configuration(),
        schedule=replace(SCHEDULE, delivery_sla="P7D"),
        definition=replace(
            DEFINITION, finality_policy=ReportingSourceFinalPolicy("policy", "is_final")
        ),
    )
    context = replace(
        _account_context(config),
        capability_offering=public,
        official_offering_id=OFFICIAL_OFFERING_ID,
    )
    service = ReliableReportingService.memory(account_context=lambda _: context)
    snapshot = service.sources.register(
        "gam", ScriptedReportingAdapter(sources[0], []), capability_offerings=[public]
    )
    official = service.sources.register(
        "official", ScriptedReportingAdapter(sources[1], []), capability_offerings=[public]
    )
    await service.configure(config)
    producer = service._bindings[config.generation_key].producer
    assert producer._source is snapshot.executor
    assert producer._source_for(OFFICIAL_OFFERING_ID) is official.executor
