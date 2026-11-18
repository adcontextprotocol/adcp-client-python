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
    ReportingFinalityPolicy,
    ReportingProducer,
    ReportingScheduleSpec,
    ReportingSourceFinalPolicy,
    ReportingStabilizedPolicy,
    ReportingStatusCaller,
)
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.production.contracts import ReportingProductionSourceBinding
from adcp.reporting.service import (
    ProducerFactory,
    ReliableReportingConfigurationError,
    ReliableReportingService,
    ReliableReportingState,
    ReportingAccountContext,
)
from adcp.reporting.source import (
    ReportingFinalityEvidenceV1,
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


@pytest.mark.parametrize("also_offers_definition", [False, True])
async def test_registered_source_publishes_only_its_bound_product(
    also_offers_definition: bool,
) -> None:
    config = replace(
        _configuration(),
        required_finality="official",
        deactivated_at=NOW,
        definition=replace(
            DEFINITION, finality_policy=ReportingSourceFinalPolicy("selected", "closed")
        ),
    )

    class BoundAdapter(ScriptedReportingAdapter):
        def configuration_binding(
            self, configuration: ReportingConfiguration
        ) -> ReportingProductionSourceBinding:
            return ReportingProductionSourceBinding.for_configuration(
                configuration,
                capabilities_sha256=self.capabilities.capabilities_sha256,
                media_buy_products=(("media-buy-redacted", "real-product"),),
            )

        def fetch_slice(self, request: Any) -> InlineFetchResult:
            self.calls.append(request)
            product = request.coverage.constituents[0].product_id
            return InlineFetchResult(
                rows=[{**_rows(12)[0], "product_id": product}],
                finality_evidence=ReportingFinalityEvidenceV1(
                    basis="provider_declared",
                    evidence_ref="closed",
                    observed_at=NOW + timedelta(days=1),
                ),
            )

    service = _finality_service(
        config,
        snapshot=False,
        official=True,
        products=(
            ("real-product", config.report_definition_id)
            if also_offers_definition
            else ("real-product",)
        ),
        adapter_type=BoundAdapter,
        clock=DeterministicReportingClock(NOW + timedelta(days=3)),
    )
    await service.configure(config)
    turn = await service.run_worker()
    assert not turn.configuration_errors
    adapter = service.sources.get("gam").adapter
    assert isinstance(adapter, BoundAdapter)
    assert {c.product_id for req in adapter.calls for c in req.coverage.constituents} == {
        "real-product"
    }
    revisions = turn.configurations[config.generation_key].revisions_committed
    assert len(revisions) == len(adapter.calls)
    for revision_id in revisions:
        page = await service.store.read_revision_rows(
            account_id=config.account_id, reporting_revision_id=revision_id
        )
        assert [row["product_id"] for row in page.rows] == ["real-product"]


@pytest.mark.parametrize("signal", [None, "delivery.other", "delivery.is_final"])
async def test_registered_inline_adapter_supplies_explicit_source_final_evidence(
    signal: str | None,
) -> None:
    config = replace(
        _configuration(),
        required_finality="official",
        deactivated_at=NOW,
        definition=replace(
            DEFINITION,
            finality_policy=ReportingSourceFinalPolicy("selected-v1", "delivery.is_final"),
        ),
    )
    evidence_time = NOW + timedelta(days=1)
    answer = InlineFetchResult(rows=_rows(12))
    if signal is not None:
        answer = replace(
            answer,
            finality_evidence=ReportingFinalityEvidenceV1(
                basis="provider_job_terminal",
                observed_at=evidence_time,
                evidence_ref=signal,
            ),
        )
    service = _finality_service(
        config,
        snapshot=False,
        official=True,
        results=(answer,),
        clock=DeterministicReportingClock(NOW + timedelta(days=3)),
    )
    await service.configure(config)
    turn = await service.run_worker(now=NOW + timedelta(days=3))
    if signal != "delivery.is_final":
        error = turn.configuration_errors[config.generation_key]
        assert isinstance(error, LedgerConflictError) and error.code == "MANIFEST_MISMATCH"
    else:
        assert not turn.configuration_errors
        identifier = turn.configurations[config.generation_key].revisions_committed[0]
        revision = await service.store.get_revision(
            account_id=config.account_id, reporting_revision_id=identifier
        )
        assert revision is not None and revision.finality_policy_id == "selected-v1"
        assert revision.finalized_at == evidence_time


@pytest.mark.parametrize(
    ("policy", "source_class", "expected_offering"),
    [
        (
            ReportingSourceFinalPolicy("source-v1", "delivery.is_final"),
            "official",
            OFFICIAL_OFFERING_ID,
        ),
        (ReportingSourceFinalPolicy("source-v1", "s" * 512), "official", OFFICIAL_OFFERING_ID),
        (ReportingContractualCutoffPolicy("cutoff-v1", "P2D"), "snapshot", SNAPSHOT_OFFERING_ID),
        (ReportingStabilizedPolicy("stable-v1", "P1D", "PT6H"), "snapshot", SNAPSHOT_OFFERING_ID),
    ],
)
async def test_official_configuration_routes_by_selected_policy(
    policy: ReportingFinalityPolicy, source_class: str, expected_offering: str
) -> None:
    config = replace(
        _configuration(),
        required_finality="official",
        definition=replace(DEFINITION, finality_policy=policy),
    )
    service = _finality_service(
        config, snapshot=source_class == "snapshot", official=source_class == "official"
    )
    await service.configure(config)
    offerings = service._bindings[config.generation_key].producer._offerings
    assert offerings.offering_for(source_class) == expected_offering


@pytest.mark.parametrize(("required", "close"), [("official", False), ("snapshot", True)])
async def test_official_configuration_without_selected_policy_fails_before_obligation(
    required: str, close: bool
) -> None:
    config = replace(_configuration(), required_finality=required)
    service = _finality_service(config, snapshot=True, official=True, close=close)
    await service.initialize()
    with pytest.raises(ReliableReportingConfigurationError, match="selected finality policy"):
        await service.configure(config)
    state = await service.store.read_status_snapshot(
        caller=ReportingStatusCaller(config.account_id, config.consumer_id)
    )
    assert not state.configurations and not state.obligations


@pytest.mark.parametrize(
    ("policy", "required", "snapshot", "official", "close"),
    [
        (
            ReportingSourceFinalPolicy("source-v1", "delivery.is_final"),
            "official",
            True,
            False,
            False,
        ),
        (
            ReportingSourceFinalPolicy("source-v1", "delivery.is_final"),
            "snapshot",
            True,
            False,
            True,
        ),
        (ReportingContractualCutoffPolicy("cutoff-v1", "P2D"), "official", False, True, False),
        (ReportingStabilizedPolicy("stable-v1", "P1D", "PT6H"), "official", False, True, False),
    ],
)
async def test_selected_policy_rejects_wrong_source_class(
    policy, required, snapshot, official, close
) -> None:
    config = replace(
        _configuration(),
        required_finality=required,
        definition=replace(DEFINITION, finality_policy=policy),
    )
    service = _finality_service(config, snapshot=snapshot, official=official, close=close)
    with pytest.raises(ReliableReportingConfigurationError, match="source offering"):
        await service.configure(config)


async def test_stabilized_official_configuration_requires_observation_storage() -> None:
    class SnapshotStore(InMemoryReportingLedgerStore):
        reserve_provisional_acquisition = None

    config = replace(
        _configuration(),
        required_finality="official",
        definition=replace(
            DEFINITION, finality_policy=ReportingStabilizedPolicy("stable-v1", "P1D", "PT6H")
        ),
    )
    service = _finality_service(config, snapshot=True, official=False, store=SnapshotStore())
    with pytest.raises(ReliableReportingConfigurationError, match="observation"):
        await service.configure(config)


async def test_stabilized_admission_requires_publication_lock_before_persisting() -> None:
    class UnlockedHistory(InMemoryReportingLedgerStore):
        source_publication = None

    config = replace(
        _configuration(),
        required_finality="official",
        definition=replace(
            DEFINITION, finality_policy=ReportingStabilizedPolicy("stable", "PT0S", "PT1H")
        ),
    )
    store = UnlockedHistory()
    service = _finality_service(config, snapshot=True, official=False, store=store)
    await service.initialize()
    with pytest.raises(ReliableReportingConfigurationError, match="publication"):
        await service.configure(config)
    assert not await store.list_all_configurations()


async def test_initialize_recovers_frozen_stabilization_cadence_and_healthy_binding() -> None:
    clock = DeterministicReportingClock(NOW + timedelta(days=1))
    store = InMemoryReportingLedgerStore(clock=clock)
    config = replace(
        _configuration(),
        required_finality="official",
        activated_at=NOW.replace(hour=1, minute=0),
        deactivated_at=NOW.replace(hour=2, minute=0),
        definition=replace(
            DEFINITION, finality_policy=ReportingStabilizedPolicy("stable", "PT0S", "P30D")
        ),
    )
    original = _finality_service(
        config,
        snapshot=True,
        official=False,
        cadence="PT6H",
        store=store,
        clock=clock,
        results=(InlineFetchResult(rows=_rows(10)),),
    )
    await original.configure(config)
    turn = await original.run_worker()
    assert len(turn.configurations[config.generation_key].revisions_committed) == 1
    healthy = replace(
        config,
        delivery_config_id="healthy",
        definition=replace(
            DEFINITION, finality_policy=ReportingStabilizedPolicy("short", "PT0S", "PT6H")
        ),
    )
    await store.put_configuration(healthy)
    recovered = _finality_service(
        config,
        snapshot=True,
        official=False,
        cadence="PT1H",
        store=store,
        clock=clock,
        results=(InlineFetchResult(rows=_rows(10)),),
    )
    await recovered.initialize()
    assert set(recovered._bindings) == {config.generation_key, healthy.generation_key}
    assert not recovered.initialization_errors
    restarted = recovered._bindings[config.generation_key].producer
    (obligation_id,) = turn.configurations[config.generation_key].obligations_committed
    owner = dict(account_id=config.account_id, reporting_obligation_id=obligation_id)
    first = await store.get_provisional_observation(**owner)
    assert first is not None
    clock.advance(timedelta(hours=1))
    await restarted.run_configuration(config)
    assert await store.get_provisional_observation(**owner) == first
    clock.advance(timedelta(hours=5))
    await restarted.run_configuration(config)
    second = await store.get_provisional_observation(**owner)
    assert second is not None and second.checked_at == clock()
    assert second.acquisition.policy.cadence == timedelta(hours=6)


@pytest.mark.parametrize(
    "reserved_only,configure_before_init", [(True, False), (True, True), (False, True)]
)
async def test_stabilization_startup_recovers_admitted_schedule(
    reserved_only: bool, configure_before_init: bool
) -> None:
    clock = DeterministicReportingClock(NOW + timedelta(days=1))
    config = replace(
        _configuration(),
        required_finality="official",
        activated_at=NOW.replace(hour=1, minute=0),
        deactivated_at=NOW.replace(hour=2, minute=0),
        definition=replace(
            DEFINITION, finality_policy=ReportingStabilizedPolicy("stable", "PT0S", "P30D")
        ),
    )
    store = InMemoryReportingLedgerStore(clock=clock)
    original = _finality_service(
        config,
        snapshot=True,
        official=False,
        cadence="PT6H",
        clock=clock,
        store=store,
        results=(None,) if reserved_only else (InlineFetchResult(rows=_rows(10)),),
    )
    await original.configure(config)
    await original.run_worker()
    (obligation,) = store._obligations.values()
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    first = await store.get_provisional_acquisition(**owner, ordinal=0)
    assert first is not None and first.policy.cadence == timedelta(hours=6)
    assert (await store.get_provisional_observation(**owner) is None) == reserved_only
    recovered = _finality_service(
        config, snapshot=True, official=False, cadence="PT1H", clock=clock, store=store
    )
    if configure_before_init:
        await recovered.configure(config)
    await recovered.initialize()
    assert config.generation_key in recovered._bindings
    with pytest.raises(ReliableReportingConfigurationError, match="history"):
        await recovered.configure(replace(config, delivery_config_version=2))


async def test_stabilized_configuration_rejects_excessive_history_before_admission() -> None:
    config = replace(
        _configuration(),
        required_finality="official",
        definition=replace(
            DEFINITION, finality_policy=ReportingStabilizedPolicy("too-long", "PT0S", "P10000D")
        ),
    )
    service = _finality_service(config, snapshot=True, official=False)
    await service.initialize()
    with pytest.raises(ReliableReportingConfigurationError, match="history"):
        await service.configure(config)
    state = await service.store.read_status_snapshot(
        caller=ReportingStatusCaller(config.account_id, config.consumer_id)
    )
    assert not state.configurations and not state.obligations


@pytest.mark.parametrize("media_type", ["application/json", "application/vnd.apache.parquet"])
async def test_service_refuses_an_unreadable_official_source_format_before_admission(
    media_type: str,
) -> None:
    config = replace(
        _configuration(),
        required_finality="official",
        definition=replace(
            DEFINITION, finality_policy=ReportingSourceFinalPolicy("source", "closed")
        ),
    )
    with pytest.raises(ValueError, match="source format"):
        service = _finality_service(config, snapshot=False, official=True, source_format=media_type)
        await service.configure(config)


async def test_service_rejects_stabilized_configuration_without_history_before_persisting() -> None:
    def producer_factory(**kwargs: Any) -> ReportingProducer:
        store = kwargs.pop("store")
        publisher = SimpleNamespace(
            reserve_provisional_acquisition=store.reserve_provisional_acquisition,
            get_provisional_observation=store.get_provisional_observation,
            commit_provisional_observation=store.commit_provisional_observation,
        )
        return ReportingProducer(store=publisher, retry_store=store, **kwargs)

    config = replace(
        _configuration(),
        required_finality="official",
        definition=replace(
            DEFINITION, finality_policy=ReportingStabilizedPolicy("stable-v1", "P1D", "PT6H")
        ),
    )
    service = _finality_service(
        config, snapshot=True, official=False, producer_factory=producer_factory
    )
    await service.initialize()

    with pytest.raises(ReliableReportingConfigurationError, match="observation"):
        await service.configure(config)

    state = await service.store.read_status_snapshot(
        caller=ReportingStatusCaller(config.account_id, config.consumer_id)
    )
    assert not state.configurations and not state.obligations
    assert config.generation_key not in service._bindings


async def test_official_configuration_requires_the_source_to_match_the_pinned_definition() -> None:
    config = replace(
        _configuration(),
        required_finality="official",
        definition=replace(
            DEFINITION,
            report_definition_sha256="f" * 64,
            finality_policy=ReportingSourceFinalPolicy("source-v1", "delivery.is_final"),
        ),
    )
    service = _finality_service(config, snapshot=False, official=True)
    with pytest.raises(ReliableReportingConfigurationError, match="definition"):
        await service.configure(config)


@pytest.mark.parametrize("definition", [None, DEFINITION])
async def test_legacy_official_configuration_recovers_with_a_retired_source_route(
    definition: ReportingDefinitionBinding | None,
) -> None:
    store = InMemoryReportingLedgerStore()
    legacy = replace(_configuration(), required_finality="official", definition=definition)
    healthy = _configuration("freewheel")
    for config in (legacy, healthy):
        await store.put_configuration(config)
    resolved = []

    def resolve(config: ReportingConfiguration) -> ReportingAccountContext:
        resolved.append(config.generation_key)
        context = _account_context(config)
        if config == legacy:
            return replace(context, snapshot_offering_id=None, official_offering_id="retired")
        return context

    service = ReliableReportingService(store=store, account_context=resolve)
    for route in ("gam", "freewheel"):
        service.sources.register(route, ScriptedReportingAdapter(redacted_capabilities(), []))
    await service.initialize()
    assert resolved == [healthy.generation_key]
    assert list(service._bindings) == [healthy.generation_key]
    assert "selected finality policy" in str(service.initialization_errors[legacy.generation_key])
    assert set(await store.list_all_configurations()) == {legacy, healthy}


async def test_official_configuration_must_match_the_declared_public_definition() -> None:
    definition = replace(
        DEFINITION,
        report_definition_sha256="f" * 64,
        finality_policy=ReportingContractualCutoffPolicy("cutoff-v1", "P2D"),
    )
    config = replace(_configuration(), required_finality="official", definition=definition)
    raw = redacted_capabilities().model_dump(mode="json")
    raw["offerings"][0]["contract"][
        "report_definition_sha256"
    ] = definition.report_definition_sha256
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    public = {**_capability_offering("gam"), "supported_finality": ["official"]}
    context = replace(_account_context(config), capability_offering=public)
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(ReportingSourceCapabilitiesV1.model_validate(raw), []),
        capability_offerings=[public],
    )
    with pytest.raises(ReliableReportingConfigurationError, match="public offering"):
        await service.configure(config)


async def test_snapshot_close_selected_policy_checks_the_initial_snapshot_definition() -> None:
    config = replace(
        _configuration(),
        definition=replace(
            DEFINITION, finality_policy=ReportingSourceFinalPolicy("source-v1", "delivery.is_final")
        ),
    )
    raw = redacted_capabilities().model_dump(mode="json")
    raw["offerings"][0].update(restatement_window="P1D", official_close_lag="P2D")
    raw["offerings"][0]["contract"]["report_definition_sha256"] = "f" * 64
    raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
    context = replace(_account_context(config), official_offering_id=OFFICIAL_OFFERING_ID)
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register(
        "gam", ScriptedReportingAdapter(ReportingSourceCapabilitiesV1.model_validate(raw), [])
    )
    with pytest.raises(ReliableReportingConfigurationError, match="definition"):
        await service.configure(config)


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
    assert service.capability_block()["offerings"]
    await service.initialize()
    assert service.capability_block()["offerings"]
    await service.start()
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
