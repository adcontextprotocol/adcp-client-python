"""Lifecycle operations consume a current, consistent account surface."""

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from adcp.reporting.outbox import (
    ReportingNotificationError,
    ReportingStatusAccountSurface,
    ReportingStatusService,
    ReportingStatusSupport,
)
from adcp.reporting.outbox.status import StatusTurn


def surface(*account_ids: str) -> ReportingStatusAccountSurface:
    return ReportingStatusAccountSurface(
        account_ids=account_ids,
        account_surface_complete=True,
        readiness_subscriptions=(),
    )


@dataclass
class ServiceState:
    surface: ReportingStatusAccountSurface = field(default_factory=lambda: surface("acct_a"))
    next_surface: ReportingStatusAccountSurface | None = None
    resolver_calls: int = 0
    baselined: list[str] = field(default_factory=list)
    turn_accounts: list[str] = field(default_factory=list)
    history: list[tuple[str, str | None]] = field(default_factory=list)
    project_work_remaining: int = 0

    async def resolve_surface(self) -> ReportingStatusAccountSurface:
        self.resolver_calls += 1
        return self.surface

    def record(self, component: str, account_id: str | None = None) -> None:
        self.history.append((component, account_id))
        if self.next_surface is not None:
            self.surface, self.next_surface = self.next_surface, None


@dataclass
class StatusStore:
    state: ServiceState

    async def baseline(self, *, account_id: str) -> bool:
        self.state.baselined.append(account_id)
        self.state.record("baseline", account_id)
        return False

    async def rebuild_one(self) -> StatusTurn:
        self.state.record("rebuild")
        return StatusTurn(False)


@dataclass
class StatusComponent:
    state: ServiceState
    name: str

    async def run_once(self, *, account_id: str) -> StatusTurn:
        self.state.record(self.name, account_id)
        if self.name == "project":
            self.state.turn_accounts.append(account_id)
            if self.state.project_work_remaining:
                self.state.project_work_remaining -= 1
                return StatusTurn(True)
        return StatusTurn(False)


@dataclass
class StatusWorker:
    state: ServiceState

    async def expand_one(self, *, account_id: str) -> bool:
        self.state.record("expand", account_id)
        return False

    async def deliver_one(self, *, account_id: str) -> bool:
        self.state.record("deliver", account_id)
        return False


@pytest.fixture
def dynamic_service() -> tuple[ReportingStatusService, ServiceState]:
    state = ServiceState()
    store: Any = StatusStore(state)
    worker: Any = StatusWorker(state)
    projector: Any = StatusComponent(state, "project")
    sweeper: Any = StatusComponent(state, "sweep")
    support = ReportingStatusSupport(
        store=store,
        worker=worker,
        projector=projector,
        sweeper=sweeper,
        scheduled=True,
        account_ids=("acct_a", "acct_b"),
        account_surface_complete=True,
        account_surface_resolver=state.resolve_surface,
    )
    return ReportingStatusService(support), state


# Catches baseline retaining constructor IDs after enrollment changes.
async def test_service_baseline_resolves_new_accounts_each_call(
    dynamic_service: tuple[ReportingStatusService, ServiceState],
) -> None:
    service, state = dynamic_service
    state.surface = surface("acct_a")
    await service.baseline()
    state.surface = surface("acct_a", "acct_b")
    await service.baseline()
    assert state.baselined == ["acct_a", "acct_a", "acct_b"]
    assert state.resolver_calls == 2


# Catches baseline re-resolving its account list after the first store operation.
async def test_service_baseline_uses_one_surface_snapshot(
    dynamic_service: tuple[ReportingStatusService, ServiceState],
) -> None:
    service, state = dynamic_service
    state.surface = surface("acct_b", "acct_a", "acct_b")
    state.next_surface = surface("acct_a", "acct_c")
    await service.baseline()
    assert state.baselined == ["acct_a", "acct_b"]
    assert state.resolver_calls == 1


# Catches any explicit operation accepting an account removed from the current surface.
@pytest.mark.parametrize(
    "operation", ["project_dirty_once", "sweep_due_once", "expand_once", "deliver_once"]
)
async def test_service_explicit_turn_rejects_account_removed_from_current_surface(
    dynamic_service: tuple[ReportingStatusService, ServiceState], operation: str
) -> None:
    service, state = dynamic_service
    state.surface = surface("acct_a")
    with pytest.raises(ReportingNotificationError, match="status_account_unavailable"):
        await getattr(service, operation)(account_id="acct_b")
    assert state.history == []
    assert state.resolver_calls == 1


# Catches explicit operations rejecting enrolled accounts absent from constructor IDs.
@pytest.mark.parametrize(
    ("operation", "component"),
    [
        ("project_dirty_once", "project"),
        ("sweep_due_once", "sweep"),
        ("expand_once", "expand"),
        ("deliver_once", "deliver"),
    ],
)
async def test_service_explicit_turn_accepts_account_from_current_surface(
    dynamic_service: tuple[ReportingStatusService, ServiceState], operation: str, component: str
) -> None:
    service, state = dynamic_service
    state.surface = surface("acct_c")
    result = await getattr(service, operation)(account_id="acct_c")
    assert result == (StatusTurn(False) if component in {"project", "sweep"} else False)
    assert state.history == [(component, "acct_c")]
    assert state.resolver_calls == 1


# Catches public operation re-resolution mixing account sets during one drain turn.
async def test_drain_uses_one_surface_snapshot_per_turn(
    dynamic_service: tuple[ReportingStatusService, ServiceState],
) -> None:
    service, state = dynamic_service
    state.surface = surface("acct_a", "acct_b")
    state.next_surface = surface("acct_a", "acct_c")
    assert await service.drain(max_turns=1) == 0
    assert state.resolver_calls == 1
    assert state.turn_accounts == ["acct_a", "acct_b"]
    assert state.history == [
        ("rebuild", None),
        ("project", "acct_a"),
        ("sweep", "acct_a"),
        ("expand", "acct_a"),
        ("deliver", "acct_a"),
        ("project", "acct_b"),
        ("sweep", "acct_b"),
        ("expand", "acct_b"),
        ("deliver", "acct_b"),
    ]


# Catches drain retaining the first snapshot when work requires another turn.
async def test_drain_resolves_accounts_again_on_the_next_turn(
    dynamic_service: tuple[ReportingStatusService, ServiceState],
) -> None:
    service, state = dynamic_service
    state.surface = surface("acct_a")
    state.next_surface = surface("acct_a", "acct_b")
    state.project_work_remaining = 1
    assert await service.drain(max_turns=2) == 1
    assert state.resolver_calls == 2
    assert state.turn_accounts == ["acct_a", "acct_a", "acct_b"]


# Catches invalid snapshots dispatching work using stale static registrations.
@pytest.mark.parametrize(
    "operation",
    ["baseline", "drain", "project_dirty_once", "sweep_due_once", "expand_once", "deliver_once"],
)
@pytest.mark.parametrize("invalid", ["incomplete", "account_id"])
async def test_service_rejects_invalid_current_surface_before_dispatch(
    dynamic_service: tuple[ReportingStatusService, ServiceState], operation: str, invalid: str
) -> None:
    service, state = dynamic_service
    state.surface = (
        replace(surface("acct_a"), account_surface_complete=False)
        if invalid == "incomplete"
        else surface("acct_a", "")
    )
    kwargs = {} if operation in {"baseline", "drain"} else {"account_id": "acct_a"}
    with pytest.raises(ReportingNotificationError, match="^status_account_surface_unavailable$"):
        await getattr(service, operation)(**kwargs)
    assert state.history == []
    assert state.resolver_calls == 1


# Catches treating a complete empty surface as stale constructor accounts or an error.
async def test_service_baseline_accepts_complete_empty_surface(
    dynamic_service: tuple[ReportingStatusService, ServiceState],
) -> None:
    service, state = dynamic_service
    state.surface = surface()
    await service.baseline()
    assert state.baselined == []
    assert state.resolver_calls == 1


# Catches suppressing deployment-wide selector rebuilds without current registrations.
async def test_drain_rebuilds_with_complete_empty_surface(
    dynamic_service: tuple[ReportingStatusService, ServiceState],
) -> None:
    service, state = dynamic_service
    state.surface = surface()
    assert await service.drain(max_turns=1) == 0
    assert state.history == [("rebuild", None)]
    assert state.resolver_calls == 1


# Catches drain dropping its bounded-work error while refreshing account snapshots.
async def test_drain_preserves_turn_limit(
    dynamic_service: tuple[ReportingStatusService, ServiceState],
) -> None:
    service, state = dynamic_service
    state.surface = surface("acct_a")
    state.project_work_remaining = 2
    with pytest.raises(ReportingNotificationError, match="^status_drain_limit$"):
        await service.drain(max_turns=2)
    assert state.turn_accounts == ["acct_a", "acct_a"]
    assert state.resolver_calls == 2
