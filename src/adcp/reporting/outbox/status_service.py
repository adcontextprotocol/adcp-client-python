"""Optional status lifecycle composition, without importing PostgreSQL extras."""

from __future__ import annotations

from typing import Protocol

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.status import StatusTurn
from adcp.reporting.outbox.status_support import ReportingStatusSupport


class ReportingStatusNotificationLifecycle(Protocol):
    """Opt-in lifecycle; existing ledger and outbox implementations need no changes."""

    async def migrate(self) -> None: ...

    async def baseline(self) -> None: ...

    async def ready(self) -> bool: ...

    async def project_dirty_once(self, *, account_id: str) -> StatusTurn: ...

    async def sweep_due_once(self, *, account_id: str) -> StatusTurn: ...

    async def expand_once(self, *, account_id: str) -> bool: ...

    async def deliver_once(self, *, account_id: str) -> bool: ...

    async def drain(self, *, max_turns: int = 100) -> int: ...

    async def aclose(self) -> None: ...


class ReportingStatusService:
    """Deterministic single turns for the adopter's supervised scheduler.

    The caller owns scheduling and the database pool. ``drain`` processes work
    due now; future HTTP retries and deadlines remain durable. Stop scheduling,
    await in-flight turns, drain, close this service, then close the owned pool.
    No background task or second HTTP client is created by this composition.
    """

    def __init__(self, support: ReportingStatusSupport) -> None:
        self.support = support
        self._closed = False

    def _open(self, account_id: str | None = None) -> None:
        if self._closed:
            raise ReportingNotificationError("status_service_closed")
        if account_id is not None and account_id not in self.support.account_ids:
            raise ReportingNotificationError("status_account_unavailable")

    async def migrate(self) -> None:
        self._open()
        await self.support.store.create_schema()

    async def baseline(self) -> None:
        self._open()
        for account_id in sorted(set(self.support.account_ids)):
            await self.support.store.baseline(account_id=account_id)

    async def ready(self) -> bool:
        return not self._closed and await self.support.durable()

    async def project_dirty_once(self, *, account_id: str) -> StatusTurn:
        self._open(account_id)
        if self.support.projector is None:
            raise ReportingNotificationError("status_projector_unavailable")
        return await self.support.projector.run_once(account_id=account_id)

    async def sweep_due_once(self, *, account_id: str) -> StatusTurn:
        self._open(account_id)
        if self.support.sweeper is None:
            raise ReportingNotificationError("status_sweeper_unavailable")
        return await self.support.sweeper.run_once(account_id=account_id)

    async def expand_once(self, *, account_id: str) -> bool:
        self._open(account_id)
        return await self.support.worker.expand_one(account_id=account_id)

    async def deliver_once(self, *, account_id: str) -> bool:
        self._open(account_id)
        return await self.support.worker.deliver_one(account_id=account_id)

    async def drain(self, *, max_turns: int = 100) -> int:
        self._open()
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        for turn in range(max_turns):
            worked = False
            for account_id in sorted(set(self.support.account_ids)):
                worked = (await self.project_dirty_once(account_id=account_id)).did_work or worked
                worked = (await self.sweep_due_once(account_id=account_id)).did_work or worked
                worked = await self.expand_once(account_id=account_id) or worked
                worked = await self.deliver_once(account_id=account_id) or worked
            if not worked:
                return turn
        raise ReportingNotificationError("status_drain_limit")

    async def aclose(self) -> None:
        self._closed = True
