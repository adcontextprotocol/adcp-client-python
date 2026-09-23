"""Typed #1168B mounting/lifecycle example; install ``adcp[pg]``.

Call ``register_approved_subscription`` after account authorization and endpoint
proof verification. ``run_reporting_service`` accepts the adopter's existing
platform, trusted configuration/key stores, and async HTTP service entrypoint.
It starts both worker phases and stops the service if the worker exits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from adcp.decisioning import (
    DecisioningPlatform,
    create_adcp_server_from_platform,
    validate_capabilities_response_shape_async,
)
from adcp.decisioning.accounts import ResolveContext
from adcp.decisioning.handler import PlatformHandler
from adcp.reporting.ledger import PgReportingLedgerStore
from adcp.reporting.outbox import (
    PgReportingOutbox,
    ReportingActivityProjector,
    ReportingActivitySupport,
    ReportingEnvelopeCipher,
    ReportingNotificationSubscription,
    ReportingNotificationWorker,
    ReportingSigningResolver,
    ReportingSubscriptionResolver,
    resolve_reporting_consumer,
)


class TrustedRegistrations(ReportingSubscriptionResolver, Protocol):
    """Adopter-owned durable, access-controlled subscription configuration."""

    async def put(self, subscription: ReportingNotificationSubscription) -> None: ...


async def register_approved_subscription(
    *,
    context: ResolveContext,
    registrations: TrustedRegistrations,
    account_id: str,
    subscriber_id: str,
    url: str,
    configuration_revision: str,
    authorization_ref: str,
    proof_of_control_ref: str,
    signing_scope_id: str,
) -> None:
    # All present registry, flat auth, and signed credential identities must
    # agree. The request body and account reference cannot supply this value.
    principal = resolve_reporting_consumer(auth_info=context.auth_info, agent=context.agent)
    await registrations.put(
        ReportingNotificationSubscription(
            account_id=account_id,
            subscriber_id=subscriber_id,
            principal_id=principal,
            url=url,
            event_types=("reporting.ledger_changed",),
            configuration_revision=configuration_revision,
            authorization_ref=authorization_ref,
            proof_of_control_ref=proof_of_control_ref,
            signing_scope_id=signing_scope_id,
            active=True,
            authorized=True,
            proof_valid=True,
        )
    )


async def _work(
    worker: ReportingNotificationWorker, accounts: Sequence[str], stop: asyncio.Event
) -> None:
    while not stop.is_set():
        worked = False
        for account_id in accounts:
            if stop.is_set():
                break
            worked = await worker.expand_one(account_id=account_id) or worked
            if not stop.is_set():
                worked = await worker.deliver_one(account_id=account_id) or worked
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                # The poll interval elapsed; check for work or shutdown on the next turn.
                pass


async def run_reporting_service(
    *,
    platform: DecisioningPlatform,
    serve: Callable[[PlatformHandler, PgReportingLedgerStore], Awaitable[None]],
    conninfo: str,
    account_ids: Sequence[str],
    subscriptions: ReportingSubscriptionResolver,
    signing: ReportingSigningResolver,
    cipher: ReportingEnvelopeCipher,
) -> None:
    """Own the pool and worker until the HTTP service stops or the worker fails.

    The existing platform declares its normal reporting capabilities. It may
    set reporting ``supports_webhook_activity=True`` with this mounting. An
    existing account notification declaration may additionally set its own
    flag; this example does not create an account status emitter. The service
    callback receives the ledger to mount on its reporting producer.

    The platform declares its RFC 9421 webhook signing and retry horizon with
    ``webhook_signing_managed_externally=True``; this service owns delivery.
    """
    async with AsyncConnectionPool(conninfo, open=False) as pool:
        await pool.wait(timeout=10)
        ledger = PgReportingLedgerStore(pool=pool, notifications=True)
        # All six packaged steps are atomic/repeatable. Migrate before serving.
        await ledger.create_schema()
        outbox = PgReportingOutbox(pool=pool)
        worker = ReportingNotificationWorker(
            outbox=outbox,
            activity=outbox,
            subscriptions=subscriptions,
            signing=signing,
            cipher=cipher,
        )
        projector = ReportingActivityProjector(outbox)
        support = ReportingActivitySupport(worker, ledger, projector)
        if not await support.durable():
            raise RuntimeError("reporting_activity_unready")
        handler, executor, _ = create_adcp_server_from_platform(
            platform,
            account_activity=projector,
            reporting_activity=support,
            auto_emit_task_webhooks=False,
            validate_at_init=False,
        )
        stop = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []
        try:
            # Resolves readiness on this event loop; no shared capability model
            # is mutated. Contradictory static/request overrides fail closed.
            await validate_capabilities_response_shape_async(handler)

            async def serve_http() -> None:
                await serve(handler, ledger)

            worker_task = asyncio.create_task(_work(worker, account_ids, stop))
            http_task = asyncio.create_task(serve_http())
            tasks.extend((worker_task, http_task))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            stop.set()  # Stop new claims; let an in-flight attempt finish first.
            try:
                if tasks:
                    tasks[-1].cancel()  # Stop serving before withdrawing the worker.
                    try:
                        await asyncio.wait_for(asyncio.shield(tasks[0]), timeout=10)
                    except asyncio.TimeoutError:
                        tasks[0].cancel()  # Uncertain activity remains pending.
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                executor.shutdown(wait=True)
        # The pool closes only after worker HTTP/transactions have drained.
