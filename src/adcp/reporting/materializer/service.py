"""Service-owned heartbeat around destination I/O outside database transactions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from adcp.reporting.materializer.contracts import (
    ReportingDestinationWriter,
    ReportingIOContext,
    ReportingWriterError,
    ReportingWriterFailure,
    _join_tasks,
    failure,
)
from adcp.reporting.materializer.verification import ReportingDestinationIO
from adcp.reporting.materializer.work import (
    ReportingMaterializerLease,
    ReportingMaterializerStore,
    ReportingMaterializerTurn,
    validate_lease_seconds,
)


class _LeaseHeartbeat:
    def __init__(
        self,
        store: ReportingMaterializerStore,
        lease: ReportingMaterializerLease,
        seconds: int,
        cancel: asyncio.Event,
    ) -> None:
        self.store, self.lease, self.seconds, self.cancel = store, lease, seconds, cancel
        self.stopped = asyncio.Event()
        self.lost = False

    async def checkpoint(self) -> None:
        if self.lost:
            raise failure("LEASE_LOST")

    async def run(self) -> None:
        while not self.stopped.is_set():
            try:
                await asyncio.wait_for(self.stopped.wait(), self.seconds / 3)
                return
            except asyncio.TimeoutError:
                pass
            held = False
            try:
                held = await self.store.renew_materialization(
                    self.lease, lease_seconds=self.seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                held = False  # Driver/provider bodies never escape service diagnostics.
            if not held:
                self.lost = True
                self.cancel.set()
                return


@dataclass(frozen=True)
class ReportingMaterializerService:
    """One autonomous turn; schedule repeatedly, including after process restart.

    The destination must implement its advertised conditional/idempotent write
    semantics durably. A timeout or interrupted outcome commit retains the same
    immutable attempt. The SDK opens fresh authorization sessions for write and
    paginated readback; the service checks current ledger authority before each.
    """

    store: ReportingMaterializerStore
    io: ReportingDestinationIO = field(repr=False)
    writer: ReportingDestinationWriter = field(repr=False)
    lease_seconds: int = 30
    io_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        validate_lease_seconds(self.lease_seconds)
        if type(self.io_timeout_seconds) is not int or not 1 <= self.io_timeout_seconds <= 3600:
            raise ValueError("materializer I/O deadline requires 1..3600 seconds")
        if type(self.io) is not ReportingDestinationIO:
            raise failure("UNSUPPORTED_VERIFICATION")

    async def run_once(self) -> ReportingMaterializerTurn:
        keys = tuple(
            v.key
            for v in self.io.registry.verifiers
            if v.key.capability in self.writer.capabilities
        )
        reserved = await self.store.claim_materialization(
            keys=keys, lease_seconds=self.lease_seconds
        )
        if isinstance(reserved, ReportingMaterializerTurn):
            return reserved
        cancel = asyncio.Event()
        heartbeat = _LeaseHeartbeat(self.store, reserved, self.lease_seconds, cancel)
        task = asyncio.create_task(heartbeat.run())
        context = ReportingIOContext(
            datetime.now(timezone.utc) + timedelta(seconds=self.io_timeout_seconds),
            cancel,
            heartbeat,
        )
        canceled = False
        turn = ReportingMaterializerTurn(
            "pending", "effect_unknown", reserved.attempt.reporting_materialization_id
        )
        try:
            try:
                await self.store.authorize_materialization(reserved)
                target = reserved.context
                if target.delivery is None:
                    raise failure("BINDING_MISMATCH")
                prepared = await self.io.registry.prepare(
                    key=reserved.request.verification_key,
                    binding=target.binding,
                    delivery=target.delivery,
                    obligation=target.obligation,
                    revisions=target.revisions,
                    attempt=reserved.attempt,
                    reader=self.store,
                    context=context,
                )
                await self.store.authorize_materialization(reserved)
                locator = await self.io.write(prepared, context=context)
                await self.store.authorize_materialization(reserved)
                verified = await self.io.verify(prepared, locator, context=context)
                turn = await self.store.finish_materialization(
                    reserved, prepared=prepared, verified=verified
                )
            except ReportingWriterError as exc:
                record = exc.failure
                # The writer owns whether a failure is known and terminal. All
                # uncertain I/O and lost leases keep the external identity.
                if record.code in {"DEADLINE_EXCEEDED", "LEASE_LOST"}:
                    record = ReportingWriterFailure(record.code, "same_identity", "unknown")
                turn = await self.store.finish_materialization(reserved, error=record)
        except asyncio.CancelledError:
            canceled = not heartbeat.lost
        except Exception:
            # Includes commit/connection loss and failed atomic outbox enqueue.
            # The lease expires; a restart reuses the original identity.
            turn = ReportingMaterializerTurn(
                "pending", "effect_unknown", reserved.attempt.reporting_materialization_id
            )
        finally:
            heartbeat.stopped.set()
            task.cancel()
            canceled = await _join_tasks(task) or canceled
        if canceled:
            raise asyncio.CancelledError
        return turn
