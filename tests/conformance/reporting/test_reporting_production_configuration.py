"""Frozen provider configuration, mounted admission and semantic replay checks."""

import copy
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adcp.reporting.ledger import ReportingScheduleSpec
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.materializer.contracts import ReportingWriterError, binding_fingerprint
from adcp.reporting.materializer.work import ReportingMaterializerLease
from adcp.reporting.production.configuration import ReportingConfigurationAdmission
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


@pytest.mark.parametrize(
    "policy,offered_zone,accepted",
    [
        ("account_resolved", None, True),
        ("fixed", "America/Los_Angeles", False),
    ],
)
async def test_source_timezone_admission_uses_the_resolved_network_zone(
    policy, offered_zone, accepted, tmp_path
):
    class AccountResolvedSource(Source):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            raw = self.capabilities.model_dump(mode="json", exclude_none=True)
            raw["offerings"][0].pop("source_timezone")
            raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
            self.capabilities = ReportingSourceCapabilitiesV1.model_validate(raw)

    offered_schedule = {
        "period_duration": "P1D",
        "delivery_sla": "PT1H",
        "alignment": "source_timezone",
        "period_timezone_policy": policy,
    }
    if offered_zone is not None:
        offered_schedule["period_timezone"] = offered_zone
    async with production_harness(
        "memory",
        tmp_path / "destination.sqlite",
        count=0,
        source_factory=AccountResolvedSource,
        offering_schedule=offered_schedule,
    ) as h:
        installed = h.production.offerings[0]
        source = installed.producer._source
        assert source.capabilities.scope == "effective_account"
        assert source.capabilities.offering(installed.source_offering_id).source_timezone is None
        config = replace(
            h.item.config,
            delivery_config_version=2,
            account_timezone="UTC",
            activated_at=datetime(2026, 3, 8, 5, tzinfo=timezone.utc),
            schedule=ReportingScheduleSpec("P1D", "PT1H", "source_timezone", "America/New_York"),
        )
        binding = replace(h.item.binding, generation_key=config.generation_key)
        source.bind_generation(config)
        h.item.writer.grant(binding)
        await h.production.activate(account_id=config.account_id)
        if accepted:
            await h.store.admit_production_configuration(
                config, binding, offering_id=installed.offering_id
            )
            assert installed.configuration_schedule(config) == {
                "period_duration": "P1D",
                "delivery_sla": "PT1H",
                "alignment": "source_timezone",
                "period_timezone": "America/New_York",
            }
            assert config in await h.store.list_configurations(
                caller=binding.principal, delivery_config_ids=[config.delivery_config_id]
            )
        else:
            with pytest.raises(ReportingWriterError) as denied:
                await h.store.admit_production_configuration(
                    config, binding, offering_id=installed.offering_id
                )
            assert denied.value.failure.code == "BINDING_MISMATCH"
            assert config not in await h.store.list_configurations(
                caller=binding.principal, delivery_config_ids=[config.delivery_config_id]
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


async def test_production_refuses_all_media_buys_at_admission(tmp_path):
    from adcp.reporting.ledger import CompleteMediaBuyScope, LedgerConflictError

    async with production_harness("memory", tmp_path / "destination.sqlite", count=0) as h:
        explicit = wire_configuration(h)

        def admission(config, scope):
            wire = {**explicit, "scope": scope, "required_finality": config.required_finality}
            return ReportingConfigurationAdmission(
                h.production.offerings[0].offering_id,
                config,
                h.item.binding,
                configuration_wire=wire,
            )

        # Explicit media_buy_ids admit as before, with no resolver wired.
        config = h.item.config
        admission(config, {"media_buy_ids": list(config.media_buy_ids)}).check(h.production)
        all_scope = replace(
            h.item.config,
            media_buy_ids=(),
            media_buy_scope="all_media_buys",
            required_finality="snapshot",
        )

        async def resolver(configuration, period):
            return CompleteMediaBuyScope(())

        # Refused before admission, with or without a resolver and at either
        # finality: production binds one source buy set per generation.
        for wired in (None, resolver):
            h.production.media_buy_scope_resolver = wired
            for finality in ("snapshot", "official"):
                generation = replace(all_scope, required_finality=finality)
                with pytest.raises(
                    LedgerConflictError, match="per-period production source binding"
                ) as refused:
                    admission(generation, {"all_media_buys": True}).check(h.production)
                assert refused.value.code == "UNSUPPORTED_FEATURE"
        # all_media_buys is never an explicit empty list, in either direction.
        explicit_empty = replace(h.item.config, media_buy_ids=(), required_finality="snapshot")
        for config, scope in (
            (explicit_empty, {"all_media_buys": True}),
            (all_scope, {"media_buy_ids": list(h.item.config.media_buy_ids)}),
        ):
            with pytest.raises(ReportingWriterError) as mismatch:
                admission(config, scope).check(h.production)
            assert mismatch.value.failure.code == "BINDING_MISMATCH"


async def test_truthful_incomplete_ready_coverage_is_action_required(tmp_path):
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
        ids = state["current_coverage"]["media_buy_ids"]
        state["current_coverage"].update(
            status="unknown",
            fully_covered_media_buy_ids=[],
            unknown_media_buy_ids=ids,
            limitations=[{"reason": "capability_unknown", "media_buy_id": ids[0]}],
        )
        selected["coverage"] = copy.deepcopy(state["current_coverage"])
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
        "memory", tmp_path / "destination.sqlite", count=0, account_handler=handle
    ) as h:
        selected["h"] = h
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        request = {
            "idempotency_key": "configuration-incomplete-0001",
            "accounts": [
                {
                    "account": {"account_id": h.item.config.account_id},
                    "reporting_delivery_configs": [wire_configuration(h)],
                }
            ],
        }
        async with mounted.client() as client:
            _, result = await mounted.call(client, "sync_accounts", request)
        assert result.get("status") == "completed", result
        validator = get_named_validator("account/sync-accounts-response.json")
        assert not list(validator.iter_errors(result)), result
        (state,) = result["accounts"][0]["reporting_delivery_configs"]
        assert state["state"] == "action_required"
        assert state["current_coverage"] == selected["coverage"]
        (issue,) = state["issues"]
        assert (issue["code"], issue["severity"], issue["recommended_action"]) == (
            "REPORTING_COVERAGE_INCOMPLETE",
            "action_required",
            "change_reporting_scope",
        )
        assert issue["media_buy_ids"] == selected["coverage"]["media_buy_ids"]
