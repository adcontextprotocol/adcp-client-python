"""Optional owned notification delivery, independent of complete polling."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from adcp.reporting.ledger.notification_models import ReportingNotificationError
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
from adcp.reporting.production.store import (
    ReportingProductionProjection,
    ReportingProductionStore,
)
from adcp.signing.crypto import ALG_ED25519, ALLOWED_ALGS
from adcp.webhooks import WEBHOOK_SIGNING_PROFILE_ADCP, WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from adcp.reporting.production.service import ReportingProductionSupport


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
    #: Also sign subscriptions that select ``web-bot-auth`` (AdCP 3.3). Every
    #: resolved material must then carry ``web_bot_auth``, ``ed25519`` must be
    #: advertised, and the capability lists both profiles.
    web_bot_auth: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "algorithms", tuple(self.algorithms))
        if (
            not self.algorithms
            or len(set(self.algorithms)) != len(self.algorithms)
            or not set(self.algorithms) <= ALLOWED_ALGS
            or not callable(getattr(self.resolver, "resolve", None))
            or type(self.web_bot_auth) is not bool
            or (self.web_bot_auth and ALG_ED25519 not in self.algorithms)
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
        if self.web_bot_auth and material.web_bot_auth is None:
            raise ReportingNotificationError("notification_signing_unready")
        ReportingSigningMaterial.__post_init__(material)
        return material

    def wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "supported": True,
            "profile": WEBHOOK_SIGNING_PROFILE_ADCP,
            "algorithms": list(self.algorithms),
            "legacy_hmac_fallback": False,
            "delivery_retry_horizon_seconds": RETRY_HORIZON_SECONDS,
        }
        if self.web_bot_auth:
            wire["profiles"] = [WEBHOOK_SIGNING_PROFILE_ADCP, WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH]
        return wire


@dataclass(frozen=True)
class _SignedSubscriptions:
    resolver: ReportingSubscriptionResolver = field(repr=False)
    web_bot_auth: bool = False

    @staticmethod
    def _check(value: ReportingNotificationSubscription) -> ReportingNotificationSubscription:
        if type(value) is not ReportingNotificationSubscription or value.authentication is not None:
            raise ReportingNotificationError("notification_signing_unready")
        return value

    def _signable(self, value: ReportingNotificationSubscription) -> bool:
        """Whether this seller signs the profile the subscription selected.

        A ``web-bot-auth`` subscription without ``web_bot_auth`` signing is
        skipped, not raised: one such subscriber must not stop deliveries to
        every other subscriber of the account. The registration service
        should refuse it, since the capability does not list the profile.
        """
        if value.signing_profile == WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH and not self.web_bot_auth:
            logger.warning(
                "[adcp.reporting] skipping a web-bot-auth subscription: production "
                "signing is not configured with web_bot_auth=True"
            )
            return False
        return True

    async def list_active(
        self, *, account_id: str, notification_type: str
    ) -> tuple[ReportingNotificationSubscription, ...]:
        values = await self.resolver.list_active(
            account_id=account_id, notification_type=notification_type
        )
        return tuple(v for v in map(self._check, values) if self._signable(v))

    async def get_active(
        self, *, account_id: str, subscriber_id: str, notification_type: str
    ) -> ReportingNotificationSubscription | None:
        value = await self.resolver.get_active(
            account_id=account_id, subscriber_id=subscriber_id, notification_type=notification_type
        )
        if value is None:
            return None
        checked = self._check(value)
        return checked if self._signable(checked) else None


def production_notification_workers(
    store: ReportingProductionStore,
    projection: ReportingProductionProjection,
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
    if (
        not isinstance(store, ReportingProductionStore)
        or projection.ledger is not store
        or not projection.policy["notifications_enabled"]
        or type(signing) is not ReportingProductionSigning
    ):
        raise ReportingNotificationError("notification_chain_unready")
    ReportingProductionSigning.__post_init__(signing)
    signed_subscriptions = _SignedSubscriptions(subscriptions, web_bot_auth=signing.web_bot_auth)
    # The store builds its own exact queue classes; a subclass is refused there.
    outboxes = store.production_outboxes(projection)
    return tuple(
        ReportingNotificationWorker(
            outbox=outbox,
            subscriptions=signed_subscriptions,
            cipher=cipher,
            signing=signing,
            activity=outbox,
            clock=store.clock,
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
    if not support.store.notification_workers_linked(workers):
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
    if subscriptions.web_bot_auth is not signing.web_bot_auth or not all(
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


@runtime_checkable
class _DueAccountQueue(Protocol):
    async def next_due_account(self, *, delivery: bool, now: datetime) -> str | None: ...


async def next_account(worker: ReportingNotificationWorker, *, delivery: bool) -> str | None:
    """Sample one indexed due queue row; never enumerate adopter accounts."""
    outbox = worker.outbox
    if not isinstance(outbox, _DueAccountQueue):
        raise ReportingNotificationError("notification_chain_unready")
    return await outbox.next_due_account(delivery=delivery, now=worker._clock())


async def notification_turn(support: ReportingProductionSupport) -> None:
    for worker in support.notification_workers:
        for delivery in (False, True):
            support._assert_components()
            account_id = await next_account(worker, delivery=delivery)
            if account_id is not None:
                support._assert_components()
                operation = worker.deliver_one if delivery else worker.expand_one
                await operation(account_id=account_id)
