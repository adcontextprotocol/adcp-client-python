"""Strictly typed #1168C Core mounting; install ``adcp[pg]``.

The adopter supplies real account configuration and immutable revision-content
handlers, trusted caller resolution and a durable subscription/key registry.
This example does not implement sync_accounts registration echo. Registration
remains an adopter registry operation after authorization and endpoint proof.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from adcp.reporting.ledger import (
    ConsumerStatusIngest,
    PgReportingLedgerStore,
    ReportingDeliveryEscalation,
    ReportingStatusCallerResolver,
    ReportingStatusHandler,
    ReportingStatusNotificationHandler,
)
from adcp.reporting.outbox import (
    PgStatusNotificationStore,
    ReportingEnvelopeCipher,
    ReportingNotificationError,
    ReportingNotificationSubscription,
    ReportingNotificationWorker,
    ReportingSigningResolver,
    ReportingStatusNotificationLifecycle,
    ReportingStatusProjector,
    ReportingStatusService,
    ReportingStatusSupport,
    ReportingStatusSweeper,
    ReportingSubscriptionResolver,
)
from adcp.reporting.outbox.status_support import validate_status_claims
from adcp.server.base import ToolContext
from adcp.types import (
    GetAdcpCapabilitiesRequest,
    GetMediaBuyDeliveryRequest,
    ReportingDeliveryCapabilities,
    SyncAccountsRequest,
    SyncReportingStatusRequest,
)

Task = Callable[[dict[str, Any], ToolContext | None], Awaitable[dict[str, Any]]]


def request_dict(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    return value if isinstance(value, dict) else value.model_dump(mode="json", exclude_unset=True)


class TrustedStatusRegistrations(ReportingSubscriptionResolver, Protocol):
    """Expand only approved account configs into normalized subscriptions.

    Global agent configs require explicit all_authorized_accounts=True before
    expanding into these account rows. get_active rechecks authorization for
    every HTTP attempt; it must withdraw a revoked grant immediately.
    """

    async def put(self, subscription: ReportingNotificationSubscription) -> None: ...


class ReportingSeller(ReportingStatusNotificationHandler):
    """Real overrides appear in tools/list and both MCP/A2A dispatch tables."""

    def __init__(
        self,
        *,
        ledger: PgReportingLedgerStore,
        resolve_caller: ReportingStatusCallerResolver,
        account_configuration: Task,
        revision_content: Task,
        capabilities: dict[str, Any],
        reporting: ReportingDeliveryCapabilities,
        escalation: ReportingDeliveryEscalation | None = None,
    ) -> None:
        super().__init__(
            ReportingStatusHandler(ledger, consumer_status_enabled=True, escalation=escalation),
            resolve_caller=resolve_caller,
        )
        self.ledger = ledger
        self.account_configuration = account_configuration
        self.revision_content = revision_content
        self.capabilities = deepcopy(capabilities)
        self.reporting = reporting.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        if any(
            self.reporting.get(field)
            for field in (
                "managed_delivery",
                "reconciled_billing",
                "readiness_notification",
                "status_notification",
                "ledger_notification",
                "supports_webhook_activity",
            )
        ):
            raise ReportingNotificationError("example_requires_core_status_only")
        self.support: ReportingStatusSupport | None = None

    async def sync_accounts(
        self,
        params: SyncAccountsRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        return await self.account_configuration(request_dict(params), context)

    async def get_media_buy_delivery(
        self,
        params: GetMediaBuyDeliveryRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        return await self.revision_content(request_dict(params), context)

    async def sync_reporting_status(
        self,
        params: SyncReportingStatusRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        request = request_dict(params)
        caller = await self._resolve_status_caller(request, context)
        # The intake observation is captured from PostgreSQL. The participant
        # independently revalidates evidence and timing under its transaction.
        snapshot = await self.ledger.read_status_snapshot(account_id=caller.account_id)
        return await ConsumerStatusIngest(
            self.ledger, enabled=True, clock=lambda: snapshot.as_of
        ).handle(
            request,
            account_id=caller.account_id,
            consumer_id=caller.consumer_id,
        )

    async def get_adcp_capabilities(
        self,
        params: GetAdcpCapabilitiesRequest | dict[str, Any] | None = None,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        response = deepcopy(self.capabilities)
        reporting = deepcopy(self.reporting)
        if self.support is not None:
            reporting.update(await self.support.advertised_notifications())
        response.setdefault("media_buy", {})["reporting_delivery"] = reporting
        await validate_status_claims(response, support=self.support, handler=self)
        return response


async def one_turn(service: ReportingStatusNotificationLifecycle, account_id: str) -> bool:
    dirty = await service.project_dirty_once(account_id=account_id)
    due = await service.sweep_due_once(account_id=account_id)
    expanded = await service.expand_once(account_id=account_id)
    delivered = await service.deliver_once(account_id=account_id)
    return dirty.did_work or due.did_work or expanded or delivered


async def run_status_service(
    *,
    conninfo: str,
    account_ids: Sequence[str],
    build_handler: Callable[[PgReportingLedgerStore], ReportingSeller],
    subscriptions: TrustedStatusRegistrations,
    signing: ReportingSigningResolver,
    readiness_subscriptions: Sequence[ReportingNotificationSubscription],
    cipher: ReportingEnvelopeCipher,
    serve: Callable[[ReportingSeller], Awaitable[None]],
    escalation: ReportingDeliveryEscalation | None = None,
) -> None:
    """Migrate -> baseline ALL accounts -> ready -> dirty -> due -> expand -> HTTP.

    account_ids must enumerate the complete deployment/authenticated capability
    surface. A body account/context/ext is never a readiness selector. The HTTP
    callback mounts this ADCPHandler using the standard MCP/A2A server adapters.
    A supervision failure stops serving; pool closure follows drain/shutdown.
    """
    async with AsyncConnectionPool(conninfo, open=False) as pool:
        await pool.wait(timeout=10)
        ledger = PgReportingLedgerStore(pool=pool, notifications=True)
        store = PgStatusNotificationStore(ledger, escalation=escalation)
        handler = build_handler(ledger)
        worker = ReportingNotificationWorker(
            outbox=store.outbox, subscriptions=subscriptions, signing=signing, cipher=cipher
        )
        support = ReportingStatusSupport(
            store,
            worker,
            ReportingStatusProjector(store),
            ReportingStatusSweeper(store),
            scheduled=True,
            account_ids=tuple(account_ids),
            account_surface_complete=True,
            handler=handler,
            readiness_subscriptions=tuple(readiness_subscriptions),
        )
        handler.support = support
        service: ReportingStatusNotificationLifecycle = ReportingStatusService(support)
        await service.migrate()
        await service.baseline()
        if not await service.ready():
            raise ReportingNotificationError("status_service_unready")
        # Explicit auto-advertisement is enabled only after the whole declared
        # account surface and signing probes are ready, never by model defaults.
        await handler.get_adcp_capabilities()
        stop = asyncio.Event()

        async def work() -> None:
            while not stop.is_set():
                worked = False
                for account_id in account_ids:
                    worked = await one_turn(service, account_id) or worked
                if not worked:
                    try:
                        await asyncio.wait_for(stop.wait(), 1)
                    except asyncio.TimeoutError:
                        pass

        async def serve_http() -> None:
            await serve(handler)

        tasks = [asyncio.create_task(work()), asyncio.create_task(serve_http())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            stop.set()
            tasks[1].cancel()  # Withdraw HTTP before withdrawing lifecycle work.
            try:
                await asyncio.wait_for(asyncio.shield(tasks[0]), 15)
                await service.drain()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await service.aclose()
