"""Reporting subscriptions delivered through production managed delivery.

Mirrors adcontextprotocol/adcp-client#3523: the service binds each managed
subscription generation to its destination before its first obligation, freezes
each obligation's constituents from the host's authorized pool, produces and
materializes owed obligations, stamps ``account_id`` on rows, and holds a
delivery when the owner's authority is lost. Each test runs against the memory
store and, with ``ADCP_PG_TEST_URL``, the PostgreSQL production store.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from importlib.resources import files
from types import SimpleNamespace
from typing import Any

import pytest

from adcp.decisioning.capabilities import Account as AccountCapabilities
from adcp.reporting.ledger import (
    LedgerConflictError,
    ProducerOfferings,
    ReportingConfigurationGenerationKey,
    ReportingDestinationBinding,
    ReportingStatusCaller,
    ReportingStatusHandler,
)
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.materializer import ReportingRevisionVerifierRegistry
from adcp.reporting.materializer.contracts import ReportingWriterCapability
from adcp.reporting.materializer.reference import reference_verifier
from adcp.reporting.materializer.verification import ReportingRevisionVerifier
from adcp.reporting.production import (
    ReportingProductionConfigurationTask,
    ReportingProductionSourceBinding,
)
from adcp.reporting.receipts import ReportingReceiptError
from adcp.reporting.service import ReliableReportingService, ReportingAccountContext
from adcp.reporting.service_production import ReportingProductionOptions, ReportingServiceOffering
from adcp.reporting.service_subscriptions import ReportingSubscriptionServiceOptions
from adcp.reporting.subscriptions import (
    AuthorizedAccount,
    ReportingSubscriptionEngine,
    ReportingSubscriptionSupport,
    SubscriptionBuy,
    SubscriptionOffering,
)
from adcp.reporting.testing import DeterministicReportingClock
from adcp.server import PrincipalIdentity, PrincipalService
from adcp.server.responses import capabilities_response
from adcp.server.serve import create_mcp_server
from adcp.types import ReportingDeliveryOffering
from tests.test_reporting_subscription_engine import Clock, Host

from ._generation_support import END, START, isolated_reporting_pool
from ._materializer_support import reference_rows
from ._production_support import Source, SQLiteDestination
from .test_reliable_reporting_production_admission import Application

pytestmark = pytest.mark.parametrize("backend", ["memory", "postgres"])

ACCOUNT = "acct-subscriptions"
AGENCY = PrincipalIdentity("https://agency.example/agent", "buyer_agent")
OFFERING_ID = "managed-adapter"
PATH = "analytics/{account_id}/{period_start}"
DESTINATION = {
    "pattern": "warehouse_materialization",
    "destination_id": "warehouse",
    "active": True,
    "provider": {"domain": "fixture.example.test"},
    "transport": "fixture-sql",
    "location": "project.reporting",
    "accepted_verification_profiles": ["canonical_digest"],
}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _subscription_verifier() -> ReportingRevisionVerifier:
    """The reference definition, with a row schema that admits ``account_id``."""
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
    base = reference_verifier(capability)
    root = files("adcp.reporting.materializer").joinpath("assets")
    schema = json.loads(root.joinpath("reference-row-schema.json").read_text())
    schema["properties"]["account_id"] = {"type": "string", "minLength": 1}
    schema_bytes = json.dumps(schema).encode()
    schema_sha256 = hashlib.sha256(schema_bytes).hexdigest()
    contract = json.loads(base.canonicalization_bytes)
    contract["schema_sha256"] = schema_sha256
    contract_bytes = json.dumps(contract).encode()
    definition = replace(base.key.definition, schema_sha256=schema_sha256)
    canonicalization = replace(
        base.key.canonicalization,
        canonicalization_sha256=hashlib.sha256(contract_bytes).hexdigest(),
    )
    return ReportingRevisionVerifier(
        replace(base.key, definition=definition, canonicalization=canonicalization),
        base.definition_bytes,
        schema_bytes,
        contract_bytes,
    )


class _Adapter:
    def __init__(self, source: Source) -> None:
        self.source, self.capabilities = source, source.capabilities

    def configuration_binding(self, configuration):
        return self.source.configuration_binding(configuration)

    async def fetch_slice(self, request):
        return await self.source.fetch(request)


class _App(Application):
    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"], sandbox=True, idempotency={"supported": False})


async def _store(stack: AsyncExitStack, backend: str, *, notifications: bool) -> Any:
    if backend == "memory":
        return None
    from adcp_reporting_storage.production.pg import PgReportingProductionStore

    pool = await stack.enter_async_context(isolated_reporting_pool(autocommit=True))
    store = PgReportingProductionStore(pool=pool, notifications=notifications)
    await store.create_schema()
    return store


@asynccontextmanager
async def _seller(
    tmp_path,
    backend: str,
    *,
    pool_buys: tuple[str, ...] = ("mb-1",),
    notifications: bool = False,
):
    async with AsyncExitStack() as stack:
        clock = DeterministicReportingClock(END)
        store = await _store(stack, backend, notifications=notifications)
        async with _composed(tmp_path, clock, store, pool_buys, notifications) as h:
            yield h


@asynccontextmanager
async def _composed(tmp_path, clock, store, pool_buys, notifications):
    verifier = _subscription_verifier()
    key = verifier.key
    writer = SQLiteDestination(tmp_path / "destination.sqlite", key)
    source = Source(key, tmp_path / "source", reference_rows(2), clock=clock)
    adapter = _Adapter(source)
    profile = ProducerOfferings(
        snapshot_offering_id=source.source_id,
        source_scope=source.capabilities.source_scope,
        publication_namespace=source.capabilities.offerings[0].publication_namespace,
    )
    wire = ReportingDeliveryOffering.model_validate(
        {
            "offering_id": OFFERING_ID,
            "feed_purpose": "analytics",
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
                "canonicalization_contract_version": "1.0",
                "canonicalization_media_type": (
                    "application/vnd.adcp.reporting-canonicalization+json"
                ),
                "canonicalization_uri": key.canonicalization.canonicalization_uri,
                "canonicalization_sha256": key.canonicalization.canonicalization_sha256,
            },
            "schedule": {"period_duration": "PT1H", "alignment": "utc", "delivery_sla": "PT1H"},
            "supported_finality": ["snapshot"],
            "reconciliation_mode": "delivery_only",
            "method": writer.delivery_methods[0].wire(),
        }
    )

    # The subscription runtime.
    engine_clock = Clock("2026-08-31T22:00:00Z")
    host = Host()
    principals = PrincipalService()
    engine = ReportingSubscriptionEngine(
        principals=principals,
        host=host,
        offerings=[
            SubscriptionOffering(
                offering_id=OFFERING_ID,
                report_definition_id=key.report_definition_id,
                reporting_profile=key.reporting_profile,
                feed_purpose="analytics",
                period_duration="PT1H",
                alignment="utc",
                delivery_sla="PT1H",
                supported_finality=frozenset({"snapshot"}),
                reconciliation_mode="delivery_only",
                dimensions=frozenset({"package"}),
                method_pattern="warehouse_materialization",
                method_transport="fixture-sql",
            )
        ],
        support=ReportingSubscriptionSupport(dimensions=("package",)),
        clock=engine_clock,
    )
    principals.set_reporting_subscriptions(engine.principal_options())
    h = SimpleNamespace(
        clock=clock,
        engine_clock=engine_clock,
        host=host,
        engine=engine,
        principals=principals,
        writer=writer,
        source=source,
        bound=[],
        pool_buys=pool_buys,
    )

    def bind(configuration, version):
        destination = version["destination"]
        binding = ReportingDestinationBinding(
            configuration.generation_key,
            configuration.consumer_id,
            "warehouse-generation-1",
            "trusted-provider-binding",
            "warehouse_materialization",
            "fixture-sql",
            "canonical_digest",
            "delivery_only",
            configuration.feed_purpose,
            400,
            START,
            "jsonl",
            ("fixture-sql-v1",),
            "delivered",
            path_template=destination["path_template"],
        )
        writer.grant(binding)
        # The host's authorized pool for this generation.
        source.bindings[configuration.generation_key] = (
            ReportingProductionSourceBinding.for_configuration(
                configuration,
                capabilities_sha256=source.capabilities.capabilities_sha256,
                media_buy_products=tuple((buy, "catalog-7391") for buy in h.pool_buys),
            )
        )
        h.bound.append(configuration.generation_key)
        return binding

    def context(configuration):
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

    async def accounts(request, context, admit):  # account configurations are not used here
        return {"accounts": []}

    async def authorize(account, context, consumer):
        raise ReportingReceiptError("UNAUTHORIZED")

    options = ReportingProductionOptions(
        (ReportingServiceOffering("adapter", wire, profile, source.source_id, key),),
        writer,
        ReportingRevisionVerifierRegistry((verifier,)),
        ReportingProductionConfigurationTask(
            accounts,
            AccountCapabilities(supported_billing=["operator"], require_operator_auth=True),
        ),
        authorize,
        notifications=notifications,
        poll_seconds=60,
        read_jitter_window=timedelta(0),
    )
    failures: list[Any] = []
    h.failures = failures
    subscriptions = ReportingSubscriptionServiceOptions(
        engine=engine,
        list_principals=lambda: [AGENCY],
        resolve_principal=lambda _request, _context: AGENCY,
        bind_managed_generation=bind,
        planning_error_handler=failures.append,
    )
    h.service = (
        ReliableReportingService.memory(
            account_context=context,
            clock=clock,
            production=options,
            reporting_subscriptions=subscriptions,
        )
        if store is None
        else ReliableReportingService(
            store=store,
            account_context=context,
            clock=clock,
            production=options,
            reporting_subscriptions=subscriptions,
        )
    )
    h.service.sources.register(
        "adapter",
        adapter,
        constituent_of=lambda row, request: request.coverage.constituents[0].constituent_id,
    )
    handler = h.service.install(_App())
    h.mount = create_mcp_server(handler)  # production requires a mounted handler
    h.production = h.service._production
    h.store = h.service.store
    h.pool = getattr(store, "_pool", None)
    try:
        yield h
    finally:
        await h.service.close()


async def _subscribe(h, buys: list[SubscriptionBuy]) -> None:
    h.host.accounts[ACCOUNT] = AuthorizedAccount(
        ACCOUNT, timezone="UTC", authorized_at=at("2026-08-01T00:00:00Z")
    )
    h.host.buys[ACCOUNT] = buys
    response = await h.principals.sync_principal(
        AGENCY,
        {
            "idempotency_key": "production-subscription-0001",
            "configuration": {
                "reporting_destinations": [DESTINATION],
                "reporting_subscriptions": [
                    {
                        "subscription_id": "analytics-hourly",
                        "subscription_version": 1,
                        "active": True,
                        "scope": {"all_authorized_accounts": True},
                        "selector": {
                            "feed_purpose": "analytics",
                            "period_duration": "PT1H",
                            "required_finality": "snapshot",
                            "dimensions": ["package"],
                        },
                        "reconciliation_mode": "delivery_only",
                        "destination": {"destination_id": "warehouse", "path_template": PATH},
                    }
                ],
            },
        },
    )
    assert response.result.kind == "applied", response
    h.engine_clock.set("2026-08-31T22:30:00Z")
    await h.engine.reconcile(AGENCY)
    h.engine_clock.set("2026-09-01T01:00:00Z")


def _buy(media_buy_id: str) -> SubscriptionBuy:
    return SubscriptionBuy(
        media_buy_id=media_buy_id,
        confirmed_at=at("2026-08-31T23:00:00Z"),
        start_time=at("2026-08-31T23:00:00Z"),
        end_time=at("2026-09-01T01:00:00Z"),
        reporting_delivery_offering_ids=(OFFERING_ID,),
    )


async def _advance(h, delta: timedelta) -> None:
    """Advance the service clock; PostgreSQL schedules held work on its own clock."""
    h.clock.advance(delta)
    if h.pool is not None:
        async with h.pool.connection() as connection:
            await connection.execute(
                "UPDATE reporting_production_work SET due_at=due_at-%s WHERE state='pending'",
                (delta,),
            )
            await connection.execute(
                "UPDATE reporting_materializer_accounts SET due_at=due_at-%s"
                " WHERE due_at IS NOT NULL",
                (delta,),
            )


def _delivered(h) -> list[dict[str, Any]]:
    with sqlite3.connect(h.writer.path) as c:
        return [json.loads(row[0]) for row in c.execute("SELECT content FROM artifacts")]


async def test_owed_obligations_are_bound_produced_and_delivered(tmp_path, backend) -> None:
    async with _seller(tmp_path, backend) as h:
        await _subscribe(h, [_buy("mb-1")])
        await h.service.start()  # one full turn: plan, produce, materialize once
        assert h.failures == []
        (key,) = h.bound  # bound once, before the first obligation
        assert key.offering_id == OFFERING_ID
        obligations = await h.store.list_generation_obligations(key)
        assert [o.period.start for o in obligations] == [
            at("2026-08-31T23:00:00Z"),
            at("2026-09-01T00:00:00Z"),
        ]
        constituents = [
            await h.store.producer_constituents(
                (
                    await h.store.list_configurations(
                        caller=ReportingStatusCaller(ACCOUNT, AGENCY.subject)
                    )
                )[0],
                o,
            )
            for o in obligations
        ]
        assert {tuple(c.media_buy_id for c in items) for items in constituents} == {("mb-1",)}
        revisions = [
            await h.store.list_revisions(
                account_id=ACCOUNT, reporting_obligation_id=o.reporting_obligation_id
            )
            for o in obligations
        ]
        assert [len(items) for items in revisions] == [1, 1]
        while (await h.production.materializer.run_once()).state == "verified":
            pass
        delivered = _delivered(h)
        assert len(delivered) == 2, "every owed obligation is materialized"
        rows = [json.loads(row) for artifact in delivered for row in artifact["rows"]]
        assert rows and {row["account_id"] for row in rows} == {ACCOUNT}
        payload = await ReportingStatusHandler(h.store).handle(
            {"view": "periods"}, caller=ReportingStatusCaller(ACCOUNT, AGENCY.subject)
        )
        assert {p["offering_id"] for p in payload["periods"]} == {OFFERING_ID}
        # The mounted handler's hooks add coverage for the owner.
        caller = ReportingDeliveryPrincipal(ACCOUNT, AGENCY.subject)
        covered = await h.production.subscription_hooks.coverage(
            {"status": "completed", "view": "summary"}, caller, {}, None
        )
        assert covered["subscription_coverage"][0]["subscription_id"] == "analytics-hourly"
        # A second turn binds and writes nothing new.
        await h.service._production_subscription_cycle()
        assert len(h.bound) == 1


async def test_a_buy_outside_the_authorized_pool_writes_nothing(tmp_path, backend) -> None:
    async with _seller(tmp_path, backend, pool_buys=("mb-other",)) as h:
        await _subscribe(h, [_buy("mb-1")])
        await h.service.start()
        assert [failure.subscription_id for failure in h.failures] == ["analytics-hourly"]
        (key,) = h.bound
        assert await h.store.list_generation_obligations(key) == ()


async def test_a_lost_grant_holds_delivery(tmp_path, backend) -> None:
    async with _seller(tmp_path, backend) as h:
        await _subscribe(h, [_buy("mb-1")])
        await h.service.start()  # the first turn delivers one of the two obligations
        assert len(_delivered(h)) == 1
        del h.host.accounts[ACCOUNT]
        await h.engine.reconcile(AGENCY)
        with pytest.raises(LedgerConflictError):
            await h.production.subscription_hooks.require(
                ReportingDeliveryPrincipal(ACCOUNT, AGENCY.subject),
                {},
                None,
                "sync_reporting_status",
            )
        held = await h.production.materializer.run_once()
        # Held under the same identity, never failed; nothing more is written.
        assert held.state == "pending", held
        assert len(_delivered(h)) == 1
        # A re-grant delivers it.
        h.host.accounts[ACCOUNT] = AuthorizedAccount(
            ACCOUNT, timezone="UTC", authorized_at=at("2026-08-01T00:00:00Z")
        )
        await h.engine.reconcile(AGENCY)
        await _advance(h, timedelta(minutes=2))
        assert (await h.production.materializer.run_once()).state == "verified"
        assert len(_delivered(h)) == 2


async def test_owed_obligations_are_delivered_with_status_notifications(tmp_path, backend) -> None:
    async with _seller(tmp_path, backend, notifications=True) as h:
        await _subscribe(h, [_buy("mb-1")])
        await h.service.start()
        assert h.failures == []
        while (await h.production.materializer.run_once()).state == "verified":
            pass
        assert len(_delivered(h)) == 2
        payload = await ReportingStatusHandler(h.store).handle(
            {"view": "periods"}, caller=ReportingStatusCaller(ACCOUNT, AGENCY.subject)
        )
        assert {p["offering_id"] for p in payload["periods"]} == {OFFERING_ID}
        if h.pool is not None:
            # Status checkpoints record the subscription generation's offering.
            async with h.pool.connection() as connection:
                offerings = await (
                    await connection.execute(
                        "SELECT DISTINCT offering_id FROM reporting_status_scope_checkpoints"
                    )
                ).fetchall()
            assert offerings == [(OFFERING_ID,)]


async def test_a_second_offering_of_one_version_is_refused(tmp_path, backend) -> None:
    async with _seller(tmp_path, backend) as h:
        await _subscribe(h, [_buy("mb-1")])
        # Another offering of the same subscription version is already enrolled.
        other = (ACCOUNT, AGENCY.subject, "analytics-hourly", 1, "other-offering")
        if h.pool is None:
            h.store._production_owed_sets.add(ReportingConfigurationGenerationKey(*other))
        else:
            async with h.pool.connection() as connection:
                await connection.execute(
                    "INSERT INTO adcp_reporting_production_owed_sets VALUES"
                    " (%s,%s,%s,%s,%s,%s,'{}'::jsonb)",
                    (*other, "0" * 64),
                )
        await h.service.start()
        # Status checkpoints hold one configuration scope per subscription version.
        assert [failure.subscription_id for failure in h.failures] == ["analytics-hourly"]
        assert h.failures[0].error.code == "reporting_production_offering_conflict"
        (key,) = h.bound
        assert await h.store.list_generation_obligations(key) == ()
