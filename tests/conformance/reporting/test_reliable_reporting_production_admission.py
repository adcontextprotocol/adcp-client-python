"""Actual mounted typed admission and indexed recovery through the service bridge."""

import asyncio
import copy
import hashlib
import json
import os
import sys
from dataclasses import replace

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.production import (
    ReportingConfigurationAdmission,
    ReportingProductionSourceRegistry,
)
from adcp.reporting.production.handler import ReportingProductionHandler
from adcp.reporting.service import ReliableReportingService, ReportingAccountContext
from adcp.server.base import ADCPHandler
from adcp.server.mcp_tools import get_tools_for_handler
from adcp.server.responses import capabilities_response

from ._generation_support import obligation_for
from ._production_context_source import DurableBindingSource
from ._production_support import production_harness
from ._production_transport import MountedProduction
from .test_reporting_production_bindings import source_documents
from .test_reporting_production_configuration import state_for, wire_configuration
from .test_reporting_production_lock_order import source_turn


class Application(ADCPHandler):
    async def get_products(self, params, context=None):
        return {"products": []}

    async def get_media_buy_delivery(self, params, context=None):
        return {"media_buy_deliveries": [], "aggregated_totals": {"impressions": 17}}

    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"], sandbox=True)


def registry_factory(selected):
    def make(offerings):
        def resolve(config):
            selected.setdefault("resolved", []).append(config.generation_key)
            p = offerings[0].producer._offerings
            context = ReportingAccountContext(
                config.account_id,
                "adapter",
                p.currency,
                p.source_scope,
                account_timezone=config.account_timezone,
                snapshot_offering_id=p.snapshot_offering_id,
                official_offering_id=p.official_offering_id,
                publication_namespace=p.publication_namespace,
                requested_metrics=p.requested_metrics,
                requested_dimensions=p.requested_dimensions,
                slice_timeout=p.slice_timeout,
            )
            return replace(context, **selected.get("context_change", {}))

        registry = ReportingProductionSourceRegistry(account_context=resolve)
        registry.register("adapter", offerings[0].producer)
        return registry

    return make


def service_factory(support):
    service = ReliableReportingService.from_production(support)
    handler = service.install(Application())
    assert handler is support.handler and type(handler) is ReportingProductionHandler
    return service


async def account_task(selected, request, context, admit):
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
                "reporting_delivery_configs": [state_for(h, wire)],
            }
        ]
    }


async def test_delegate_cannot_replace_production_wire_versions(tmp_path):
    class LegacyApplication(Application):
        async def get_adcp_capabilities(self, params, context=None):
            return capabilities_response(
                ["signals"],
                major_versions=[2],
                adcp_version="2.5",
                supported_versions=["2.5"],
                build_version="1.2.3",
                sandbox=True,
                idempotency={"supported": False},
            )

    application = LegacyApplication()
    assert not hasattr(application, "get_adcp_version")

    def install(support):
        service = ReliableReportingService.from_production(support)
        service.install(application)
        return service

    async with production_harness(
        "memory",
        tmp_path / "delegated-version.sqlite",
        count=0,
        source_publication=True,
        source_registry_factory=registry_factory({}),
        service_factory=install,
    ) as h:
        response = await h.production.handler.get_adcp_capabilities({})
        assert response["adcp_version"] == h.production._protocol_version
        assert response["adcp"]["supported_versions"] == [h.production._protocol_version]
        assert response["adcp"]["major_versions"] == [3]
        assert response["adcp"]["build_version"] == "1.2.3"
        assert response["sandbox"] is True
        assert response["supported_protocols"] == ["signals", "media_buy"]
        assert await h.production.handler.get_products({}) == {"products": []}
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        async with mounted.client() as client:
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, served = await mounted.call(
                    client, "get_adcp_capabilities", {}, transport=transport
                )
                assert served.get("status") == "completed", served
                assert served["adcp_version"] == h.production._protocol_version
                assert served["adcp"]["supported_versions"] == [h.production._protocol_version]
                assert served["adcp"]["major_versions"] == [3]
                assert served["adcp"]["build_version"] == "1.2.3"


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_late_account_mounted_admission_freezes_context_and_uses_indexed_lease(
    backend, tmp_path
):
    selected = {}

    async def handle(request, context, admit):
        return await account_task(selected, request, context, admit)

    async with production_harness(
        backend,
        tmp_path / "destination.sqlite",
        count=0,
        source_publication=True,
        account_handler=handle,
        source_registry_factory=registry_factory(selected),
        service_factory=service_factory,
    ) as h:
        assert h.service.ready and not h.service._bindings
        assert not await source_documents(h)
        late = replace(h.item.config, account_id="late-account")
        h.item.config = late
        h.item.binding = replace(h.item.binding, generation_key=late.generation_key)
        h.item.obligation = obligation_for(late)
        h.item.writer.grant(h.item.binding)
        producer = h.production.offerings[0].producer
        producer._source.bind_generation(late)
        selected["h"] = h
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        request = {
            "idempotency_key": "service-context-admission-0001",
            "accounts": [
                {
                    "account": {"account_id": late.account_id},
                    "reporting_delivery_configs": [wire_configuration(h)],
                }
            ],
        }
        async with mounted.client() as client:
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, response = await mounted.call(
                    client, "sync_accounts", request, transport=transport
                )
                assert response.get("status") == "completed", response
            frozen = await source_documents(h)
            assert len(frozen) == 1
            doc = frozen[0][3]
            assert doc["service_context"]["account_id"] == late.account_id
            assert (
                doc["service_context_sha256"]
                == hashlib.sha256(canonical_json_utf8_v1(doc["service_context"])).hexdigest()
            )
            for change in (
                {"currency": "EUR"},
                {"adapter": "missing"},
                {"requested_metrics": ("impressions",)},
            ):
                selected["context_change"] = change
                _, denied = await mounted.call(client, "sync_accounts", request)
                assert denied.get("status") != "completed"
                assert await source_documents(h) == frozen
            selected.pop("context_change")
        result = await source_turn(h.production)
        assert result.leased.account_id == late.account_id
        assert len(result.revisions_committed) == 1
        assert producer._source.requests[-1].identity.account_id == late.account_id
        names = {
            t["name"] for t in get_tools_for_handler(h.production.handler, _include_schemas=False)
        }
        assert "get_products" in names and "create_media_buy" not in names
        assert await h.production.handler.get_products({}) == {"products": []}
        assert (await h.production.handler.get_media_buy_delivery({}))["aggregated_totals"][
            "impressions"
        ] == 17
        before = len(selected["resolved"])
        assert (
            h.production._check_source_binding(
                late, h.production.offerings[0]._producer_key, doc
            ).document()
            == doc
        )
        assert len(selected["resolved"]) == before


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_failed_context_admission_rolls_back_configuration_and_destination(backend, tmp_path):
    selected = {"context_change": {"currency": "EUR"}}
    async with production_harness(
        backend,
        tmp_path / "rollback.sqlite",
        count=0,
        source_publication=True,
        source_registry_factory=registry_factory(selected),
        service_factory=service_factory,
    ) as h:
        new = replace(h.item.config, delivery_config_version=2)
        binding = replace(h.item.binding, generation_key=new.generation_key)
        h.item.writer.grant(binding)
        h.production.offerings[0].producer._source.bind_generation(new)
        old = h.item.config
        h.item.config, h.item.binding = new, binding
        value = ReportingConfigurationAdmission(
            h.production.offerings[0].offering_id,
            new,
            binding,
            configuration_wire=wire_configuration(h),
        )
        with pytest.raises(ValueError, match="profile"):
            await h.production._admit_configuration(value)
        assert not await source_documents(h)
        assert new.generation_key not in {
            c.generation_key for c in await h.store.list_configurations(account_id=new.account_id)
        }
        assert old.generation_key != new.generation_key


async def test_context_insert_guard_rejects_tampering_and_keeps_legacy_documents(tmp_path):
    raise_exception = pytest.importorskip("psycopg").errors.RaiseException

    from adcp.reporting.production.schema import validate_production_schema

    selected = {}
    async with production_harness(
        "postgres",
        tmp_path / "guard.sqlite",
        count=0,
        source_publication=True,
        source_registry_factory=registry_factory(selected),
        service_factory=service_factory,
    ) as h:
        offering = h.production.offerings[0]
        context = await h.production.source_registry.resolve(
            h.item.config, offering, offering.check_source(effective=True)
        )
        document = h.production._source_binding(
            h.item.config, offering._producer_key, service_context=context
        ).document()
        identity = (
            h.item.config.account_id,
            h.item.config.delivery_config_id,
            h.item.config.delivery_config_version,
        )
        async with h.pool.connection() as connection:
            await validate_production_schema(connection, service_context=True)
            for field, value in (
                ("service_context_sha256", "0" * 64),
                ("account_id", "other"),
                ("configuration_sha256", "0" * 64),
                ("delivery_config_version", 9),
            ):
                mutated = {**document, field: value}
                with pytest.raises(raise_exception, match="context is inconsistent"):
                    async with connection.transaction():
                        await connection.execute(
                            "INSERT INTO reporting_production_generations"
                            " VALUES(%s,%s,%s,%s,%s::jsonb)",
                            (*identity, offering._producer_key, json.dumps(mutated)),
                        )
            for field, value in (
                ("account_id", "other"),
                ("account_timezone", "Europe/Paris"),
                ("version", 2),
                ("capabilities_sha256", "0" * 64),
            ):
                mutated = copy.deepcopy(document)
                mutated["service_context"][field] = value
                mutated["service_context_sha256"] = hashlib.sha256(
                    canonical_json_utf8_v1(mutated["service_context"])
                ).hexdigest()
                with pytest.raises(raise_exception, match="context is inconsistent"):
                    async with connection.transaction():
                        await connection.execute(
                            "INSERT INTO reporting_production_generations"
                            " VALUES(%s,%s,%s,%s,%s::jsonb)",
                            (*identity, offering._producer_key, json.dumps(mutated)),
                        )
            malformed = copy.deepcopy(document)
            del malformed["capabilities_sha256"]
            del malformed["service_context"]["capabilities_sha256"]
            malformed["service_context"]["unknown"] = 42
            malformed["service_context_sha256"] = hashlib.sha256(
                canonical_json_utf8_v1(malformed["service_context"])
            ).hexdigest()
            with pytest.raises(raise_exception, match="context is inconsistent"):
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO reporting_production_generations"
                        " VALUES(%s,%s,%s,%s,%s::jsonb)",
                        (*identity, offering._producer_key, json.dumps(malformed)),
                    )
            await connection.execute(
                "INSERT INTO reporting_production_generations VALUES(%s,%s,%s,%s,%s::jsonb)",
                (*identity, offering._producer_key, json.dumps(document)),
            )
            for statement in (
                "UPDATE reporting_production_generations SET source_binding=source_binding"
                ' || \'{"service_context_sha256":"bad"}\'',
                "DELETE FROM reporting_production_generations",
            ):
                with pytest.raises(pytest.importorskip("psycopg").errors.CheckViolation):
                    async with connection.transaction():
                        await connection.execute(statement)
            before = (await source_documents(h))[0][3]
            await h.store.create_schema()
            await validate_production_schema(connection, service_context=True)
            assert (await source_documents(h))[0][3] == before


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_enrollment_fault_rolls_back_whole_admission(backend, tmp_path, monkeypatch):
    selected = {}
    async with production_harness(
        backend,
        tmp_path / "atomic.sqlite",
        count=0,
        source_publication=True,
        source_registry_factory=registry_factory(selected),
        service_factory=service_factory,
    ) as h:
        new = replace(h.item.config, delivery_config_version=2)
        binding = replace(h.item.binding, generation_key=new.generation_key)
        h.item.writer.grant(binding)
        h.production.offerings[0].producer._source.bind_generation(new)
        h.item.config, h.item.binding = new, binding
        value = ReportingConfigurationAdmission(
            h.production.offerings[0].offering_id,
            new,
            binding,
            configuration_wire=wire_configuration(h),
        )
        if backend == "memory":
            original = h.store._enroll

            def broken(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("injected after context enrollment")

            monkeypatch.setattr(h.store, "_enroll", broken)
        else:
            original = h.store._enroll_on

            async def broken(*args, **kwargs):
                await original(*args, **kwargs)
                raise RuntimeError("injected after context enrollment")

            monkeypatch.setattr(h.store, "_enroll_on", broken)
        with pytest.raises(RuntimeError, match="injected"):
            await h.production._admit_configuration(value)
        assert not await source_documents(h)
        assert new.generation_key not in {
            c.generation_key for c in await h.store.list_configurations(account_id=new.account_id)
        }


async def test_service_failure_stops_admission_but_preserves_ordinary_delegate(tmp_path):
    from adcp.reporting.service import (
        ReliableReportingServiceError,
        ReliableReportingUnavailableError,
    )

    async with production_harness(
        "memory",
        tmp_path / "lifetime.sqlite",
        count=0,
        source_registry_factory=registry_factory({}),
        service_factory=service_factory,
    ) as h:
        task = h.production._task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(ReliableReportingServiceError):
            await asyncio.wait_for(h.service.wait(), 2)
        assert not h.service.ready
        with pytest.raises(ReliableReportingUnavailableError):
            await h.production.handler.sync_accounts({})
        assert await h.production.handler.get_products({}) == {"products": []}
        assert (await h.production.handler.get_media_buy_delivery({}))["aggregated_totals"][
            "impressions"
        ] == 17


async def test_fresh_process_recovers_late_account_without_context_resolver(tmp_path):
    selected = {}

    async def handle(request, context, admit):
        return await account_task(selected, request, context, admit)

    path = tmp_path / "restart.sqlite"
    async with production_harness(
        "postgres",
        path,
        count=0,
        source_publication=True,
        source_factory=DurableBindingSource,
        account_handler=handle,
        source_registry_factory=registry_factory(selected),
        service_factory=service_factory,
    ) as h:
        late = replace(h.item.config, account_id="late-after-empty-start")
        h.item.config, h.item.obligation = late, obligation_for(late)
        h.item.binding = replace(h.item.binding, generation_key=late.generation_key)
        h.item.writer.grant(h.item.binding)
        h.production.offerings[0].producer._source.bind_generation(late)
        selected["h"] = h
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        async with mounted.client() as client:
            _, result = await mounted.call(
                client,
                "sync_accounts",
                {
                    "idempotency_key": "fresh-process-admission-0001",
                    "accounts": [
                        {
                            "account": {"account_id": late.account_id},
                            "reporting_delivery_configs": [wire_configuration(h)],
                        }
                    ],
                },
            )
            assert result.get("status") == "completed", result
        document = (await source_documents(h))[0][3]
        await h.service.close()
        async with h.pool.connection() as connection:
            schema = (await (await connection.execute("SELECT current_schema()")).fetchone())[0]
        env = {**os.environ, "ADCP_CONTEXT_TEST_SCHEMA": schema}
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.conformance.reporting._production_context_worker",
            str(path),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(worker.communicate(), 90)
        assert worker.returncode == 0, err.decode()
        recovered = json.loads(out)
        assert recovered == {
            "resolved": 0,
            "local_bindings": 0,
            "requests": [late.account_id],
            "contexts": [document["service_context_sha256"]],
            "ready": True,
        }


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_legacy_generation_is_never_guessed_or_backfilled(backend, tmp_path):
    path = tmp_path / "legacy.sqlite"
    async with production_harness(backend, path, count=0, source_publication=True) as original:
        await original.production.activate(account_id=original.item.config.account_id)
        before = await source_documents(original)
        assert "service_context" not in before[0][3]
        await original.production.aclose()
        async with production_harness(
            backend,
            path,
            count=0,
            source_publication=True,
            existing_store=original.store if backend == "memory" else None,
            existing_pool=original.pool,
            source_registry_factory=registry_factory({}),
            service_factory=service_factory,
        ) as h:
            offering = h.production.offerings[0]
            from adcp.reporting.materializer.contracts import ReportingWriterError

            with pytest.raises(ReportingWriterError) as refused:
                h.production._check_source_binding(
                    h.item.config, offering._producer_key, before[0][3]
                )
            assert refused.value.failure.code == "BINDING_MISMATCH"
            assert offering.producer._source.requests == []
            value = ReportingConfigurationAdmission(
                offering.offering_id,
                h.item.config,
                h.item.binding,
                configuration_wire=wire_configuration(h),
            )
            from adcp.reporting.ledger.notification_models import ReportingNotificationError

            with pytest.raises(ReportingNotificationError, match="source_conflict"):
                await h.production._admit_configuration(value)
            assert await source_documents(h) == before


async def test_delegate_collision_and_actual_mount_mutation_fail_closed(tmp_path):
    class Conflicting(Application):
        async def sync_accounts(self, params, context=None):
            return {"accounts": []}

    def refused(support):
        service = ReliableReportingService.from_production(support)
        with pytest.raises(ValueError, match="conflict"):
            service.install(Conflicting())
        service.install(Application())
        return service

    async with production_harness(
        "memory",
        tmp_path / "mount.sqlite",
        count=0,
        source_registry_factory=registry_factory({}),
        service_factory=refused,
    ) as h:
        assert type(h.production.handler) is ReportingProductionHandler
        assert h.production._mounted()
        h.production.handler.sync_accounts = Application().sync_accounts
        from adcp.reporting.ledger.notification_models import ReportingNotificationError

        with pytest.raises(ReportingNotificationError):
            h.production._assert_components()


async def test_production_refuses_an_unused_local_adapter_registry(tmp_path):
    from adcp.reporting.service import ReliableReportingConfigurationError

    def unused(support):
        service = service_factory(support)
        producer = support.offerings[0].producer
        service.sources.register_executor(
            "ignored", producer._source, object_reader=producer._object_reader
        )
        return service

    with pytest.raises(ReliableReportingConfigurationError, match="frozen source registry"):
        async with production_harness(
            "memory",
            tmp_path / "unused.sqlite",
            count=0,
            source_registry_factory=registry_factory({}),
            service_factory=unused,
        ):
            pytest.fail("an unused adapter must not be silently accepted")
