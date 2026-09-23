"""Stateless turns over separately durable expansion and HTTP delivery phases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

from adcp.reporting.ledger.notification_models import (
    ReportingNotificationError,
    decode_event,
)
from adcp.reporting.outbox._transport_logging import protected_transport_logs
from adcp.reporting.outbox.models import (
    DeliveryLease,
    ErrorCode,
    ReportingNotificationOutbox,
    WorkState,
)
from adcp.reporting.outbox.routing import (
    OpenedReportingDelivery,
    ReportingEnvelopeCipher,
    ReportingNotificationSubscription,
    ReportingSigningMaterial,
    ReportingSigningResolver,
    ReportingSubscriptionResolver,
)
from adcp.signing.jwks import SSRFValidationError
from adcp.webhook_sender import (
    PreparedWebhookAttemptExpiredError,
    ScopePermanentlyUnknown,
    ScopeTransientlyUnavailable,
    WebhookSender,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.delivery_models import ReportingDeliveryScope
    from adcp.reporting.ledger.store import ReportingLedgerStore


@dataclass(frozen=True)
class _Outcome:
    state: WorkState
    error: ErrorCode | None = None


class ReportingNotificationWorker:
    """At-least-once delivery with immutable bytes and per-attempt signing.

    A worker has no general-purpose sender injection. It constructs the exact
    SDK sender from trusted current key material, with the SDK-owned IP-pinned
    transport, public HTTPS/443 only, no rewrite hooks, and no redirects.

    Cancellation/process death intentionally leaves the lease to expire. Claims
    never exhaust a retry budget. A receiver must dedupe by authenticated sender
    and idempotency key: HTTP acceptance and our ACK cannot be one transaction.
    """

    def __init__(
        self,
        *,
        outbox: ReportingNotificationOutbox,
        subscriptions: ReportingSubscriptionResolver,
        cipher: ReportingEnvelopeCipher,
        signing: ReportingSigningResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: float = 60,
        retry_seconds: float = 5,
    ) -> None:
        if lease_seconds < 1 or retry_seconds <= 0:
            raise ValueError("positive retry and at least one second of lease are required")
        self.outbox, self.subscriptions, self.cipher, self.signing = (
            outbox,
            subscriptions,
            cipher,
            signing,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds, self.retry_seconds = lease_seconds, retry_seconds

    async def advertised_notifications(
        self,
        ledger: ReportingLedgerStore,
        *,
        account_id: str,
        ready_scope: ReportingDeliveryScope | None = None,
    ) -> dict[str, str | bool]:
        """Check the installed chain at startup before publishing these fields.

        Merge the result into the producer's capability block while this worker
        is scheduled. Ready support additionally requires a retained Managed
        scope. No status or activity projection is claimed by this slice.
        """
        from adcp.reporting.outbox._capabilities import advertised_notifications

        return await advertised_notifications(
            self, ledger, account_id=account_id, ready_scope=ready_scope
        )

    async def expand_one(self, *, account_id: str) -> bool:
        lease = await self.outbox.claim_expansion(
            account_id=account_id, now=self._clock(), lease_seconds=self.lease_seconds
        )
        if lease is None:
            return False
        try:
            event = decode_event(lease.event)
            if (
                event.account_id != lease.account_id
                or event.notification_id != lease.notification_id
                or event.consumer_namespace != lease.consumer_namespace
            ):
                raise ReportingNotificationError("invalid_event")
        except Exception:
            await self.outbox.finish_expansion(
                lease, now=self._clock(), state="quarantined", error_code="invalid_payload"
            )
            return True

        try:
            # One external snapshot. Its membership is fixed only when the
            # subsequent all-N insertion + complete checkpoint commits.
            resolved = await asyncio.wait_for(
                self.subscriptions.list_active(
                    account_id=account_id, notification_type=event.notification_type
                ),
                timeout=self.lease_seconds * 0.8,
            )
        except Exception:
            now = self._clock()
            await self.outbox.finish_expansion(
                lease,
                now=now,
                state="pending",
                error_code="subscription_unavailable",
                retry_at=now + timedelta(seconds=self.retry_seconds),
            )
            return True

        try:
            if not isinstance(resolved, (list, tuple)):
                raise ReportingNotificationError()
            snapshot = tuple(resolved)
            subscribers: set[str] = set()
            for subscription in snapshot:
                if (
                    type(subscription) is not ReportingNotificationSubscription
                    or subscription.account_id != account_id
                    or subscription.subscriber_id in subscribers
                ):
                    raise ReportingNotificationError()
                # Reconstruct the closed normalized type rather than trusting
                # an overridden fingerprint/matches property on a subclass.
                ReportingNotificationSubscription.__post_init__(subscription)
                subscribers.add(subscription.subscriber_id)
            deliveries = tuple(
                self.cipher.prepare(event, subscription, lease.emission_generation)
                for subscription in snapshot
                if subscription.matches(event)
            )
        except (ValueError, TypeError, AttributeError):
            await self.outbox.finish_expansion(
                lease, now=self._clock(), state="quarantined", error_code="invalid_configuration"
            )
            return True
        # Empty valid membership completes too. Failure here rolls back the
        # entire snapshot. A restarted worker may then observe new membership;
        # it can never combine committed rows from two snapshots.
        await self.outbox.complete_expansion(lease, deliveries, now=self._clock())
        return True

    async def deliver_one(self, *, account_id: str) -> bool:
        lease = await self.outbox.claim_delivery(
            account_id=account_id, now=self._clock(), lease_seconds=self.lease_seconds
        )
        if lease is None:
            return False
        try:
            opened = self.cipher.open(lease.delivery)
        except (ReportingNotificationError, ValueError, TypeError):
            outcome = _Outcome("quarantined", "integrity_failure")
        else:
            try:
                outcome = await asyncio.wait_for(
                    self._attempt(lease, opened), timeout=self.lease_seconds * 0.8
                )
            except (TimeoutError, asyncio.TimeoutError):
                outcome = _Outcome("pending", "network")
        now = self._clock()
        # A DB failure after HTTP acceptance is intentionally not converted to
        # success. Expiry/restart retries these exact protected body bytes/key.
        await self.outbox.finish_delivery(
            lease,
            now=now,
            state=outcome.state,
            error_code=outcome.error,
            retry_at=(
                now + timedelta(seconds=self.retry_seconds) if outcome.state == "pending" else None
            ),
        )
        return True

    async def _attempt(self, lease: DeliveryLease, opened: OpenedReportingDelivery) -> _Outcome:
        binding = lease.delivery.binding
        try:
            current = await self.subscriptions.get_active(
                account_id=binding.account_id,
                subscriber_id=binding.subscriber_id,
                notification_type=binding.notification_type,
            )
        except Exception:
            return _Outcome("pending", "subscription_unavailable")
        if type(current) is ReportingNotificationSubscription:
            try:
                ReportingNotificationSubscription.__post_init__(current)
            except (ValueError, TypeError, AttributeError):
                return _Outcome("suppressed", "subscription_changed")
        if (
            type(current) is not ReportingNotificationSubscription
            or not current.matches(opened.event)
            or current.subscriber_id != binding.subscriber_id
            or current.fingerprint != binding.subscription_fingerprint
        ):
            return _Outcome("suppressed", "subscription_changed")
        try:
            sender = await self._sender(opened.subscription)
        except ScopePermanentlyUnknown:
            return _Outcome("quarantined", "permanent_scope")
        except ScopeTransientlyUnavailable:
            return _Outcome("pending", "signing_unavailable")
        try:
            if not await self.outbox.delivery_lease_current(lease, now=self._clock()):
                return _Outcome("pending", "lease_expired")
            try:
                # Invoke the concrete SDK seam. Adopter-provided sender objects,
                # subclasses, overridden send methods, clients and hooks never
                # cross this boundary. URL/DNS validation is inside this seam.
                async def current_fence() -> bool:
                    return await self.outbox.delivery_lease_current(lease, now=self._clock())

                with protected_transport_logs():
                    result = await WebhookSender.send_prepared(
                        sender, opened.prepared, before_attempt=current_fence
                    )
            except PreparedWebhookAttemptExpiredError:
                return _Outcome("pending", "lease_expired")
            except SSRFValidationError as error:
                return (
                    _Outcome("pending", "network")
                    if error.transient
                    else (_Outcome("quarantined", "invalid_configuration"))
                )
            except (httpx.TransportError, OSError, TimeoutError):
                return _Outcome("pending", "network")
            except (ValueError, TypeError):
                return _Outcome("quarantined", "invalid_payload")
            except Exception:
                # Signing backends can fail transiently; retain no exception
                # prose, traceback, headers, URL, or response/provider body.
                return _Outcome("pending", "signing_unavailable")
            if result.ok:
                return _Outcome("complete")
            if result.status_code in {408, 425, 429} or 500 <= result.status_code < 600:
                return _Outcome("pending", "retryable_http")
            return _Outcome("quarantined", "permanent_http")
        finally:
            await sender.aclose()

    async def _sender(self, subscription: ReportingNotificationSubscription) -> WebhookSender:
        timeout = min(10.0, self.lease_seconds * 0.5)
        if subscription.authentication is not None:
            auth = subscription.authentication
            if subscription.signing_scope_id is not None:
                raise ScopePermanentlyUnknown from None
            if auth.scheme == "Bearer":
                return WebhookSender.from_bearer_token(
                    auth.credentials,
                    timeout_seconds=timeout,
                    allowed_destination_ports=frozenset({443}),
                )
            return WebhookSender.from_adcp_legacy_hmac(
                auth.credentials.encode("utf-8"),
                key_id="reporting-registration",
                timeout_seconds=timeout,
                allowed_destination_ports=frozenset({443}),
            )
        if self.signing is None or subscription.signing_scope_id is None:
            raise ScopePermanentlyUnknown from None
        try:
            material = await self.signing.resolve(
                account_id=subscription.account_id,
                principal_id=subscription.principal_id,
                signing_scope_id=subscription.signing_scope_id,
            )
        except (ScopePermanentlyUnknown, ScopeTransientlyUnavailable):
            raise
        except Exception:
            raise ScopeTransientlyUnavailable from None
        if type(material) is not ReportingSigningMaterial:
            raise ScopePermanentlyUnknown from None
        try:
            ReportingSigningMaterial.__post_init__(material)
            return WebhookSender(
                private_key=material.private_key,
                key_id=material.key_id,
                alg=material.algorithm,
                timeout_seconds=timeout,
                allowed_destination_ports=frozenset({443}),
            )
        except (ValueError, TypeError, AttributeError):
            raise ScopePermanentlyUnknown from None
