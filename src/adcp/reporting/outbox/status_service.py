"""Optional status lifecycle composition, without importing PostgreSQL extras."""

from __future__ import annotations

from typing import Protocol

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.status import StatusSelectorRebuildStore, StatusTurn
from adcp.reporting.outbox.status_support import (
    ReportingStatusAccountSurface,
    ReportingStatusSupport,
)


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

    def _open(self) -> None:
        if self._closed:
            raise ReportingNotificationError("status_service_closed")

    async def _account_surface(self) -> ReportingStatusAccountSurface:
        self._open()
        surface = await self.support.resolve_account_surface()
        if (
            not surface.account_surface_complete
            and self.support.account_surface_resolver is not None
        ) or any(not a for a in surface.account_ids):
            raise ReportingNotificationError("status_account_surface_unavailable")
        return surface

    def _require_account(self, *, account_id: str, surface: ReportingStatusAccountSurface) -> None:
        self._open()
        if account_id not in surface.account_ids:
            raise ReportingNotificationError("status_account_unavailable")

    async def migrate(self) -> None:
        self._open()
        await self.support.store.create_schema()

    async def baseline(self) -> None:
        surface = await self._account_surface()
        for account_id in sorted(set(surface.account_ids)):
            await self.support.store.baseline(account_id=account_id)

    async def ready(self) -> bool:
        return not self._closed and await self.support.durable()

    async def project_dirty_once(self, *, account_id: str) -> StatusTurn:
        surface = await self._account_surface()
        return await self._project_dirty_once(account_id=account_id, surface=surface)

    async def _project_dirty_once(
        self, *, account_id: str, surface: ReportingStatusAccountSurface
    ) -> StatusTurn:
        self._require_account(account_id=account_id, surface=surface)
        if self.support.projector is None:
            raise ReportingNotificationError("status_projector_unavailable")
        return await self.support.projector.run_once(account_id=account_id)

    async def rebuild_selector_once(self) -> StatusTurn:
        """Indexed C cutover, including retained accounts outside current registrations."""
        self._open()
        store = self.support.store
        if not isinstance(store, StatusSelectorRebuildStore):
            return StatusTurn(False)  # Existing custom lifecycle protocols stay compatible.
        return await store.rebuild_one()

    async def sweep_due_once(self, *, account_id: str) -> StatusTurn:
        surface = await self._account_surface()
        return await self._sweep_due_once(account_id=account_id, surface=surface)

    async def _sweep_due_once(
        self, *, account_id: str, surface: ReportingStatusAccountSurface
    ) -> StatusTurn:
        self._require_account(account_id=account_id, surface=surface)
        if self.support.sweeper is None:
            raise ReportingNotificationError("status_sweeper_unavailable")
        return await self.support.sweeper.run_once(account_id=account_id)

    async def expand_once(self, *, account_id: str) -> bool:
        surface = await self._account_surface()
        return await self._expand_once(account_id=account_id, surface=surface)

    async def _expand_once(
        self, *, account_id: str, surface: ReportingStatusAccountSurface
    ) -> bool:
        self._require_account(account_id=account_id, surface=surface)
        return await self.support.worker.expand_one(account_id=account_id)

    async def deliver_once(self, *, account_id: str) -> bool:
        surface = await self._account_surface()
        return await self._deliver_once(account_id=account_id, surface=surface)

    async def _deliver_once(
        self, *, account_id: str, surface: ReportingStatusAccountSurface
    ) -> bool:
        self._require_account(account_id=account_id, surface=surface)
        return await self.support.worker.deliver_one(account_id=account_id)

    async def drain(self, *, max_turns: int = 100) -> int:
        self._open()
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        for turn in range(max_turns):
            surface = await self._account_surface()
            worked = (await self.rebuild_selector_once()).did_work
            for account_id in sorted(set(surface.account_ids)):
                worked = (
                    await self._project_dirty_once(account_id=account_id, surface=surface)
                ).did_work or worked
                worked = (
                    await self._sweep_due_once(account_id=account_id, surface=surface)
                ).did_work or worked
                worked = await self._expand_once(account_id=account_id, surface=surface) or worked
                worked = await self._deliver_once(account_id=account_id, surface=surface) or worked
            if not worked:
                return turn
        raise ReportingNotificationError("status_drain_limit")

    async def aclose(self) -> None:
        self._closed = True
