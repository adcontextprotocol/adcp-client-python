"""Optional owned notification delivery, independent of complete polling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.memory import InMemoryReportingOutbox
from adcp.reporting.outbox.routing import (
    ReportingEnvelopeCipher,
    ReportingNotificationSubscription,
    ReportingSigningMaterial,
    ReportingSigningResolver,
    ReportingSubscriptionResolver,
)
from adcp.reporting.outbox.worker import ReportingNotificationWorker
from adcp.reporting.production.delivery_window import (
    RETRY_HORIZON_SECONDS,
    ProductionDeliveryWindow,
)
from adcp.reporting.production.memory import (
    InMemoryReportingProductionOutbox,
    InMemoryReportingProductionStore,
)
from adcp.reporting.projection.memory import InMemoryReportingStatusProjection
from adcp.signing.crypto import ALLOWED_ALGS

if TYPE_CHECKING:
    from adcp.reporting.production.pg import PgReportingProductionStore
    from adcp.reporting.production.service import ReportingProductionSupport
    from adcp.reporting.projection.pg import PgReportingStatusProjection


@dataclass(frozen=True)
class ReportingProductionSigning:
    """A declared RFC 9421 algorithm contract checked on every resolved key.

    Key material remains in the trusted resolver. The public declaration and
    actual sender share this immutable contract; rotation cannot switch to an
    unadvertised algorithm or a legacy authentication path.
    """

    resolver: ReportingSigningResolver = field(repr=False)
    algorithms: tuple[str, ...]
    brand_json_url: str = field(kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "algorithms", tuple(self.algorithms))
        if (
            not self.algorithms
            or len(set(self.algorithms)) != len(self.algorithms)
            or not set(self.algorithms) <= ALLOWED_ALGS
            or not callable(getattr(self.resolver, "resolve", None))
        ):
            raise ReportingNotificationError("notification_signing_unready")
        try:
            if type(self.brand_json_url) is not str:
                raise ValueError
            identity = httpx.URL(self.brand_json_url)
            valid = (
                identity.scheme == "https"
                and bool(identity.host)
                and not identity.userinfo
                and not identity.query
                and not identity.fragment
                and identity.port in (None, 443)
            )
        except (TypeError, ValueError, httpx.InvalidURL):
            valid = False
        if not valid:
            raise ReportingNotificationError("notification_signing_unready")

    async def resolve(
        self, *, account_id: str, principal_id: str, signing_scope_id: str
    ) -> ReportingSigningMaterial:
        material = await self.resolver.resolve(
            account_id=account_id, principal_id=principal_id, signing_scope_id=signing_scope_id
        )
        if type(
            material
        ) is not ReportingSigningMaterial or material.advertised_algorithms != frozenset(
            self.algorithms
        ):
            raise ReportingNotificationError("notification_signing_unready")
        ReportingSigningMaterial.__post_init__(material)
        return material

    def wire(self) -> dict[str, Any]:
        return {
            "supported": True,
            "profile": "adcp/webhook-signing/v1",
            "algorithms": list(self.algorithms),
            "legacy_hmac_fallback": False,
            "delivery_retry_horizon_seconds": RETRY_HORIZON_SECONDS,
        }


@dataclass(frozen=True)
class _SignedSubscriptions:
    resolver: ReportingSubscriptionResolver = field(repr=False)

    @staticmethod
    def _check(value: ReportingNotificationSubscription) -> ReportingNotificationSubscription:
        if type(value) is not ReportingNotificationSubscription or value.authentication is not None:
            raise ReportingNotificationError("notification_signing_unready")
        return value

    async def list_active(
        self, *, account_id: str, notification_type: str
    ) -> tuple[ReportingNotificationSubscription, ...]:
        values = await self.resolver.list_active(
            account_id=account_id, notification_type=notification_type
        )
        return tuple(self._check(v) for v in values)

    async def get_active(
        self, *, account_id: str, subscriber_id: str, notification_type: str
    ) -> ReportingNotificationSubscription | None:
        value = await self.resolver.get_active(
            account_id=account_id, subscriber_id=subscriber_id, notification_type=notification_type
        )
        return self._check(value) if value is not None else None


def production_notification_workers(
    store: InMemoryReportingProductionStore | PgReportingProductionStore,
    projection: InMemoryReportingStatusProjection | PgReportingStatusProjection,
    *,
    subscriptions: ReportingSubscriptionResolver,
    cipher: ReportingEnvelopeCipher,
    signing: ReportingProductionSigning,
) -> tuple[ReportingNotificationWorker, ...]:
    """Build all three real SDK queue workers for ``ReportingProductionSupport``.

    The support schedules these workers itself. Omitting them keeps polling
    and enabled atomic logical enqueue available, without advertising push.
    Retained epoch-zero readiness queues are never among these participants.
    """
    from adcp.reporting.outbox.pg import PgReportingOutbox
    from adcp.reporting.production.pg import PgReportingProductionOutbox, PgReportingProductionStore

    if (
        projection.ledger is not store
        or not projection.policy["notifications_enabled"]
        or type(signing) is not ReportingProductionSigning
    ):
        raise ReportingNotificationError("notification_chain_unready")
    ReportingProductionSigning.__post_init__(signing)
    signed_subscriptions = _SignedSubscriptions(subscriptions)
    outboxes: tuple[Any, ...]
    if type(store) is InMemoryReportingProductionStore:
        outboxes = (
            InMemoryReportingOutbox(store),
            projection.outbox,
            InMemoryReportingProductionOutbox(store),
        )
    elif type(store) is PgReportingProductionStore:
        outboxes = (
            PgReportingOutbox(pool=store._pool, clock=store._clock),
            projection.outbox,
            PgReportingProductionOutbox(pool=store._pool, clock=store._clock),
        )
    else:
        raise ReportingNotificationError("notification_chain_unready")
    return tuple(
        ReportingNotificationWorker(
            outbox=outbox,
            subscriptions=signed_subscriptions,
            cipher=cipher,
            signing=signing,
            activity=outbox,
            clock=store._clock,
            delivery_window=ProductionDeliveryWindow(store, queue),
        )
        for outbox, queue in zip(outboxes, ("core", "status", "ready"))
    )


def worker_identity(worker: ReportingNotificationWorker) -> tuple[int, ...]:
    return tuple(
        id(component)
        for component in (
            worker,
            worker.outbox,
            worker.subscriptions,
            worker.cipher,
            worker.signing,
            worker.activity,
            worker.delivery_window,
            getattr(worker.signing, "resolver", None),
            getattr(worker.signing, "algorithms", None),
            getattr(worker.signing, "brand_json_url", None),
            getattr(worker.subscriptions, "resolver", None),
        )
    )


def check_workers(support: ReportingProductionSupport) -> None:
    workers = support.notification_workers
    if not workers:
        return
    if (
        type(workers) is not tuple
        or len(workers) != 3
        or not support.notifications_enabled
        or any(type(w) is not ReportingNotificationWorker for w in workers)
        or any(type(w.cipher) is not ReportingEnvelopeCipher for w in workers)
        or any(type(w.signing) is not ReportingProductionSigning for w in workers)
        or any(type(w.subscriptions) is not _SignedSubscriptions for w in workers)
        or any(type(w.delivery_window) is not ProductionDeliveryWindow for w in workers)
        or any(id(w.activity) != id(w.outbox) for w in workers)
        or any(
            w.subscriptions is not workers[0].subscriptions
            or w.signing is not workers[0].signing
            or w.cipher is not workers[0].cipher
            for w in workers
        )
        or workers[1].outbox is not support.projection.outbox
        or tuple(worker_identity(w) for w in workers) != support._notification_identity
    ):
        raise ReportingNotificationError("notification_chain_unready")
    expected: tuple[type[Any], ...]
    if isinstance(support.store, InMemoryReportingProductionStore):
        from adcp.reporting.projection.memory import InMemoryReportingProjectionOutbox

        expected = (
            InMemoryReportingOutbox,
            InMemoryReportingProjectionOutbox,
            InMemoryReportingProductionOutbox,
        )
        linked = all(getattr(w.outbox, "_store", None) is support.store for w in workers)
    else:
        from adcp.reporting.outbox.pg import PgReportingOutbox
        from adcp.reporting.production.pg import PgReportingProductionOutbox
        from adcp.reporting.projection.notifications import PgReportingProjectionOutbox

        expected = (PgReportingOutbox, PgReportingProjectionOutbox, PgReportingProductionOutbox)
        linked = all(getattr(w.outbox, "_pool", None) is support.store._pool for w in workers)
    if not linked or tuple(type(w.outbox) for w in workers) != expected:
        raise ReportingNotificationError("notification_chain_unready")
    for worker, queue in zip(workers, ("core", "status", "ready")):
        window = worker.delivery_window
        if (
            not isinstance(window, ProductionDeliveryWindow)
            or window.store is not support.store
            or window.queue != queue
        ):
            raise ReportingNotificationError("notification_chain_unready")
    signing = workers[0].signing
    assert isinstance(signing, ReportingProductionSigning)
    ReportingProductionSigning.__post_init__(signing)
    subscriptions = workers[0].subscriptions
    assert isinstance(subscriptions, _SignedSubscriptions)
    if not all(
        callable(getattr(subscriptions.resolver, method, None))
        for method in ("list_active", "get_active")
    ):
        raise ReportingNotificationError("notification_chain_unready")


def signing_capabilities(support: ReportingProductionSupport) -> dict[str, Any]:
    check_workers(support)
    signing = support.notification_workers[0].signing
    assert isinstance(signing, ReportingProductionSigning)
    return signing.wire()


def signing_identity(support: ReportingProductionSupport) -> dict[str, str]:
    check_workers(support)
    signing = support.notification_workers[0].signing
    assert isinstance(signing, ReportingProductionSigning)
    return {"brand_json_url": signing.brand_json_url}


async def check_account_notifications(support: ReportingProductionSupport, account_id: str) -> None:
    """Resolve trusted registrations/signing before admitting this account.

    This check does not replace dispatch's authorization/revocation checks.
    A supported empty seller needs no invented account to advertise discovery.
    """
    import asyncio

    check_workers(support)
    if not support.notification_workers:
        return
    worker = support.notification_workers[0]
    for event_type in (
        "reporting.ledger_changed",
        "reporting.status_changed",
        "reporting.delivery_ready",
    ):
        subscriptions = await asyncio.wait_for(
            worker.subscriptions.list_active(account_id=account_id, notification_type=event_type),
            timeout=worker.lease_seconds * 0.8,
        )
        if not isinstance(subscriptions, (tuple, list)):
            raise ReportingNotificationError("notification_chain_unready")
        identifiers = set()
        for subscription in subscriptions:
            if (
                type(subscription) is not ReportingNotificationSubscription
                or subscription.account_id != account_id
                or subscription.subscriber_id in identifiers
                or event_type not in subscription.event_types
                or not (
                    subscription.active and subscription.authorized and subscription.proof_valid
                )
            ):
                raise ReportingNotificationError("notification_chain_unready")
            ReportingNotificationSubscription.__post_init__(subscription)
            identifiers.add(subscription.subscriber_id)
            sender = await asyncio.wait_for(
                worker._sender(subscription), timeout=worker.lease_seconds * 0.8
            )
            await sender.aclose()
    check_workers(support)


async def next_account(worker: ReportingNotificationWorker, *, delivery: bool) -> str | None:
    """Sample one indexed due queue row; never enumerate adopter accounts."""
    from adcp.reporting.outbox.pg import PgReportingOutbox, database_now

    outbox = worker.outbox
    if isinstance(outbox, InMemoryReportingOutbox):
        now = worker._clock()
        async with outbox._store._lock:
            pending = (
                ((key[0], work) for key, (_, work) in outbox._state.deliveries.items())
                if delivery
                else ((key[0], work) for key, work in outbox._state.expansions.items())
            )
            values = [(work.due_at, account) for account, work in pending if work.available(now)]
            return min(values)[1] if values else None
    if not isinstance(outbox, PgReportingOutbox):
        raise ReportingNotificationError("notification_chain_unready")
    # Both identifiers are closed SDK literals. The concrete queue adapter
    # selects the matching isolated table on this same actual connection.
    table = "reporting_notification_deliveries" if delivery else "reporting_notification_expansions"
    async with outbox._connection() as connection:
        now = await database_now(connection, outbox._clock)
        row = await (
            await connection.execute(
                f"SELECT account_id FROM {table}"  # nosec B608
                " WHERE due_at<=%s AND (state='pending' OR"
                " (state='leased' AND lease_expires_at<=%s))"
                " ORDER BY due_at,account_id LIMIT 1",
                (now, now),
            )
        ).fetchone()
        return str(row[0]) if row is not None else None


async def notification_turn(support: ReportingProductionSupport) -> None:
    for worker in support.notification_workers:
        for delivery in (False, True):
            support._assert_components()
            account_id = await next_account(worker, delivery=delivery)
            if account_id is not None:
                support._assert_components()
                operation = worker.deliver_one if delivery else worker.expand_one
                await operation(account_id=account_id)
