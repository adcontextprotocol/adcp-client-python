"""Adapter registration builds a durable source and the actual production graph."""

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from adcp.decisioning.capabilities import Account as AccountCapabilities
from adcp.reporting import (
    DeterministicReportingClock,
    ReliableReportingService,
    ReportingAccountContext,
    ReportingProductionOptions,
    ReportingServiceOffering,
    ScriptedReportingAdapter,
)
from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import InMemorySealStore, InMemoryStagingStore
from adcp.reporting.inline_storage import PgReportingSealStore, PgReportingStagingStore
from adcp.reporting.ledger import ProducerOfferings, ReportingDestinationBinding
from adcp.reporting.materializer import ReportingRevisionVerifierRegistry
from adcp.reporting.materializer.contracts import ReportingWriterCapability, ReportingWriterError
from adcp.reporting.materializer.reference import reference_verifier
from adcp.reporting.outbox.routing import ReportingEnvelopeCipher
from adcp.reporting.production import (
    ReportingProductionConfigurationTask,
    ReportingProductionSigning,
)
from adcp.reporting.receipts import ReportingReceiptError
from adcp.reporting.source import parse_verified_source_batch_manifest_v1
from adcp.server.responses import capabilities_response
from adcp.server.serve import create_mcp_server
from adcp.types import ReportingDeliveryOffering
from tests.test_reliable_reporting_service import _account_context, _rows

from ._generation_support import END, START, configuration, isolated_reporting_pool, obligation_for
from ._materializer_support import reference_rows
from ._production_support import Source, SQLiteDestination
from ._production_transport import MountedProduction
from ._projection_support import drain
from ._reliable_support import FailurePlan, ScriptedSigning, ScriptedSubscriptions
from .test_reliable_reporting_production_admission import Application, account_task
from .test_reporting_production_configuration import wire_configuration
from .test_reporting_production_lock_order import source_turn


@pytest.mark.parametrize("autocommit", [False, True])
async def test_postgres_factory_replays_source_after_restart_without_refetch(autocommit):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        request = redacted_snapshot_request()
        clock = DeterministicReportingClock(request.period.source_read_cutoff_at)
        first = ReliableReportingService.postgres(
            pool=pool, account_context=_account_context, clock=clock
        )
        adapter = ScriptedReportingAdapter(redacted_capabilities(), [_rows(9)])
        registered = first.sources.register("gam", adapter)
        assert isinstance(registered.object_reader, PgReportingStagingStore)
        assert isinstance(registered.executor._seals, PgReportingSealStore)
        await first.initialize()
        try:
            original = await registered.executor.execute(request, cancel=asyncio.Event())
            assert original.ok, original.error
        finally:
            await first.close()

        restarted = ReliableReportingService.postgres(
            pool=pool, account_context=_account_context, clock=clock
        )
        empty = ScriptedReportingAdapter(redacted_capabilities())
        replay = restarted.sources.register("gam", empty)
        await restarted.initialize()
        try:
            result = await replay.executor.execute(request, cancel=asyncio.Event())
            assert result == original
            assert len(adapter.calls) == 1 and empty.calls == []
            assert result.response is not None
            manifest = parse_verified_source_batch_manifest_v1(
                result.response.manifest, result.manifest_bytes
            )
            rows = []
            for item in manifest.objects:
                payload = await replay.object_reader.read(
                    object_ref=item.object_ref,
                    object_generation=item.object_generation,
                    account_id=request.identity.account_id,
                    source_scope=request.identity.source_scope,
                    cancel=asyncio.Event(),
                )
                rows.extend(json.loads(line) for line in payload.splitlines())
            assert rows == _rows(9)
        finally:
            await restarted.close()
        async with pool.connection() as connection:
            assert await (await connection.execute("SELECT 1")).fetchone() == (1,)


async def test_postgres_factory_preserves_explicit_source_store_overrides():
    async with isolated_reporting_pool() as pool:
        service = ReliableReportingService.postgres(pool=pool, account_context=_account_context)
        staging, seals = InMemoryStagingStore(), InMemorySealStore()
        registered = service.sources.register(
            "gam", ScriptedReportingAdapter(redacted_capabilities()), staging=staging, seals=seals
        )
        assert registered.object_reader is staging and registered.executor._seals is seals
        await service.close()


class Adapter:
    """Provider facts, a normalized fetch and the existing live authority hook."""

    def __init__(self, source):
        self.source = source
        self.capabilities = source.capabilities
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.checks = 0

    def configuration_binding(self, config):
        self.checks += 1
        return self.source.configuration_binding(config)

    async def fetch_slice(self, request):
        self.entered.set()
        await self.release.wait()
        return await self.source.fetch(request)


class FactoryApplication(Application):
    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"], sandbox=True, idempotency={"supported": False})


@asynccontextmanager
async def factory_harness(backend, path, *, push=False, existing_pool=None):
    async with AsyncExitStack() as stack:
        pool = existing_pool
        if backend == "postgres" and pool is None:
            pool = await stack.enter_async_context(isolated_reporting_pool(autocommit=True))
        # PostgreSQL delivery leases use database time. Keep this end-to-end
        # factory clock live; deterministic scheduling is covered in memory.
        clock = (
            (lambda: datetime.now(timezone.utc))
            if pool is not None
            else DeterministicReportingClock(END)
        )
        activated = clock().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        capability = ReportingWriterCapability(
            "warehouse_materialization",
            "fixture-sql",
            "jsonl",
            "canonical_digest",
            "destination",
            "immutable_location",
            "sha256",
            "conditional_create",
        )
        verifier = reference_verifier(capability)
        key = verifier.key
        config = configuration()
        config = replace(
            config,
            activated_at=activated,
            schedule=replace(config.schedule, period_anchor=activated),
            deactivated_at=None,
            definition=key.definition,
            report_definition_id=key.report_definition_id,
        )
        binding = ReportingDestinationBinding(
            config.generation_key,
            "https://buyer.example.test/agent",
            "destination",
            "trusted-provider-binding",
            capability.method,
            capability.transport,
            capability.verification_profile,
            "delivery_only",
            config.feed_purpose,
            400,
            START,
            capability.format,
            ("fixture-sql-v1",),
            "delivered",
        )
        writer = SQLiteDestination(path, key)
        writer.grant(binding)
        source = Source(key, path.with_name("source"), reference_rows(2), clock=clock)
        source.bind_generation(config)
        adapter = Adapter(source)
        profile = ProducerOfferings(
            snapshot_offering_id=source.source_id,
            source_scope=source.capabilities.source_scope,
            publication_namespace=source.capabilities.offerings[0].publication_namespace,
        )
        wire = ReportingDeliveryOffering.model_validate(
            {
                "offering_id": "managed-adapter",
                "feed_purpose": config.feed_purpose,
                "report_definition_id": key.report_definition_id,
                "report_definition_uri": key.definition.report_definition_uri,
                "report_definition_sha256": key.definition.report_definition_sha256,
                "reporting_profile": {
                    "id": key.reporting_profile,
                    "version": key.definition.schema_version,
                    "schema_uri": key.definition.schema_uri,
                    "schema_sha256": key.definition.schema_sha256,
                    "schema_dialect": key.definition.schema_dialect,
                    "schema_ref_policy": key.definition.schema_ref_policy,
                    "grain": "row",
                    "primary_keys": ["row_id"],
                    "canonicalization_id": key.canonicalization.canonicalization_id,
                    "canonicalization_uri": key.canonicalization.canonicalization_uri,
                    "canonicalization_sha256": key.canonicalization.canonicalization_sha256,
                },
                "schedule": {"period_duration": "PT1H", "alignment": "utc", "delivery_sla": "PT1H"},
                "supported_finality": ["snapshot"],
                "reconciliation_mode": "delivery_only",
                "method": writer.delivery_methods[0].wire(),
            }
        )
        h = SimpleNamespace(
            pool=pool,
            clock=clock,
            adapter=adapter,
            source=source,
            resolved=[],
            authorized_bindings={(config.account_id, binding.consumer_id)},
            item=SimpleNamespace(
                config=config,
                binding=binding,
                writer=writer,
                obligation=obligation_for(config),
                verifier=verifier,
            ),
        )

        def context(configuration):
            h.resolved.append(configuration.generation_key)
            return ReportingAccountContext(
                configuration.account_id,
                "adapter",
                profile.currency,
                profile.source_scope,
                account_timezone=configuration.account_timezone,
                snapshot_offering_id=profile.snapshot_offering_id,
                publication_namespace=profile.publication_namespace,
                requested_metrics=profile.requested_metrics,
                requested_dimensions=profile.requested_dimensions,
                slice_timeout=profile.slice_timeout,
            )

        async def accounts(request, context, admit):
            return await account_task({"h": h}, request, context, admit)

        async def authorize(account, context, consumer):
            if (account.get("account_id"), consumer) not in h.authorized_bindings:
                raise ReportingReceiptError("UNAUTHORIZED")
            return account["account_id"]

        notification_inputs = {}
        if push:
            plan = FailurePlan()
            notification_inputs = {
                "subscriptions": ScriptedSubscriptions(plan),
                "cipher": ReportingEnvelopeCipher(b"b" * 32),
                "signing": ReportingProductionSigning(
                    ScriptedSigning(plan),
                    ("ed25519",),
                    brand_json_url="https://seller.example.test/brand.json",
                ),
            }
        options = ReportingProductionOptions(
            (ReportingServiceOffering("adapter", wire, profile, source.source_id, key),),
            writer,
            ReportingRevisionVerifierRegistry((verifier,)),
            ReportingProductionConfigurationTask(
                accounts,
                AccountCapabilities(supported_billing=["operator"], require_operator_auth=True),
            ),
            authorize,
            notifications=push,
            poll_seconds=60,
            **notification_inputs,
        )
        factory = (
            ReliableReportingService.memory if pool is None else ReliableReportingService.postgres
        )
        h.service = factory(
            account_context=context,
            clock=clock,
            production=options,
            **({"pool": pool} if pool is not None else {}),
        )
        h.service.sources.register(
            "adapter",
            adapter,
            constituent_of=lambda row, request: request.coverage.constituents[0].constituent_id,
        )
        handler = h.service.install(FactoryApplication())
        h.production = h.service._production
        h.store = h.service.store
        h.mount = create_mcp_server(handler)
        await h.service.start()
        try:
            yield h
        finally:
            adapter.release.set()
            await h.service.close()


async def admit(h):
    mounted = MountedProduction(h)
    mounted.authorize(h.item)
    async with mounted.client() as client:
        _, response = await mounted.call(
            client,
            "sync_accounts",
            {
                "idempotency_key": "factory-admission-0001",
                "accounts": [
                    {
                        "account": {"account_id": h.item.config.account_id},
                        "reporting_delivery_configs": [wire_configuration(h)],
                    }
                ],
            },
        )
        assert response.get("status") == "completed", response
    return mounted


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("push", [False, True])
async def test_factory_admits_registered_adapter_and_materializes(backend, push, tmp_path):
    async with factory_harness(backend, tmp_path / "destination.sqlite", push=push) as h:
        await admit(h)
        assert h.service.ready
        assert len(h.production.notification_workers) == (3 if push else 0)
        result = await source_turn(h.production)
        assert len(result.revisions_committed) == 1 and not result.slices_failed
        assert h.resolved == [h.item.config.generation_key]
        materialized = await h.production.materializer.run_once()
        assert materialized.state == "verified", materialized
        await drain(h.production.projection, h.item.config.account_id)
        mounted = MountedProduction(h)
        mounted.authorize(h.item)
        async with mounted.client() as client:
            _, exact = await mounted.call(
                client,
                "get_media_buy_delivery",
                {
                    "account": {"account_id": h.item.config.account_id},
                    "reporting_revision_id": result.revisions_committed[0],
                    "pagination": {"max_results": 100},
                },
            )
            assert exact.get("status") == "completed", exact
            assert exact["reporting_rows"] == reference_rows(2)
            _, caps = await mounted.call(client, "get_adcp_capabilities", {})
            assert caps.get("status") == "completed", caps
            assert bool(
                caps["media_buy"].get("reporting_delivery", {}).get("managed_delivery")
            ) == (backend == "postgres")
        assert h.adapter.checks >= 3


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("remap", [False, True])
async def test_factory_revocation_discards_blocked_fetch_and_restoration_retries(
    backend, remap, tmp_path
):
    async with factory_harness(backend, tmp_path / "destination.sqlite") as h:
        await admit(h)
        original = h.source.bindings.pop(h.item.config.generation_key)
        denied = await source_turn(h.production)
        assert not denied.revisions_committed and h.source.requests == []
        h.source.bindings[h.item.config.generation_key] = original
        h.adapter.release.clear()
        task = asyncio.create_task(source_turn(h.production))
        try:
            await asyncio.wait_for(h.adapter.entered.wait(), 10)
            if remap:
                h.source.bind_generation(h.item.config, product_id="catalog-5820")
            else:
                h.source.bindings.pop(h.item.config.generation_key)
        finally:
            h.adapter.release.set()
        if remap:
            with pytest.raises(ReportingWriterError) as denied_publish:
                await asyncio.wait_for(task, 10)
            assert denied_publish.value.failure.code == "BINDING_MISMATCH"
        else:
            discarded = await asyncio.wait_for(task, 10)
            assert not discarded.revisions_committed
        request = h.source.requests[-1]
        seals = h.service.sources.get("adapter").executor._seals
        assert (
            await seals.get(
                account_id=request.identity.account_id,
                source_execution_key=request.identity.source_execution_key,
            )
            is None
        )
        h.source.bindings[h.item.config.generation_key] = original
        restored = await source_turn(h.production)
        assert len(restored.revisions_committed) == 1
        assert len(h.source.requests) == 2
