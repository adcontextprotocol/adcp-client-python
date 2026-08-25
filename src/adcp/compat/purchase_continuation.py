"""Durable continuation of lossy AdCP 3.2 legacy purchases.

``products_available`` compatibility projections can expose a
``legacy_create`` continuation when an established 2.5/3.0/3.1 seller returned
products without an atomic 3.2 proposal.  This module redeems that continuation
without weakening its security boundary: all bindings are checked before the
first seller mutation and the token is claimed exactly once in durable state.

The coordinator is application-owned.  It does not choose credentials, derive
the authenticated principal, or route a seller connection.  Applications must
provide those values and an executor bound to the original seller session.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import inspect
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Protocol, TypeAlias, runtime_checkable
from urllib.parse import unquote_plus, urlsplit

import rfc8785
from pydantic import BaseModel, TypeAdapter, ValidationError

from adcp.types import AccountReference, CompatibilityPurchaseCoordinatorInput
from adcp.types.core import TaskResult, TaskStatus
from adcp.validation import (
    get_bundle_adcp_version,
    validate_request,
    validate_response,
)

JsonObject: TypeAlias = dict[str, Any]
_ACCOUNT_REFERENCE_ADAPTER: TypeAdapter[AccountReference] = TypeAdapter(AccountReference)
LegacyPurchaseResult: TypeAlias = Mapping[str, Any] | BaseModel | TaskResult[Any]
LegacyPurchaseExecutor: TypeAlias = Callable[
    ["LegacyPurchaseExecution"], LegacyPurchaseResult | Awaitable[LegacyPurchaseResult]
]
LegacyPurchaseReconciler: TypeAlias = Callable[
    ["LegacyPurchaseExecution", "CompatibilityPurchaseOperation"],
    "ReconciliationResult | Awaitable[ReconciliationResult]",
]
LegacyPurchasePendingPoller: TypeAlias = Callable[
    ["LegacyPurchaseExecution", "CompatibilityPurchaseOperation"],
    "PendingTaskResolution | Awaitable[PendingTaskResolution]",
]

_SOURCE_VERSION_RE = re.compile(r"^(?:2\.5|3\.[01])\.\d+$")
_REQUIRED_LOSSES = frozenset({"feed_version_not_atomic", "pricing_version_not_atomic"})
_MUTATION_LOSS = "mutation_idempotency_not_guaranteed"
_ALLOWED_LOSSES = _REQUIRED_LOSSES | {_MUTATION_LOSS}
_MIN_TOKEN_DERIVATION_KEY_BYTES = 32
_FORBIDDEN_PERSISTED_KEYS = frozenset(
    {
        "access_token",
        "access_key",
        "access_key_id",
        "api_key",
        "api_token",
        "auth_token",
        "auth",
        "authentication",
        "authorization",
        "authorization_code",
        "bearer_token",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "id_token",
        "jwt",
        "key",
        "password",
        "passwd",
        "private_key",
        "proxy_authorization",
        "push_notification_config",
        "refresh_token",
        "secret_key",
        "secret",
        "set_cookie",
        "signing_secret",
        "signature",
        "webhook_secret",
        "webhook_url",
        "callback_url",
        "token",
    }
)
_SENSITIVE_URL_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "credential",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    }
)
_FORBIDDEN_COMPACT_KEYS = frozenset(key.replace("_", "") for key in _FORBIDDEN_PERSISTED_KEYS)
_FORBIDDEN_COMPACT_SUFFIXES = (
    "accesskey",
    "accesskeyid",
    "accesstoken",
    "apikey",
    "authtoken",
    "authorizationcode",
    "authorization",
    "callbackurl",
    "clientsecret",
    "credential",
    "credentials",
    "idtoken",
    "jwt",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "signature",
    "setcookie",
    "token",
    "webhookurl",
)


class CompatibilityContinuationErrorCode(str, Enum):
    """SDK-local error categories for continuation failures.

    These names are not AdCP wire error codes and are never sent to a seller.
    """

    NOT_FOUND = "continuation_not_found"
    EXPIRED = "continuation_expired"
    BINDING_MISMATCH = "continuation_binding_mismatch"
    INVALID_INPUT = "invalid_continuation_input"
    INVALID_LEGACY_REQUEST = "invalid_legacy_create_request"
    LOSS_MISMATCH = "loss_acceptance_mismatch"
    ALREADY_CLAIMED = "continuation_already_claimed"
    IDEMPOTENCY_CONFLICT = "continuation_idempotency_conflict"
    AMBIGUOUS_MUTATION = "ambiguous_legacy_mutation"
    INVALID_LEGACY_RESPONSE = "invalid_legacy_create_response"
    STORE_CONFLICT = "continuation_store_conflict"
    PERSISTENCE_POLICY = "continuation_persistence_policy"
    STORE_QUOTA_EXCEEDED = "continuation_store_quota_exceeded"
    PENDING_RESOLUTION_REQUIRED = "pending_legacy_resolution_required"


class CompatibilityContinuationError(Exception):
    """Typed SDK-local failure raised before or around legacy execution."""

    code: CompatibilityContinuationErrorCode
    recovery_guidance: str
    details: JsonObject

    def __init__(
        self,
        code: CompatibilityContinuationErrorCode,
        message: str,
        *,
        recovery_guidance: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.recovery_guidance = recovery_guidance
        self.details = dict(details or {})
        super().__init__(message)


class CompatibilityOperationState(str, Enum):
    """Durable operation states used by continuation stores."""

    CLAIMED = "claimed"
    IN_FLIGHT = "in_flight"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class LegacyPurchaseContinuation:
    """Immutable, token-bound compatibility context stored at projection time.

    ``token_hash`` is SHA-256 of the opaque token.  The raw bearer token must
    never be persisted.  ``observed_request`` and ``observed_response`` retain
    the complete legacy discovery transaction, including every observed product
    and pricing option, rather than a reconstructable subset.
    """

    token_hash: str
    issuance_fingerprint: str | None
    issuance_binding_hash: str | None
    principal_id: str
    account_identity: str
    source_adcp_version: str
    expires_at: datetime
    observed_request: JsonObject
    observed_response: JsonObject
    observed_payload_hash: str
    product_ids: tuple[str, ...]
    projected_products: tuple[JsonObject, ...] | None
    losses: frozenset[str]
    mutation_idempotency_guaranteed: bool
    target_binding: str
    listed_purchase_context: JsonObject | None = None


@dataclass(frozen=True)
class CompatibilityPurchaseOperation:
    """Durable single-use operation returned by a continuation store."""

    operation_id: str
    principal_id: str
    idempotency_key: str
    token_hash: str
    payload_hash: str
    state: CompatibilityOperationState
    revision: int
    execution_input: JsonObject
    reserved_result_bytes: int = 0
    result: JsonObject | None = None


@dataclass(frozen=True)
class LegacyPurchaseExecution:
    """Execution context passed to the application-owned legacy executor."""

    operation_id: str
    principal_id: str
    idempotency_key: str
    source_adcp_version: str
    account: JsonObject
    target_binding: str
    selected_product_ids: tuple[str, ...]
    legacy_create_request: JsonObject
    observed_request: JsonObject
    observed_response: JsonObject
    listed_purchase_context: JsonObject | None


@dataclass(frozen=True)
class PendingTaskResolution:
    """Task-bound result returned by a read-only pending-task poller."""

    task_id: str
    result: LegacyPurchaseResult


class ReconciliationStatus(str, Enum):
    APPLIED = "authoritatively_applied"
    NOT_APPLIED = "authoritatively_not_applied"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ReconciliationResult:
    """Authoritative result of reconciling an interrupted seller mutation."""

    status: ReconciliationStatus
    result: LegacyPurchaseResult | None = None

    @classmethod
    def applied(cls, result: LegacyPurchaseResult) -> ReconciliationResult:
        return cls(ReconciliationStatus.APPLIED, copy.deepcopy(result))

    @classmethod
    def not_applied(cls) -> ReconciliationResult:
        return cls(ReconciliationStatus.NOT_APPLIED)

    @classmethod
    def ambiguous(cls) -> ReconciliationResult:
        return cls(ReconciliationStatus.AMBIGUOUS)


@runtime_checkable
class CompatibilityContinuationStore(Protocol):
    """Atomic persistence contract for compatibility continuations.

    Production implementations must set :attr:`is_durable` to ``True`` and
    make :meth:`claim` atomic across every process that can execute a purchase.
    The supplied ``now`` is only a lower bound: after taking the transaction
    lock, the store must refresh time from an authoritative clock before
    checking expiry. A claimed token is never made available again merely
    because time passed.
    """

    is_durable: ClassVar[bool]

    async def put_continuation(self, continuation: LegacyPurchaseContinuation) -> None: ...

    async def get_continuation(
        self, token_hash: str, *, principal_id: str
    ) -> LegacyPurchaseContinuation | None: ...

    async def claim(
        self,
        token_hash: str,
        *,
        principal_id: str,
        idempotency_key: str,
        payload_hash: str,
        execution_input: Mapping[str, Any],
        now: datetime,
    ) -> CompatibilityPurchaseOperation: ...

    async def get_operation(
        self, operation_id: str, *, principal_id: str
    ) -> CompatibilityPurchaseOperation | None: ...

    async def get_operation_by_idempotency_key(
        self, idempotency_key: str, *, principal_id: str
    ) -> CompatibilityPurchaseOperation | None: ...

    async def mark_in_flight(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation: ...

    async def mark_ambiguous(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation: ...

    async def complete(
        self,
        operation: CompatibilityPurchaseOperation,
        result: Mapping[str, Any],
        *,
        state: CompatibilityOperationState = CompatibilityOperationState.SUCCEEDED,
    ) -> CompatibilityPurchaseOperation: ...

    async def fence_in_flight(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        """CAS ``IN_FLIGHT`` to ``AMBIGUOUS`` using the operation revision."""
        ...

    async def resume_after_not_applied(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        """Atomically resume only ``AMBIGUOUS`` after authoritative absence."""
        ...


class InMemoryCompatibilityContinuationStore:
    """Process-local reference store for tests and development only."""

    is_durable: ClassVar[bool] = False

    def __init__(self, *, max_records: int = 20_000, max_bytes: int = 64 * 1024 * 1024) -> None:
        if type(max_records) is not int or max_records <= 0:
            raise ValueError("max_records must be a positive integer")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._continuations: dict[str, LegacyPurchaseContinuation] = {}
        self._claimed_by: dict[str, str] = {}
        self._operations: dict[tuple[str, str], CompatibilityPurchaseOperation] = {}
        self._lock = asyncio.Lock()
        self._clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self._max_records = max_records
        self._max_bytes = max_bytes

    async def put_continuation(self, continuation: LegacyPurchaseContinuation) -> None:
        async with self._lock:
            copied = _copy_continuation(continuation)
            _validate_continuation_persistence(copied)
            if any(
                value.issuance_fingerprint is None
                and value.principal_id == copied.principal_id
                and value.observed_payload_hash == copied.observed_payload_hash
                and value.account_identity == copied.account_identity
                and value.target_binding == copied.target_binding
                for value in self._continuations.values()
            ):
                raise _error(
                    CompatibilityContinuationErrorCode.STORE_CONFLICT,
                    "equivalent pre-migration authorization requires operator resolution",
                    "Resolve or quarantine the legacy continuation before reissuing.",
                )
            existing = self._continuations.get(continuation.token_hash)
            by_fingerprint = next(
                (
                    value
                    for value in self._continuations.values()
                    if value.issuance_fingerprint is not None
                    and value.issuance_fingerprint == continuation.issuance_fingerprint
                    and value.principal_id == continuation.principal_id
                ),
                None,
            )
            if existing == copied and (by_fingerprint is None or by_fingerprint == existing):
                return
            if existing is not None or by_fingerprint is not None:
                raise _error(
                    CompatibilityContinuationErrorCode.STORE_CONFLICT,
                    "continuation issuance fingerprint is already registered differently",
                    "Use the same token derivation key and exact issuance inputs.",
                )
            self._check_quota(additional=copied)
            self._continuations[continuation.token_hash] = copied

    async def get_continuation(
        self, token_hash: str, *, principal_id: str
    ) -> LegacyPurchaseContinuation | None:
        async with self._lock:
            record = self._continuations.get(token_hash)
            if record is None or record.principal_id != principal_id:
                return None
            return _copy_continuation(record)

    async def claim(
        self,
        token_hash: str,
        *,
        principal_id: str,
        idempotency_key: str,
        payload_hash: str,
        execution_input: Mapping[str, Any],
        now: datetime,
    ) -> CompatibilityPurchaseOperation:
        async with self._lock:
            claim_time = max(_aware_utc(now, field="claim time"), self._clock())
            key = (principal_id, idempotency_key)
            existing = self._operations.get(key)
            if existing is not None:
                if existing.payload_hash != payload_hash or existing.token_hash != token_hash:
                    raise _error(
                        CompatibilityContinuationErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key was already used with a different logical payload",
                        "Use the original payload or start a new projected purchase.",
                    )
                if existing.execution_input != _json_copy(execution_input):
                    raise _store_state_error("stored execution input changed for idempotent claim")
                return _copy_operation(existing)

            record = self._continuations.get(token_hash)
            if record is None or record.principal_id != principal_id:
                raise _not_found()
            if claim_time >= record.expires_at:
                raise _error(
                    CompatibilityContinuationErrorCode.EXPIRED,
                    "continuation expired before it could be claimed",
                    "Repeat product discovery and obtain a new continuation.",
                )
            if token_hash in self._claimed_by:
                raise _error(
                    CompatibilityContinuationErrorCode.ALREADY_CLAIMED,
                    "continuation was already claimed by another operation",
                    "Replay the original idempotency key, or restart product discovery.",
                )

            operation = CompatibilityPurchaseOperation(
                operation_id=secrets.token_urlsafe(24),
                principal_id=principal_id,
                idempotency_key=idempotency_key,
                token_hash=token_hash,
                payload_hash=payload_hash,
                state=CompatibilityOperationState.CLAIMED,
                revision=1,
                execution_input=_json_copy(execution_input),
            )
            _validate_persistable_payload(operation.execution_input, context="execution input")
            self._check_quota(additional=operation)
            self._operations[key] = operation
            self._claimed_by[token_hash] = operation.operation_id
            return _copy_operation(operation)

    async def get_operation(
        self, operation_id: str, *, principal_id: str
    ) -> CompatibilityPurchaseOperation | None:
        async with self._lock:
            for operation in self._operations.values():
                if (
                    operation.operation_id == operation_id
                    and operation.principal_id == principal_id
                ):
                    return _copy_operation(operation)
        return None

    async def get_operation_by_idempotency_key(
        self, idempotency_key: str, *, principal_id: str
    ) -> CompatibilityPurchaseOperation | None:
        async with self._lock:
            operation = self._operations.get((principal_id, idempotency_key))
            return _copy_operation(operation) if operation is not None else None

    async def mark_in_flight(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await self._transition(
            operation,
            allowed={CompatibilityOperationState.CLAIMED},
            target=CompatibilityOperationState.IN_FLIGHT,
        )

    async def mark_ambiguous(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await self._transition(
            operation,
            allowed={CompatibilityOperationState.CLAIMED, CompatibilityOperationState.IN_FLIGHT},
            target=CompatibilityOperationState.AMBIGUOUS,
        )

    async def complete(
        self,
        operation: CompatibilityPurchaseOperation,
        result: Mapping[str, Any],
        *,
        state: CompatibilityOperationState = CompatibilityOperationState.SUCCEEDED,
    ) -> CompatibilityPurchaseOperation:
        if state not in {
            CompatibilityOperationState.PENDING,
            CompatibilityOperationState.SUCCEEDED,
            CompatibilityOperationState.FAILED,
        }:
            raise ValueError("completed result requires pending, succeeded, or failed state")
        copied = _json_copy(result)
        _validate_persistable_payload(copied, context="legacy result")
        return await self._transition(
            operation,
            allowed={
                CompatibilityOperationState.IN_FLIGHT,
                CompatibilityOperationState.AMBIGUOUS,
                CompatibilityOperationState.PENDING,
            },
            target=state,
            result=copied,
        )

    async def fence_in_flight(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await self._transition(
            operation,
            allowed={CompatibilityOperationState.IN_FLIGHT},
            target=CompatibilityOperationState.AMBIGUOUS,
        )

    async def resume_after_not_applied(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await self._transition(
            operation,
            allowed={CompatibilityOperationState.AMBIGUOUS},
            target=CompatibilityOperationState.CLAIMED,
        )

    async def _transition(
        self,
        operation: CompatibilityPurchaseOperation,
        *,
        allowed: set[CompatibilityOperationState],
        target: CompatibilityOperationState,
        result: JsonObject | None = None,
    ) -> CompatibilityPurchaseOperation:
        async with self._lock:
            key = (operation.principal_id, operation.idempotency_key)
            current = self._operations.get(key)
            if current is None or current.operation_id != operation.operation_id:
                raise _store_state_error("operation is missing from continuation store")
            if current.revision != operation.revision:
                raise _store_state_error("operation revision changed concurrently")
            if current.state not in allowed:
                raise _store_state_error(
                    f"cannot transition operation from {current.state.value} to {target.value}"
                )
            updated = replace(
                current,
                state=target,
                revision=current.revision + 1,
                result=copy.deepcopy(result),
            )
            self._operations[key] = updated
            try:
                self._check_current_quota()
            except BaseException:
                self._operations[key] = current
                raise
            return _copy_operation(updated)

    def _check_quota(
        self,
        *,
        additional: LegacyPurchaseContinuation | CompatibilityPurchaseOperation,
    ) -> None:
        records = len(self._continuations) + len(self._operations) + 1
        values: list[Any] = [*self._continuations.values(), *self._operations.values(), additional]
        logical_bytes = sum(len(repr(value).encode("utf-8")) for value in values)
        if records > self._max_records or logical_bytes > self._max_bytes:
            raise _quota_error(self._max_records, self._max_bytes)

    def _check_current_quota(self) -> None:
        values: list[Any] = [*self._continuations.values(), *self._operations.values()]
        logical_bytes = sum(len(repr(value).encode("utf-8")) for value in values)
        if len(values) > self._max_records or logical_bytes > self._max_bytes:
            raise _quota_error(self._max_records, self._max_bytes)


class LegacyPurchaseCoordinator:
    """Validate, atomically claim, execute, and replay a legacy purchase."""

    def __init__(
        self,
        *,
        store: CompatibilityContinuationStore,
        executor: LegacyPurchaseExecutor,
        reconciler: LegacyPurchaseReconciler | None = None,
        pending_poller: LegacyPurchasePendingPoller | None = None,
        token_derivation_key: bytes | bytearray | memoryview | None = None,
        allow_non_durable_store: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, CompatibilityContinuationStore):
            raise TypeError("store must implement CompatibilityContinuationStore")
        if not store.is_durable and not allow_non_durable_store:
            raise ValueError(
                "production continuation coordination requires a durable store; "
                "set allow_non_durable_store=True only for tests or local development"
            )
        self.store = store
        self.executor = executor
        self.reconciler = reconciler
        self.pending_poller = pending_poller
        if token_derivation_key is None:
            if store.is_durable:
                raise ValueError(
                    "durable continuation coordination requires a stable "
                    "token_derivation_key of at least 32 bytes"
                )
            token_derivation_key = secrets.token_bytes(_MIN_TOKEN_DERIVATION_KEY_BYTES)
        if not isinstance(token_derivation_key, (bytes, bytearray, memoryview)):
            raise TypeError("token_derivation_key must be bytes-like")
        key = bytes(token_derivation_key)
        if len(key) < _MIN_TOKEN_DERIVATION_KEY_BYTES or not any(key):
            raise ValueError("token_derivation_key must be a high-entropy secret of 32+ bytes")
        self._token_derivation_key = key
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def issue_legacy_create_continuation(
        self,
        *,
        principal_id: str,
        issuance_idempotency_key: str,
        account: Mapping[str, Any] | Any,
        source_adcp_version: str,
        expires_at: datetime,
        observed_request: Mapping[str, Any],
        observed_response: Mapping[str, Any],
        product_ids: list[str] | tuple[str, ...],
        buyer_visible_products: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        losses: list[str] | tuple[str, ...] | frozenset[str],
        target_binding: str,
        mutation_idempotency_guaranteed: bool = False,
        listed_purchase_context: Mapping[str, Any] | None = None,
    ) -> str:
        """Persist all projection bindings and return the opaque bearer token."""

        _require_text(principal_id, "principal_id")
        _require_text(issuance_idempotency_key, "issuance_idempotency_key")
        _require_text(target_binding, "target_binding")
        if type(mutation_idempotency_guaranteed) is not bool:
            raise _invalid("mutation_idempotency_guaranteed must be a boolean")
        _validate_source_version(source_adcp_version)
        expires_at = _aware_utc(expires_at, field="expires_at")
        now = _aware_utc(self._clock(), field="clock result")
        if expires_at <= now:
            raise _invalid("expires_at must be in the future")

        account_payload = _account_payload(account)
        account_identity = canonical_account_identity(account_payload)
        ids = _unique_nonempty_strings(product_ids, field="product_ids")
        loss_set = _validate_loss_set(
            losses,
            source_adcp_version=source_adcp_version,
            mutation_idempotency_guaranteed=mutation_idempotency_guaranteed,
        )
        observed_req = _json_copy(observed_request)
        observed_resp = _json_copy(observed_response)
        _validate_source_discovery(
            observed_req,
            observed_resp,
            source_adcp_version=source_adcp_version,
            account_identity=account_identity,
        )
        _validate_observed_product_ids(observed_resp, ids)
        projected = _validate_projected_products(
            buyer_visible_products,
            observed_resp,
            ids,
            source_adcp_version=source_adcp_version,
        )
        listed = (
            _json_copy(listed_purchase_context) if listed_purchase_context is not None else None
        )
        for context, value in (
            ("observed request", observed_req),
            ("observed response", observed_resp),
            ("buyer-visible products", {"products": list(projected)}),
            ("listed purchase context", listed),
        ):
            if value is not None:
                _validate_persistable_payload(value, context=context)

        issuance_fingerprint = _full_hash(
            {
                "principal_id": principal_id,
                "issuance_idempotency_key": issuance_idempotency_key,
            }
        )
        issuance_binding_hash = _full_hash(
            {
                "account_identity": account_identity,
                "source_adcp_version": source_adcp_version,
                "expires_at": expires_at.isoformat(),
                "observed_request": observed_req,
                "observed_response": observed_resp,
                "product_ids": list(ids),
                "projected_products": list(projected),
                "losses": sorted(loss_set),
                "mutation_idempotency_guaranteed": mutation_idempotency_guaranteed,
                "target_binding": target_binding,
                "listed_purchase_context": listed,
            }
        )
        token = _derive_token(
            self._token_derivation_key, issuance_fingerprint, issuance_binding_hash
        )
        token_hash = _token_hash(token)
        observed_payload_hash = _full_hash({"request": observed_req, "response": observed_resp})
        record = LegacyPurchaseContinuation(
            token_hash=token_hash,
            issuance_fingerprint=issuance_fingerprint,
            issuance_binding_hash=issuance_binding_hash,
            principal_id=principal_id,
            account_identity=account_identity,
            source_adcp_version=source_adcp_version,
            expires_at=expires_at,
            observed_request=observed_req,
            observed_response=observed_resp,
            observed_payload_hash=observed_payload_hash,
            product_ids=ids,
            projected_products=projected,
            losses=loss_set,
            mutation_idempotency_guaranteed=mutation_idempotency_guaranteed,
            target_binding=target_binding,
            listed_purchase_context=listed,
        )
        await self.store.put_continuation(record)
        return token

    async def continue_legacy_purchase(
        self,
        purchase_input: CompatibilityPurchaseCoordinatorInput | Mapping[str, Any],
        *,
        principal_id: str,
        target_binding: str,
    ) -> JsonObject:
        """Redeem a ``legacy_create`` continuation or deterministically replay it."""

        _require_text(principal_id, "principal_id")
        _require_text(target_binding, "target_binding")
        payload = _parse_input(purchase_input)
        token_hash = _token_hash(payload["continuation_token"])
        record = await self.store.get_continuation(token_hash, principal_id=principal_id)
        if record is None:
            raise _not_found()
        self._validate_bindings(payload, record, target_binding=target_binding)

        payload_hash = _full_hash({"input": payload, "target_binding": target_binding})
        now = _aware_utc(self._clock(), field="clock result")
        operation = await self.store.claim(
            token_hash,
            principal_id=principal_id,
            idempotency_key=payload["idempotency_key"],
            payload_hash=payload_hash,
            execution_input=_execution_input(payload),
            now=now,
        )
        return await self._drive(operation, record, target_binding=target_binding)

    async def refresh_pending_legacy_purchase(
        self,
        operation: CompatibilityPurchaseOperation,
        *,
        principal_id: str,
        target_binding: str,
    ) -> JsonObject:
        """Poll and CAS-advance a pending seller task through an application callback.

        The poller must be an idempotent, read-only lookup of the already-created
        seller task. Input/approval submission happens outside this callback.
        The returned task identity is checked for pending and terminal results.
        """

        _require_text(principal_id, "principal_id")
        _require_text(target_binding, "target_binding")
        if operation.principal_id != principal_id:
            raise _not_found()
        current = await self.store.get_operation(operation.operation_id, principal_id=principal_id)
        if current is None:
            raise _not_found()
        if current.revision != operation.revision:
            raise _store_state_error("operation revision changed before pending refresh")
        if current.state != CompatibilityOperationState.PENDING or current.result is None:
            raise _error(
                CompatibilityContinuationErrorCode.PENDING_RESOLUTION_REQUIRED,
                "operation is not a revision-bearing pending seller task",
                "Look up a fresh pending operation snapshot before refreshing it.",
                details={"operation_id": current.operation_id},
            )
        if self.pending_poller is None:
            raise _error(
                CompatibilityContinuationErrorCode.PENDING_RESOLUTION_REQUIRED,
                "no pending seller task poller is configured",
                "Configure pending_poller to read the original seller task state.",
                details={"operation_id": current.operation_id},
            )
        record = await self.store.get_continuation(current.token_hash, principal_id=principal_id)
        if record is None:
            raise _not_found()
        self._validate_bindings(current.execution_input, record, target_binding=target_binding)
        execution = _execution_from(current, record, current.execution_input, target_binding)
        resolution = await _call_callback(
            self.pending_poller, _copy_execution(execution), _copy_operation(current)
        )
        previous_task_id = current.result.get("task_id")
        if not isinstance(resolution, PendingTaskResolution):
            raise TypeError("pending_poller must return PendingTaskResolution")
        if resolution.task_id != previous_task_id:
            raise _invalid_legacy_response(record.source_adcp_version, [])
        copied, state = _validated_result(
            resolution.result, source_adcp_version=record.source_adcp_version
        )
        if (
            state == CompatibilityOperationState.PENDING
            and copied.get("task_id") != previous_task_id
        ):
            raise _invalid_legacy_response(record.source_adcp_version, [])
        completed = await _shielded_transition(self.store.complete(current, copied, state=state))
        assert completed.result is not None
        return copy.deepcopy(completed.result)

    async def get_legacy_purchase_operation(
        self, operation_id: str, *, principal_id: str
    ) -> CompatibilityPurchaseOperation:
        """Return a principal-scoped operation snapshot carrying its CAS revision."""

        _require_text(operation_id, "operation_id")
        _require_text(principal_id, "principal_id")
        operation = await self.store.get_operation(operation_id, principal_id=principal_id)
        if operation is None:
            raise _not_found()
        return _copy_operation(operation)

    async def get_legacy_purchase_operation_by_idempotency_key(
        self, idempotency_key: str, *, principal_id: str
    ) -> CompatibilityPurchaseOperation:
        """Look up a principal-scoped operation when only the buyer key is known."""

        _require_text(idempotency_key, "idempotency_key")
        _require_text(principal_id, "principal_id")
        operation = await self.store.get_operation_by_idempotency_key(
            idempotency_key, principal_id=principal_id
        )
        if operation is None:
            raise _not_found()
        return _copy_operation(operation)

    async def recover_legacy_purchase(
        self,
        operation: CompatibilityPurchaseOperation,
        *,
        principal_id: str,
        target_binding: str,
    ) -> JsonObject:
        """Fence an abandoned executor, reconcile, and resume using a CAS snapshot.

        Callers must first ensure the old executor cannot still reach the seller.
        The operation revision fences stale durable completions; it cannot revoke
        external network credentials or an already-running seller request.
        """

        _require_text(principal_id, "principal_id")
        _require_text(target_binding, "target_binding")
        if operation.principal_id != principal_id:
            raise _not_found()
        current = await self.store.get_operation(operation.operation_id, principal_id=principal_id)
        if current is None:
            raise _not_found()
        if current.revision != operation.revision:
            raise _store_state_error("operation revision changed before recovery fence")
        record = await self.store.get_continuation(current.token_hash, principal_id=principal_id)
        if record is None:
            raise _not_found()
        if not current.execution_input:
            raise _error(
                CompatibilityContinuationErrorCode.STORE_CONFLICT,
                "migrated operation has no recoverable execution snapshot",
                "Submit one exact retry through continue_legacy_purchase first, then look up "
                "a fresh revision-bearing operation snapshot.",
                details={"operation_id": current.operation_id},
            )
        payload = _json_copy(current.execution_input)
        self._validate_bindings(payload, record, target_binding=target_binding)
        if current.state == CompatibilityOperationState.IN_FLIGHT:
            current = await self.store.fence_in_flight(current)
        return await self._drive(current, record, target_binding=target_binding, recovery=True)

    async def _drive(
        self,
        operation: CompatibilityPurchaseOperation,
        record: LegacyPurchaseContinuation,
        *,
        target_binding: str,
        recovery: bool = False,
    ) -> JsonObject:
        execution = _execution_from(operation, record, operation.execution_input, target_binding)
        if operation.state in {
            CompatibilityOperationState.PENDING,
            CompatibilityOperationState.SUCCEEDED,
            CompatibilityOperationState.FAILED,
        }:
            if operation.result is None:
                raise _store_state_error("terminal operation has no stored result")
            return copy.deepcopy(operation.result)
        if operation.state in {
            CompatibilityOperationState.IN_FLIGHT,
            CompatibilityOperationState.AMBIGUOUS,
        }:
            operation = await self._reconcile(execution, operation, allow_not_applied=recovery)
            if operation.state in {
                CompatibilityOperationState.PENDING,
                CompatibilityOperationState.SUCCEEDED,
                CompatibilityOperationState.FAILED,
            }:
                assert operation.result is not None
                return copy.deepcopy(operation.result)

        # A migrated row may replay a terminal result, or reconcile an already
        # applied mutation, without relying on a seller replay guarantee. The
        # guarantee becomes mandatory only before this coordinator can issue
        # another mutation call.
        if record.projected_products is None:
            raise _invalid(
                "legacy continuation predates buyer-visible pricing binding and cannot execute"
            )
        _validate_loss_set(
            record.losses,
            source_adcp_version=record.source_adcp_version,
            mutation_idempotency_guaranteed=record.mutation_idempotency_guaranteed,
        )

        try:
            operation = await self._reserve_execution(operation)
        except CompatibilityContinuationError as exc:
            if exc.code != CompatibilityContinuationErrorCode.STORE_CONFLICT:
                raise
            latest = await self.store.get_operation(
                operation.operation_id, principal_id=operation.principal_id
            )
            if latest is None:
                raise
            return await self._drive(
                latest, record, target_binding=target_binding, recovery=recovery
            )

        try:
            result = await _call_callback(self.executor, _copy_execution(execution))
        except asyncio.CancelledError:
            await self._mark_ambiguous_after_interruption(operation)
            raise
        except Exception as exc:
            await self._mark_ambiguous_after_interruption(operation)
            raise _ambiguous_error(operation) from exc
        try:
            copied, result_state = _validated_result(
                result, source_adcp_version=record.source_adcp_version
            )
        except CompatibilityContinuationError as exc:
            await self._mark_ambiguous_after_interruption(operation)
            exc.details.setdefault("operation_id", operation.operation_id)
            raise
        except Exception as exc:
            await self._mark_ambiguous_after_interruption(operation)
            raise _ambiguous_error(operation) from exc

        try:
            completed = await _shielded_transition(
                self.store.complete(operation, copied, state=result_state)
            )
        except asyncio.CancelledError:
            raise
        except Exception as store_exc:
            try:
                await asyncio.shield(self.store.mark_ambiguous(operation))
            except Exception:
                pass
            raise _ambiguous_error(operation) from store_exc
        assert completed.result is not None
        return copy.deepcopy(completed.result)

    async def _reserve_execution(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        task = asyncio.create_task(self.store.mark_in_flight(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                reserved = await task
                await asyncio.shield(self.store.mark_ambiguous(reserved))
            except Exception as exc:
                raise _ambiguous_error(operation) from exc
            raise

    async def _mark_ambiguous_after_interruption(
        self, operation: CompatibilityPurchaseOperation
    ) -> None:
        try:
            await _shielded_transition(self.store.mark_ambiguous(operation))
        except Exception as store_exc:
            raise _ambiguous_error(operation) from store_exc

    async def _reconcile(
        self,
        execution: LegacyPurchaseExecution,
        operation: CompatibilityPurchaseOperation,
        *,
        allow_not_applied: bool = False,
    ) -> CompatibilityPurchaseOperation:
        if self.reconciler is None:
            raise _ambiguous_error(operation)
        try:
            outcome = await _call_callback(
                self.reconciler, _copy_execution(execution), _copy_operation(operation)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _ambiguous_error(operation) from exc
        try:
            if not isinstance(outcome, ReconciliationResult):
                raise TypeError("legacy purchase reconciler must return ReconciliationResult")
            if outcome.status == ReconciliationStatus.APPLIED:
                if outcome.result is None:
                    raise ValueError("applied reconciliation requires a result")
                copied, state = _validated_result(
                    outcome.result, source_adcp_version=execution.source_adcp_version
                )
                return await self.store.complete(operation, copied, state=state)
            if outcome.status == ReconciliationStatus.NOT_APPLIED:
                # IN_FLIGHT may still have a live executor in another worker. An
                # instantaneous seller lookup can report "not applied" immediately
                # before that executor commits, so reopening it would permit two
                # calls. Only the exception/cancellation path's durably AMBIGUOUS
                # state proves this coordinator no longer has a live owner.
                if (
                    operation.state != CompatibilityOperationState.AMBIGUOUS
                    or not allow_not_applied
                ):
                    raise _ambiguous_error(operation)
                return await self.store.resume_after_not_applied(operation)
            raise _ambiguous_error(operation)
        except CompatibilityContinuationError as exc:
            if exc.code == CompatibilityContinuationErrorCode.AMBIGUOUS_MUTATION:
                raise
            raise _ambiguous_error(operation) from exc
        except Exception as exc:
            raise _ambiguous_error(operation) from exc

    def _validate_bindings(
        self,
        payload: JsonObject,
        record: LegacyPurchaseContinuation,
        *,
        target_binding: str,
    ) -> None:
        if (
            _full_hash(
                {
                    "request": record.observed_request,
                    "response": record.observed_response,
                }
            )
            != record.observed_payload_hash
        ):
            raise _store_state_error("stored observed transaction failed its payload binding")
        if target_binding != record.target_binding:
            raise _binding_error("seller target/session does not match the issued continuation")
        if canonical_account_identity(payload["account"]) != record.account_identity:
            raise _binding_error("input account does not match the issued continuation")

        selected = _unique_nonempty_strings(
            payload["selected_product_ids"], field="selected_product_ids"
        )
        if not set(selected).issubset(record.product_ids):
            raise _binding_error("selected products are not a subset of token-bound products")
        accepted = _validate_loss_set(
            payload["accepted_losses"],
            source_adcp_version=record.source_adcp_version,
            mutation_idempotency_guaranteed=record.mutation_idempotency_guaranteed,
            enforce_guarantee=False,
        )
        if accepted != record.losses:
            raise _error(
                CompatibilityContinuationErrorCode.LOSS_MISMATCH,
                "accepted_losses does not exactly match the issued loss set",
                "Accept the complete current loss set exactly, or restart discovery.",
            )

        request = payload["legacy_create_request"]
        outcome = validate_request("create_media_buy", request, version=record.source_adcp_version)
        if not outcome.valid or outcome.variant == "skipped":
            raise _error(
                CompatibilityContinuationErrorCode.INVALID_LEGACY_REQUEST,
                "legacy_create_request does not validate against its exact source version",
                "Construct the request using the source-version create_media_buy schema.",
                details={
                    "source_adcp_version": record.source_adcp_version,
                    "issues": [
                        {
                            "pointer": issue.pointer,
                            "keyword": issue.keyword,
                            "message": issue.message,
                        }
                        for issue in outcome.issues
                    ],
                },
            )
        packages = request.get("packages")
        if not isinstance(packages, list) or not packages or request.get("proposal_id") is not None:
            raise _legacy_request_error("explicit-package mode is required")
        package_ids: list[str] = []
        observed_pricing = (
            _projected_pricing_options(record.projected_products)
            if record.projected_products is not None
            else {}
        )
        for package in packages:
            if not isinstance(package, Mapping) or not isinstance(package.get("product_id"), str):
                raise _legacy_request_error("every package must carry a product_id")
            product_id = package["product_id"]
            package_ids.append(product_id)
            pricing_option_id = package.get("pricing_option_id")
            if not isinstance(pricing_option_id, str) or (
                record.projected_products is not None
                and pricing_option_id not in observed_pricing.get(product_id, frozenset())
            ):
                raise _binding_error(
                    "legacy package pricing_option_id was not observed for its product"
                )
        if set(package_ids) != set(selected):
            raise _binding_error(
                "distinct legacy package product IDs do not equal selected_product_ids"
            )

        if not record.source_adcp_version.startswith("2.5."):
            request_account = request.get("account")
            if not isinstance(request_account, Mapping):
                raise _legacy_request_error("source-version request account is required")
            if canonical_account_identity(request_account) != record.account_identity:
                raise _binding_error("legacy request account does not match the continuation")


def canonical_account_identity(account: Mapping[str, Any] | Any) -> str:
    """Return the AdCP account natural-key identity as canonical JSON.

    Mutable/display-only account fields are excluded.  In particular,
    ``operator_unit.name`` and brand governance/creative overrides do not
    participate in identity.
    """

    payload = _account_payload(account)
    if "account_id" in payload:
        identity: JsonObject = {"account_id": payload["account_id"]}
    else:
        brand = payload.get("brand")
        if not isinstance(brand, Mapping):
            raise _invalid("natural-key account requires brand")
        brand_identity: JsonObject = {"domain": brand.get("domain")}
        if brand.get("brand_id") is not None:
            brand_identity["brand_id"] = brand["brand_id"]
        if brand.get("countries") is not None:
            brand_identity["countries"] = sorted(str(v) for v in brand["countries"])
        identity = {
            "brand": brand_identity,
            "operator": payload.get("operator"),
            "sandbox": bool(payload.get("sandbox", False)),
        }
        operator_unit = payload.get("operator_unit")
        if isinstance(operator_unit, Mapping):
            identity["operator_unit_id"] = operator_unit.get("id")
        for key in ("currency", "timezone"):
            if payload.get(key) is not None:
                identity[key] = payload[key]
    try:
        return rfc8785.dumps(identity).decode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _invalid("account identity is not JSON-canonicalizable") from exc


def _parse_input(
    value: CompatibilityPurchaseCoordinatorInput | Mapping[str, Any],
) -> JsonObject:
    try:
        source = (
            value.model_dump(mode="python", by_alias=True)
            if isinstance(value, CompatibilityPurchaseCoordinatorInput)
            else value
        )
        model = CompatibilityPurchaseCoordinatorInput.model_validate(source)
        payload = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    except (ValidationError, TypeError, ValueError) as exc:
        issues = (
            exc.errors(include_input=False, include_url=False)
            if isinstance(exc, ValidationError)
            else [{"type": type(exc).__name__}]
        )
        raise _error(
            CompatibilityContinuationErrorCode.INVALID_INPUT,
            "compatibility purchase input failed beta.4 schema validation",
            "Correct the SDK-local coordinator input before retrying.",
            details={"issues": issues},
        ) from exc
    if not isinstance(payload, dict):
        raise _invalid("compatibility purchase input must be an object")
    if (
        not isinstance(payload.get("legacy_create_request"), dict)
        or not payload["legacy_create_request"]
    ):
        raise _invalid("legacy_create_request must be a non-empty object")
    if not isinstance(payload.get("accepted_losses"), list):
        raise _invalid("accepted_losses must be an array")
    return _json_copy(payload)


def _execution_from(
    operation: CompatibilityPurchaseOperation,
    record: LegacyPurchaseContinuation,
    payload: JsonObject,
    target_binding: str,
) -> LegacyPurchaseExecution:
    return LegacyPurchaseExecution(
        operation_id=operation.operation_id,
        principal_id=operation.principal_id,
        idempotency_key=operation.idempotency_key,
        source_adcp_version=record.source_adcp_version,
        account=copy.deepcopy(payload["account"]),
        target_binding=target_binding,
        selected_product_ids=tuple(payload["selected_product_ids"]),
        legacy_create_request=copy.deepcopy(payload["legacy_create_request"]),
        observed_request=copy.deepcopy(record.observed_request),
        observed_response=copy.deepcopy(record.observed_response),
        listed_purchase_context=copy.deepcopy(record.listed_purchase_context),
    )


def _execution_input(payload: JsonObject) -> JsonObject:
    """Persist the validated execution fields without the bearer continuation token."""

    return _json_copy({key: value for key, value in payload.items() if key != "continuation_token"})


def _validate_source_version(version: str) -> None:
    if not isinstance(version, str) or _SOURCE_VERSION_RE.fullmatch(version) is None:
        raise _invalid("source_adcp_version must be an exact 2.5.x, 3.0.x, or 3.1.x release")
    bundled_version = get_bundle_adcp_version(version=version)
    if bundled_version != version:
        raise _invalid(
            "source_adcp_version must exactly match the bundled source schema release "
            f"(requested {version!r}, bundled {bundled_version!r})"
        )


def _validate_loss_set(
    values: Any,
    *,
    source_adcp_version: str,
    mutation_idempotency_guaranteed: bool,
    enforce_guarantee: bool = True,
) -> frozenset[str]:
    if not isinstance(values, (list, tuple, frozenset)):
        raise _invalid("losses must be an array")
    raw = [str(value) for value in values]
    if len(raw) != len(set(raw)):
        raise _invalid("losses must not contain duplicates")
    result = frozenset(raw)
    if not _REQUIRED_LOSSES.issubset(result) or not result.issubset(_ALLOWED_LOSSES):
        raise _invalid("losses must contain both atomicity losses and no unknown values")
    requires_mutation_loss = source_adcp_version.startswith("2.5.") or not bool(
        mutation_idempotency_guaranteed
    )
    if enforce_guarantee and requires_mutation_loss and _MUTATION_LOSS not in result:
        raise _invalid(
            "continuations without a verified peer replay guarantee require the mutation "
            "idempotency loss"
        )
    if enforce_guarantee and not requires_mutation_loss and _MUTATION_LOSS in result:
        raise _invalid(
            "mutation idempotency loss must be omitted when a peer replay guarantee is bound"
        )
    return result


def _unique_nonempty_strings(values: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not values:
        raise _invalid(f"{field} must be a non-empty array")
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise _invalid(f"{field} must contain non-empty strings")
    if len(result) != len(set(result)):
        raise _invalid(f"{field} must not contain duplicates")
    return result


def _validate_observed_product_ids(response: JsonObject, expected: tuple[str, ...]) -> None:
    products = response.get("products")
    if not isinstance(products, list) or not products:
        raise _invalid("observed_response must retain a non-empty products array")
    observed: list[str] = []
    for product in products:
        if not isinstance(product, Mapping) or not isinstance(product.get("product_id"), str):
            raise _invalid("every observed product must carry product_id")
        if not isinstance(product.get("pricing_options"), list) or not product["pricing_options"]:
            raise _invalid("every observed product must retain its pricing_options")
        observed.append(product["product_id"])
    if len(observed) != len(set(observed)):
        raise _invalid("observed product IDs must be unique")
    if not set(expected).issubset(observed):
        raise _invalid("product_ids must be a subset of the observed product set")


def _observed_pricing_options(response: JsonObject) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for product in response["products"]:
        option_ids: list[str] = []
        for option in product["pricing_options"]:
            if not isinstance(option, Mapping) or not isinstance(
                option.get("pricing_option_id"), str
            ):
                raise _invalid("every observed pricing option must carry pricing_option_id")
            option_ids.append(option["pricing_option_id"])
        if len(option_ids) != len(set(option_ids)):
            raise _invalid("observed pricing option IDs must be unique per product")
        result[product["product_id"]] = frozenset(option_ids)
    return result


def _validate_projected_products(
    values: Any,
    observed_response: JsonObject,
    product_ids: tuple[str, ...],
    *,
    source_adcp_version: str,
) -> tuple[JsonObject, ...]:
    if not isinstance(values, (list, tuple)) or not values:
        raise _invalid("buyer_visible_products must be a non-empty array")
    projected = tuple(_json_copy(value) for value in values if isinstance(value, Mapping))
    if len(projected) != len(values):
        raise _invalid("every buyer-visible product must be an object")
    projected_pricing = _projected_pricing_options(projected)
    if set(projected_pricing) != set(product_ids):
        raise _invalid("product_ids must exactly match the buyer-visible product projection")
    observed_pricing = _observed_pricing_options(observed_response)
    observed_terms = _pricing_options_by_id(observed_response["products"])
    for product_id, option_ids in projected_pricing.items():
        if not option_ids.issubset(observed_pricing.get(product_id, frozenset())):
            raise _invalid("buyer-visible pricing options must be present in observed discovery")
    projected_terms = _pricing_options_by_id(projected)
    for product_id, options in projected_terms.items():
        for option_id, option in options.items():
            observed_option = observed_terms.get(product_id, {}).get(option_id)
            if observed_option is None or not _projected_option_matches_observed(
                option,
                observed_option,
                legacy_25=source_adcp_version.startswith("2.5."),
            ):
                raise _binding_error("buyer-visible pricing terms differ from observed discovery")
    return projected


def _projected_option_matches_observed(
    projected: JsonObject, observed: JsonObject, *, legacy_25: bool
) -> bool:
    return _normalized_pricing_terms(
        projected, projected=True, legacy_25=legacy_25
    ) == _normalized_pricing_terms(observed, projected=False, legacy_25=legacy_25)


def _normalized_pricing_terms(value: JsonObject, *, projected: bool, legacy_25: bool) -> JsonObject:
    # Preserve unknown/extension fields so a projection cannot silently alter
    # commercial behavior. Only the explicit 2.5 representation differences
    # are rewritten to their compact canonical equivalents.
    normalized = copy.deepcopy(value)
    if not projected and legacy_25:
        is_fixed = normalized.pop("is_fixed", None)
        rate = normalized.pop("rate", None)
        if "fixed_price" not in normalized and is_fixed is True and rate is not None:
            normalized["fixed_price"] = rate
        guidance = normalized.get("price_guidance")
        if isinstance(guidance, dict) and "floor" in guidance:
            guidance = copy.deepcopy(guidance)
            normalized["floor_price"] = guidance.pop("floor")
            if guidance:
                normalized["price_guidance"] = guidance
            else:
                normalized.pop("price_guidance", None)
    return normalized


def _pricing_options_by_id(
    products: list[Any] | tuple[JsonObject, ...],
) -> dict[str, dict[str, JsonObject]]:
    result: dict[str, dict[str, JsonObject]] = {}
    for product in products:
        if not isinstance(product, Mapping) or not isinstance(product.get("product_id"), str):
            raise _invalid("every product must carry product_id")
        options = product.get("pricing_options")
        if not isinstance(options, list):
            raise _invalid("every product must carry pricing_options")
        keyed: dict[str, JsonObject] = {}
        for option in options:
            if not isinstance(option, Mapping) or not isinstance(
                option.get("pricing_option_id"), str
            ):
                raise _invalid("every pricing option must carry pricing_option_id")
            option_id = option["pricing_option_id"]
            if option_id in keyed:
                raise _invalid("pricing option IDs must be unique per product")
            keyed[option_id] = _json_copy(option)
        result[product["product_id"]] = keyed
    return result


def _projected_pricing_options(
    products: tuple[JsonObject, ...] | list[JsonObject],
) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for product in products:
        product_id = product.get("product_id")
        options = product.get("pricing_options")
        if not isinstance(product_id, str) or not product_id:
            raise _invalid("every buyer-visible product must carry product_id")
        if product_id in result:
            raise _invalid("buyer-visible product IDs must be unique")
        if not isinstance(options, list) or not options:
            raise _invalid("every buyer-visible product must retain non-empty pricing_options")
        option_ids: list[str] = []
        for option in options:
            if not isinstance(option, Mapping) or not isinstance(
                option.get("pricing_option_id"), str
            ):
                raise _invalid("every buyer-visible pricing option must carry pricing_option_id")
            option_ids.append(option["pricing_option_id"])
        if len(option_ids) != len(set(option_ids)):
            raise _invalid("buyer-visible pricing option IDs must be unique per product")
        result[product_id] = frozenset(option_ids)
    return result


def _validate_source_discovery(
    request: JsonObject,
    response: JsonObject,
    *,
    source_adcp_version: str,
    account_identity: str,
) -> None:
    request_outcome = validate_request("get_products", request, version=source_adcp_version)
    response_outcome = validate_response("get_products", response, version=source_adcp_version)
    for side, outcome in (("request", request_outcome), ("response", response_outcome)):
        if not outcome.valid or outcome.variant == "skipped":
            raise _error(
                CompatibilityContinuationErrorCode.INVALID_INPUT,
                f"observed get_products {side} does not validate against its source version",
                "Persist the complete source-version discovery transaction before projection.",
                details={
                    "source_adcp_version": source_adcp_version,
                    "side": side,
                    "issues": [
                        {
                            "pointer": issue.pointer,
                            "keyword": issue.keyword,
                            "message": issue.message,
                        }
                        for issue in outcome.issues
                    ],
                },
            )
    if not source_adcp_version.startswith("2.5."):
        observed_account = request.get("account")
        if not isinstance(observed_account, Mapping):
            raise _binding_error("observed discovery request account is missing")
        if canonical_account_identity(observed_account) != account_identity:
            raise _binding_error(
                "observed discovery request account does not match the continuation"
            )


def _account_payload(value: Mapping[str, Any] | Any) -> JsonObject:
    try:
        source = (
            value.model_dump(mode="python", by_alias=True)
            if isinstance(value, BaseModel)
            else value
        )
        model = _ACCOUNT_REFERENCE_ADAPTER.validate_python(source)
        payload = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    except ValidationError as exc:
        raise _invalid("account must be a valid beta.4 AccountReference") from exc
    if not isinstance(payload, dict):
        raise _invalid("account must serialize to an object")
    return _json_copy(payload)


def _full_hash(payload: Mapping[str, Any]) -> str:
    try:
        return hashlib.sha256(rfc8785.dumps(dict(payload))).hexdigest()
    except (TypeError, ValueError) as exc:
        raise _invalid("payload is not JSON-canonicalizable") from exc


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _derive_token(key: bytes, issuance_fingerprint: str, issuance_binding_hash: str) -> str:
    message = f"{issuance_fingerprint}:{issuance_binding_hash}".encode("ascii")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _validate_continuation_persistence(value: LegacyPurchaseContinuation) -> None:
    for context, payload in (
        ("observed request", value.observed_request),
        ("observed response", value.observed_response),
        (
            "buyer-visible products",
            (
                {"products": list(value.projected_products)}
                if value.projected_products is not None
                else None
            ),
        ),
        ("listed purchase context", value.listed_purchase_context),
    ):
        if payload is not None:
            _validate_persistable_payload(payload, context=context)


def _validate_persistable_payload(value: Any, *, context: str) -> None:
    """Reject credentials and signed URLs before payloads cross the durable boundary."""

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                normalized = _normalize_sensitive_key(raw_key)
                if _is_forbidden_persisted_key(normalized):
                    raise _persistence_policy_error(context, "credential-bearing field")
                walk(child)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
            return
        if not isinstance(item, str):
            return
        if re.match(
            r"^\s*(?:authorization|cookie|proxy-authorization|x-api-key)\s*:",
            item,
            flags=re.IGNORECASE,
        ):
            raise _persistence_policy_error(context, "credential-bearing header")
        try:
            parsed = urlsplit(item)
        except ValueError:
            return
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return
        if parsed.username is not None or parsed.password is not None:
            raise _persistence_policy_error(context, "URL user information")
        decoded_query = unquote_plus(parsed.query)
        query_keys = {
            _normalize_sensitive_key(part.partition("=")[0])
            for part in re.split(r"[&;]", decoded_query)
            if part
        }
        if query_keys & _SENSITIVE_URL_QUERY_KEYS or any(
            key.startswith(("x_amz_", "x_goog_"))
            or key.endswith(("access_key_id", "credential", "signature", "token"))
            for key in query_keys
        ):
            raise _persistence_policy_error(context, "credential-bearing URL")
        decoded_fragment = unquote_plus(parsed.fragment)
        fragment_keys = {
            _normalize_sensitive_key(part.partition("=")[0])
            for part in re.split(r"[&;]", decoded_fragment)
            if part
        }
        if fragment_keys & _SENSITIVE_URL_QUERY_KEYS:
            raise _persistence_policy_error(context, "credential-bearing URL fragment")

    walk(value)


def _normalize_sensitive_key(value: object) -> str:
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(value))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()


def _is_forbidden_persisted_key(normalized: str) -> bool:
    candidates = {normalized}
    candidate = normalized
    while True:
        stripped = next(
            (
                candidate[: -len(wrapper)]
                for wrapper in ("_value", "_data", "_string", "_bytes")
                if candidate.endswith(wrapper)
            ),
            None,
        )
        if stripped is None:
            break
        candidates.add(stripped)
        candidate = stripped
    for candidate in candidates:
        compact = candidate.replace("_", "")
        if (
            candidate in _FORBIDDEN_PERSISTED_KEYS
            or compact in _FORBIDDEN_COMPACT_KEYS
            or compact.endswith(_FORBIDDEN_COMPACT_SUFFIXES)
            or candidate.split("_")[-1]
            in {
                "authorization",
                "cookie",
                "credential",
                "credentials",
                "jwt",
                "password",
                "secret",
                "signature",
                "token",
            }
        ):
            return True
    return False


def _json_copy(value: Mapping[str, Any]) -> JsonObject:
    payload = copy.deepcopy(dict(value))
    # RFC 8785 both validates the JSON domain and gives deterministic handling
    # of numbers; copy.deepcopy alone would retain arbitrary Python objects.
    try:
        rfc8785.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise _invalid("stored compatibility payload must contain JSON values only") from exc
    return payload


def _raw_result_payload(value: LegacyPurchaseResult) -> JsonObject:
    if isinstance(value, TaskResult):
        payload = _raw_result_payload(value.data) if value.data is not None else {}
        if value.status == TaskStatus.COMPLETED:
            if value.data is None:
                raise TypeError("completed TaskResult requires schema-shaped data")
            return payload
        if value.status == TaskStatus.FAILED:
            if value.data is None and value.adcp_error is not None:
                payload = {"errors": [value.adcp_error]}
            return payload
        status = {
            TaskStatus.SUBMITTED: "submitted",
            TaskStatus.WORKING: "working",
            TaskStatus.NEEDS_INPUT: "input-required",
        }.get(value.status)
        if status is None:
            raise TypeError(f"unsupported TaskResult status {value.status.value!r}")
        payload.setdefault("status", status)
        if payload.get("status") != status:
            raise TypeError("TaskResult status conflicts with its data envelope")
        task_id = None
        if value.submitted is not None:
            task_id = value.submitted.operation_id
        if value.metadata is not None:
            task_id = value.metadata.get("task_id", task_id)
        if task_id is not None:
            payload.setdefault("task_id", task_id)
        if not isinstance(payload.get("task_id"), str) or not payload["task_id"]:
            raise TypeError("pending TaskResult requires a non-empty task identity")
        if value.message:
            payload.setdefault("message", value.message)
        return _json_copy(payload)
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        if not isinstance(dumped, dict):
            raise TypeError("legacy purchase result model must serialize to an object")
        return _json_copy(dumped)
    if isinstance(value, Mapping):
        return _json_copy(value)
    raise TypeError("legacy purchase executor must return a mapping, Pydantic model, or TaskResult")


def _validated_result(
    value: LegacyPurchaseResult, *, source_adcp_version: str
) -> tuple[JsonObject, CompatibilityOperationState]:
    try:
        payload = _raw_result_payload(value)
    except (TypeError, ValueError, CompatibilityContinuationError) as exc:
        raise _invalid_legacy_response(source_adcp_version, []) from exc
    outcome = validate_response("create_media_buy", payload, version=source_adcp_version)
    if not outcome.valid or outcome.variant == "skipped":
        raise _invalid_legacy_response(
            source_adcp_version,
            [
                {
                    "pointer": issue.pointer,
                    "keyword": issue.keyword,
                    "message": issue.message,
                }
                for issue in outcome.issues
            ],
        )
    if outcome.variant in {"submitted", "working", "input-required"}:
        state = CompatibilityOperationState.PENDING
    elif "errors" in payload:
        state = CompatibilityOperationState.FAILED
    else:
        state = CompatibilityOperationState.SUCCEEDED
    if state == CompatibilityOperationState.PENDING and (
        not isinstance(payload.get("task_id"), str) or not payload["task_id"]
    ):
        raise _invalid_legacy_response(source_adcp_version, [])
    if isinstance(value, TaskResult):
        expected = {
            TaskStatus.SUBMITTED: CompatibilityOperationState.PENDING,
            TaskStatus.WORKING: CompatibilityOperationState.PENDING,
            TaskStatus.NEEDS_INPUT: CompatibilityOperationState.PENDING,
            TaskStatus.FAILED: CompatibilityOperationState.FAILED,
        }.get(value.status)
        if expected is not None and state != expected:
            raise _invalid_legacy_response(source_adcp_version, [])
    _validate_persistable_payload(payload, context="legacy result")
    return payload, state


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _invalid(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{field} must be a non-empty string")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_callback(callback: Callable[..., Any], *args: Any) -> Any:
    call = callback
    is_async = inspect.iscoroutinefunction(call) or inspect.iscoroutinefunction(
        getattr(call, "__call__", None)
    )
    if is_async:
        return copy.deepcopy(await _maybe_await(call(*args)))

    def invoke_sync() -> Any:
        value = call(*args)
        return value if inspect.isawaitable(value) else copy.deepcopy(value)

    value = await asyncio.to_thread(invoke_sync)
    return copy.deepcopy(await _maybe_await(value))


async def _shielded_transition(value: Awaitable[Any]) -> Any:
    task: asyncio.Future[Any] = asyncio.ensure_future(value)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Let the durable CAS settle so cancellation cannot strand an unknown
        # transition between seller completion and local persistence.
        await task
        raise


def _copy_continuation(value: LegacyPurchaseContinuation) -> LegacyPurchaseContinuation:
    return replace(
        value,
        observed_request=copy.deepcopy(value.observed_request),
        observed_response=copy.deepcopy(value.observed_response),
        projected_products=copy.deepcopy(value.projected_products),
        listed_purchase_context=copy.deepcopy(value.listed_purchase_context),
    )


def _copy_operation(value: CompatibilityPurchaseOperation) -> CompatibilityPurchaseOperation:
    return replace(
        value,
        execution_input=copy.deepcopy(value.execution_input),
        result=copy.deepcopy(value.result),
    )


def _copy_execution(value: LegacyPurchaseExecution) -> LegacyPurchaseExecution:
    return replace(
        value,
        account=copy.deepcopy(value.account),
        legacy_create_request=copy.deepcopy(value.legacy_create_request),
        observed_request=copy.deepcopy(value.observed_request),
        observed_response=copy.deepcopy(value.observed_response),
        listed_purchase_context=copy.deepcopy(value.listed_purchase_context),
    )


def _error(
    code: CompatibilityContinuationErrorCode,
    message: str,
    recovery: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> CompatibilityContinuationError:
    return CompatibilityContinuationError(
        code, message, recovery_guidance=recovery, details=details
    )


def _invalid(message: str) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.INVALID_INPUT,
        message,
        "Correct the compatibility projection or coordinator input before retrying.",
    )


def _persistence_policy_error(context: str, reason: str) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.PERSISTENCE_POLICY,
        f"{context} contains a {reason} that cannot be persisted",
        "Remove credentials, push notification configuration, and signed URLs before "
        "issuing or advancing a durable continuation.",
        details={"context": context},
    )


def _quota_error(max_records: int, max_bytes: int) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.STORE_QUOTA_EXCEEDED,
        "continuation ledger quota would be exceeded",
        "Resolve retained operations or purge eligible terminal/expired rows before retrying.",
        details={"max_records": max_records, "max_bytes": max_bytes},
    )


def _legacy_request_error(message: str) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.INVALID_LEGACY_REQUEST,
        message,
        "Construct an explicit-package request using the exact source-version schema.",
    )


def _invalid_legacy_response(
    source_adcp_version: str, issues: list[JsonObject]
) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.INVALID_LEGACY_RESPONSE,
        "legacy create_media_buy result failed exact source-version validation",
        "Reconcile the seller mutation authoritatively; do not treat this payload as success.",
        details={"source_adcp_version": source_adcp_version, "issues": issues},
    )


def _binding_error(message: str) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.BINDING_MISMATCH,
        message,
        "Do not retarget or substitute the continuation; restart product discovery.",
    )


def _not_found() -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.NOT_FOUND,
        "continuation was not found for the authenticated principal",
        "Verify the principal or restart product discovery.",
    )


def _store_state_error(message: str) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.STORE_CONFLICT,
        message,
        "Stop mutation and inspect the durable continuation ledger.",
    )


def _ambiguous_error(
    operation: CompatibilityPurchaseOperation,
) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.AMBIGUOUS_MUTATION,
        "legacy seller mutation may have committed; automatic replay is disabled",
        "Reconcile authoritatively using the original seller transaction identity; "
        "never resend the create request blindly.",
        details={"operation_id": operation.operation_id},
    )


__all__ = [
    "CompatibilityContinuationError",
    "CompatibilityContinuationErrorCode",
    "CompatibilityContinuationStore",
    "CompatibilityOperationState",
    "CompatibilityPurchaseOperation",
    "InMemoryCompatibilityContinuationStore",
    "LegacyPurchaseContinuation",
    "LegacyPurchaseCoordinator",
    "LegacyPurchaseExecution",
    "LegacyPurchaseExecutor",
    "LegacyPurchasePendingPoller",
    "LegacyPurchaseReconciler",
    "LegacyPurchaseResult",
    "PendingTaskResolution",
    "ReconciliationResult",
    "ReconciliationStatus",
    "canonical_account_identity",
]
