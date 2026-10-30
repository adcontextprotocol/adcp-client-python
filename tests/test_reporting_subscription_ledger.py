"""Reporting-subscription owed sets in the reporting ledger (AdCP 3.3).

The engine decides what a subscription owes; the planner writes it into the
ledger as ordinary obligations of one generation per (account, principal,
subscription_id, version, offering_id). These tests drive the in-memory
ledger end to end: planning, idempotency, several offerings of one version,
two principals with the same subscription_id, per-principal status reads,
owed-set completeness and status ingest.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    ProducerOfferings,
    ReportingProducer,
    ReportingStatusCaller,
    ReportingStatusHandler,
)
from adcp.reporting.ledger.consumer_status import ConsumerStatusIngest
from adcp.reporting.subscriptions import SubscriptionObligationPlanner
from adcp.server import PrincipalIdentity
from tests.test_reporting_subscription_engine import (
    OFFERINGS,
    World,
    at,
    buy,
    subscription,
)

AGENCY = PrincipalIdentity("https://agency.example/agent", "buyer_agent")
OTHER = PrincipalIdentity("https://other.example/agent", "buyer_agent")


class _NoSource:
    @property
    def capabilities(self):  # pragma: no cover - never reached
        raise AssertionError("not reached")

    async def execute(self, request, *, cancel, heartbeat=None):  # pragma: no cover
        raise AssertionError("not reached")


async def _world_with(identity: PrincipalIdentity, buys, *, selector=None):
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = buys
    config = subscription() if selector is None else subscription(selector=selector)
    world._key += 1
    response = await world.principals.sync_principal(
        identity,
        {
            "idempotency_key": f"ledger-test-key-{world._key:06d}",
            "configuration": {"reporting_subscriptions": [config]},
        },
    )
    assert response.result.kind == "applied"
    world.clock.set("2026-10-01T00:00:00Z")
    await world.engine.reconcile(identity)
    return world


def _planner(store) -> SubscriptionObligationPlanner:
    return SubscriptionObligationPlanner(store, offerings=OFFERINGS, currency="USD")


async def _plan(world: World, identity: PrincipalIdentity, store, now: str):
    planner = _planner(store)
    results = []
    for owed in await world.engine.frozen_owed_sets(identity, now=at(now)):
        results.append(await planner.plan(owed))
    return results


@pytest.mark.asyncio
async def test_owed_set_is_written_sparse_and_idempotently() -> None:
    world = await _world_with(
        AGENCY,
        [
            buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z"),
            buy("mb-2", "2026-10-05T04:00:00Z", "2026-10-06T04:00:00Z"),
        ],
    )
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-08T00:00:00Z"))
    (first,) = await _plan(world, AGENCY, store, "2026-10-08T00:00:00Z")
    assert len(first.obligations_written) == 3
    (key,) = first.generations
    assert key.offering_id == "pkg" and key.delivery_config_id == "analytics"
    obligations = await store.list_generation_obligations(key)
    # Sparse: Oct 3 and Oct 4 are owed by no buy, so no obligation exists.
    assert [(o.period.start, o.media_buy_ids) for o in obligations] == [
        (at("2026-10-01T04:00:00Z"), ("mb-1",)),
        (at("2026-10-02T04:00:00Z"), ("mb-1",)),
        (at("2026-10-05T04:00:00Z"), ("mb-2",)),
    ]
    assert {o.offering_id for o in obligations} == {"pkg"}
    assert obligations[0].period.expected_at == at("2026-10-02T10:00:00Z")
    assert await store.owed_set_through(key) == at("2026-10-08T00:00:00Z")
    # A second cycle writes nothing and never regresses the horizon.
    (again,) = await _plan(world, AGENCY, store, "2026-10-08T00:00:00Z")
    assert again.obligations_written == ()
    await store.record_owed_set_progress(key, at("2026-10-07T00:00:00Z"))
    assert await store.owed_set_through(key) == at("2026-10-08T00:00:00Z")
    # The producer never grid-fills an owed set.
    (configuration,) = [
        c
        for c in await store.list_configurations(
            caller=ReportingStatusCaller("acct-1", AGENCY.subject)
        )
    ]
    producer = ReportingProducer(source=_NoSource(), offerings=ProducerOfferings(), store=store)
    assert await producer.close_elapsed_periods(configuration, now=at("2026-10-08T00:00:00Z")) == []


@pytest.mark.asyncio
async def test_several_offerings_of_one_version_never_collide() -> None:
    world = await _world_with(
        AGENCY,
        [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z")],
        selector={
            "feed_purpose": "analytics",
            "period_duration": "P1D",
            "required_finality": "official",
            "dimensions": ["package"],
            "match": "all",
        },
    )
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-04T00:00:00Z"))
    (result,) = await _plan(world, AGENCY, store, "2026-10-04T00:00:00Z")
    assert sorted(key.offering_id or "" for key in result.generations) == [
        "pkg",
        "pkg-country",
        "pkg-creative",
        "pkg-region-utc",
    ]
    assert {(k.delivery_config_id, k.delivery_config_version) for k in result.generations} == {
        ("analytics", 1)
    }
    payload = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller("acct-1", AGENCY.subject)
    )
    # Every offering's obligations, one wire generation per (id, version, purpose).
    assert {o["offering_id"] for o in payload["periods"]} == {
        "pkg",
        "pkg-country",
        "pkg-creative",
        "pkg-region-utc",
    }
    assert payload["scope"]["delivery_config_generations"] == [
        {
            "delivery_config_id": "analytics",
            "delivery_config_version": 1,
            "feed_purpose": "analytics",
        }
    ]


@pytest.mark.asyncio
async def test_two_principals_with_one_subscription_id_see_only_their_own() -> None:
    agency = await _world_with(
        AGENCY, [buy("mb-a", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z")]
    )
    other = await _world_with(OTHER, [buy("mb-b", "2026-10-02T04:00:00Z", "2026-10-03T04:00:00Z")])
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-04T00:00:00Z"))
    await _plan(agency, AGENCY, store, "2026-10-04T00:00:00Z")
    await _plan(other, OTHER, store, "2026-10-04T00:00:00Z")
    handler = ReportingStatusHandler(store)
    mine = await handler.handle(
        {"view": "periods"}, caller=ReportingStatusCaller("acct-1", AGENCY.subject)
    )
    theirs = await handler.handle(
        {"view": "periods"}, caller=ReportingStatusCaller("acct-1", OTHER.subject)
    )
    nobody = await handler.handle(
        {"view": "periods"}, caller=ReportingStatusCaller("acct-1", "https://third.example")
    )
    assert {tuple(o["media_buy_ids"]) for o in mine["periods"]} == {("mb-a",)}
    assert len(mine["periods"]) == 2
    assert {tuple(o["media_buy_ids"]) for o in theirs["periods"]} == {("mb-b",)}
    assert nobody["periods"] == []


@pytest.mark.asyncio
async def test_scope_stays_open_until_the_owed_set_is_frozen_through_it() -> None:
    world = await _world_with(AGENCY, [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-10T04:00:00Z")])
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-08T00:00:00Z"))
    await _plan(world, AGENCY, store, "2026-10-04T00:00:00Z")
    caller = ReportingStatusCaller("acct-1", AGENCY.subject)
    handler = ReportingStatusHandler(store)
    frozen = await handler.handle(
        {
            "view": "periods",
            "period": {"start": "2026-10-01T04:00:00Z", "end": "2026-10-03T04:00:00Z"},
        },
        caller=caller,
    )
    assert frozen["scope"]["scope_closed"] is True
    open_scope = await handler.handle(
        {
            "view": "periods",
            "period": {"start": "2026-10-01T04:00:00Z", "end": "2026-10-07T04:00:00Z"},
        },
        caller=caller,
    )
    # The owed set is frozen only through 10-04: later periods may still be
    # added, which is not missing history.
    assert open_scope["scope"]["scope_closed"] is False
    assert open_scope["scope"]["coverage_complete"] is True


@pytest.mark.asyncio
async def test_status_ingest_finds_the_owed_obligation_by_period() -> None:
    world = await _world_with(AGENCY, [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z")])
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-04T00:00:00Z"))
    await _plan(world, AGENCY, store, "2026-10-04T00:00:00Z")
    handler = ConsumerStatusIngest(store, enabled=True, clock=lambda: at("2026-10-04T00:00:00Z"))

    def statement(identifier: str, start: str, end: str) -> dict:
        return {
            "reporting_status_id": identifier,
            "delivery_config_id": "analytics",
            "delivery_config_version": 1,
            "report_definition_id": "def-pkg",
            "period": {"start": start, "end": end, "source_timezone": "America/New_York"},
            "consumer_status": "obligation_missing",
            "status_as_of": "2026-10-03T12:00:00Z",
        }

    response = await handler.handle(
        {
            "statuses": [
                # A period of the owed set: matched to its owed obligation.
                statement("rps_subscription_0001", "2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z"),
                # Not owed (no buy): never a bare grid slot.
                statement("rps_subscription_0002", "2026-10-03T04:00:00Z", "2026-10-04T04:00:00Z"),
            ]
        },
        account_id="acct-1",
        consumer_id=AGENCY.subject,
    )
    owed, not_owed = response["results"]
    assert owed["result"] in {"recorded", "unchanged"}, owed
    assert not_owed["result"] == "failed"
    assert not_owed["errors"][0]["code"] in {"INVALID_STATUS_PERIOD", "INVALID_CONSUMER_STATUS"}


def test_the_planner_refuses_a_store_without_owed_set_support() -> None:
    class _AccountOnlyStore:
        pass

    with pytest.raises(TypeError, match="owed sets"):
        SubscriptionObligationPlanner(_AccountOnlyStore(), offerings=OFFERINGS, currency="USD")


@pytest.mark.asyncio
async def test_account_generations_keep_their_identity() -> None:
    from tests.test_reporting_ledger import _configuration

    configuration = _configuration()
    assert configuration.generation_key.offering_id is None
    assert not configuration.owed_set
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(configuration)
    await store.put_configuration(configuration)  # unchanged content is idempotent
    assert configuration.generation_key in {
        c.generation_key
        for c in await store.list_configurations(
            caller=ReportingStatusCaller(configuration.account_id, configuration.consumer_id)
        )
    }


@pytest.mark.asyncio
async def test_the_producer_reads_only_elapsed_owed_obligations() -> None:
    from dataclasses import replace

    from adcp.reporting.ledger import ReportingSubscriptionGeneration
    from tests.test_reporting_ledger import _obligation
    from tests.test_reporting_producer_retry import NOW, FailingSource, configuration, producer

    account_generation = configuration(config="analytics")
    owed = replace(
        account_generation,
        activated_at=None,
        media_buy_ids=(),
        subscription=ReportingSubscriptionGeneration("analytics", "pkg"),
    )
    store = InMemoryReportingLedgerStore(clock=lambda: NOW)
    await store.put_configuration(owed)
    hour = timedelta(hours=1)
    for identifier, start in (("rpo_elapsed", NOW - 2 * hour), ("rpo_later", NOW + hour)):
        template = _obligation(account_generation)
        start = start.replace(minute=0)
        await store.commit_obligation(
            replace(
                template,
                reporting_obligation_id=identifier,
                media_buy_ids=("mb-1",),
                offering_id="pkg",
                period=replace(
                    template.period,
                    period_key=start.strftime("%Y-%m-%dT%H:%M:%SZ") + "_PT1H",
                    start=start,
                    end=start + hour,
                    expected_at=start + hour + timedelta(minutes=10),
                ),
                scope_resolved_at=start + hour,
                created_at=NOW,
            )
        )
    source = FailingSource()
    turn = await producer(source, store).run_configuration(owed, now=NOW)
    # The owed set is not grid-filled, and a period that has not ended is not read.
    assert len(await store.list_generation_obligations(owed.generation_key)) == 2
    assert source.calls == 1
    assert turn.slices_failed in ([], ["rpo_elapsed"])
    assert not await store.lease_period_close(worker_id="w", now=NOW, lease_seconds=30)


def test_account_generation_bytes_are_unchanged_by_the_offering_dimension() -> None:
    """Every digest and persisted scope of an account generation stays byte-stable."""
    from dataclasses import asdict

    from adcp.reporting.ledger.models import (
        ReportingConfigurationGenerationKey,
        ledger_asdict,
    )
    from adcp.reporting.ledger.notification_models import (
        ReportingStatusScope,
        decode_status_scope,
    )

    key = ReportingConfigurationGenerationKey("acct-1", "buyer", "daily", 1)
    assert ledger_asdict(key) == {
        "account_id": "acct-1",
        "consumer_id": "buyer",
        "delivery_config_id": "daily",
        "delivery_config_version": 1,
    }
    owed = ReportingConfigurationGenerationKey("acct-1", "buyer", "daily", 1, "pkg")
    assert ledger_asdict(owed) == {**ledger_asdict(key), "offering_id": "pkg"}
    assert owed != key and len({owed, key}) == 2
    # A scope persisted before subscriptions existed still decodes.
    scope = ReportingStatusScope("acct-1", key, consumer_id="buyer")
    legacy = asdict(scope)
    del legacy["generation_key"]["offering_id"]
    assert decode_status_scope(legacy) == scope
    assert ledger_asdict(scope) == legacy


def test_reporting_code_never_serializes_a_generation_key_with_bare_asdict() -> None:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = [
        str(path.relative_to(root))
        for base in ("src/adcp/reporting", "packages/adcp-reporting-storage/src")
        for path in (root / base).rglob("*.py")
        if not path.as_posix().endswith("ledger/models.py")
        and re.search(r"(?<![\w.])asdict\(", path.read_text())
    ]
    assert offenders == []
