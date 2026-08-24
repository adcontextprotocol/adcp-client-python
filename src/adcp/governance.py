"""Cross-role campaign-governance helpers for AdCP 3.2.

Governance agents authorize one exact downstream request with a compact JWS.
The helpers in this module keep policy interpretation at the governance agent:
services verify only the signature, audience, caller, task, payload, monetary
ceiling, freshness, revocation, and replay bindings before performing a side
effect.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias, cast
from urllib.parse import urlsplit

import rfc8785
from pydantic import BaseModel

from adcp._version import resolve_adcp_version
from adcp.signing.crypto import (
    ALG_ED25519,
    ALG_ES256,
    b64url_decode,
    b64url_encode,
    public_key_from_jwk,
    sign_signature_base,
    verify_signature,
)
from adcp.signing.jwks import AsyncJwksResolver, JwksResolver
from adcp.signing.replay import ReplayStore, supports_atomic_claim
from adcp.types import CheckGovernanceRequest, ReportPlanOutcomeRequest

GOVERNANCE_AUTHORIZATION_TYP = "adcp-gov+jws"
GOVERNANCE_AUTHORIZATION_CRITICAL_CLAIMS = (
    "authorized_commitment",
    "authorized_task",
    "authorized_payload_hash",
)
GOVERNANCE_MAX_TOKEN_BYTES = 4096
GOVERNANCE_MAX_OFFLINE_LIFETIME_SECONDS = 15 * 60
GOVERNANCE_MAX_EXECUTION_LIFETIME_SECONDS = 30 * 24 * 60 * 60

GovernanceEnforcementMode = Literal["signed_context", "online_execution_check"]
GovernancePhase = Literal["intent", "purchase", "modification", "delivery"]
GovernanceAuthorizationErrorCode = Literal[
    "governance_token_invalid",
    "governance_key_unknown",
    "governance_token_expired",
    "governance_token_not_yet_valid",
    "governance_token_not_applicable",
    "governance_token_replayed",
    "governance_token_revoked",
]
GovernanceReplayResult = Literal["ok", "conflict", "replayed", "rate_abuse"]

_COMPACT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class GovernanceCommitment:
    """A non-negative monetary ceiling or actual commitment."""

    amount: float
    currency: str


@dataclass(frozen=True)
class GovernanceEnforcementTask:
    task: str
    modes: tuple[GovernanceEnforcementMode, ...]


@dataclass(frozen=True)
class NormalizedGovernanceApproved:
    verdict: Literal["approved"]
    check_id: str
    check_type: Literal["intent", "execution", "legacy"]
    explanation: str
    raw: object
    governance_context: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class NormalizedGovernanceDenied:
    verdict: Literal["denied"]
    check_id: str
    check_type: Literal["intent", "execution", "legacy"]
    explanation: str
    raw: object


@dataclass(frozen=True)
class NormalizedGovernanceConditions:
    verdict: Literal["conditions"]
    check_id: str
    check_type: Literal["intent", "legacy"]
    explanation: str
    raw: object
    governance_context: str | None = None
    consultation_context: str | None = None


NormalizedGovernanceVerdict: TypeAlias = (
    NormalizedGovernanceApproved | NormalizedGovernanceDenied | NormalizedGovernanceConditions
)


@dataclass(frozen=True)
class GovernanceReplayBinding:
    caller: str
    task: str
    payload_hash: str
    idempotency_key: str | None = None


class GovernanceReplayStore(Protocol):
    """Atomically consume one ``(issuer, audience, jti)`` tuple."""

    async def consume(
        self,
        issuer: str,
        audience: str,
        jti: str,
        expires_at: float,
        now: float,
        binding: GovernanceReplayBinding | None = None,
    ) -> GovernanceReplayResult: ...


def _is_same_idempotent_replay(
    previous: GovernanceReplayBinding | None,
    current: GovernanceReplayBinding | None,
) -> bool:
    """Return whether two claims are the same explicitly idempotent operation."""
    return (
        previous is not None
        and current is not None
        and bool(previous.idempotency_key)
        and previous == current
    )


class InMemoryGovernanceReplayStore:
    """Process-local replay store for tests and single-process services."""

    def __init__(self, *, max_entries: int = 100_000) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise TypeError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._scopes: dict[
            tuple[str, str], dict[str, tuple[float, GovernanceReplayBinding | None]]
        ] = {}
        self._lock = threading.RLock()

    def preload(self, issuer: str, audience: str, jti: str, expires_at: float) -> None:
        """Deterministically preload a consumed token for conformance tests."""
        with self._lock:
            self._scopes.setdefault((issuer, audience), {})[jti] = (expires_at, None)

    async def consume(
        self,
        issuer: str,
        audience: str,
        jti: str,
        expires_at: float,
        now: float,
        binding: GovernanceReplayBinding | None = None,
    ) -> GovernanceReplayResult:
        with self._lock:
            scope = self._scopes.setdefault((issuer, audience), {})
            prior = scope.get(jti)
            if prior is not None and prior[0] > now:
                if _is_same_idempotent_replay(prior[1], binding):
                    return "ok"
                return "conflict" if prior[1] is not None else "replayed"
            if prior is not None:
                del scope[jti]
            if len(scope) >= self._max_entries:
                expired = [key for key, (expiry, _) in scope.items() if expiry <= now]
                for key in expired:
                    del scope[key]
                if len(scope) >= self._max_entries:
                    return "rate_abuse"
            scope[jti] = (expires_at, binding)
            return "ok"


class GovernanceReplayStoreAdapter:
    """Reuse an atomic request-signing replay backend in one service process.

    Binding comparisons are retained in this adapter process. Multi-replica
    services must instead provide a shared :class:`GovernanceReplayStore` that
    atomically persists both the JTI and its idempotency binding.
    """

    def __init__(self, replay_store: ReplayStore) -> None:
        if not supports_atomic_claim(replay_store):
            raise TypeError("governance replay protection requires an atomic replay store")
        self._store = replay_store
        self._bindings: dict[tuple[str, str, str], tuple[float, GovernanceReplayBinding | None]] = (
            {}
        )
        self._binding_lock = threading.RLock()
        self._claims_since_sweep = 0

    async def consume(
        self,
        issuer: str,
        audience: str,
        jti: str,
        expires_at: float,
        now: float,
        binding: GovernanceReplayBinding | None = None,
    ) -> GovernanceReplayResult:
        assert supports_atomic_claim(self._store)
        key = (issuer, audience, jti)
        with self._binding_lock:
            self._claims_since_sweep += 1
            if self._claims_since_sweep >= 64:
                self._bindings = {
                    stored_key: stored
                    for stored_key, stored in self._bindings.items()
                    if stored[0] > now
                }
                self._claims_since_sweep = 0

            prior = self._bindings.get(key)
            if prior is not None and prior[0] <= now:
                del self._bindings[key]
                prior = None
            if prior is not None:
                if _is_same_idempotent_replay(prior[1], binding):
                    return "ok"
                return "conflict" if prior[1] is not None else "replayed"

            result = self._store.claim(f"{issuer}^_{audience}", jti, max(0.0, expires_at - now))
            if result == "claimed":
                self._bindings[key] = (expires_at, binding)
                return "ok"
            if result == "capacity":
                return "rate_abuse"
            # A nonce consumed outside this adapter has no trustworthy binding.
            return "replayed"


@dataclass(frozen=True)
class GovernanceRevocationStatus:
    issuer: str
    key_revoked: bool
    jti_revoked: bool
    next_update: float


class GovernanceRevocationResolver(Protocol):
    def resolve(
        self, issuer: str, kid: str, jti: str
    ) -> GovernanceRevocationStatus | Awaitable[GovernanceRevocationStatus]: ...


@dataclass(frozen=True)
class GovernanceAuthorizationSuccess:
    ok: Literal[True]
    claims: Mapping[str, Any]
    protected_header: Mapping[str, Any]
    payload_hash: str


@dataclass(frozen=True)
class GovernanceAuthorizationFailure:
    ok: Literal[False]
    error: GovernanceAuthorizationErrorCode
    message: str


GovernanceAuthorizationResult: TypeAlias = (
    GovernanceAuthorizationSuccess | GovernanceAuthorizationFailure
)


class GovernanceAuthorizationError(Exception):
    """Raised by enforcement hooks when a governance token is rejected."""

    def __init__(self, failure: GovernanceAuthorizationFailure) -> None:
        super().__init__(failure.message)
        self.code = failure.error


def build_governance_commitment(amount: float, currency: str) -> GovernanceCommitment:
    """Validate and build a task-neutral monetary commitment."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise TypeError("governance commitment amount must be a number")
    normalized_amount = float(amount)
    if not math.isfinite(normalized_amount) or normalized_amount < 0:
        raise TypeError("governance commitment amount must be finite and non-negative")
    if not isinstance(currency, str) or _CURRENCY_RE.fullmatch(currency) is None:
        raise TypeError("governance commitment currency must be an ISO 4217 uppercase code")
    return GovernanceCommitment(normalized_amount, currency)


build_governance_proposed_commitment = build_governance_commitment
build_governance_execution_commitment = build_governance_commitment


_EXPLICIT_PROPOSED_COMMITMENT_TASKS = frozenset(
    {
        "update_media_buy",
        "buy_products",
        "accept_proposal",
        "control_media_buy",
        "acquire_rights",
        "update_rights",
        "activate_signal",
        "build_creative",
    }
)
_GOVERNANCE_ENFORCEMENT_TASKS = _EXPLICIT_PROPOSED_COMMITMENT_TASKS | {"create_media_buy"}
_ONLINE_EXECUTION_TASKS = frozenset(
    {
        "create_media_buy",
        "update_media_buy",
        "buy_products",
        "accept_proposal",
        "control_media_buy",
    }
)


def governance_task_requires_proposed_commitment(task: str) -> bool:
    return task in _EXPLICIT_PROPOSED_COMMITMENT_TASKS


def governance_purchase_type_for_task(task: str) -> str | None:
    if task in {
        "create_media_buy",
        "update_media_buy",
        "buy_products",
        "accept_proposal",
        "control_media_buy",
    }:
        return "media_buy"
    if task == "activate_signal":
        return "signal_activation"
    if task in {"acquire_rights", "update_rights"}:
        return "rights_license"
    if task == "build_creative":
        return "creative_services"
    return None


def build_governance_intent_request(
    *,
    plan_id: str,
    caller: str,
    target_agent: str,
    tool: str,
    payload: Mapping[str, Any],
    purchase_type: str | None = None,
    proposed_commitment: GovernanceCommitment | Mapping[str, Any] | None = None,
    consultation_context: str | None = None,
    proposal: object | None = None,
    runtime_attestations: object | None = None,
    invoice_recipient: object | None = None,
) -> CheckGovernanceRequest:
    """Build an intent-shaped check without leaking authorization context."""
    if not all(isinstance(value, str) and value for value in (plan_id, caller, target_agent, tool)):
        raise TypeError("governance intent requires plan_id, caller, target_agent, and tool")
    if governance_task_requires_proposed_commitment(tool) and proposed_commitment is None:
        raise TypeError(f"{tool} governance intent requires proposed_commitment")
    if tool == "accept_proposal" and proposal is None:
        raise TypeError("accept_proposal governance intent requires proposal")
    data: dict[str, Any] = {
        "adcp_version": resolve_adcp_version(None),
        "plan_id": plan_id,
        "caller": caller,
        "target_agent": target_agent,
        "tool": tool,
        "payload": copy.deepcopy(dict(payload)),
    }
    inferred_purchase_type = purchase_type or governance_purchase_type_for_task(tool)
    if inferred_purchase_type is not None:
        data["purchase_type"] = inferred_purchase_type
    if proposed_commitment is not None:
        data["proposed_commitment"] = _commitment_dict(proposed_commitment)
    for key, value in (
        ("consultation_context", consultation_context),
        ("proposal", proposal),
        ("runtime_attestations", runtime_attestations),
        ("invoice_recipient", invoice_recipient),
    ):
        if value is not None:
            data[key] = _wire_copy(value)
    request = CheckGovernanceRequest.model_validate(data)
    validate_governance_request(request)
    return request


def build_governance_execution_request(
    *,
    caller: str,
    governance_context: str,
    planned_delivery: object,
    phase: str = "purchase",
    execution_commitment: GovernanceCommitment | Mapping[str, Any] | None = None,
    delivery_metrics: object | None = None,
    modification_summary: str | None = None,
) -> CheckGovernanceRequest:
    """Build an execution check without exposing a buyer plan or intent payload."""
    if not caller or not governance_context:
        raise TypeError("governance execution requires caller and governance_context")
    delivery = _wire_copy(planned_delivery)
    if not isinstance(delivery, Mapping):
        raise TypeError("planned_delivery must be a model or mapping")
    if phase in {"modification", "delivery"} and not delivery.get("media_buy_id"):
        raise TypeError(f"governance {phase} execution requires planned_delivery.media_buy_id")
    if phase == "modification" and execution_commitment is None:
        raise TypeError("governance modification execution requires execution_commitment")
    if phase == "delivery" and delivery_metrics is None:
        raise TypeError("governance delivery execution requires delivery_metrics")
    data: dict[str, Any] = {
        "adcp_version": resolve_adcp_version(None),
        "caller": caller,
        "governance_context": governance_context,
        "planned_delivery": delivery,
        "phase": phase,
    }
    if execution_commitment is not None:
        data["execution_commitment"] = _commitment_dict(execution_commitment)
    if delivery_metrics is not None:
        data["delivery_metrics"] = _wire_copy(delivery_metrics)
    if modification_summary is not None:
        data["modification_summary"] = modification_summary
    request = CheckGovernanceRequest.model_validate(data)
    validate_governance_request(request)
    return request


def build_governance_outcome_request(
    *,
    plan_id: str,
    check_id: str,
    idempotency_key: str,
    outcome: Literal["completed", "failed", "delivery"],
    governance_context: str,
    purchase_type: str | None = None,
    seller_response: object | None = None,
    error: object | None = None,
    delivery: object | None = None,
) -> ReportPlanOutcomeRequest:
    """Build a terminal or delivery outcome bound to an approved check."""
    if not all(
        isinstance(value, str) and value
        for value in (plan_id, check_id, idempotency_key, governance_context)
    ):
        raise TypeError(
            "governance outcome requires plan_id, check_id, idempotency_key, and governance_context"
        )
    required_value = {
        "completed": seller_response,
        "failed": error,
        "delivery": delivery,
    }[outcome]
    if required_value is None:
        raise TypeError(f"governance {outcome} outcome requires its {outcome} payload")
    populated = sum(value is not None for value in (seller_response, error, delivery))
    if populated != 1:
        raise TypeError("governance outcome must populate exactly one outcome payload")
    data: dict[str, Any] = {
        "adcp_version": resolve_adcp_version(None),
        "plan_id": plan_id,
        "check_id": check_id,
        "idempotency_key": idempotency_key,
        "outcome": outcome,
        "governance_context": governance_context,
    }
    if purchase_type is not None:
        data["purchase_type"] = purchase_type
    if seller_response is not None:
        data["seller_response"] = _wire_copy(seller_response)
    if error is not None:
        data["error"] = _wire_copy(error)
    if delivery is not None:
        data["delivery"] = _wire_copy(delivery)
    request = ReportPlanOutcomeRequest.model_validate(data)
    validate_governance_outcome_request(request)
    return request


def normalize_governance_verdict(response: object) -> NormalizedGovernanceVerdict | None:
    """Normalize modern ``verdict`` and legacy ``status`` responses.

    Invalid modern field combinations fail closed. In particular, conditions
    and denied verdicts can never leak an authorization token.
    """
    raw = _record(response, exclude_unset=True)
    if raw is None:
        return None
    if "runtime_attestation_evaluations" in raw and not isinstance(
        raw.get("runtime_attestation_binding_digest"), str
    ):
        return None
    raw_check_type = raw.get("check_type")
    if "check_type" in raw and raw_check_type not in {"intent", "execution"}:
        return None
    check_type: Literal["intent", "execution", "legacy"] = (
        raw_check_type if raw_check_type in {"intent", "execution"} else "legacy"
    )
    verdict = raw.get("verdict")
    if check_type == "legacy" and verdict not in {"approved", "denied", "conditions"}:
        verdict = raw.get("status")
    if verdict not in {"approved", "denied", "conditions"}:
        return None
    check_id = raw.get("check_id")
    explanation = raw.get("explanation")
    if not isinstance(check_id, str) or not isinstance(explanation, str):
        return None
    governance_context = raw.get("governance_context")
    consultation_context = raw.get("consultation_context")
    expires_at = raw.get("expires_at")
    if check_type != "legacy":
        if verdict == "approved" and (
            not isinstance(governance_context, str) or not _as_datetime_string(expires_at)
        ):
            return None
        if verdict == "conditions":
            if (
                check_type != "intent"
                or not isinstance(consultation_context, str)
                or "governance_context" in raw
                or "expires_at" in raw
                or not isinstance(raw.get("conditions"), list)
                or not raw["conditions"]
            ):
                return None
        if verdict != "conditions" and ("conditions" in raw or "consultation_context" in raw):
            return None
        if verdict != "approved" and ("governance_context" in raw or "expires_at" in raw):
            return None
        if verdict == "denied" and (
            not isinstance(raw.get("findings"), list) or not raw["findings"]
        ):
            return None
    if verdict == "approved":
        return NormalizedGovernanceApproved(
            "approved",
            check_id,
            check_type,
            explanation,
            response,
            governance_context if isinstance(governance_context, str) else None,
            _as_datetime_string(expires_at),
        )
    if verdict == "conditions":
        return NormalizedGovernanceConditions(
            "conditions",
            check_id,
            "intent" if check_type == "intent" else "legacy",
            explanation,
            response,
            governance_context if isinstance(governance_context, str) else None,
            consultation_context if isinstance(consultation_context, str) else None,
        )
    return NormalizedGovernanceDenied("denied", check_id, check_type, explanation, response)


def is_governance_approved(response: object) -> bool:
    normalized = normalize_governance_verdict(response)
    return normalized is not None and normalized.verdict == "approved"


def is_governance_denied(response: object) -> bool:
    normalized = normalize_governance_verdict(response)
    return normalized is not None and normalized.verdict == "denied"


def is_governance_conditions(response: object) -> bool:
    normalized = normalize_governance_verdict(response)
    return normalized is not None and normalized.verdict == "conditions"


def validate_governance_verdict(
    response: object,
    *,
    expected_check_type: Literal["intent", "execution"] | None = None,
) -> NormalizedGovernanceVerdict:
    """Return a normalized verdict or raise on an unsafe response shape."""
    normalized = normalize_governance_verdict(response)
    if normalized is None:
        raise ValueError("invalid check_governance verdict response")
    if expected_check_type is not None and normalized.check_type != expected_check_type:
        raise ValueError("check_governance response check_type does not match the request shape")
    return normalized


def validate_governance_request(
    request: CheckGovernanceRequest | Mapping[str, Any],
) -> Literal["intent", "execution"]:
    """Enforce the schema's conditional request invariants.

    The generated model preserves the fields but cannot represent every JSON
    Schema ``if``/``then`` and negative intersection. Server dispatch calls
    this before adopter code so malformed hybrid requests cannot reach policy
    or settlement logic.
    """
    raw = _record(request, exclude_unset=True)
    if raw is None:
        raise ValueError("invalid check_governance request")
    intent_keys = {
        "target_agent",
        "tool",
        "payload",
        "proposed_commitment",
        "proposal",
        "consultation_context",
        "runtime_attestations",
        "invoice_recipient",
    }
    execution_keys = {"planned_delivery", "delivery_metrics", "execution_commitment"}
    has_intent = any(key in raw for key in intent_keys)
    has_execution = any(key in raw for key in execution_keys)
    if has_intent and has_execution:
        raise ValueError("check_governance request cannot mix intent and execution fields")
    if has_intent:
        missing = [
            key for key in ("plan_id", "tool", "payload", "target_agent") if raw.get(key) is None
        ]
        if missing or "governance_context" in raw:
            raise ValueError("intent check requires plan_id, tool, payload, and target_agent only")
        tool = raw.get("tool")
        if governance_task_requires_proposed_commitment(cast(str, tool)) and (
            "proposed_commitment" not in raw
        ):
            raise ValueError(f"{tool} intent check requires proposed_commitment")
        if tool == "accept_proposal" and "proposal" not in raw:
            raise ValueError("accept_proposal intent check requires proposal")
        if "proposal" in raw and tool != "accept_proposal":
            raise ValueError("proposal is valid only for accept_proposal intent checks")
        if "consultation_context" in raw and "governance_context" in raw:
            raise ValueError("consultation context is non-authorizing")
        if "runtime_attestations" in raw:
            payload = raw.get("payload")
            if (
                raw.get("purchase_type") != "signal_activation"
                or tool != "activate_signal"
                or not isinstance(payload, Mapping)
                or payload.get("action", "activate") != "activate"
            ):
                raise ValueError(
                    "runtime_attestations require an activate_signal activation intent"
                )
        return "intent"
    if has_execution:
        if raw.get("governance_context") is None or any(key in raw for key in intent_keys):
            raise ValueError("execution check requires governance_context and no intent payload")
        if "planned_delivery" not in raw:
            raise ValueError("execution check requires planned_delivery")
        phase = raw.get("phase", "purchase")
        delivery = raw.get("planned_delivery")
        if isinstance(delivery, BaseModel):
            delivery = delivery.model_dump(mode="json", exclude_unset=True)
        if not isinstance(delivery, Mapping):
            raise ValueError("execution check requires planned_delivery")
        if phase in {"modification", "delivery"} and (not delivery.get("media_buy_id")):
            raise ValueError(f"{phase} execution check requires planned_delivery.media_buy_id")
        if delivery.get("total_budget") is not None and delivery.get("currency") is None:
            raise ValueError("planned_delivery.total_budget requires currency")
        if phase == "modification" and "execution_commitment" not in raw:
            raise ValueError("modification execution check requires execution_commitment")
        if phase == "delivery" and "delivery_metrics" not in raw:
            raise ValueError("delivery execution check requires delivery_metrics")
        if phase == "delivery":
            metrics = raw.get("delivery_metrics")
            if not isinstance(metrics, Mapping):
                raise ValueError("delivery execution check requires delivery_metrics")
            required_metrics = {
                "statement_id",
                "statement_digest",
                "sequence",
                "issued_at",
                "reporting_period",
                "cumulative_spend",
                "currency",
            }
            if any(metrics.get(key) is None for key in required_metrics):
                raise ValueError("delivery_metrics is missing its canonical statement binding")
        return "execution"
    raise ValueError("check_governance request must be an intent or execution check")


def validate_governance_outcome_request(
    request: ReportPlanOutcomeRequest | Mapping[str, Any],
    *,
    allow_legacy_delivery: bool = False,
    resolved_version: str | None = None,
) -> ReportPlanOutcomeRequest | Mapping[str, Any]:
    """Enforce outcome/body/check binding before governance adopter code."""
    raw = _record(request, exclude_unset=True)
    if raw is None:
        raise ValueError("invalid report_plan_outcome request")
    outcome = raw.get("outcome")
    if not isinstance(outcome, str):
        raise ValueError("invalid governance outcome")
    expected_body = {
        "completed": "seller_response",
        "failed": "error",
        "delivery": "delivery",
    }.get(outcome)
    if expected_body is None:
        raise ValueError("invalid governance outcome")
    version = raw.get("adcp_version") or resolved_version
    modern_version = (
        isinstance(version, str)
        and re.match(r"^3\.(?:[2-9]|\d{2,})(?:[.-]|$)", version) is not None
    )
    legacy_delivery = (
        allow_legacy_delivery
        and outcome == "delivery"
        and not modern_version
        and raw.get("check_id") is None
        and raw.get("governance_context") is not None
    )
    if not legacy_delivery and not all(
        key in raw and raw[key] is not None for key in ("check_id", "governance_context")
    ):
        raise ValueError("governance outcome requires check_id and governance_context")
    populated = [
        key for key in ("seller_response", "error", "delivery") if raw.get(key) is not None
    ]
    if populated != [expected_body]:
        raise ValueError(f"{outcome} outcome requires exactly the {expected_body} body")
    if outcome == "delivery":
        delivery = raw["delivery"]
        if (
            isinstance(delivery, Mapping)
            and delivery.get("source") == "seller_statement_copy"
            and (
                delivery.get("seller_statement_id") is None
                or delivery.get("seller_statement_digest") is None
            )
        ):
            raise ValueError(
                "seller_statement_copy requires seller_statement_id and seller_statement_digest"
            )
    return request


def governance_request_check_type(
    request: CheckGovernanceRequest | Mapping[str, Any],
    *,
    resolved_version: str | None = None,
) -> Literal["intent", "execution"] | None:
    """Identify a modern request shape without reclassifying legacy checks."""
    raw = _record(request, exclude_unset=True)
    if raw is None:
        return None
    version = raw.get("adcp_version") or resolved_version
    modern_version = (
        isinstance(version, str)
        and re.match(r"^3\.(?:[2-9]|\d{2,})(?:[.-]|$)", version) is not None
    )
    intent = raw.get("target_agent") is not None or bool(
        modern_version
        and any(
            key in raw
            for key in (
                "tool",
                "payload",
                "proposed_commitment",
                "proposal",
                "consultation_context",
                "runtime_attestations",
                "invoice_recipient",
            )
        )
    )
    execution = "execution_commitment" in raw or bool(
        modern_version
        and any(key in raw for key in ("planned_delivery", "phase", "delivery_metrics"))
    )
    if intent and execution:
        raise ValueError("check_governance request cannot mix intent and execution fields")
    if intent:
        return "intent"
    if execution:
        return "execution"
    if modern_version:
        raise ValueError("AdCP 3.2 governance request must select an intent or execution shape")
    return None


def get_governance_enforcement_tasks(capabilities: object) -> list[GovernanceEnforcementTask]:
    """Parse and validate the task-scoped enforcement declaration."""
    root = _record(capabilities)
    if root is None:
        return []
    adcp = root.get("adcp")
    if not isinstance(adcp, Mapping):
        return []
    enforcement = adcp.get("governance_enforcement")
    if enforcement is None:
        return []
    if not isinstance(enforcement, Mapping) or not isinstance(enforcement.get("tasks"), list):
        raise ValueError("invalid governance_enforcement capability declaration")
    if set(enforcement) != {"tasks"}:
        raise ValueError("governance_enforcement contains unsupported properties")
    if not enforcement["tasks"]:
        raise ValueError("governance_enforcement.tasks must not be empty")
    seen: set[str] = set()
    result: list[GovernanceEnforcementTask] = []
    for item in enforcement["tasks"]:
        item = _record(item)
        if item is None or not isinstance(item.get("task"), str) or not item["task"]:
            raise ValueError("invalid governance_enforcement task declaration")
        if set(item) != {"task", "modes"}:
            raise ValueError("governance_enforcement task contains unsupported properties")
        if not isinstance(item.get("modes"), list) or not item["modes"]:
            raise ValueError("governance_enforcement task modes must not be empty")
        task = item["task"]
        if task not in _GOVERNANCE_ENFORCEMENT_TASKS:
            raise ValueError(f"unsupported governance enforcement task: {task}")
        if task in seen:
            raise ValueError(f"duplicate governance_enforcement task declaration: {task}")
        seen.add(task)
        if len(set(item["modes"])) != len(item["modes"]):
            raise ValueError(f"duplicate governance enforcement mode for task: {task}")
        if any(mode not in {"signed_context", "online_execution_check"} for mode in item["modes"]):
            raise ValueError(f"invalid governance enforcement mode for task: {task}")
        if "signed_context" not in item["modes"]:
            raise ValueError(f"signed_context is required for task: {task}")
        if "online_execution_check" in item["modes"] and task not in _ONLINE_EXECUTION_TASKS:
            raise ValueError(f"online execution checking is not defined for task: {task}")
        modes = cast(
            tuple[GovernanceEnforcementMode, ...],
            tuple(dict.fromkeys(item["modes"])),
        )
        result.append(GovernanceEnforcementTask(task, modes))
    return result


def target_declares_governance_enforcement(
    capabilities: object,
    task: str,
    mode: GovernanceEnforcementMode = "signed_context",
) -> bool:
    root = _record(capabilities)
    if root is None:
        return False
    features = root.get("experimental_features")
    if not isinstance(features, list) or "governance.campaign" not in features:
        return False
    return any(
        declaration.task == task and mode in declaration.modes
        for declaration in get_governance_enforcement_tasks(capabilities)
    )


def target_declares_legacy_governance_awareness(capabilities: object, task: str) -> bool:
    if task != "create_media_buy":
        return False
    root = _record(capabilities)
    media_buy = root.get("media_buy") if root is not None else None
    return isinstance(media_buy, Mapping) and media_buy.get("governance_aware") is True


_CONDITIONAL_OPERATIONAL_KEYS = frozenset(
    {
        "account",
        "adcp_major_version",
        "adcp_version",
        "context",
        "governance_context",
        "idempotency_key",
        "media_buy_id",
        "push_notification_config",
        "revision",
        "rights_id",
    }
)


def stateless_governance_applicability(task: str, payload: Mapping[str, Any]) -> bool | None:
    """Return a stateless trigger/exemption answer, or ``None`` if state is needed."""
    if task == "activate_signal":
        return payload.get("action") != "deactivate"
    if task == "build_creative" and payload.get("mode") == "estimate":
        return False
    if task == "update_rights":
        if payload.get("paused") is False:
            return True
        if payload.get("paused") is True and _has_only_operational_and(payload, {"paused"}):
            return False
        return None
    if task in {"update_media_buy", "control_media_buy"}:
        if payload.get("paused") is False:
            return True
        if payload.get("canceled") is True and _has_only_operational_and(
            payload, {"canceled", "cancellation_reason"}
        ):
            return False
        if payload.get("paused") is True and _has_only_operational_and(payload, {"paused"}):
            return False
        return None
    return None


def compute_governed_payload_hash(payload: Mapping[str, Any]) -> str:
    """Hash RFC 8785 JCS after removing the two excluded top-level fields."""
    business_payload = {
        key: value for key, value in payload.items() if key not in {"governance_context", "context"}
    }
    return b64url_encode(hashlib.sha256(rfc8785.dumps(business_payload)).digest())


def compute_governance_outcome_hash(payload: Mapping[str, Any]) -> str:
    """Return the exact outcome replay identity, excluding retry metadata."""
    replay_payload = {
        key: value for key, value in payload.items() if key not in {"idempotency_key", "context"}
    }
    return b64url_encode(hashlib.sha256(rfc8785.dumps(replay_payload)).digest())


def read_governance_authorization_issuer(token: object) -> str | None:
    """Read the untrusted ``iss`` claim for authenticated trust resolution.

    This does not verify the token. Services use the returned value only to
    locate a matching governance-typed entry in the already authenticated
    buyer's brand configuration before passing its JWKS to the verifier.
    """
    if not isinstance(token, str) or not token or len(token) > GOVERNANCE_MAX_TOKEN_BYTES:
        return None
    parts = token.split(".")
    if len(parts) != 3 or any(_COMPACT_SEGMENT_RE.fullmatch(part) is None for part in parts):
        return None
    try:
        claims = _decode_json_segment(parts[1])
    except (TypeError, ValueError):
        return None
    issuer = claims.get("iss")
    return issuer if isinstance(issuer, str) and issuer.startswith("https://") else None


def issue_governance_authorization(
    *,
    private_key: Any,
    key_id: str,
    alg: Literal["EdDSA", "ES256"],
    issuer: str,
    subject: str,
    plan_hash: str,
    audience: str,
    caller: str,
    check_id: str,
    task: str,
    payload: Mapping[str, Any],
    authorized_commitment: GovernanceCommitment | Mapping[str, Any],
    issued_at: int | None = None,
    not_before: int | None = None,
    expires_at: int | None = None,
    jti: str | None = None,
    phase: GovernancePhase = "intent",
    media_buy_id: str | None = None,
    extra_claims: Mapping[str, Any] | None = None,
) -> str:
    """Issue a compact JWS for one exact governed downstream request."""
    for name, value in (
        ("key_id", key_id),
        ("issuer", issuer),
        ("subject", subject),
        ("plan_hash", plan_hash),
        ("audience", audience),
        ("caller", caller),
        ("check_id", check_id),
        ("task", task),
        ("phase", phase),
    ):
        if not isinstance(value, str) or not value:
            raise TypeError(f"{name} must be a non-empty string")
    if phase not in {"intent", "purchase", "modification", "delivery"}:
        raise ValueError("unsupported governance authorization phase")
    if phase == "intent" and media_buy_id is not None:
        raise ValueError("intent authorization cannot carry media_buy_id")
    if phase in {"modification", "delivery"} and not media_buy_id:
        raise ValueError(f"{phase} authorization requires media_buy_id")
    now = int(time.time()) if issued_at is None else issued_at
    nbf = now if not_before is None else not_before
    exp = now + GOVERNANCE_MAX_OFFLINE_LIFETIME_SECONDS if expires_at is None else expires_at
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (now, nbf, exp)):
        raise TypeError("governance token timestamps must be integer epoch seconds")
    max_lifetime = (
        GOVERNANCE_MAX_OFFLINE_LIFETIME_SECONDS
        if phase == "intent"
        else GOVERNANCE_MAX_EXECUTION_LIFETIME_SECONDS
    )
    if exp <= now or exp - now > max_lifetime:
        raise ValueError(
            f"{phase} governance token lifetime must be in (0, {max_lifetime}] seconds"
        )
    commitment = _commitment_dict(authorized_commitment)
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "plan_hash": plan_hash,
        "aud": audience,
        "iat": now,
        "nbf": nbf,
        "exp": exp,
        "jti": jti or secrets.token_urlsafe(24),
        "phase": phase,
        "caller": caller,
        "check_id": check_id,
        "authorized_commitment": commitment,
        "authorized_task": task,
        "authorized_payload_hash": compute_governed_payload_hash(payload),
    }
    if media_buy_id is not None:
        claims["media_buy_id"] = media_buy_id
    if extra_claims:
        overlap = (set(claims) | {"media_buy_id"}) & set(extra_claims)
        if overlap:
            raise ValueError(f"extra_claims cannot replace reserved claims: {sorted(overlap)}")
        claims.update(copy.deepcopy(dict(extra_claims)))
    header: dict[str, Any] = {
        "alg": alg,
        "kid": key_id,
        "typ": GOVERNANCE_AUTHORIZATION_TYP,
        "crit": list(GOVERNANCE_AUTHORIZATION_CRITICAL_CLAIMS),
        **{name: True for name in GOVERNANCE_AUTHORIZATION_CRITICAL_CLAIMS},
    }
    encoded_header = b64url_encode(_compact_json(header))
    encoded_claims = b64url_encode(_compact_json(claims))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    internal_alg = ALG_ED25519 if alg == "EdDSA" else ALG_ES256
    signature = sign_signature_base(
        alg=internal_alg, private_key=private_key, signature_base=signing_input
    )
    return f"{encoded_header}.{encoded_claims}.{b64url_encode(signature)}"


async def verify_governance_authorization(
    *,
    token: object,
    expected_issuer: str,
    expected_audience: str,
    authenticated_caller: str,
    expected_task: str,
    payload: Mapping[str, Any],
    actual_commitment: GovernanceCommitment | Mapping[str, Any],
    jwks: JwksResolver | AsyncJwksResolver | object,
    replay_store: GovernanceReplayStore,
    expected_phase: GovernancePhase = "intent",
    expected_subject: str | None = None,
    expected_media_buy_id: str | None = None,
    revocation_checker: Callable[[str], bool | Awaitable[bool]] | None = None,
    is_jti_revoked: Callable[[str, str], bool | Awaitable[bool]] | None = None,
    revocation_resolver: GovernanceRevocationResolver | None = None,
    now: Callable[[], float] | None = None,
    clock_skew_seconds: float = 60,
) -> GovernanceAuthorizationResult:
    """Verify the published compact-JWS profile and all service bindings."""

    def reject(
        code: GovernanceAuthorizationErrorCode, message: str
    ) -> GovernanceAuthorizationFailure:
        return GovernanceAuthorizationFailure(False, code, message)

    if not authenticated_caller:
        return reject("governance_token_invalid", "authenticated caller is required")
    try:
        actual = _commitment_dict(actual_commitment)
    except (TypeError, ValueError) as exc:
        return reject("governance_token_invalid", str(exc))
    if not isinstance(token, str) or not token or len(token) > GOVERNANCE_MAX_TOKEN_BYTES:
        return reject("governance_token_invalid", "governance_context is missing or too large")
    parts = token.split(".")
    if len(parts) != 3 or any(_COMPACT_SEGMENT_RE.fullmatch(part) is None for part in parts):
        return reject("governance_token_invalid", "governance_context is not a compact JWS")
    try:
        header = _decode_json_segment(parts[0])
        claims = _decode_json_segment(parts[1])
        signature = b64url_decode(parts[2])
        if b64url_encode(signature) != parts[2]:
            raise ValueError("non-canonical signature base64url")
    except (TypeError, ValueError):
        return reject("governance_token_invalid", "governance_context contains invalid JSON")
    alg = header.get("alg")
    if alg not in {"EdDSA", "ES256"} or header.get("typ") != GOVERNANCE_AUTHORIZATION_TYP:
        return reject("governance_token_invalid", "unsupported governance JWS alg or typ")
    critical = header.get("crit")
    if (
        not isinstance(critical, list)
        or any(not isinstance(name, str) for name in critical)
        or len(set(critical)) != len(critical)
        or any(name not in GOVERNANCE_AUTHORIZATION_CRITICAL_CLAIMS for name in critical)
    ):
        return reject("governance_token_invalid", "invalid governance JWS crit header")
    for name in GOVERNANCE_AUTHORIZATION_CRITICAL_CLAIMS:
        has_claim = name in claims
        has_marker = name in critical and header.get(name) is True
        if has_claim != has_marker:
            return reject(
                "governance_token_invalid", f"{name} claim and critical marker must match"
            )
    if ("authorized_task" in claims) != ("authorized_payload_hash" in claims):
        return reject(
            "governance_token_invalid",
            "authorized_task and authorized_payload_hash must appear together",
        )
    if claims.get("iss") != expected_issuer:
        return reject("governance_token_invalid", "governance token issuer mismatch")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        return reject("governance_key_unknown", "governance signing kid is missing")
    try:
        jwk = await _resolve_jwk(jwks, kid)
    except Exception:
        return reject("governance_key_unknown", "governance signing key could not be resolved")
    if (
        not isinstance(jwk, Mapping)
        or jwk.get("kid") != kid
        or jwk.get("adcp_use") != "governance-signing"
        or jwk.get("use") != "sig"
        or not isinstance(jwk.get("key_ops"), list)
        or "verify" not in jwk["key_ops"]
        or (jwk.get("alg") is not None and jwk.get("alg") != alg)
    ):
        return reject("governance_key_unknown", "governance signing key is not authorized")
    try:
        verified = verify_signature(
            alg=ALG_ED25519 if alg == "EdDSA" else ALG_ES256,
            public_key=public_key_from_jwk(dict(jwk)),
            signature_base=f"{parts[0]}.{parts[1]}".encode("ascii"),
            signature=signature,
        )
    except (TypeError, ValueError):
        verified = False
    if not verified:
        return reject("governance_token_invalid", "governance token signature verification failed")
    try:
        if revocation_checker is not None and await _maybe_await(revocation_checker(kid)):
            return reject("governance_token_revoked", "governance signing key is revoked")
        if (
            is_jti_revoked is not None
            and isinstance(claims.get("jti"), str)
            and await _maybe_await(is_jti_revoked(expected_issuer, claims["jti"]))
        ):
            return reject("governance_token_revoked", "governance token is revoked")
    except Exception:
        return reject(
            "governance_token_revoked", "governance revocation status could not be established"
        )
    audience = claims.get("aud")
    if not isinstance(audience, str):
        return reject("governance_token_invalid", "governance token audience is missing")
    if audience != expected_audience:
        return reject("governance_token_not_applicable", "governance token audience mismatch")
    caller = claims.get("caller")
    if not isinstance(caller, str) or not caller:
        return reject("governance_token_invalid", "governance token caller is missing")
    if caller != authenticated_caller:
        return reject("governance_token_not_applicable", "governance token caller mismatch")
    for name in ("sub", "plan_hash", "check_id"):
        if not isinstance(claims.get(name), str) or not claims[name]:
            return reject("governance_token_invalid", f"governance token {name} is missing")
    if expected_phase != "intent" and not expected_subject:
        return reject(
            "governance_token_invalid",
            "execution token verification requires the prior opaque subject binding",
        )
    if expected_subject is not None and claims["sub"] != expected_subject:
        return reject("governance_token_not_applicable", "governance token subject mismatch")
    if not isinstance(claims.get("phase"), str):
        return reject("governance_token_invalid", "governance token phase is missing")
    if claims["phase"] != expected_phase:
        return reject("governance_token_not_applicable", "governance token phase mismatch")
    media_buy_id = claims.get("media_buy_id")
    if expected_phase == "intent" and "media_buy_id" in claims:
        return reject("governance_token_not_applicable", "intent token has a media_buy_id")
    if expected_phase in {"modification", "delivery"} and (
        not isinstance(expected_media_buy_id, str)
        or not expected_media_buy_id
        or media_buy_id != expected_media_buy_id
    ):
        return reject("governance_token_not_applicable", "governance media_buy_id mismatch")
    if (
        expected_phase == "purchase"
        and media_buy_id is not None
        and (not isinstance(expected_media_buy_id, str) or media_buy_id != expected_media_buy_id)
    ):
        return reject("governance_token_not_applicable", "governance media_buy_id mismatch")
    current_time = time.time() if now is None else now()
    if (
        isinstance(current_time, bool)
        or not isinstance(current_time, (int, float))
        or not math.isfinite(float(current_time))
        or isinstance(clock_skew_seconds, bool)
        or not isinstance(clock_skew_seconds, (int, float))
        or not math.isfinite(float(clock_skew_seconds))
        or clock_skew_seconds < 0
    ):
        return reject("governance_token_invalid", "invalid governance verifier clock")
    iat = claims.get("iat")
    nbf = claims.get("nbf")
    exp = claims.get("exp")
    if not _finite_number(iat):
        return reject("governance_token_invalid", "governance token iat is invalid")
    iat_number = float(cast(int | float, iat))
    current_number = float(current_time)
    skew_number = float(clock_skew_seconds)
    if iat_number > current_number + skew_number:
        return reject("governance_token_not_yet_valid", "governance token iat is in the future")
    if nbf is not None:
        if not _finite_number(nbf):
            return reject("governance_token_invalid", "governance token nbf is invalid")
        if float(cast(int | float, nbf)) > current_number + skew_number:
            return reject("governance_token_not_yet_valid", "governance token is not active")
    if not _finite_number(exp):
        return reject("governance_token_invalid", "governance token exp is invalid")
    exp_number = float(cast(int | float, exp))
    if exp_number < current_number - skew_number:
        return reject("governance_token_expired", "governance token is expired")
    if exp_number <= iat_number:
        return reject("governance_token_invalid", "governance token expiry precedes issuance")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        return reject("governance_token_invalid", "governance token jti is missing")
    fresh_combined_revocation = False
    if revocation_resolver is not None:
        try:
            status = await _maybe_await(revocation_resolver.resolve(expected_issuer, kid, jti))
            issuer_url = urlsplit(expected_issuer)
            revocation_issuer = f"{issuer_url.scheme}://{issuer_url.netloc}"
            if (
                not isinstance(status, GovernanceRevocationStatus)
                or status.issuer != revocation_issuer
                or status.next_update < current_number
            ):
                return reject("governance_token_revoked", "governance revocation status is stale")
            if status.key_revoked or status.jti_revoked:
                return reject(
                    "governance_token_revoked", "governance signing key or token is revoked"
                )
            fresh_combined_revocation = True
        except Exception:
            return reject(
                "governance_token_revoked", "governance revocation status could not be established"
            )
    token_lifetime = exp_number - iat_number
    if expected_phase != "intent" and not fresh_combined_revocation:
        return reject(
            "governance_token_revoked",
            "execution token requires a fresh signed revocation status",
        )
    if expected_phase == "intent" and token_lifetime > GOVERNANCE_MAX_OFFLINE_LIFETIME_SECONDS:
        return reject(
            "governance_token_invalid",
            "intent token lifetime exceeds 15 minutes",
        )
    if expected_phase != "intent" and token_lifetime > GOVERNANCE_MAX_EXECUTION_LIFETIME_SECONDS:
        return reject("governance_token_invalid", "execution token lifetime exceeds 30 days")
    authorized_task = claims.get("authorized_task")
    if expected_phase == "intent" and not isinstance(authorized_task, str):
        return reject("governance_token_invalid", "governance token task is missing")
    if authorized_task is not None and authorized_task != expected_task:
        return reject("governance_token_not_applicable", "governance token task mismatch")
    try:
        payload_hash = compute_governed_payload_hash(payload)
    except (TypeError, ValueError, rfc8785.CanonicalizationError):
        return reject("governance_token_invalid", "governed payload is not canonical JSON")
    authorized_hash = claims.get("authorized_payload_hash")
    if expected_phase == "intent" and not isinstance(authorized_hash, str):
        return reject("governance_token_invalid", "governance token payload hash is missing")
    if authorized_hash is not None and authorized_hash != payload_hash:
        return reject("governance_token_not_applicable", "governance token payload hash mismatch")
    authorized_commitment = claims.get("authorized_commitment")
    if not isinstance(authorized_commitment, Mapping):
        return reject(
            "governance_token_not_applicable", "governance token has no monetary authorization"
        )
    try:
        authorized = _commitment_dict(authorized_commitment)
    except (TypeError, ValueError) as exc:
        return reject("governance_token_invalid", str(exc))
    if authorized["currency"] != actual["currency"] or actual["amount"] > authorized["amount"]:
        return reject(
            "governance_token_not_applicable",
            "actual commitment exceeds or mismatches authorization",
        )
    binding = GovernanceReplayBinding(
        authenticated_caller,
        expected_task,
        payload_hash,
        payload.get("idempotency_key") if isinstance(payload.get("idempotency_key"), str) else None,
    )
    try:
        replay = await replay_store.consume(
            expected_issuer,
            expected_audience,
            jti,
            exp_number + skew_number,
            current_number,
            binding,
        )
    except Exception:
        return reject("governance_token_replayed", "governance replay state could not be committed")
    if replay != "ok":
        return reject("governance_token_replayed", "governance token was already consumed")
    return GovernanceAuthorizationSuccess(True, claims, header, payload_hash)


async def enforce_governance_authorization(
    *,
    call_next: Callable[[GovernanceAuthorizationSuccess], Awaitable[Any]],
    **verify_options: Any,
) -> Any:
    """Verify and consume replay state before allowing a side effect."""
    result = await verify_governance_authorization(**verify_options)
    if not result.ok:
        raise GovernanceAuthorizationError(result)
    return await call_next(result)


def _record(value: object, *, exclude_unset: bool = False) -> dict[str, Any] | None:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=exclude_unset)
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _wire_copy(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", exclude_none=True)
    return copy.deepcopy(value)


def _commitment_dict(
    value: GovernanceCommitment | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(value, GovernanceCommitment):
        commitment = value
    elif isinstance(value, Mapping):
        amount = value.get("amount")
        currency = value.get("currency")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise TypeError("governance commitment amount must be a number")
        if not isinstance(currency, str):
            raise TypeError("governance commitment currency must be a string")
        commitment = build_governance_commitment(amount, currency)
    else:
        raise TypeError("governance commitment must be a GovernanceCommitment or mapping")
    return {"amount": commitment.amount, "currency": commitment.currency}


def _as_datetime_string(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) and value else None


def _has_only_operational_and(payload: Mapping[str, Any], allowed: set[str]) -> bool:
    return set(payload).issubset(_CONDITIONAL_OPERATIONAL_KEYS | allowed)


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _compact_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _decode_json_segment(segment: str) -> dict[str, Any]:
    raw = b64url_decode(segment)
    if b64url_encode(raw) != segment:
        raise ValueError("non-canonical base64url")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object member")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("JWS segment is not a JSON object")
    return value


async def _resolve_jwk(resolver: object, kid: str) -> object:
    method = getattr(resolver, "resolve", None)
    value = method(kid) if callable(method) else resolver(kid)  # type: ignore[operator]
    return await _maybe_await(value)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


__all__ = [
    "GOVERNANCE_AUTHORIZATION_CRITICAL_CLAIMS",
    "GOVERNANCE_AUTHORIZATION_TYP",
    "GovernanceAuthorizationError",
    "GovernanceAuthorizationFailure",
    "GovernanceAuthorizationResult",
    "GovernanceAuthorizationSuccess",
    "GovernanceCommitment",
    "GovernanceEnforcementMode",
    "GovernanceEnforcementTask",
    "GovernancePhase",
    "GovernanceReplayBinding",
    "GovernanceReplayStore",
    "GovernanceReplayStoreAdapter",
    "GovernanceRevocationResolver",
    "GovernanceRevocationStatus",
    "InMemoryGovernanceReplayStore",
    "NormalizedGovernanceApproved",
    "NormalizedGovernanceConditions",
    "NormalizedGovernanceDenied",
    "NormalizedGovernanceVerdict",
    "build_governance_commitment",
    "build_governance_execution_commitment",
    "build_governance_execution_request",
    "build_governance_intent_request",
    "build_governance_outcome_request",
    "build_governance_proposed_commitment",
    "compute_governed_payload_hash",
    "compute_governance_outcome_hash",
    "enforce_governance_authorization",
    "get_governance_enforcement_tasks",
    "governance_request_check_type",
    "governance_purchase_type_for_task",
    "governance_task_requires_proposed_commitment",
    "is_governance_approved",
    "is_governance_conditions",
    "is_governance_denied",
    "issue_governance_authorization",
    "normalize_governance_verdict",
    "read_governance_authorization_issuer",
    "stateless_governance_applicability",
    "target_declares_governance_enforcement",
    "target_declares_legacy_governance_awareness",
    "validate_governance_outcome_request",
    "validate_governance_request",
    "validate_governance_verdict",
    "verify_governance_authorization",
]
