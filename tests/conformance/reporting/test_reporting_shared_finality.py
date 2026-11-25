"""Production subscriptions route a shared delivery to its source finality."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    reporting_source_capabilities_sha256_v1,
)

from ._generation_support import END, obligation_for
from ._production_support import Source, production_harness, reference_source_final


class ClosingSource(Source):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        raw = self.capabilities.model_dump(mode="json")
        raw["offerings"][0].update(official_close_lag="PT0S", restatement_window="PT1H")
        raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
        self.capabilities = ReportingSourceCapabilitiesV1.model_validate(raw)
        self.inline._capabilities = self.capabilities


async def test_shared_context_cannot_change_requested_fields(tmp_path):
    from adcp.reporting.production.source_registry import ReportingProductionSourceRegistry
    from adcp.reporting.service import ReportingAccountContext

    def registry_factory(offerings):
        snapshot, official = offerings
        official.producer._offerings = replace(
            official.producer._offerings, requested_metrics=("impressions",)
        )
        profile = snapshot.producer._offerings
        registry = ReportingProductionSourceRegistry(
            account_context=lambda config: ReportingAccountContext(
                config.account_id,
                "snapshot",
                profile.currency,
                profile.source_scope,
                account_timezone=config.account_timezone,
                snapshot_offering_id=snapshot.source_offering_id,
                official_offering_id=official.source_offering_id,
                requested_metrics=profile.requested_metrics,
                requested_dimensions=profile.requested_dimensions,
                publication_namespace=profile.publication_namespace,
            )
        )
        registry.register("snapshot", snapshot.producer)
        registry.register("official", official.producer)
        return registry

    async with production_harness(
        "memory",
        tmp_path / "fields.sqlite",
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
        source_registry_factory=registry_factory,
    ) as h:
        config = replace(h.item.config, required_finality="official")
        official = h.production.offerings[1]
        with pytest.raises(ValueError, match="fixed production profile"):
            await h.production.source_registry.resolve(
                config, official, official.check_source(effective=True)
            )


@pytest.mark.parametrize("finality,index", [("snapshot", 0), ("official", 1)])
async def test_shared_delivery_routes_to_source(finality, index, tmp_path):
    async with production_harness(
        "memory",
        tmp_path / "shared.sqlite",
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
    ) as h:
        config = replace(h.item.config, delivery_config_id=finality, required_finality=finality)
        binding = replace(h.item.binding, generation_key=config.generation_key)
        h.item.writer.grant(binding)
        for offering in h.production.offerings:
            offering.producer._source.bind_generation(config)
        await h.store.admit_production_configuration(config, binding, offering_id="managed-fixture")
        obligation = await h.store.commit_obligation(
            replace(obligation_for(config), reporting_obligation_id=f"obligation-{finality}")
        )
        await h.production.activate(account_id=config.account_id)
        producer = h.production.offerings[index].producer
        token = h.production._producer_turn.set(producer)
        try:
            revision = await producer.acquire_obligation(config, obligation, now=END)
        finally:
            h.production._producer_turn.reset(token)
        assert revision is not None and revision.finality == finality
        assert producer._source.requests[-1].offering_id == producer._offerings.offering_for(
            finality
        )
        assert h.production.offerings[1 - index].producer._source.requests == []


@pytest.mark.parametrize("finality,index", [("snapshot", 0), ("official", 1)])
async def test_registered_shared_sources_resolve_account_context(finality, index, tmp_path):
    from adcp.reporting.production.source_registry import ReportingProductionSourceRegistry
    from adcp.reporting.service import ReportingAccountContext

    def registry_factory(offerings):
        first, second = offerings
        profile = first.producer._offerings

        def context(config):
            return ReportingAccountContext(
                config.account_id,
                "snapshot",
                profile.currency,
                profile.source_scope,
                account_timezone=config.account_timezone,
                snapshot_offering_id=first.source_offering_id,
                official_offering_id=second.source_offering_id,
                publication_namespace=profile.publication_namespace,
            )

        registry = ReportingProductionSourceRegistry(account_context=context)
        registry.register("snapshot", first.producer)
        registry.register("official", second.producer)
        return registry

    async with production_harness(
        "memory",
        tmp_path / "registry.sqlite",
        source_publication=True,
        shared_finality=True,
        different_source_scope=True,
        finality_policy=reference_source_final(),
        source_registry_factory=registry_factory,
    ) as h:
        config = replace(h.item.config, delivery_config_id=finality, required_finality=finality)
        selected = h.production.offerings[index]
        resolved = await h.production.source_registry.resolve(
            config, selected, selected.check_source(effective=True)
        )
        assert resolved.document()["source_offering_id"] == selected.source_offering_id


@pytest.mark.parametrize("finality,index", [("snapshot", 0), ("official", 1)])
async def test_mounted_admission_selects_shared_source(finality, index, tmp_path):
    from adcp.reporting.production.source_registry import ReportingProductionSourceRegistry
    from adcp.reporting.service import ReportingAccountContext

    from ._generation_support import obligation_for
    from ._production_transport import MountedProduction
    from .test_reliable_reporting_production_admission import account_task, service_factory
    from .test_reporting_production_configuration import wire_configuration

    selected = {}

    def registry_factory(offerings):
        snapshot, official = offerings
        profile = snapshot.producer._offerings

        def context(config):
            return ReportingAccountContext(
                config.account_id,
                "snapshot",
                profile.currency,
                profile.source_scope,
                account_timezone=config.account_timezone,
                snapshot_offering_id=snapshot.source_offering_id,
                official_offering_id=official.source_offering_id,
                publication_namespace=profile.publication_namespace,
            )

        registry = ReportingProductionSourceRegistry(account_context=context)
        registry.register("snapshot", snapshot.producer)
        registry.register("official", official.producer)
        return registry

    async def handle(request, context, admit):
        try:
            return await account_task(selected, request, context, admit)
        except Exception as error:
            selected["failure"] = error
            raise

    async with production_harness(
        "memory",
        tmp_path / "mounted.sqlite",
        source_publication=True,
        shared_finality=True,
        different_source_scope=True,
        finality_policy=reference_source_final(),
        source_registry_factory=registry_factory,
        service_factory=service_factory,
        account_handler=handle,
    ) as h:
        config = replace(
            h.item.config,
            account_id="late-account",
            media_buy_ids=("mb_late-account",),
            delivery_config_id=finality,
            required_finality=finality,
        )
        h.item.config = config
        h.item.obligation = obligation_for(config)
        h.item.binding = replace(h.item.binding, generation_key=config.generation_key)
        h.item.writer.grant(h.item.binding)
        for offering in h.production.offerings:
            offering.producer._source.bind_generation(config)
        selected["h"] = h
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        async with mounted.client() as client:
            _, result = await mounted.call(
                client,
                "sync_accounts",
                {
                    "idempotency_key": "shared-finality-admission-001",
                    "accounts": [
                        {
                            "account": {"account_id": config.account_id},
                            "reporting_delivery_configs": [wire_configuration(h)],
                        }
                    ],
                },
            )
        assert result.get("status") == "completed", (
            result,
            repr(selected.get("failure")),
            repr(getattr(selected.get("failure"), "__cause__", None)),
        )
        saved = h.store._production_source_bindings[config.generation_key].document()
        assert (
            saved["service_context"]["source_offering_id"]
            == h.production.offerings[index].source_offering_id
        )


async def test_snapshot_automatic_close_uses_authoritative_adapter(tmp_path):
    async with production_harness(
        "memory",
        tmp_path / "close.sqlite",
        source_publication=True,
        shared_finality=True,
        different_source_scope=True,
        finality_policy=reference_source_final(),
        source_factory=ClosingSource,
    ) as h:
        config = h.item.config
        for offering in h.production.offerings:
            offering.producer._source.bind_generation(config)
        await h.store.admit_production_configuration(
            config, h.item.binding, offering_id="managed-fixture"
        )
        await h.production.activate(account_id=config.account_id)
        from .test_reporting_production_lock_order import source_turn

        result = await source_turn(h.production)
        assert result.revisions_committed
        h.source_clock.now = END + timedelta(hours=1)
        result = await source_turn(h.production)
        assert result.revisions_committed
        official_source = h.production.offerings[1].producer._source
        assert official_source.requests[-1].publication_class == "AUTHORITATIVE"
        assert official_source.requests[-1].identity.source_scope["adapter"] == "official"
        assert official_source.requests[-1].publication_namespace == "reporting-source:official"
        history = await h.store.list_revisions(
            account_id=config.account_id,
            reporting_obligation_id=h.item.obligation.reporting_obligation_id,
        )
        assert [revision.finality for revision in history] == ["snapshot", "official"]


async def test_shared_source_context_rejects_swapped_source_classes(tmp_path):
    from adcp.reporting.production.source_registry import ReportingProductionSourceRegistry
    from adcp.reporting.service import ReportingAccountContext

    def registry_factory(offerings):
        snapshot, official = offerings
        profile = snapshot.producer._offerings
        registry = ReportingProductionSourceRegistry(
            account_context=lambda config: ReportingAccountContext(
                config.account_id,
                "snapshot",
                profile.currency,
                profile.source_scope,
                account_timezone=config.account_timezone,
                snapshot_offering_id=official.source_offering_id,
                official_offering_id=snapshot.source_offering_id,
                publication_namespace=profile.publication_namespace,
            )
        )
        registry.register("snapshot", snapshot.producer)
        registry.register("official", official.producer)
        return registry

    async with production_harness(
        "memory",
        tmp_path / "swapped.sqlite",
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
        source_registry_factory=registry_factory,
    ) as h:
        offering = h.production.offerings[0]
        with pytest.raises(ValueError, match="fixed production profile"):
            await h.production.source_registry.resolve(
                h.item.config, offering, offering.check_source()
            )


async def test_automatic_close_rechecks_authoritative_source_authorization(tmp_path):
    from .test_reporting_production_lock_order import source_turn

    async with production_harness(
        "memory",
        tmp_path / "revocation.sqlite",
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
        source_factory=ClosingSource,
    ) as h:
        config = h.item.config
        snapshot, official = (o.producer._source for o in h.production.offerings)
        snapshot.bind_generation(config)
        official.bind_generation(config)
        await h.store.admit_production_configuration(
            config, h.item.binding, offering_id="managed-fixture"
        )
        await h.production.activate(account_id=config.account_id)
        assert (await source_turn(h.production)).revisions_committed
        binding = official.bindings.pop(config.generation_key)
        h.source_clock.now = END + timedelta(hours=1)
        await source_turn(h.production)
        assert official.requests == []
        history = await h.store.list_revisions(
            account_id=config.account_id,
            reporting_obligation_id=h.item.obligation.reporting_obligation_id,
        )
        assert [revision.finality for revision in history] == ["snapshot"]
        official.bindings[config.generation_key] = binding
        result = await source_turn(h.production)
        assert result.revisions_committed
        assert official.requests[-1].publication_class == "AUTHORITATIVE"


async def test_production_rejects_changed_authoritative_route(tmp_path):
    from adcp.reporting.ledger.notification_models import ReportingNotificationError

    async with production_harness(
        "memory",
        tmp_path / "route.sqlite",
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
    ) as h:
        snapshot, official = h.production.offerings
        routes = snapshot.producer._source_routes
        source_id = official.source_offering_id
        original = routes[source_id]
        routes[source_id] = (snapshot.producer._source, original[1], original[2])
        try:
            with pytest.raises(ReportingNotificationError, match="component_unready"):
                h.production._assert_components()
        finally:
            routes[source_id] = original


async def test_automatic_close_uses_authoritative_source_after_restart(tmp_path):
    from .test_reporting_production_lock_order import source_turn

    path = tmp_path / "restart.sqlite"
    async with production_harness(
        "memory",
        path,
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
        source_factory=ClosingSource,
    ) as first:
        for offering in first.production.offerings:
            offering.producer._source.bind_generation(first.item.config)
        await first.store.admit_production_configuration(
            first.item.config, first.item.binding, offering_id="managed-fixture"
        )
        await first.production.activate(account_id=first.item.config.account_id)
        assert (await source_turn(first.production)).revisions_committed
        await first.production.aclose()
        async with production_harness(
            "memory",
            path,
            source_publication=True,
            shared_finality=True,
            finality_policy=reference_source_final(),
            source_factory=ClosingSource,
            existing_store=first.store,
        ) as recovered:
            recovered.production.offerings[1].producer._source.bind_generation(
                recovered.item.config
            )
            recovered.source_clock.now = END + timedelta(hours=1)
            result = await source_turn(recovered.production)
            assert result.revisions_committed
            assert (
                recovered.production.offerings[1].producer._source.requests[-1].publication_class
                == "AUTHORITATIVE"
            )
            history = await recovered.store.list_revisions(
                account_id=recovered.item.config.account_id,
                reporting_obligation_id=recovered.item.obligation.reporting_obligation_id,
            )
            assert [revision.finality for revision in history] == ["snapshot", "official"]


async def test_automatic_close_refuses_official_source_without_requested_metric(tmp_path):
    with pytest.raises(ValueError, match="authoritative source cannot serve"):
        async with production_harness(
            "memory",
            tmp_path / "missing-metric.sqlite",
            source_publication=True,
            shared_finality=True,
            finality_policy=reference_source_final(),
            source_factory=ClosingSource,
            official_missing_metric=True,
        ):
            pass


async def test_authoritative_retry_scope_does_not_block_snapshot_source(tmp_path):
    from adcp.reporting.ledger.producer import WorkerTurn

    async with production_harness(
        "memory",
        tmp_path / "retry.sqlite",
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
        source_factory=ClosingSource,
        different_source_scope=True,
    ) as h:
        snapshot, official = h.production.offerings
        producer = snapshot.producer
        obligation = h.item.obligation
        await producer._schedule_retry(
            obligation,
            WorkerTurn(),
            now=END,
            scope="source",
            offering_id=official.source_offering_id,
        )
        same_source = await producer._retry_not_before(
            replace(obligation, reporting_obligation_id="official-peer"),
            WorkerTurn(),
            now=END,
            offering_id=official.source_offering_id,
        )
        other_source = await producer._retry_not_before(
            replace(obligation, reporting_obligation_id="snapshot-peer"),
            WorkerTurn(),
            now=END,
            offering_id=snapshot.source_offering_id,
        )
        assert same_source is True
        assert other_source is False


async def test_failed_production_composition_does_not_replace_live_source_reader(tmp_path):
    from adcp.reporting.ledger import ReportingProducer
    from adcp.reporting.production.offerings import ReportingProductionOffering
    from adcp.reporting.production.service import ReportingProductionSupport

    async with production_harness(
        "memory",
        tmp_path / "composition.sqlite",
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
    ) as h:
        snapshot, official = h.production.offerings
        original = snapshot.producer._source_routes[official.source_offering_id]
        other = ReportingProducer(
            source=official.producer._source,
            offerings=official.producer._offerings,
            object_reader=snapshot.producer._object_reader,
            store=h.store,
            escalation=official.producer.escalation,
            revision_verifier=official.producer._revision_verifier,
        )
        competing = ReportingProductionOffering(
            official.offering, other, official.verification_key, official.source_offering_id
        )
        with pytest.raises(ValueError, match="one producer cannot route"):
            ReportingProductionSupport(
                h.production.materializer,
                h.production.projection,
                offerings=(snapshot, competing),
                configuration_task=h.production.configuration_task,
                resolve_account=lambda *_: None,
            )
        assert snapshot.producer._source_routes[official.source_offering_id] == original
        h.production._assert_components()


async def test_failed_production_validation_does_not_install_source_route(tmp_path):
    from adcp.reporting.ledger import ReportingProducer
    from adcp.reporting.ledger.notification_models import ReportingNotificationError
    from adcp.reporting.production.offerings import ReportingProductionOffering
    from adcp.reporting.production.service import ReportingProductionSupport

    async with production_harness(
        "memory",
        tmp_path / "validation.sqlite",
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
    ) as h:
        snapshot, official = h.production.offerings
        producer = ReportingProducer(
            source=snapshot.producer._source,
            offerings=snapshot.producer._offerings,
            object_reader=snapshot.producer._object_reader,
            store=h.store,
            escalation=snapshot.producer.escalation,
            revision_verifier=snapshot.producer._revision_verifier,
        )
        offering = ReportingProductionOffering(
            snapshot.offering, producer, snapshot.verification_key, snapshot.source_offering_id
        )
        owner = h.store.production_support
        writer = h.production.materializer.writer
        h.store.production_support = None
        writer.production_eligible = False
        try:
            with pytest.raises(
                ReportingNotificationError, match="reporting_production_component_unready"
            ):
                ReportingProductionSupport(
                    h.production.materializer,
                    h.production.projection,
                    offerings=(offering, official),
                    configuration_task=h.production.configuration_task,
                    resolve_account=lambda *_: None,
                )
            assert not producer._source_routes
        finally:
            h.store.production_support = owner
            writer.production_eligible = True
        h.production._assert_components()


async def test_snapshot_close_stops_when_authoritative_sla_drifts(tmp_path):
    from .test_reporting_production_lock_order import source_turn

    async with production_harness(
        "memory",
        tmp_path / "drift.sqlite",
        source_publication=True,
        shared_finality=True,
        finality_policy=reference_source_final(),
        source_factory=ClosingSource,
    ) as h:
        for offering in h.production.offerings:
            offering.producer._source.bind_generation(h.item.config)
        await h.store.admit_production_configuration(
            h.item.config, h.item.binding, offering_id="managed-fixture"
        )
        await h.production.activate(account_id=h.item.config.account_id)
        assert (await source_turn(h.production)).revisions_committed
        official = h.production.offerings[1].producer._source
        raw = official.capabilities.model_dump(mode="json")
        raw["offerings"][0]["worst_case_availability_lag"] = "PT2H"
        raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
        official.capabilities = ReportingSourceCapabilitiesV1.model_validate(raw)
        official.inline._capabilities = official.capabilities
        official.bind_generation(h.item.config)
        h.source_clock.now = END + timedelta(hours=1)
        result = await source_turn(h.production)
        assert not result.revisions_committed
        assert official.requests == []
