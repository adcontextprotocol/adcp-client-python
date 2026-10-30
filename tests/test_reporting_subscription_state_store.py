"""Durable reporting-subscription engine state (#1695 item 6).

The engine keeps one JSON document per principal in a
``ReportingSubscriptionStateStore`` and writes it by compare-and-swap, so a
restarted engine, or another worker on the same store, owes exactly the same
periods.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from adcp.reporting.subscriptions import (
    InMemoryReportingSubscriptionStore,
    ReportingSubscriptionEngine,
    ReportingSubscriptionStateConflictError,
    ReportingSubscriptionSupport,
    decode_principal_state,
    encode_principal_state,
)
from tests.test_reporting_subscription_engine import (
    ALL_IDS,
    DEST_A,
    IDENTITY,
    MANAGED,
    OFFERINGS,
    World,
    _sync_raw,
    at,
    buy,
    subscription,
    table,
)

CAPACITY = ReportingSubscriptionSupport(max_accounts=1)


async def _scenario(world: World) -> None:
    """Grants, a loss and a re-grant, a capacity exception and a destination snapshot."""
    world.engine._offerings = (*world.engine._offerings, MANAGED)
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.grant("acct-2", "2026-09-01T00:00:00Z")
    for account_id in ("acct-1", "acct-2"):
        world.host.buys[account_id] = [
            buy(
                "mb-" + account_id,
                "2026-10-01T04:00:00Z",
                "2026-10-12T04:00:00Z",
                offering_ids=(*ALL_IDS, MANAGED.offering_id),
            )
        ]
    await _sync_raw(
        world,
        {
            "reporting_destinations": [DEST_A],
            "reporting_subscriptions": [
                subscription(),
                subscription(
                    "files",
                    scope={"account_ids": ["acct-1", "acct-2"]},
                    destination={"destination_id": "dest-a", "path_template": "x/{account_id}"},
                ),
            ],
        },
    )
    await world.reconcile("2026-10-01T00:00:00Z")
    world.revoke("acct-1", "2026-10-04T08:00:00Z")
    await world.reconcile("2026-10-04T08:00:00Z")
    world.grant("acct-1", "2026-10-06T15:00:00Z")
    await world.reconcile("2026-10-06T15:00:00Z")


async def _observed(world: World, engine: ReportingSubscriptionEngine) -> dict:
    owed = {}
    for subscription_id in ("analytics", "files"):
        for account_id in ("acct-1", "acct-2"):
            periods = await engine.owed_periods(
                IDENTITY, subscription_id, account_id, now=at("2026-10-10T00:00:00Z")
            )
            owed[(subscription_id, account_id)] = table(periods)
    coverage = {
        account_id: [
            item.model_dump(mode="json")
            for item in await engine.subscription_coverage(
                IDENTITY, account_id, adcp_version="3.3-beta.2"
            )
        ]
        for account_id in ("acct-1", "acct-2")
    }
    return {"owed": owed, "coverage": coverage}


@pytest.mark.asyncio
async def test_document_round_trips_and_is_plain_json() -> None:
    world = World(support=CAPACITY)
    await _scenario(world)
    stored = await world.engine._store.load(IDENTITY.subject)
    assert stored is not None
    document = json.loads(json.dumps(stored.document))
    assert encode_principal_state(decode_principal_state(document)) == document
    assert document["accounts"]["acct-1"]["grants"][0]["unauthorized_at"] == "2026-10-04T08:00:00Z"
    assert document["destinations"][0]["destination"]["location"] == DEST_A["location"]


@pytest.mark.asyncio
async def test_a_restarted_engine_owes_the_same_periods() -> None:
    store = InMemoryReportingSubscriptionStore()
    world = World(support=CAPACITY)
    world.engine = ReportingSubscriptionEngine(
        principals=world.principals,
        host=world.host,
        offerings=OFFERINGS,
        support=CAPACITY,
        store=store,
        clock=world.clock,
    )
    world.principals.set_reporting_subscriptions(world.engine.principal_options())
    await _scenario(world)
    before = await _observed(world, world.engine)

    restarted = ReportingSubscriptionEngine(
        principals=world.principals,
        host=world.host,
        offerings=(*OFFERINGS, MANAGED),
        support=CAPACITY,
        store=store,
        clock=world.clock,
    )
    assert await _observed(world, restarted) == before
    assert before["owed"][("files", "acct-1")]
    assert any(entry for entry in before["coverage"]["acct-2"])


class _ConflictingStore(InMemoryReportingSubscriptionStore):
    """Loses the first ``conflicts`` writes to a simulated concurrent writer."""

    def __init__(self, conflicts: int) -> None:
        super().__init__()
        self.conflicts = conflicts
        self.attempts = 0

    async def compare_and_swap(self, subject, expected_revision, document):
        self.attempts += 1
        if self.conflicts:
            self.conflicts -= 1
            return False
        return await super().compare_and_swap(subject, expected_revision, document)


@pytest.mark.asyncio
async def test_a_conflicting_write_is_reloaded_and_reapplied() -> None:
    world = World()
    store = _ConflictingStore(conflicts=2)
    world.engine._store = store
    await world.engine.authorization_changed(
        IDENTITY, "acct-1", authorized=True, at=at("2026-10-01T00:00:00Z"), timezone="UTC"
    )
    assert store.attempts == 3
    stored = await store.load(IDENTITY.subject)
    assert stored is not None and stored.revision == 1
    assert stored.document["accounts"]["acct-1"]["grants"] == [
        # A pushed grant records its source; listed grants omit it.
        {"authorized_at": "2026-10-01T00:00:00Z", "unauthorized_at": None, "source": "pushed"}
    ]


@pytest.mark.asyncio
async def test_endless_conflicts_raise_instead_of_dropping_the_change() -> None:
    world = World()
    world.engine._store = _ConflictingStore(conflicts=1_000)
    with pytest.raises(ReportingSubscriptionStateConflictError):
        await world.engine.authorization_changed(
            IDENTITY, "acct-1", authorized=True, at=at("2026-10-01T00:00:00Z"), timezone="UTC"
        )


@pytest.mark.asyncio
async def test_an_unchanged_document_is_not_rewritten() -> None:
    world = World(support=CAPACITY)
    await _scenario(world)
    before = await world.engine._store.load(IDENTITY.subject)
    await world.reconcile("2026-10-06T15:00:00Z")
    after = await world.engine._store.load(IDENTITY.subject)
    assert before is not None and after is not None
    assert after.revision == before.revision


@pytest.mark.asyncio
async def test_exceptions_closed_beyond_retention_are_pruned_on_write() -> None:
    world = World(support=CAPACITY, retention_days=10)
    await _scenario(world)
    stored = await world.engine._store.load(IDENTITY.subject)
    assert stored is not None
    state = decode_principal_state(stored.document)
    closed = [
        entry.until
        for entries in state.exclusions.values()
        for entry in entries
        if entry.until is not None
    ]
    assert closed, "the scenario must close an exception"
    horizon = max(closed) + timedelta(days=11)
    world.clock.now = horizon
    await world.engine.authorization_changed(
        IDENTITY, "acct-3", authorized=True, at=horizon, timezone="UTC"
    )
    pruned = decode_principal_state((await world.engine._store.load(IDENTITY.subject)).document)
    assert not [
        entry
        for entries in pruned.exclusions.values()
        for entry in entries
        if entry.until is not None and entry.until < horizon - timedelta(days=10)
    ]


def test_an_unknown_document_version_is_refused() -> None:
    with pytest.raises(ValueError, match="document version"):
        decode_principal_state({"document_version": 99})


@pytest.mark.asyncio
async def test_principal_records_round_trip_through_a_json_document() -> None:
    from adcp.server import principal_record_from_document, principal_record_to_document

    world = World(support=CAPACITY)
    await _scenario(world)
    record = await world.principals._store.get(IDENTITY.subject)
    assert record is not None and record.configuration is not None
    document = json.loads(json.dumps(principal_record_to_document(record)))
    restored = principal_record_from_document(document, store_revision=record.store_revision)
    assert principal_record_to_document(restored) == document
    assert restored.store_revision == record.store_revision
    assert restored.configuration == record.configuration
    assert restored.idempotency.keys() == record.idempotency.keys()
    with pytest.raises(ValueError, match="document version"):
        principal_record_from_document({**document, "document_version": 2}, store_revision=1)
