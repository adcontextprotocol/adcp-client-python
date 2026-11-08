"""The reporting-issue observer: lifecycle events recorded with each transition (#1258).

The memory reference store records an event in the same mutation as every
issue-lifecycle write, the dispatcher delivers them at least once, and
``ReliableReportingService(issue_observer=...)`` runs it on the worker turn.
``tests/conformance/reporting/test_reporting_issue_observer_pg.py`` runs the
service flow below against PostgreSQL.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from adcp.reporting import ReportingIssueEvent, ReportingIssueEventDispatcher
from adcp.reporting.fixtures import redacted_capabilities
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    ReportingDeliveryEscalation,
    ReportingStatusCaller,
    issue_event_id,
    supports_issue_events,
)
from adcp.reporting.service import (
    ReliableReportingConfigurationError,
    ReliableReportingService,
)
from adcp.reporting.testing import DeterministicReportingClock, ScriptedReportingAdapter
from tests.test_reliable_reporting_service import (
    NOW,
    _account_context,
    _configuration,
    _rows,
)

ACCOUNT = "account-redacted"
CONSUMER = "buyer-1"
ESCALATION = ReportingDeliveryEscalation(
    consumer_mismatch_escalation=timedelta(hours=1),
    operations_contact_email="reporting-ops@example.test",
)


# -- the service flow, shared with the PostgreSQL conformance test -----------


@dataclass
class Observed:
    """What the observer and the worker error handler saw."""

    events: list[ReportingIssueEvent] = field(default_factory=list)
    errors: list[tuple[str, BaseException]] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)

    async def observe(self, event: ReportingIssueEvent) -> None:
        if event.kind in self.fail:
            self.fail.discard(event.kind)
            raise RuntimeError(f"observer rejected {event.kind}")
        self.events.append(event)

    def report(self, component: str, error: BaseException) -> None:
        self.errors.append((component, error))

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


def service_options(clock: DeterministicReportingClock, observed: Observed) -> dict[str, Any]:
    caller = ReportingStatusCaller(account_id=ACCOUNT, consumer_id=CONSUMER)
    return {
        "account_context": _account_context,
        "caller_resolver": lambda _request, _context: caller,
        "clock": clock,
        "consumer_status_enabled": True,
        "escalation": ESCALATION,
        "issue_observer": observed.observe,
        "worker_error_handler": observed.report,
    }


async def published_period(service: ReliableReportingService) -> tuple[dict[str, Any], dict]:
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(17)]))
    await service.configure(_configuration())
    await service.run_worker(now=NOW)
    status = await service.get_reporting_status({"view": "periods"})
    return status["periods"][0], status["revisions"][0]


def unreadable(period: dict[str, Any], revision: dict[str, Any], at: Any) -> dict[str, Any]:
    """The buyer cannot read a revision the seller serves: the provider must act."""
    return {
        "reporting_status_id": "status-unreadable-0001",
        "delivery_config_id": period["delivery_config_id"],
        "delivery_config_version": period["delivery_config_version"],
        "report_definition_id": period["report_definition_id"],
        "period": period["period"],
        "consumer_status": "unreadable",
        "failure_code": "transport_failed",
        "reporting_obligation_id": period["reporting_obligation_id"],
        "reporting_revision_id": revision["reporting_revision_id"],
        "status_as_of": at.isoformat(),
    }


def agreeing(statement: dict[str, Any], revision: dict[str, Any], at: Any) -> dict[str, Any]:
    agreed = {
        **statement,
        "reporting_status_id": "status-received-0001",
        "supersedes_reporting_status_id": statement["reporting_status_id"],
        "consumer_status": "received",
        "observed_revision_content_sha256": revision["revision_content_sha256"],
        "status_as_of": at.isoformat(),
    }
    del agreed["failure_code"]
    return agreed


async def run_lifecycle_flow(
    service: ReliableReportingService, clock: DeterministicReportingClock, observed: Observed
) -> None:
    """Open on ingest, escalate on time, resolve on agreement: one event each."""
    period, revision = await published_period(service)
    clock.advance(timedelta(minutes=1))
    opened_at = clock()
    statement = unreadable(period, revision, opened_at)
    recorded = await service.sync_reporting_status({"statuses": [statement]})
    assert recorded["results"][0]["result"] == "recorded"
    assert observed.events == []  # delivered by the worker turn, after commit

    turn = await service.run_worker()
    assert turn.issue_events is not None and turn.issue_events.delivered == 1
    (opened,) = observed.events
    assert opened.kind == "opened" and opened.occurred_at == opened_at
    assert opened.event_id == issue_event_id(opened.issue_id, "opened")
    assert (opened.account_id, opened.consumer_id, opened.attempt) == (ACCOUNT, CONSUMER, 1)
    assert opened.reporting_status_id == statement["reporting_status_id"]
    issue = opened.issue
    assert issue is not None and issue.issue_id == opened.issue_id
    assert issue.code == "CONSUMER_STATUS_MISMATCH"
    assert (issue.responsible_party, issue.recommended_action) == ("provider", "contact_provider")
    assert (issue.severity, issue.issue_state, issue.opened_at) == (
        "action_required",
        "open",
        opened_at,
    )
    assert issue.reporting_obligation_id == period["reporting_obligation_id"]

    # An exact retry, a status read and an idle turn change nothing.
    replay = await service.sync_reporting_status({"statuses": [statement]})
    assert replay["results"][0]["result"] == "unchanged"
    status = await service.get_reporting_status({"view": "summary"})
    assert [item["issue_id"] for item in status["issues"]] == [opened.issue_id]
    assert not (await service.run_worker()).issue_events.did_work
    assert observed.kinds() == ["opened"]

    # Escalation fires when the boundary passes, with nobody reading status.
    clock.set(opened_at + ESCALATION.consumer_mismatch_escalation - timedelta(seconds=1))
    assert not (await service.run_worker()).issue_events.did_work
    clock.set(opened_at + ESCALATION.consumer_mismatch_escalation)
    turn = await service.run_worker()
    assert turn.issue_events.escalations_recorded == 1 and turn.issue_events.delivered == 1
    escalated = observed.events[-1]
    assert escalated.kind == "escalated" and escalated.issue_id == opened.issue_id
    assert escalated.occurred_at == opened_at + ESCALATION.consumer_mismatch_escalation
    assert escalated.issue is not None
    assert escalated.issue.recommended_action == "contact_provider"
    assert escalated.issue.severity == "action_required"
    assert not (await service.run_worker()).issue_events.did_work  # once

    # The buyer files an agreeing statement: resolved.
    clock.advance(timedelta(minutes=1))
    agreed = await service.sync_reporting_status(
        {"statuses": [agreeing(statement, revision, clock())]}
    )
    assert agreed["results"][0]["result"] == "recorded"
    await service.run_worker()
    resolved = observed.events[-1]
    assert (resolved.kind, resolved.issue_id, resolved.issue) == ("resolved", opened.issue_id, None)
    assert resolved.occurred_at == clock()
    assert observed.kinds() == ["opened", "escalated", "resolved"]
    assert len({event.event_id for event in observed.events}) == 3
    assert observed.errors == []


async def run_failing_observer_flow(
    service: ReliableReportingService, clock: DeterministicReportingClock, observed: Observed
) -> None:
    """A raising observer fails no call, is reported, and is retried with the same id."""
    period, revision = await published_period(service)
    clock.advance(timedelta(minutes=1))
    observed.fail.add("opened")
    statement = unreadable(period, revision, clock())
    recorded = await service.sync_reporting_status({"statuses": [statement]})
    assert recorded["results"][0]["result"] == "recorded"
    turn = await service.run_worker()
    assert turn.issue_events is not None
    assert (turn.issue_events.delivered, turn.issue_events.failed) == (0, 1)
    assert turn.extension_errors == {}  # the turn itself succeeded
    ((component, error),) = observed.errors
    assert component.startswith("issue_observer:rpie_")
    assert str(error) == "observer rejected opened"
    # Not due again until the backoff passes; then delivered once.
    assert not (await service.run_worker()).issue_events.did_work
    clock.advance(timedelta(seconds=30))
    await service.run_worker()
    (opened,) = observed.events
    assert opened.kind == "opened" and opened.attempt == 2
    assert component == f"issue_observer:{opened.event_id}"
    assert not (await service.run_worker()).issue_events.did_work


# -- the memory reference -----------------------------------------------------


async def test_the_lifecycle_reaches_the_observer_once_per_transition() -> None:
    clock = DeterministicReportingClock(NOW)
    observed = Observed()
    service = ReliableReportingService.memory(**service_options(clock, observed))
    await run_lifecycle_flow(service, clock, observed)


async def test_a_failing_observer_is_reported_and_retried_with_the_same_event_id() -> None:
    clock = DeterministicReportingClock(NOW)
    observed = Observed()
    service = ReliableReportingService.memory(**service_options(clock, observed))
    await run_failing_observer_flow(service, clock, observed)


def test_issue_event_ids_are_stable_and_match_the_storage_distribution() -> None:
    assert issue_event_id("rpti_x", "opened") == issue_event_id("rpti_x", "opened")
    assert issue_event_id("rpti_x", "opened") != issue_event_id("rpti_x", "resolved")
    assert issue_event_id("rpti_x", "opened").startswith("rpie_")
    storage = pytest.importorskip("adcp_reporting_storage.ledger.issue_events")
    for kind in ("opened", "escalated", "acknowledged", "waived", "resolved"):
        assert storage.issue_event_id("rpti_x", kind) == issue_event_id("rpti_x", kind)


async def _events(store: InMemoryReportingLedgerStore) -> list[tuple[str, str, Any]]:
    return [
        (event.issue_id, event.kind, event.occurred_at)
        for event in sorted(store._issue_events.values(), key=lambda e: e.seq)
    ]


async def _open(store: InMemoryReportingLedgerStore, clock: Any, key: str = "rpik_condition"):
    return await store.ensure_issue_opened(
        issue_key=key, account_id=ACCOUNT, consumer_id=CONSUMER, observed_at=clock()
    )


async def test_the_memory_store_records_each_lifecycle_write_once() -> None:
    clock = DeterministicReportingClock(NOW)
    store = InMemoryReportingLedgerStore(clock=clock)
    assert supports_issue_events(store) and await store.issue_events_ready()
    first = await _open(store, clock)
    await _open(store, clock)  # idempotent
    acknowledged_at = clock.advance(timedelta(minutes=1))
    for ref in (None, None, "TICKET-1"):  # re-acknowledging and attaching a ref record nothing
        await store.set_issue_state(
            issue_key=first.issue_key,
            account_id=ACCOUNT,
            state="acknowledged",
            at=acknowledged_at,
            external_ref=ref,
        )
    resolved_at = clock.advance(timedelta(minutes=1))
    await store.retire_issue(issue_key=first.issue_key, account_id=ACCOUNT, at=resolved_at)
    second = await _open(store, clock)
    waived_at = clock.advance(timedelta(minutes=1))
    await store.set_issue_state(
        issue_key=second.issue_key, account_id=ACCOUNT, state="waived", at=waived_at
    )
    assert await _events(store) == [
        (first.issue_id, "opened", NOW),
        (first.issue_id, "acknowledged", acknowledged_at),
        (first.issue_id, "resolved", resolved_at),
        (second.issue_id, "opened", resolved_at),
        (second.issue_id, "waived", waived_at),
    ]


async def test_a_rolled_back_lifecycle_write_records_no_event() -> None:
    clock = DeterministicReportingClock(NOW)
    store = InMemoryReportingLedgerStore(clock=clock)
    with pytest.raises(RuntimeError, match="abort"):
        async with store.transaction():
            await _open(store, clock)
            raise RuntimeError("abort")
    assert await _events(store) == []


async def test_memory_claims_lease_one_event_per_occurrence_in_order() -> None:
    clock = DeterministicReportingClock(NOW)
    store = InMemoryReportingLedgerStore(clock=clock)
    a = await _open(store, clock, "rpik_a")
    await store.set_issue_state(
        issue_key=a.issue_key, account_id=ACCOUNT, state="acknowledged", at=clock()
    )
    b = await _open(store, clock, "rpik_b")
    first = await store.claim_issue_events(lease_seconds=30, limit=10)
    assert [(lease.issue_id, lease.kind) for lease in first] == [
        (a.issue_id, "opened"),
        (b.issue_id, "opened"),
    ]
    assert await store.claim_issue_events(lease_seconds=30) == ()  # leased
    assert await store.complete_issue_event(first[0])
    assert not await store.complete_issue_event(first[0])
    (acknowledged,) = await store.claim_issue_events(lease_seconds=30)
    assert (acknowledged.issue_id, acknowledged.kind) == (a.issue_id, "acknowledged")
    assert await store.release_issue_event(acknowledged, retry_after=timedelta(seconds=10))
    assert await store.claim_issue_events(lease_seconds=30) == ()
    clock.advance(timedelta(seconds=10))
    (again,) = await store.claim_issue_events(lease_seconds=30)
    assert (again.event_id, again.attempts) == (acknowledged.event_id, 2)
    clock.advance(timedelta(seconds=31))  # both leases lapse
    reclaimed = await store.claim_issue_events(lease_seconds=30)
    assert {lease.event_id for lease in reclaimed} == {first[1].event_id, again.event_id}
    assert not await store.complete_issue_event(first[1])  # the stale lease lost it
    for lease in reclaimed:
        assert await store.complete_issue_event(lease)
    clock.advance(timedelta(days=8))
    assert await store.prune_issue_events(older_than=timedelta(days=7)) == 3
    assert await _events(store) == []


async def test_escalation_is_recorded_once_and_never_for_a_retired_occurrence() -> None:
    clock = DeterministicReportingClock(NOW)
    store = InMemoryReportingLedgerStore(clock=clock)
    opened = await _open(store, clock, "rpik_open")
    resolved = await _open(store, clock, "rpik_resolved")
    await store.retire_issue(issue_key=resolved.issue_key, account_id=ACCOUNT, at=clock())
    waived = await _open(store, clock, "rpik_waived")
    await store.set_issue_state(
        issue_key=waived.issue_key, account_id=ACCOUNT, state="waived", at=clock()
    )
    clock.set(NOW + timedelta(minutes=59, seconds=59))
    assert await store.sweep_issue_escalations(after=timedelta(hours=1)) == 0
    clock.set(NOW + timedelta(hours=1))
    results = await asyncio.gather(
        *(store.sweep_issue_escalations(after=timedelta(hours=1)) for _ in range(3))
    )
    assert sum(results) == 1
    escalated = [event for event in await _events(store) if event[1] == "escalated"]
    assert escalated == [(opened.issue_id, "escalated", NOW + timedelta(hours=1))]


async def test_an_occurrence_waits_for_its_earlier_event_to_be_delivered() -> None:
    clock = DeterministicReportingClock(NOW)
    observed = Observed(fail={"opened"})
    store = InMemoryReportingLedgerStore(clock=clock)
    dispatcher = ReportingIssueEventDispatcher(
        store, observed.observe, escalation=ESCALATION, on_error=_collect(observed)
    )
    await _open(store, clock)
    clock.set(NOW + timedelta(hours=1))
    first = await dispatcher.run_once()
    assert (first.escalations_recorded, first.delivered, first.failed) == (1, 0, 1)
    assert observed.events == []  # 'escalated' waits behind the failed 'opened'
    clock.advance(timedelta(seconds=30))
    second = await dispatcher.run_once()
    assert second.delivered == 2
    assert observed.kinds() == ["opened", "escalated"]


def _collect(observed: Observed):
    async def report(component: str, error: BaseException) -> None:
        observed.report(component, error)

    return report


async def test_a_hung_observer_times_out_and_a_sync_observer_works() -> None:
    clock = DeterministicReportingClock(NOW)
    store = InMemoryReportingLedgerStore(clock=clock)
    await _open(store, clock)
    observed = Observed()

    async def hang(event: ReportingIssueEvent) -> None:
        await asyncio.sleep(60)

    hung = ReportingIssueEventDispatcher(
        store, hang, observer_timeout=timedelta(milliseconds=20), on_error=_collect(observed)
    )
    assert (await hung.run_once()).failed == 1
    ((component, error),) = observed.errors
    assert component.startswith("issue_observer:") and isinstance(error, asyncio.TimeoutError)
    seen: list[ReportingIssueEvent] = []
    clock.advance(timedelta(seconds=30))
    assert (await ReportingIssueEventDispatcher(store, seen.append).run_once()).delivered == 1
    assert [event.kind for event in seen] == ["opened"]
    assert seen[0].attempt == 2


async def test_operator_transitions_render_in_their_state() -> None:
    """Acknowledged and waived come from store.set_issue_state, outside any request."""
    clock = DeterministicReportingClock(NOW)
    observed = Observed()
    service = ReliableReportingService.memory(**service_options(clock, observed))
    period, revision = await published_period(service)
    clock.advance(timedelta(minutes=1))
    await service.sync_reporting_status({"statuses": [unreadable(period, revision, clock())]})
    await service.run_worker()
    (opened,) = observed.events
    key = next(iter(service.store._issues.values())).issue_key
    clock.advance(timedelta(minutes=5))
    await service.store.set_issue_state(
        issue_key=key, account_id=ACCOUNT, state="acknowledged", at=clock()
    )
    clock.advance(timedelta(minutes=5))
    await service.store.set_issue_state(
        issue_key=key, account_id=ACCOUNT, state="waived", at=clock()
    )
    await service.run_worker()
    assert observed.kinds() == ["opened", "acknowledged", "waived"]
    acknowledged, waived = observed.events[1:]
    assert acknowledged.issue is not None and acknowledged.issue.issue_state == "acknowledged"
    assert waived.issue is not None and waived.issue.issue_state == "waived"
    assert waived.issue.responsible_party == "provider"
    assert {event.issue_id for event in observed.events} == {opened.issue_id}


async def test_a_condition_that_cleared_unobserved_resolves_instead_of_escalating() -> None:
    clock = DeterministicReportingClock(NOW)
    observed = Observed()
    service = ReliableReportingService.memory(**service_options(clock, observed))
    period, revision = await published_period(service)
    clock.advance(timedelta(minutes=1))
    await service.sync_reporting_status({"statuses": [unreadable(period, revision, clock())]})
    # The seller now agrees the revision is unreadable; nobody reads status.
    await service.store.set_revision_readable(
        account_id=ACCOUNT, reporting_revision_id=revision["reporting_revision_id"], readable=False
    )
    clock.advance(ESCALATION.consumer_mismatch_escalation)
    turn = await service.run_worker()
    assert turn.issue_events is not None and turn.issue_events.escalations_recorded == 0
    assert observed.kinds() == ["opened", "resolved"]


def test_the_service_refuses_production_and_stores_without_issue_events() -> None:
    with pytest.raises(ReliableReportingConfigurationError, match="#1873"):
        ReliableReportingService(
            store=InMemoryReportingLedgerStore(),
            account_context=_account_context,
            production=SimpleNamespace(),
            issue_observer=lambda event: None,
        )

    class LegacyStore(InMemoryReportingLedgerStore):
        issue_events_supported = False

    with pytest.raises(ReliableReportingConfigurationError, match="0.2.0b4"):
        ReliableReportingService(
            store=LegacyStore(),
            account_context=_account_context,
            issue_observer=lambda event: None,
        )
    with pytest.raises(ReliableReportingConfigurationError, match="callable"):
        ReliableReportingService.memory(
            account_context=_account_context, issue_observer="not callable"
        )


async def test_the_service_refuses_to_start_before_the_event_table_exists() -> None:
    class Unmigrated(InMemoryReportingLedgerStore):
        async def issue_events_ready(self) -> bool:
            return False

    service = ReliableReportingService(
        store=Unmigrated(), account_context=_account_context, issue_observer=lambda event: None
    )
    with pytest.raises(ReliableReportingConfigurationError, match="python -m adcp migrate"):
        await service.initialize()
