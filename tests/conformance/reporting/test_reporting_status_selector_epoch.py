"""Shared memory/PostgreSQL cutover, directional scope and replay contract."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import InMemoryReportingLedgerStore
from adcp.reporting.ledger.status_projection import StatusProjectionInput, project_status_scope
from adcp.reporting.outbox import ReportingStatusScope, ReportingStatusSweeper

from . import test_reporting_status_projection_contract as contract
from ._generation_support import configuration, obligation_for, revision_for
from ._reliable_support import SimulatedCrash

status_harness = contract.status_harness


def old_selected(scope, target):
    """Exact v1 scope predicate, frozen here for shared-state vector setup."""
    return (
        scope.account_id == target.account_id
        and (scope.consumer_id is None or scope.consumer_id == target.consumer_id)
        and (
            scope.generation_key is None
            or target.generation_key is None
            or scope.generation_key == target.generation_key
        )
        and (
            scope.reporting_obligation_id is None
            or target.reporting_obligation_id is None
            or scope.reporting_obligation_id == target.reporting_obligation_id
        )
    )


async def mark_v1(h):
    """Restore old metadata only in a private test schema/state image.

    Actual unmodified C binaries establish their own baselines in the rolling
    tests. This helper makes the same vectors usable by the memory reference.
    """
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        state = h.status._state
        state.selector_accounts.clear()
        state.accounts = {
            a: (seq, {k: v for k, v in policy.items() if k != "selector_semantics_version"})
            for a, (seq, policy) in state.accounts.items()
        }
        state.checkpoints = {
            key: replace(c, selector_semantics_version=1, selector_writer_floor=1)
            for key, c in state.checkpoints.items()
        }
    else:
        async with h.ledger._pool.connection() as c, c.transaction():
            await c.execute(
                "ALTER TABLE reporting_status_scope_checkpoints"
                " DISABLE TRIGGER reporting_status_selector_writer_v2"
            )
            await c.execute(
                "UPDATE reporting_status_scope_checkpoints"
                " SET selector_semantics_version=1, selector_writer_floor=1"
            )
            await c.execute(
                "UPDATE reporting_status_accounts SET policy=policy-'selector_semantics_version',"
                " selector_target_version=1, selector_transition='pending'"
            )
            await c.execute(
                "ALTER TABLE reporting_status_scope_checkpoints"
                " ENABLE TRIGGER reporting_status_selector_writer_v2"
            )


async def restart(h):
    cls = type(h.status)
    await h.reliable.restart()
    h.status = cls(h.ledger)


async def two_feeds(h, *, issue=True):
    owners = {}
    for feed in ("analytics", "billing"):
        config = replace(configuration(), delivery_config_id=feed, feed_purpose=feed)
        await h.ledger.put_configuration(config)
        obligation = replace(obligation_for(config), reporting_obligation_id=f"rpo_{feed}")
        await h.ledger.commit_obligation(obligation)
        revision, rows = revision_for(obligation, suffix=feed)
        await h.ledger.commit_revision(revision, rows)
        owners[feed] = obligation
    issues = {}
    if issue:
        for key, scope in (
            (
                "billing-partial",
                ReportingStatusScope("acct_a", consumer_id="buyer", feed_purpose="billing"),
            ),
            ("analytics-public", ReportingStatusScope.for_obligation(owners["analytics"])),
            ("billing-private", ReportingStatusScope.for_obligation(owners["billing"], "auditor")),
        ):
            issues[key] = await h.ledger.ensure_issue_opened(
                issue_key=key,
                account_id="acct_a",
                consumer_id=scope.consumer_id,
                observed_at=h.clock(),
                status_scope=scope,
            )
    return owners, issues


async def test_directional_feed_public_private_configuration_obligation_scopes(status_harness):
    h = status_harness
    owners, issues = await two_feeds(h)
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    for consumer in (None, "buyer", "auditor"):
        for feed in (None, "analytics", "billing"):
            for level in ("account", "configuration", "obligation"):
                if level != "account" and feed is None:
                    continue
                owner = owners[feed] if feed else None
                scope = ReportingStatusScope(
                    "acct_a",
                    owner.generation_key if level != "account" else None,
                    owner.reporting_obligation_id if level == "obligation" else None,
                    consumer,
                    feed,
                )
                result = project_status_scope(StatusProjectionInput(snapshot, scope))
                expected = set()
                if feed in (None, "analytics"):
                    expected.add(issues["analytics-public"].issue_id)
                if feed in (None, "billing") and consumer == "buyer":
                    expected.add(issues["billing-partial"].issue_id)
                if feed in (None, "billing") and consumer == "auditor":
                    expected.add(issues["billing-private"].issue_id)
                assert {i.issue_id for i in result.issues} == expected, (consumer, feed, level)
                assert all(
                    feed is None or o.obligation.feed_purpose == feed for o in result.obligations
                )


async def test_contaminated_epoch_corrects_changed_issue_set_once_and_preserves_every_identity(
    status_harness, monkeypatch
):
    h = status_harness
    await two_feeds(h)
    import adcp.reporting.ledger.status_projection as projection

    with monkeypatch.context() as patch:
        patch.setattr(projection, "_selected", old_selected)
        await h.status.baseline(account_id="acct_a")
    await mark_v1(h)
    before = {c.scope.checkpoint_key: c for c in await h.status.checkpoints(account_id="acct_a")}
    assert not await h.status.baseline_ready(account_id="acct_a")
    assert (await h.status.rebuild_one()).events == 0  # Durable fence only.
    fenced = await h.status.checkpoints(account_id="acct_a")
    assert all(c.selector_writer_floor == 2 and c.selector_semantics_version == 1 for c in fenced)
    assert not await h.status.claim_due(account_id="acct_a")
    await restart(h)
    await h.drain()
    events = await h.status.outbox.list_events(account_id="acct_a")
    after = {c.scope.checkpoint_key: c for c in await h.status.checkpoints(account_id="acct_a")}
    changed = {k for k, c in after.items() if c.fingerprint != before[k].fingerprint}
    assert changed and len(changed) == len(events) == 2
    assert all(e.cause.health == e.cause.previous_health == "action_required" for e in events)
    for key, current in after.items():
        old = before[key]
        assert current.scope == old.scope and current.baseline == old.baseline
        assert current.source_sequence == old.source_sequence
        assert (
            current.lease_token == old.lease_token
            and current.lease_expires_at == old.lease_expires_at
        )
        assert current.generation == old.generation + (key in changed)
        assert current.selector_semantics_version == current.selector_writer_floor == 2
    assert await h.status.baseline_ready(account_id="acct_a")
    assert not (await h.status.rebuild_one()).did_work
    assert not (await h.status.project_one(account_id="acct_a")).did_work
    assert await h.status.checkpoints(account_id="acct_a") == tuple(after.values())
    assert await h.status.outbox.list_events(account_id="acct_a") == events


async def test_captured_reversal_boundaries_precede_chronological_overdue_deadlines(status_harness):
    h = status_harness
    first, revision, _ = await h.seed(readable=True)
    config = replace(configuration(), delivery_config_id="second")
    await h.ledger.put_configuration(config)
    waiting = replace(obligation_for(config), reporting_obligation_id="rpo_waiting")
    await h.ledger.commit_obligation(waiting)
    h.clock.now = waiting.period.expected_at - timedelta(seconds=5)
    await h.status.baseline(account_id="acct_a")
    await mark_v1(h)
    for readable in (False, True, False, True):
        h.clock.advance()
        await h.ledger.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=revision.reporting_revision_id,
            readable=readable,
        )
    h.clock.now = waiting.automated_recovery_deadline_at + timedelta(hours=2)
    for _ in range(30):
        turn = await h.status.rebuild_one()
        if not turn.did_work:
            break
        await restart(h)  # Every committed fence/boundary/deadline/final mark survives restart.
    else:
        pytest.fail("selector epoch did not converge")
    events = await h.events()
    ready_events = sorted(
        (
            e
            for e in events
            if e.cause.scope.reporting_obligation_id == first.reporting_obligation_id
        ),
        key=lambda e: e.cause.checkpoint_generation,
    )
    assert [e.cause.health for e in ready_events] == [
        "action_required",
        "complete",
        "action_required",
        "complete",
    ]
    assert [e.cause.previous_health for e in ready_events] == [
        "complete",
        "action_required",
        "complete",
        "action_required",
    ]
    late = sorted(
        (
            e
            for e in events
            if e.cause.scope.reporting_obligation_id == waiting.reporting_obligation_id
        ),
        key=lambda e: e.cause.checkpoint_generation,
    )
    assert [e.cause.health for e in late] == ["delayed", "action_required"]
    assert [e.cause.previous_health for e in late] == ["waiting", "delayed"]
    assert len({e.notification_id for e in events}) == len(events)
    assert not (await ReportingStatusSweeper(h.status).run_once(account_id="acct_a")).did_work


@pytest.mark.parametrize("fail_at", [1, 2, 4])
async def test_checkpoint_event_failure_rolls_back_epoch_turn_and_restart_is_once_only(
    status_harness, monkeypatch, fail_at
):
    h = status_harness
    await two_feeds(h, issue=False)
    await h.status.baseline(account_id="acct_a")
    await mark_v1(h)
    assert (await h.status.rebuild_one()).did_work
    await h.ledger.ensure_issue_opened(
        issue_key="late-public",
        account_id="acct_a",
        consumer_id=None,
        observed_at=h.clock(),
        status_scope=ReportingStatusScope("acct_a"),
    )
    before = await h.status.checkpoints(account_id="acct_a")
    from adcp.reporting.outbox import status_memory, status_pg

    original = status_pg.advance_checkpoint
    called = 0

    def crash(*args, **kwargs):
        nonlocal called
        called += 1
        if called == fail_at:
            raise SimulatedCrash("selector-checkpoint-event-pre-commit")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(status_memory, "advance_checkpoint", crash)
        patch.setattr(status_pg, "advance_checkpoint", crash)
        with pytest.raises(SimulatedCrash):
            await h.status.rebuild_one()
    assert await h.status.checkpoints(account_id="acct_a") == before
    assert not await h.status.outbox.list_events(account_id="acct_a")
    await restart(h)
    await h.drain()
    events = await h.status.outbox.list_events(account_id="acct_a")
    assert len(events) == 4 and {e.cause.checkpoint_generation for e in events} == {1}
    assert not (await h.status.rebuild_one()).did_work


@pytest.mark.parametrize("phase", ["fence", "final_mark"])
async def test_crash_before_fence_or_final_mark_commit_is_restartable(
    status_harness, monkeypatch, phase
):
    h = status_harness
    await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    await mark_v1(h)
    if phase == "final_mark":
        assert (await h.status.rebuild_one()).did_work
    before = await h.status.checkpoints(account_id="acct_a")
    name = "_rebuild" if isinstance(h.ledger, InMemoryReportingLedgerStore) else "_rebuild_on"
    original = getattr(h.status, name)

    def memory_crash(*args):
        original(*args)
        raise SimulatedCrash("selector-epoch-pre-commit")

    async def pg_crash(*args):
        await original(*args)
        raise SimulatedCrash("selector-epoch-pre-commit")

    with monkeypatch.context() as patch:
        patch.setattr(h.status, name, memory_crash if name == "_rebuild" else pg_crash)
        with pytest.raises(SimulatedCrash):
            await h.status.rebuild_one()
    assert await h.status.checkpoints(account_id="acct_a") == before
    assert not await h.status.baseline_ready(account_id="acct_a")
    await restart(h)
    await h.drain()
    assert await h.status.baseline_ready(account_id="acct_a")
    assert not await h.status.outbox.list_events(account_id="acct_a")


async def test_retained_scopes_outside_current_discovery_are_reprojected_without_deletion(
    status_harness, monkeypatch
):
    h = status_harness
    await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    await mark_v1(h)
    before = await h.status.checkpoints(account_id="acct_a")
    expired = h.clock() - timedelta(seconds=1)
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        h.status._state.checkpoints = {
            key: replace(c, next_due_at=expired) for key, c in h.status._state.checkpoints.items()
        }
    else:
        async with h.ledger._pool.connection() as c:
            await c.execute(
                "UPDATE reporting_status_scope_checkpoints SET next_due_at=%s", (expired,)
            )
    from adcp.reporting.outbox import status_memory, status_pg

    with monkeypatch.context() as patch:
        patch.setattr(status_memory, "projection_scopes", lambda snapshot: ())
        patch.setattr(status_pg, "projection_scopes", lambda snapshot: ())
        await h.drain()
    after = await h.status.checkpoints(account_id="acct_a")
    assert len(after) == len(before)
    assert [(c.scope, c.fingerprint, c.generation, c.baseline) for c in after] == [
        (c.scope, c.fingerprint, c.generation, c.baseline) for c in before
    ]
    assert all(c.selector_semantics_version == 2 and c.next_due_at is None for c in after)
    assert not (await h.status.rebuild_one()).did_work
    assert not await h.status.outbox.list_events(account_id="acct_a")


async def test_account_epoch_readiness_and_new_baseline_are_isolated(status_harness):
    h = status_harness
    for account in ("acct_a", "acct_b"):
        await h.ledger.put_configuration(configuration(account))
        await h.status.baseline(account_id=account)
    await mark_v1(h)
    assert not await h.status.baseline_ready(account_id="acct_a")
    assert not await h.status.baseline_ready(account_id="acct_b")
    await h.drain()
    assert await h.status.baseline_ready(account_id="acct_a")
    assert not await h.status.baseline_ready(account_id="acct_b")
    await restart(h)
    assert await h.status.baseline_ready(account_id="acct_a")
    assert not await h.status.baseline_ready(account_id="acct_b")
    assert await h.status.baseline(account_id="new-account")
    assert await h.status.baseline_ready(account_id="new-account")
    assert not await h.status.outbox.list_events(account_id="new-account")


async def test_old_memory_state_image_without_any_epoch_fields_is_imported_as_v1():
    from types import SimpleNamespace

    from adcp.reporting.outbox import InMemoryStatusNotificationStore

    from ._reliable_support import reliable_factory

    async with reliable_factory("memory", notifications=True) as reliable:
        h = contract.StatusHarness(reliable, InMemoryStatusNotificationStore(reliable.store))
        await h.seed(readable=True)
        await h.status.baseline(account_id="acct_a")
        before = await h.status.checkpoints(account_id="acct_a")
        state = h.status._state
        del state.selector_accounts
        state.accounts = {a: (seq, {}) for a, (seq, _) in state.accounts.items()}
        state.checkpoints = {
            key: SimpleNamespace(
                **{k: v for k, v in vars(c).items() if not k.startswith("selector_")}
            )
            for key, c in state.checkpoints.items()
        }
        h.status = InMemoryStatusNotificationStore(h.ledger)
        assert all(
            c.selector_semantics_version == 1
            for c in await h.status.checkpoints(account_id="acct_a")
        )
        assert not await h.status.baseline_ready(account_id="acct_a")
        await h.drain()
        after = await h.status.checkpoints(account_id="acct_a")
        assert after == before
        assert not await h.status.outbox.list_events(account_id="acct_a")


async def test_existing_c_service_discovers_old_accounts_without_an_account_list(status_harness):
    from types import SimpleNamespace

    from adcp.reporting.outbox import ReportingStatusService

    h = status_harness
    await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    await mark_v1(h)
    service = ReportingStatusService(SimpleNamespace(store=h.status, account_ids=()))
    assert await service.drain() >= 2
    assert await h.status.baseline_ready(account_id="acct_a")
    assert await service.drain() == 0
