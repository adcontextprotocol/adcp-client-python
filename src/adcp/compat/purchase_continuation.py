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
import copy
import hashlib
import inspect
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Protocol, TypeAlias, runtime_checkable

import rfc8785
from pydantic import BaseModel, ValidationError

from adcp.types import AccountReference, CompatibilityPurchaseCoordinatorInput
from adcp.validation import (
    get_bundle_adcp_version,
    validate_request,
    validate_response,
)

JsonObject: TypeAlias = dict[str, Any]
LegacyPurchaseResult: TypeAlias = Mapping[str, Any] | BaseModel
LegacyPurchaseExecutor: TypeAlias = Callable[
    ["LegacyPurchaseExecution"], LegacyPurchaseResult | Awaitable[LegacyPurchaseResult]
]
LegacyPurchaseReconciler: TypeAlias = Callable[
    ["LegacyPurchaseExecution", "CompatibilityPurchaseOperation"],
    "ReconciliationResult | Awaitable[ReconciliationResult]",
]

_SOURCE_VERSION_RE = re.compile(r"^(?:2\.5|3\.[01])\.\d+$")
_REQUIRED_LOSSES = frozenset({"feed_version_not_atomic", "pricing_version_not_atomic"})
_MUTATION_LOSS = "mutation_idempotency_not_guaranteed"
_ALLOWED_LOSSES = _REQUIRED_LOSSES | {_MUTATION_LOSS}


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
    STORE_CONFLICT = "continuation_store_conflict"


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
    SUCCEEDED = "succeeded"
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
    principal_id: str
    account_identity: str
    source_adcp_version: str
    expires_at: datetime
    observed_request: JsonObject
    observed_response: JsonObject
    observed_payload_hash: str
    product_ids: tuple[str, ...]
    losses: frozenset[str]
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


class ReconciliationStatus(str, Enum):
    APPLIED = "authoritatively_applied"
    NOT_APPLIED = "authoritatively_not_applied"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ReconciliationResult:
    """Authoritative result of reconciling an interrupted seller mutation."""

    status: ReconciliationStatus
    result: JsonObject | None = None

    @classmethod
    def applied(cls, result: LegacyPurchaseResult) -> ReconciliationResult:
        return cls(ReconciliationStatus.APPLIED, _result_payload(result))

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
        now: datetime,
    ) -> CompatibilityPurchaseOperation: ...

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
    ) -> CompatibilityPurchaseOperation: ...

    async def resume_after_not_applied(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        """Atomically resume only ``AMBIGUOUS`` after authoritative absence."""
        ...


class InMemoryCompatibilityContinuationStore:
    """Process-local reference store for tests and development only."""

    is_durable: ClassVar[bool] = False

    def __init__(self) -> None:
        self._continuations: dict[str, LegacyPurchaseContinuation] = {}
        self._claimed_by: dict[str, str] = {}
        self._operations: dict[tuple[str, str], CompatibilityPurchaseOperation] = {}
        self._lock = asyncio.Lock()
        self._clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def put_continuation(self, continuation: LegacyPurchaseContinuation) -> None:
        async with self._lock:
            if continuation.token_hash in self._continuations:
                raise _error(
                    CompatibilityContinuationErrorCode.STORE_CONFLICT,
                    "continuation token hash is already registered",
                    "Issue a fresh cryptographically random continuation token.",
                )
            self._continuations[continuation.token_hash] = _copy_continuation(continuation)

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
            )
            self._operations[key] = operation
            self._claimed_by[token_hash] = operation.operation_id
            return _copy_operation(operation)

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
    ) -> CompatibilityPurchaseOperation:
        copied = _json_copy(result)
        return await self._transition(
            operation,
            allowed={
                CompatibilityOperationState.IN_FLIGHT,
                CompatibilityOperationState.AMBIGUOUS,
            },
            target=CompatibilityOperationState.SUCCEEDED,
            result=copied,
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
            if current.state not in allowed:
                raise _store_state_error(
                    f"cannot transition operation from {current.state.value} to {target.value}"
                )
            updated = replace(current, state=target, result=copy.deepcopy(result))
            self._operations[key] = updated
            return _copy_operation(updated)


class LegacyPurchaseCoordinator:
    """Validate, atomically claim, execute, and replay a legacy purchase."""

    def __init__(
        self,
        *,
        store: CompatibilityContinuationStore,
        executor: LegacyPurchaseExecutor,
        reconciler: LegacyPurchaseReconciler | None = None,
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
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def issue_legacy_create_continuation(
        self,
        *,
        principal_id: str,
        account: Mapping[str, Any] | Any,
        source_adcp_version: str,
        expires_at: datetime,
        observed_request: Mapping[str, Any],
        observed_response: Mapping[str, Any],
        product_ids: list[str] | tuple[str, ...],
        losses: list[str] | tuple[str, ...] | frozenset[str],
        target_binding: str,
        listed_purchase_context: Mapping[str, Any] | None = None,
    ) -> str:
        """Persist all projection bindings and return the opaque bearer token."""

        _require_text(principal_id, "principal_id")
        _require_text(target_binding, "target_binding")
        _validate_source_version(source_adcp_version)
        expires_at = _aware_utc(expires_at, field="expires_at")
        now = _aware_utc(self._clock(), field="clock result")
        if expires_at <= now:
            raise _invalid("expires_at must be in the future")

        account_payload = _account_payload(account)
        account_identity = canonical_account_identity(account_payload)
        ids = _unique_nonempty_strings(product_ids, field="product_ids")
        loss_set = _validate_loss_set(losses, source_adcp_version=source_adcp_version)
        observed_req = _json_copy(observed_request)
        observed_resp = _json_copy(observed_response)
        _validate_source_discovery(
            observed_req,
            observed_resp,
            source_adcp_version=source_adcp_version,
            account_identity=account_identity,
        )
        _validate_observed_product_ids(observed_resp, ids)
        listed = (
            _json_copy(listed_purchase_context) if listed_purchase_context is not None else None
        )

        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(token)
        observed_payload_hash = _full_hash({"request": observed_req, "response": observed_resp})
        record = LegacyPurchaseContinuation(
            token_hash=token_hash,
            principal_id=principal_id,
            account_identity=account_identity,
            source_adcp_version=source_adcp_version,
            expires_at=expires_at,
            observed_request=observed_req,
            observed_response=observed_resp,
            observed_payload_hash=observed_payload_hash,
            product_ids=ids,
            losses=loss_set,
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
            now=now,
        )
        execution = _execution_from(operation, record, payload, target_binding)

        if operation.state == CompatibilityOperationState.SUCCEEDED:
            if operation.result is None:
                raise _store_state_error("succeeded operation has no stored result")
            return copy.deepcopy(operation.result)
        if operation.state in {
            CompatibilityOperationState.IN_FLIGHT,
            CompatibilityOperationState.AMBIGUOUS,
        }:
            operation = await self._reconcile(execution, operation)
            if operation.state == CompatibilityOperationState.SUCCEEDED:
                assert operation.result is not None
                return copy.deepcopy(operation.result)

        try:
            operation = await self.store.mark_in_flight(operation)
        except CompatibilityContinuationError as exc:
            if exc.code != CompatibilityContinuationErrorCode.STORE_CONFLICT:
                raise
            # An exact concurrent retry may have won the CLAIMED -> IN_FLIGHT
            # CAS after our claim read. Reload it through claim; never execute
            # in both callers.
            operation = await self.store.claim(
                token_hash,
                principal_id=principal_id,
                idempotency_key=payload["idempotency_key"],
                payload_hash=payload_hash,
                now=now,
            )
            if operation.state == CompatibilityOperationState.SUCCEEDED:
                if operation.result is None:
                    raise _store_state_error("succeeded operation has no stored result")
                return copy.deepcopy(operation.result)
            if operation.state in {
                CompatibilityOperationState.IN_FLIGHT,
                CompatibilityOperationState.AMBIGUOUS,
            }:
                operation = await self._reconcile(execution, operation)
                if operation.state == CompatibilityOperationState.SUCCEEDED:
                    assert operation.result is not None
                    return copy.deepcopy(operation.result)
                operation = await self.store.mark_in_flight(operation)
            else:
                raise _store_state_error(
                    "operation remained claimed after losing execution reservation"
                )
        try:
            result = await _maybe_await(self.executor(execution))
            copied = _result_payload(result)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.store.mark_ambiguous(operation))
            except Exception as store_exc:
                raise _ambiguous_error(operation) from store_exc
            raise
        except Exception as exc:
            try:
                await asyncio.shield(self.store.mark_ambiguous(operation))
            except Exception as store_exc:
                raise _ambiguous_error(operation) from store_exc
            raise _ambiguous_error(operation) from exc

        try:
            completed = await self.store.complete(operation, copied)
        except Exception as store_exc:
            # The seller returned after the mutation, but durable result
            # persistence failed. Best-effort mark AMBIGUOUS; even if that
            # write also fails, surface a typed fail-closed error rather than
            # leaking the backend exception or inviting a blind replay.
            try:
                await asyncio.shield(self.store.mark_ambiguous(operation))
            except Exception:
                pass
            raise _ambiguous_error(operation) from store_exc
        assert completed.result is not None
        return copy.deepcopy(completed.result)

    async def _reconcile(
        self,
        execution: LegacyPurchaseExecution,
        operation: CompatibilityPurchaseOperation,
    ) -> CompatibilityPurchaseOperation:
        if self.reconciler is None:
            raise _ambiguous_error(operation)
        try:
            outcome = await _maybe_await(self.reconciler(execution, operation))
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
                return await self.store.complete(operation, outcome.result)
            if outcome.status == ReconciliationStatus.NOT_APPLIED:
                # IN_FLIGHT may still have a live executor in another worker. An
                # instantaneous seller lookup can report "not applied" immediately
                # before that executor commits, so reopening it would permit two
                # calls. Only the exception/cancellation path's durably AMBIGUOUS
                # state proves this coordinator no longer has a live owner.
                if operation.state != CompatibilityOperationState.AMBIGUOUS:
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
            payload["accepted_losses"], source_adcp_version=record.source_adcp_version
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
        observed_pricing = _observed_pricing_options(record.observed_response)
        for package in packages:
            if not isinstance(package, Mapping) or not isinstance(package.get("product_id"), str):
                raise _legacy_request_error("every package must carry a product_id")
            product_id = package["product_id"]
            package_ids.append(product_id)
            pricing_option_id = package.get("pricing_option_id")
            if not isinstance(
                pricing_option_id, str
            ) or pricing_option_id not in observed_pricing.get(product_id, frozenset()):
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
        model = (
            value
            if isinstance(value, CompatibilityPurchaseCoordinatorInput)
            else CompatibilityPurchaseCoordinatorInput.model_validate(value)
        )
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


def _validate_source_version(version: str) -> None:
    if not isinstance(version, str) or _SOURCE_VERSION_RE.fullmatch(version) is None:
        raise _invalid("source_adcp_version must be an exact 2.5.x, 3.0.x, or 3.1.x release")
    bundled_version = get_bundle_adcp_version(version=version)
    if bundled_version != version:
        raise _invalid(
            "source_adcp_version must exactly match the bundled source schema release "
            f"(requested {version!r}, bundled {bundled_version!r})"
        )


def _validate_loss_set(values: Any, *, source_adcp_version: str) -> frozenset[str]:
    if not isinstance(values, (list, tuple, frozenset)):
        raise _invalid("losses must be an array")
    raw = [str(value) for value in values]
    if len(raw) != len(set(raw)):
        raise _invalid("losses must not contain duplicates")
    result = frozenset(raw)
    if not _REQUIRED_LOSSES.issubset(result) or not result.issubset(_ALLOWED_LOSSES):
        raise _invalid("losses must contain both atomicity losses and no unknown values")
    if source_adcp_version.startswith("2.5.") and _MUTATION_LOSS not in result:
        raise _invalid("AdCP 2.5 continuations require the mutation idempotency loss")
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
    if len(observed) != len(set(observed)) or set(observed) != set(expected):
        raise _invalid("product_ids must exactly match the complete observed product set")


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
        model = (
            value if isinstance(value, AccountReference) else AccountReference.model_validate(value)
        )
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


def _json_copy(value: Mapping[str, Any]) -> JsonObject:
    payload = copy.deepcopy(dict(value))
    # RFC 8785 both validates the JSON domain and gives deterministic handling
    # of numbers; copy.deepcopy alone would retain arbitrary Python objects.
    try:
        rfc8785.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise _invalid("stored compatibility payload must contain JSON values only") from exc
    return payload


def _result_payload(value: LegacyPurchaseResult) -> JsonObject:
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        if not isinstance(dumped, dict):
            raise TypeError("legacy purchase result model must serialize to an object")
        return _json_copy(dumped)
    if isinstance(value, Mapping):
        return _json_copy(value)
    raise TypeError("legacy purchase executor must return a mapping or Pydantic model")


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


def _copy_continuation(value: LegacyPurchaseContinuation) -> LegacyPurchaseContinuation:
    return replace(
        value,
        observed_request=copy.deepcopy(value.observed_request),
        observed_response=copy.deepcopy(value.observed_response),
        listed_purchase_context=copy.deepcopy(value.listed_purchase_context),
    )


def _copy_operation(value: CompatibilityPurchaseOperation) -> CompatibilityPurchaseOperation:
    return replace(value, result=copy.deepcopy(value.result))


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


def _legacy_request_error(message: str) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.INVALID_LEGACY_REQUEST,
        message,
        "Construct an explicit-package request using the exact source-version schema.",
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
    "LegacyPurchaseReconciler",
    "LegacyPurchaseResult",
    "ReconciliationResult",
    "ReconciliationStatus",
    "canonical_account_identity",
]
