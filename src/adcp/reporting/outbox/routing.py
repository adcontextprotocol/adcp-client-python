"""Trusted account registrations and authenticated, secret-free routing columns."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import principal_reference, reporting_identifier
from adcp.reporting.ledger.notification_models import (
    ReportingDomainEvent,
    ReportingNotificationError,
    decode_event,
    event_storage,
    validate_notification_payload,
)
from adcp.reporting.outbox.identity import canonical_consumer
from adcp.reporting.outbox.models import DeliveryBinding, StoredDelivery
from adcp.signing.crypto import ALG_ED25519, ALG_ES256, ALLOWED_ALGS, PrivateKey
from adcp.webhook_sender import PreparedWebhook


@dataclass(frozen=True)
class ReportingLegacyAuthentication:
    scheme: Literal["Bearer", "HMAC-SHA256"]
    credentials: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.scheme not in {"Bearer", "HMAC-SHA256"}
            or type(self.credentials) is not str
            or not self.credentials
            or not self.credentials.isprintable()
        ):
            raise ReportingNotificationError("invalid_configuration")


@dataclass(frozen=True)
class ReportingNotificationSubscription:
    """One *trusted* normalized active account notification configuration.

    The resolver must read these fields together, from authorized principal
    state and a successful account/subscriber/URL proof-of-control registration.
    References are mandatory and part of the fingerprint; they are not a
    substitute for performing those checks in the registration service.
    Never construct this from a webhook body or an unauthenticated sync request.
    """

    account_id: str
    subscriber_id: str
    principal_id: str
    url: str = field(repr=False)
    event_types: tuple[str, ...]
    configuration_revision: str
    authorization_ref: str
    proof_of_control_ref: str
    signing_scope_id: str | None = None
    authentication: ReportingLegacyAuthentication | None = field(default=None, repr=False)
    active: bool = field(kw_only=True)
    authorized: bool = field(kw_only=True)
    proof_valid: bool = field(kw_only=True)

    def __post_init__(self) -> None:
        try:
            principal_reference(self.account_id)
            canonical_consumer(self.principal_id)
            reporting_identifier(self.subscriber_id, maximum=64)
            for value in (
                self.configuration_revision,
                self.authorization_ref,
                self.proof_of_control_ref,
            ):
                reporting_identifier(value, maximum=255)
            if self.signing_scope_id is not None:
                principal_reference(self.signing_scope_id)
            if any(
                type(value) is not bool
                for value in (self.active, self.authorized, self.proof_valid)
            ):
                raise ValueError
            events = tuple(sorted(self.event_types))
            if not events or len(set(events)) != len(events):
                raise ValueError
            for event in events:
                reporting_identifier(event, maximum=255)
            object.__setattr__(self, "event_types", events)
            if (self.authentication is None) == (self.signing_scope_id is None):
                raise ValueError
            if (
                self.authentication is not None
                and type(self.authentication) is not ReportingLegacyAuthentication
            ):
                raise ValueError
            if type(self.url) is not str or not self.url.isprintable() or len(self.url) > 8192:
                raise ValueError
            parsed = httpx.URL(self.url)
            # Percent-encoding expands the canonical form, so the bound has to
            # hold on the value that is actually stored, sanitized for activity
            # and length-checked in SQL. Rejecting it here keeps a deterministic
            # failure at registration instead of quarantining every expansion.
            canonical = str(parsed)
            if (
                parsed.scheme != "https"
                or not parsed.host
                or parsed.userinfo
                or parsed.fragment
                or parsed.port not in (None, 443)
                or len(canonical) > 8192
            ):
                raise ValueError
            object.__setattr__(self, "url", canonical)
        except (ValueError, TypeError, httpx.InvalidURL):
            # Clear parser context before raising the closed configuration error below.
            pass
        else:
            return
        raise ReportingNotificationError("invalid_configuration")

    @property
    def auth_mode(self) -> str:
        return self.authentication.scheme if self.authentication is not None else "rfc9421"

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json_utf8_v1(asdict(self))).hexdigest()

    def matches(self, event: ReportingDomainEvent) -> bool:
        return (
            self.active
            and self.authorized
            and self.proof_valid
            and self.account_id == event.account_id
            and event.notification_type in self.event_types
            and (not event.consumer_namespace or self.principal_id == event.consumer_namespace)
        )


class ReportingSubscriptionResolver(Protocol):
    """External state: active-at-expansion, never claimed transactionally atomic.

    Each result must come from one normalized trusted account configuration,
    including current authorization and proof validity. Fanout reads once; the
    worker repeats exact resolution immediately before *every* HTTP attempt.
    """

    async def list_active(
        self, *, account_id: str, notification_type: str
    ) -> tuple[ReportingNotificationSubscription, ...]: ...

    async def get_active(
        self, *, account_id: str, subscriber_id: str, notification_type: str
    ) -> ReportingNotificationSubscription | None: ...


@dataclass(frozen=True)
class ReportingSigningMaterial:
    """Trusted current key material, not an adopter-supplied HTTP sender."""

    private_key: PrivateKey = field(repr=False)
    key_id: str
    algorithm: str
    advertised_algorithms: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "advertised_algorithms", frozenset(self.advertised_algorithms))
        key_matches = (
            self.algorithm == ALG_ED25519
            and isinstance(self.private_key, ed25519.Ed25519PrivateKey)
        ) or (
            self.algorithm == ALG_ES256
            and isinstance(self.private_key, ec.EllipticCurvePrivateKey)
            and isinstance(self.private_key.curve, ec.SECP256R1)
        )
        if (
            self.algorithm not in ALLOWED_ALGS
            or self.algorithm not in self.advertised_algorithms
            or not self.advertised_algorithms.issubset(ALLOWED_ALGS)
            or not key_matches
            or not self.key_id
        ):
            raise ReportingNotificationError("permanent_scope")


class ReportingSigningResolver(Protocol):
    async def resolve(
        self, *, account_id: str, principal_id: str, signing_scope_id: str
    ) -> ReportingSigningMaterial: ...


@dataclass(frozen=True)
class OpenedReportingDelivery:
    subscription: ReportingNotificationSubscription = field(repr=False)
    event: ReportingDomainEvent
    prepared: PreparedWebhook = field(repr=False)


def _decode_subscription(value: Any) -> ReportingNotificationSubscription:
    if not isinstance(value, dict):
        raise ReportingNotificationError("integrity_failure")
    candidate = dict(value)
    auth = candidate.get("authentication")
    if auth is not None:
        candidate["authentication"] = ReportingLegacyAuthentication(**auth)
    subscription = ReportingNotificationSubscription(**candidate)
    normalized = json.loads(canonical_json_utf8_v1(asdict(subscription)))
    if normalized != value:
        raise ReportingNotificationError("integrity_failure")
    return subscription


class ReportingEnvelopeCipher:
    """AES-256-GCM with all routing columns and body identity in canonical AAD.

    Key versions enable rolling encryption-key rotation while retaining old
    decryptors. Signing-key rotation is independent and resolved per attempt.
    Neither ciphertext nor plaintext routing is ever logged by this module.
    """

    def __init__(
        self,
        key: bytes,
        *,
        key_version: str = "v1",
        previous_keys: Mapping[str, bytes] | None = None,
    ) -> None:
        keys = dict(previous_keys or {})
        keys[key_version] = key
        if any(type(value) is not bytes or len(value) != 32 for value in keys.values()):
            raise ValueError("reporting outbox encryption keys must contain 32 bytes")
        for version in keys:
            reporting_identifier(version, maximum=64)
        self._keys, self.key_version = keys, key_version

    @staticmethod
    def _aad(binding: DeliveryBinding) -> bytes:
        return canonical_json_utf8_v1({"purpose": "adcp.reporting.delivery", **asdict(binding)})

    def prepare(
        self,
        event: ReportingDomainEvent,
        subscription: ReportingNotificationSubscription,
        emission_generation: int,
    ) -> StoredDelivery:
        if type(subscription) is not ReportingNotificationSubscription or not subscription.matches(
            event
        ):
            raise ReportingNotificationError("invalid_configuration")
        if type(emission_generation) is not int or emission_generation < 1:
            raise ReportingNotificationError("invalid_configuration")
        idempotency_key = str(uuid4())
        body = event.body(subscriber_id=subscription.subscriber_id, idempotency_key=idempotency_key)
        binding = DeliveryBinding(
            account_id=event.account_id,
            delivery_id=str(uuid4()),
            subscriber_id=subscription.subscriber_id,
            principal_id=subscription.principal_id,
            notification_id=event.notification_id,
            notification_type=event.notification_type,
            emission_generation=emission_generation,
            idempotency_key=idempotency_key,
            destination_sha256=hashlib.sha256(subscription.url.encode("utf-8")).hexdigest(),
            subscription_fingerprint=subscription.fingerprint,
            signing_scope_id=subscription.signing_scope_id,
            cause_kind=event.cause.kind,
            cause_id=event.cause_id,
            cause_generation=event.cause_generation,
            consumer_namespace=event.consumer_namespace,
            auth_mode=subscription.auth_mode,
            body_sha256=hashlib.sha256(body).hexdigest(),
            envelope_version=1,
            key_version=self.key_version,
        )
        plaintext = canonical_json_utf8_v1(
            {
                "subscription": asdict(subscription),
                "event": event_storage(event),
                "body": base64.b64encode(body).decode("ascii"),
            }
        )
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self._keys[self.key_version]).encrypt(
            nonce, plaintext, self._aad(binding)
        )
        return StoredDelivery(binding, nonce + encrypted)

    def open(self, delivery: StoredDelivery) -> OpenedReportingDelivery:
        """Authenticate all bound columns before any resolver/DNS/signing/HTTP."""
        try:
            binding = delivery.binding
            if (
                type(delivery.envelope) is not bytes
                or not 28 <= len(delivery.envelope) <= 1024 * 1024
            ):
                raise ValueError
            key = self._keys[binding.key_version]
            plaintext = AESGCM(key).decrypt(
                delivery.envelope[:12], delivery.envelope[12:], self._aad(binding)
            )
            if binding.envelope_version != 1:
                raise ValueError
            value = json.loads(plaintext)
            if set(value) != {"subscription", "event", "body"}:
                raise ValueError
            subscription = _decode_subscription(value["subscription"])
            event = decode_event(value["event"])
            body = base64.b64decode(value["body"], validate=True)
            payload = json.loads(body)
            validate_notification_payload(payload)
            if (
                not subscription.matches(event)
                or subscription.account_id != binding.account_id
                or subscription.subscriber_id != binding.subscriber_id
                or subscription.principal_id != binding.principal_id
                or subscription.signing_scope_id != binding.signing_scope_id
                or subscription.auth_mode != binding.auth_mode
                or subscription.fingerprint != binding.subscription_fingerprint
                or hashlib.sha256(subscription.url.encode("utf-8")).hexdigest()
                != binding.destination_sha256
                or event.notification_id != binding.notification_id
                or event.notification_type != binding.notification_type
                or event.cause.kind != binding.cause_kind
                or event.cause_id != binding.cause_id
                or event.cause_generation != binding.cause_generation
                or event.consumer_namespace != binding.consumer_namespace
                or hashlib.sha256(body).hexdigest() != binding.body_sha256
                or body
                != event.body(
                    subscriber_id=binding.subscriber_id, idempotency_key=binding.idempotency_key
                )
            ):
                raise ValueError
            return OpenedReportingDelivery(
                subscription,
                event,
                PreparedWebhook(subscription.url, binding.idempotency_key, body),
            )
        except Exception:
            # This phase is entirely local. Deeply malformed/oversized poison
            # is terminal too; it must not strand a worker before later rows.
            raise ReportingNotificationError("integrity_failure") from None
