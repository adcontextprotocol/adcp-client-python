"""Frozen provider configuration, mounted admission and semantic replay checks."""

import copy
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from adcp.reporting.ledger import (
    ProducerOfferings,
    ReportingContractualCutoffPolicy,
    ReportingProducer,
    ReportingSourceFinalPolicy,
    ReportingStabilizedPolicy,
)
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.materializer.contracts import ReportingWriterError, binding_fingerprint
from adcp.reporting.materializer.work import ReportingMaterializerLease
from adcp.reporting.production.configuration import ReportingConfigurationAdmission
from adcp.reporting.production.offerings import ReportingProductionOffering
from adcp.reporting.service import ReportingAdapterRegistry
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    reporting_source_capabilities_sha256_v1,
)
from adcp.validation.schema_loader import get_named_validator

from ._feed_support import feed_request, walk
from ._generation_support import END
from ._production_support import Source, production_harness
from ._production_transport import MountedProduction
from ._receipt_transport import error_code
from .test_reporting_production_bindings import source_documents
from .test_reporting_production_settling import SettlingSource

SOURCE_FINAL = ReportingSourceFinalPolicy("source-v1", "delivery.is_final")
CUTOFF = ReportingContractualCutoffPolicy("cutoff-v1", "P2D")
STABILIZED = ReportingStabilizedPolicy("stable-v1", "P1D", "PT6H")


@pytest.mark.parametrize(
    ("policy", "source_class", "finalities", "close", "official_route", "history", "accepted"),
    [
        (SOURCE_FINAL, "official", ["official"], False, True, True, True),
        (CUTOFF, "snapshot", ["official"], False, False, True, True),
        (CUTOFF, "snapshot", ["snapshot", "official"], False, False, True, True),
        (STABILIZED, "snapshot", ["official"], False, False, True, True),
        (STABILIZED, "snapshot", ["official"], False, False, False, False),
        (None, "official", ["official"], False, True, True, False),
        (SOURCE_FINAL, "snapshot", ["official"], False, False, True, False),
        (CUTOFF, "official", ["official"], False, True, True, False),
        (None, "snapshot", ["snapshot"], True, True, True, False),
        (SOURCE_FINAL, "snapshot", ["snapshot"], True, False, True, False),
        (SOURCE_FINAL, "snapshot", ["snapshot"], True, True, True, True),
        (CUTOFF, "snapshot", ["snapshot"], True, False, True, True),
    ],
)
async def test_production_offering_validates_selected_finality_policy(
    policy, source_class, finalities, close, official_route, history, accepted, tmp_path
):
    async with production_harness("memory", tmp_path / "destination.sqlite", count=0) as h:
        installed = h.production.offerings[0]
        definition = replace(installed.verification_key.definition, finality_policy=policy)
        key = replace(installed.verification_key, definition=definition)
        if close:
            source = SettlingSource(key, tmp_path / "selected")
            official_id = source.official_source_id
        else:
            source = Source(key, tmp_path / "selected", official=source_class == "official")
            official_id = source.source_id
        config = replace(h.item.config, definition=definition, required_finality=finalities[-1])
        source.bind_generation(config)
        store = h.store
        if not history:
            store = SimpleNamespace(
                reserve_provisional_acquisition=h.store.reserve_provisional_acquisition,
                get_provisional_observation=h.store.get_provisional_observation,
                commit_provisional_observation=h.store.commit_provisional_observation,
            )
        producer = ReportingProducer(
            source=source,
            store=store,
            retry_store=h.store,
            object_reader=source.reader,
            offerings=ProducerOfferings(
                official_offering_id=official_id if official_route else None,
                snapshot_offering_id=source.source_id if source_class == "snapshot" else None,
                publication_namespace=source.capabilities.offerings[0].publication_namespace,
                source_scope=source.capabilities.source_scope,
            ),
        )
        public = installed.offering.model_copy(update={"supported_finality": finalities})
        if not accepted:
            with pytest.raises(ReportingWriterError, match="UNSUPPORTED_VERIFICATION"):
                ReportingProductionOffering(public, producer, key, source.source_id)
            return
        offering = ReportingProductionOffering(public, producer, key, source.source_id)
        offering.check_configuration(
            config, h.item.binding, retention_days=h.item.writer.resource_retention_days
        )
        other = replace(config, definition=replace(definition, finality_policy=None))
        source.bind_generation(other)
        with pytest.raises(ReportingWriterError, match="BINDING_MISMATCH"):
            offering.check_configuration(
                other, h.item.binding, retention_days=h.item.writer.resource_retention_days
            )


@pytest.mark.parametrize("primary", ["snapshot", "official"])
@pytest.mark.parametrize("required", ["snapshot", "official"])
@pytest.mark.parametrize("unsupported", ["snapshot", "official"])
async def test_dual_finality_source_final_admits_each_configured_route(
    primary, required, unsupported, tmp_path
):
    async with production_harness("memory", tmp_path / "destination.sqlite", count=0) as h:
        installed = h.production.offerings[0]
        definition = replace(installed.verification_key.definition, finality_policy=SOURCE_FINAL)
        key = replace(installed.verification_key, definition=definition)
        source = SettlingSource(key, tmp_path / "selected")
        routes = replace(
            installed.producer._offerings,
            snapshot_offering_id=source.source_id,
            official_offering_id=source.official_source_id,
        )
        producer = ReportingProducer(
            source=source, offerings=routes, store=h.store, object_reader=source.reader
        )
        config = replace(h.item.config, definition=definition, required_finality=required)
        source.bind_generation(config)
        public = installed.offering.model_copy(
            update={"supported_finality": ["snapshot", "official"]}
        )
        offering = ReportingProductionOffering(public, producer, key, routes.offering_for(primary))
        offering.check_configuration(
            config, h.item.binding, retention_days=h.item.writer.resource_retention_days
        )
        capabilities = source.capabilities.model_dump(mode="json")
        for item in capabilities["offerings"]:
            if item["offering_id"] == routes.offering_for(unsupported):
                item["formats"] = [
                    {"media_type": "application/vnd.apache.parquet", "compression": "none"}
                ]
        capabilities["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(capabilities)
        source.capabilities = ReportingSourceCapabilitiesV1.model_validate(capabilities)
        with pytest.raises(ReportingWriterError, match="UNSUPPORTED_VERIFICATION"):
            offering.check_source(effective=True)


@pytest.mark.parametrize("policy", [CUTOFF, SOURCE_FINAL])
async def test_production_rechecks_inline_output_before_admission(policy, tmp_path):
    async with production_harness("memory", tmp_path / "destination.sqlite", count=0) as h:
        installed = h.production.offerings[0]
        key = replace(
            installed.verification_key,
            definition=replace(installed.verification_key.definition, finality_policy=policy),
        )
        binding = Source(key, tmp_path / "selected", official=policy == SOURCE_FINAL)
        adapter = SimpleNamespace(
            capabilities=binding.capabilities,
            fetch_slice=binding.fetch,
            configuration_binding=binding.configuration_binding,
        )
        registered = ReportingAdapterRegistry(clock=lambda: END).register("adapter", adapter)
        routes = replace(
            installed.producer._offerings,
            official_offering_id=binding.source_id if policy == SOURCE_FINAL else None,
            snapshot_offering_id=binding.source_id if policy == CUTOFF else None,
        )
        producer = ReportingProducer(
            source=registered.executor,
            offerings=routes,
            store=h.store,
            object_reader=registered.object_reader,
        )
        public = installed.offering.model_copy(update={"supported_finality": ["official"]})
        offering = ReportingProductionOffering(public, producer, key, binding.source_id)
        raw = registered.executor.capabilities.model_dump(mode="json")
        raw["offerings"][0]["formats"] = [
            {"media_type": "application/x-ndjson", "compression": "gzip"}
        ]
        raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
        registered.executor._capabilities = ReportingSourceCapabilitiesV1.model_validate(raw)
        with pytest.raises(ReportingWriterError, match="UNSUPPORTED_VERIFICATION"):
            offering.check_source(effective=True)
        assert not binding.requests


def wire_configuration(h):
    config, binding = h.item.config, h.item.binding
    offering = h.production.offerings[0]
    return {
        "delivery_config_id": config.delivery_config_id,
        "delivery_config_version": config.delivery_config_version,
        "offering_id": offering.offering_id,
        "active": config.deactivated_at is None,
        "feed_purpose": config.feed_purpose,
        "report_definition_id": config.report_definition_id,
        "reporting_profile": config.reporting_profile,
        "scope": {"media_buy_ids": list(config.media_buy_ids)},
        "coverage_requirement": "full",
        "required_finality": config.required_finality,
        "reconciliation_mode": binding.reconciliation_mode,
        "schedule": offering.configuration_schedule(config),
        "method": h.item.writer.configuration_binding(binding).wire(),
    }


async def test_typed_official_finality_admission_requires_selected_policy(tmp_path):
    async with production_harness("memory", tmp_path / "destination.sqlite", count=0) as h:
        config = replace(h.item.config, required_finality="official")
        wire = {**wire_configuration(h), "required_finality": "official"}
        with pytest.raises(ValueError, match="selected finality policy"):
            ReportingConfigurationAdmission(
                h.production.offerings[0].offering_id,
                config,
                h.item.binding,
                configuration_wire=wire,
            )


def state_for(h, configuration):
    ids = list(h.item.config.media_buy_ids)
    coverage = {
        "status": "full",
        "evaluated_at": END.isoformat(),
        "media_buy_ids": ids,
        "fully_covered_media_buy_ids": ids,
        "partially_covered_media_buy_ids": [],
        "unsupported_media_buy_ids": [],
        "unknown_media_buy_ids": [],
        "package_ids": [],
        "covered_package_ids": [],
        "unsupported_package_ids": [],
        "unknown_package_ids": [],
        "limitations": [],
    }
    return {
        "configuration": configuration,
        "state": "ready",
        "destination_ref": h.item.binding.destination_ref,
        "validated_at": END.isoformat(),
        "activated_at": h.item.config.activated_at.isoformat(),
        "current_coverage": coverage,
    }


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("fractional_offset", [False, True])
async def test_mounted_configuration_is_the_admitted_method_scope_and_schedule(
    backend, notifications, fractional_offset, tmp_path
):
    selected = {}

    async def handle(request, context, admit):
        h = selected["h"]
        wire = request["accounts"][0]["reporting_delivery_configs"][0]
        await admit(
            ReportingConfigurationAdmission(
                h.production.offerings[0].offering_id,
                h.item.config,
                h.item.binding,
                configuration_wire=wire,
            )
        )
        state = state_for(h, copy.deepcopy(wire))
        if fractional_offset:
            at = h.item.config.activated_at
            assert at.microsecond == 0
            state["activated_at"] = (
                at.astimezone(timezone(timedelta(hours=5, minutes=45))).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                )
                + ".00000+05:45"
            )
        if selected.get("response_mutation"):
            state["configuration"]["method"]["destination"]["location"] = "other/location"
        return {
            "accounts": [
                {
                    "account_id": h.item.config.account_id,
                    "brand": {"domain": "advertiser.example.test"},
                    "operator": "buyer.example.test",
                    "action": "unchanged",
                    "status": "active",
                    "billing": "operator",
                    "timezone": "UTC",
                    "reporting_delivery_configs": [state],
                }
            ]
        }

    async with production_harness(
        backend,
        tmp_path / "destination.sqlite",
        notifications=notifications,
        count=0,
        account_handler=handle,
    ) as h:
        selected["h"] = h
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        desired = wire_configuration(h)
        request = {
            "idempotency_key": "configuration-exact-replay-0001",
            "accounts": [
                {
                    "account": {"account_id": h.item.config.account_id},
                    "reporting_delivery_configs": [desired],
                }
            ],
        }
        async with mounted.client() as client:
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, result = await mounted.call(
                    client, "sync_accounts", request, transport=transport
                )
                assert result.get("status") == "completed", result
                validator = get_named_validator("account/sync-accounts-response.json")
                assert not list(validator.iter_errors(result)), result
                assert (
                    result["accounts"][0]["reporting_delivery_configs"][0]["configuration"]
                    == desired
                )
                expected_time = h.item.config.activated_at.isoformat()
                if fractional_offset:
                    expected_time = (
                        h.item.config.activated_at.astimezone(
                            timezone(timedelta(hours=5, minutes=45))
                        ).strftime("%Y-%m-%dT%H:%M:%S")
                        + ".00000+05:45"
                    )
                assert (
                    result["accounts"][0]["reporting_delivery_configs"][0]["activated_at"]
                    == expected_time
                )
            before = await h.image()
            frozen = await source_documents(h)
            for where, name, value in (
                ("method.destination", "location", "different/provider/location"),
                ("method.destination", "provider", {"domain": "different.example.test"}),
                ("schedule", "delivery_sla", "PT2H"),
                ("scope", "media_buy_ids", ["unrelated-media-buy"]),
                ("", "report_definition_id", "different-definition"),
            ):
                bad = copy.deepcopy(request)
                target = bad["accounts"][0]["reporting_delivery_configs"][0]
                for segment in where.split(".") if where else ():
                    target = target[segment]
                target[name] = value
                _, rejected = await mounted.call(client, "sync_accounts", bad)
                assert error_code(rejected) == "REPORTING_CONFIGURATION_UNAVAILABLE", rejected
                assert "different/provider" not in json.dumps(rejected)
                assert await h.image() == before
                assert await source_documents(h) == frozen
            selected["response_mutation"] = True
            _, rejected = await mounted.call(client, "sync_accounts", request)
            assert error_code(rejected) == "REPORTING_CONFIGURATION_UNADMITTED", rejected
            assert "other/location" not in json.dumps(rejected)
            selected.pop("response_mutation")
            # Replays reauthorize the same consumer, even after an earlier
            # accepted configuration and with the same transport key.
            h.authorized_bindings.clear()
            _, rejected = await mounted.call(client, "sync_accounts", request, transport="a2a-1.0")
            assert error_code(rejected) == "UNAUTHORIZED", rejected


async def destination_documents(h):
    if h.pool is None:
        return copy.deepcopy(h.store._production_destination_bindings)
    async with h.pool.connection() as connection:
        return await (
            await connection.execute(
                "SELECT * FROM reporting_production_destination_bindings"
                " ORDER BY account_id,consumer_id,delivery_config_id,delivery_config_version"
            )
        ).fetchall()


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_provider_binding_is_frozen_across_restart_and_later_mutation(
    backend, notifications, tmp_path
):
    path = tmp_path / "destination.sqlite"
    async with production_harness(backend, path, notifications=notifications, count=0) as h:
        support, item = h.production, h.item
        await support.activate(account_id=item.config.account_id)
        frozen = await destination_documents(h)
        source = await source_documents(h)
        request = feed_request(item)
        first = await h.store.read_reporting_feed(request, caller=item.binding.principal)
        walk_before = await walk(h.store, request, item.binding.principal, first=first)
        with sqlite3.connect(path) as connection:
            original = connection.execute(
                "SELECT method FROM methods WHERE binding=?", (binding_fingerprint(item.binding),)
            ).fetchone()[0]
            changed = json.loads(original)
            changed["destination"]["location"] = "new-location-same-label"
            connection.execute(
                "UPDATE methods SET method=? WHERE binding=?",
                (json.dumps(changed), binding_fingerprint(item.binding)),
            )
        assert item.writer.configuration_binding(item.binding).wire() == changed
        before = await h.image()
        with pytest.raises(ReportingNotificationError, match="destination_conflict"):
            await h.store.admit_production_configuration(
                item.config, item.binding, offering_id=support.offerings[0].offering_id
            )
        assert await h.image() == before
        denied = await item.claim()
        assert not isinstance(denied, ReportingMaterializerLease), denied
        assert item.writer.writes == 0
        assert await destination_documents(h) == frozen
        assert await source_documents(h) == source
        assert await walk(h.store, request, item.binding.principal, first=first) == walk_before
        await support.aclose()
        async with production_harness(
            backend,
            path,
            notifications=notifications,
            count=0,
            existing_store=h.store if h.pool is None else None,
            existing_pool=h.pool,
        ) as restarted:
            assert await destination_documents(restarted) == frozen
            assert await source_documents(restarted) == source
            assert (
                await walk(restarted.store, request, item.binding.principal, first=first)
                == walk_before
            )
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE methods SET method=? WHERE binding=?",
                    (original, binding_fingerprint(item.binding)),
                )
            await restarted.store.admit_production_configuration(
                item.config, item.binding, offering_id=restarted.production.offerings[0].offering_id
            )
            assert await destination_documents(restarted) == frozen
            # The source mapping is also a semantic-generation contract, not
            # just a lookup by business keys. Same key, changed definition fails.
            old = restarted.production.offerings[0].producer._source.bindings[
                item.config.generation_key
            ]
            changed_config = replace(item.config, account_timezone="Etc/UTC")
            with pytest.raises(ReportingWriterError):
                old.check(
                    changed_config,
                    restarted.production.offerings[0].producer._source.capabilities,
                    restarted.production.offerings[0].source_offering_id,
                )
            assert await source_documents(restarted) == source
