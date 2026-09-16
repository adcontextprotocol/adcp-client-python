"""Optional outbox adoption leaves the existing ledger/producer contracts intact."""

from collections.abc import Callable
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg_pool import AsyncConnectionPool

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    InMemoryReportingReconciliationStore,
    PgReportingReconciliationStore,
    ReportingLedgerStore,
    ReportingObligationRecord,
)
from adcp.reporting.outbox import (
    InMemoryReportingOutbox,
    PgReportingOutbox,
    ReportingEnvelopeCipher,
    ReportingNotificationOutbox,
    ReportingNotificationSubscription,
    ReportingNotificationWorker,
    ReportingSigningMaterial,
    ReportingSigningResolver,
    ReportingStatusDirty,
    ReportingStatusScope,
    ReportingSubscriptionResolver,
)


def unchanged_core() -> ReportingLedgerStore:
    return InMemoryReportingLedgerStore()


def reference(
    clock: Callable[[], datetime],
) -> tuple[ReportingLedgerStore, ReportingNotificationOutbox]:
    store = InMemoryReportingReconciliationStore(clock=clock, notifications=True)
    return store, InMemoryReportingOutbox(store)


def durable(pool: AsyncConnectionPool) -> tuple[ReportingLedgerStore, ReportingNotificationOutbox]:
    return PgReportingReconciliationStore(pool=pool, notifications=True), PgReportingOutbox(
        pool=pool
    )


class TrustedConfigurations:
    def __init__(self, subscription: ReportingNotificationSubscription) -> None:
        self.subscription = subscription

    async def list_active(
        self, *, account_id: str, notification_type: str
    ) -> tuple[ReportingNotificationSubscription, ...]:
        value = self.subscription
        return (
            (value,)
            if value.account_id == account_id and notification_type in value.event_types
            else ()
        )

    async def get_active(
        self, *, account_id: str, subscriber_id: str, notification_type: str
    ) -> ReportingNotificationSubscription | None:
        return next(
            (
                value
                for value in await self.list_active(
                    account_id=account_id, notification_type=notification_type
                )
                if value.subscriber_id == subscriber_id
            ),
            None,
        )


class TrustedKeys:
    def __init__(self, keys: dict[tuple[str, str, str], ReportingSigningMaterial]) -> None:
        self.keys = keys

    async def resolve(
        self, *, account_id: str, principal_id: str, signing_scope_id: str
    ) -> ReportingSigningMaterial:
        return self.keys[(account_id, principal_id, signing_scope_id)]


def worker(
    outbox: ReportingNotificationOutbox, key: Ed25519PrivateKey, clock: Callable[[], datetime]
) -> ReportingNotificationWorker:
    configurations: ReportingSubscriptionResolver = TrustedConfigurations(
        ReportingNotificationSubscription(
            account_id="account-a",
            subscriber_id="subscriber-a",
            principal_id="consumer-a",
            url="https://receiver.example.test/notifications",
            event_types=("reporting.ledger_changed",),
            configuration_revision="registration-1",
            authorization_ref="principal-grant-1",
            proof_of_control_ref="account-challenge-1",
            signing_scope_id="seller-keyring",
            active=True,
            authorized=True,
            proof_valid=True,
        )
    )
    keys: ReportingSigningResolver = TrustedKeys(
        {
            ("account-a", "consumer-a", "seller-keyring"): ReportingSigningMaterial(
                key, "key-1", "ed25519", frozenset({"ed25519"})
            ),
        }
    )
    return ReportingNotificationWorker(
        outbox=outbox,
        subscriptions=configurations,
        signing=keys,
        cipher=ReportingEnvelopeCipher(b"t" * 32),
        clock=clock,
    )


async def typed_issue_handoff(
    store: PgReportingReconciliationStore | InMemoryReportingReconciliationStore,
    obligation: ReportingObligationRecord,
    at: datetime,
) -> None:
    await store.ensure_issue_opened(
        issue_key="opaque-condition-reference",
        account_id=obligation.account_id,
        consumer_id="consumer-a",
        observed_at=at,
        status_scope=ReportingStatusScope.for_obligation(obligation, "consumer-a"),
    )


async def projector_checkpoint(
    outbox: ReportingNotificationOutbox,
) -> tuple[ReportingStatusDirty, ...]:
    previous = await outbox.status_checkpoint(
        account_id="account-a", projector_id="later-projector"
    )
    records = await outbox.read_status_dirty(account_id="account-a", after=previous, limit=100)
    if records:
        await outbox.advance_status_checkpoint(
            account_id="account-a",
            projector_id="later-projector",
            expected=previous,
            through=records[-1].sequence,
        )
    return records
