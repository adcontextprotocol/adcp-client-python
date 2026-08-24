from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from adcp.decisioning.errors import PermissionDeniedError
from adcp.exceptions import IdempotencyConflictError
from adcp.governance import (
    GovernanceReplayBinding,
    GovernanceReplayStoreAdapter,
    GovernanceRevocationStatus,
    InMemoryGovernanceReplayStore,
    build_governance_commitment,
    build_governance_execution_request,
    build_governance_intent_request,
    build_governance_outcome_request,
    compute_governance_outcome_hash,
    compute_governed_payload_hash,
    get_governance_enforcement_tasks,
    governance_request_check_type,
    issue_governance_authorization,
    normalize_governance_verdict,
    stateless_governance_applicability,
    target_declares_governance_enforcement,
    validate_governance_outcome_request,
    validate_governance_request,
    verify_governance_authorization,
)
from adcp.server import (
    GovernanceEnforcementDecision,
    ToolContext,
    make_governance_enforcement_middleware,
)
from adcp.server.idempotency import IdempotencyStore, MemoryBackend
from adcp.signing import InMemoryReplayStore, StaticJwksResolver, private_key_from_jwk

VECTOR_PATH = Path(__file__).parent / "conformance/vectors/governance-authorization.json"
VECTORS = json.loads(VECTOR_PATH.read_text())


def enforcement_resolver(*, required: bool, commitment: object | None):
    @asynccontextmanager
    async def resolve(_task: str, _params: object, _context: ToolContext):
        yield GovernanceEnforcementDecision(required, commitment)  # type: ignore[arg-type]

    return resolve


@pytest.mark.parametrize("case", VECTORS["payload_hash_cases"], ids=lambda case: case["id"])
def test_governed_payload_hash_vectors(case: dict[str, Any]) -> None:
    assert compute_governed_payload_hash(case["payload"]) == case["expected_hash"]


@pytest.mark.parametrize("case", VECTORS["signed_jws"]["cases"], ids=lambda case: case["id"])
@pytest.mark.asyncio
async def test_governance_authorization_signed_vectors(case: dict[str, Any]) -> None:
    signed = VECTORS["signed_jws"]
    defaults = dict(signed["verification_defaults"])
    overrides = case.get("verification_overrides", {})
    defaults.update({key: value for key, value in overrides.items() if key != "preconsumed_jti"})
    public_jwk = {
        key: value
        for key, value in signed["test_key"].items()
        if key != "_private_d_for_test_only" and not key.startswith("$")
    }
    replay_store = InMemoryGovernanceReplayStore()
    if overrides.get("preconsumed_jti"):
        replay_store.preload(
            defaults["expected_issuer"],
            defaults["expected_audience"],
            case["claims"]["jti"],
            case["claims"]["exp"] + defaults["clock_skew_seconds"],
        )

    result = await verify_governance_authorization(
        token=case["compact_jws"],
        expected_issuer=defaults["expected_issuer"],
        expected_audience=defaults["expected_audience"],
        authenticated_caller=defaults["authenticated_caller"],
        expected_task=defaults["expected_task"],
        payload=defaults["payload"],
        actual_commitment=defaults["actual_commitment"],
        expected_phase=defaults["expected_phase"],
        jwks=StaticJwksResolver({"keys": [public_jwk]}),
        replay_store=replay_store,
        now=lambda: defaults["now"],
        clock_skew_seconds=defaults["clock_skew_seconds"],
    )

    expected = case["expected"]
    assert result.ok is (expected["result"] == "accept")
    if not result.ok:
        assert result.error == expected["error"]


@pytest.mark.parametrize(
    "replay_store",
    [
        InMemoryGovernanceReplayStore(),
        GovernanceReplayStoreAdapter(InMemoryReplayStore()),
    ],
    ids=["governance-store", "signing-store-adapter"],
)
@pytest.mark.asyncio
async def test_replay_store_allows_only_the_same_idempotency_binding(replay_store: Any) -> None:
    binding = GovernanceReplayBinding(
        caller="https://buyer.example",
        task="create_media_buy",
        payload_hash="payload-hash",
        idempotency_key="request-1",
    )
    different_binding = GovernanceReplayBinding(
        caller=binding.caller,
        task=binding.task,
        payload_hash=binding.payload_hash,
        idempotency_key="request-2",
    )

    assert await replay_store.consume("issuer", "audience", "jti", 200, 100, binding) == "ok"
    assert await replay_store.consume("issuer", "audience", "jti", 200, 100, binding) == "ok"
    assert (
        await replay_store.consume("issuer", "audience", "jti", 200, 100, different_binding)
        == "conflict"
    )


@pytest.mark.asyncio
async def test_replay_store_does_not_exempt_a_binding_without_idempotency_key() -> None:
    replay_store = InMemoryGovernanceReplayStore()
    binding = GovernanceReplayBinding(
        caller="https://buyer.example",
        task="create_media_buy",
        payload_hash="payload-hash",
    )

    assert await replay_store.consume("issuer", "audience", "jti", 200, 100, binding) == "ok"
    assert await replay_store.consume("issuer", "audience", "jti", 200, 100, binding) == "conflict"


@pytest.mark.asyncio
async def test_issue_governance_authorization_round_trip_uses_wire_payload() -> None:
    test_key = dict(VECTORS["signed_jws"]["test_key"])
    test_key["d"] = test_key.pop("_private_d_for_test_only")
    token = issue_governance_authorization(
        private_key=private_key_from_jwk(test_key),
        key_id=test_key["kid"],
        alg="EdDSA",
        issuer="https://gov.example/governance",
        subject="opaque-action",
        plan_hash="opaque-plan-hash",
        audience="https://seller.example/sales",
        caller="https://buyer.example",
        check_id="check-1",
        task="create_media_buy",
        payload={"idempotency_key": "request-1", "amount": 1},
        authorized_commitment=build_governance_commitment(1, "USD"),
        issued_at=1_000,
        expires_at=1_900,
        jti="token-1",
    )
    public_jwk = {key: value for key, value in test_key.items() if key != "d"}
    result = await verify_governance_authorization(
        token=token,
        expected_issuer="https://gov.example/governance",
        expected_audience="https://seller.example/sales",
        authenticated_caller="https://buyer.example",
        expected_task="create_media_buy",
        payload={"idempotency_key": "request-1", "amount": 1},
        actual_commitment=build_governance_commitment(1, "USD"),
        jwks=StaticJwksResolver({"keys": [public_jwk]}),
        replay_store=InMemoryGovernanceReplayStore(),
        now=lambda: 1_000,
    )
    assert result.ok


@pytest.mark.asyncio
async def test_execution_authorization_binds_phase_media_buy_and_revocation() -> None:
    test_key = dict(VECTORS["signed_jws"]["test_key"])
    test_key["d"] = test_key.pop("_private_d_for_test_only")
    payload = {"idempotency_key": "request-execution-1", "media_buy_id": "mb-1"}
    token = issue_governance_authorization(
        private_key=private_key_from_jwk(test_key),
        key_id=test_key["kid"],
        alg="EdDSA",
        issuer="https://gov.example/governance",
        subject="opaque-action",
        plan_hash="opaque-plan-hash",
        audience="https://seller.example/sales",
        caller="https://seller.example/sales",
        check_id="check-execution-1",
        task="update_media_buy",
        payload=payload,
        authorized_commitment=build_governance_commitment(1, "USD"),
        issued_at=1_000,
        expires_at=2_000,
        jti="token-execution-1",
        phase="modification",
        media_buy_id="mb-1",
    )
    public_jwk = {key: value for key, value in test_key.items() if key != "d"}

    class FreshRevocationResolver:
        def resolve(self, issuer: str, kid: str, jti: str) -> GovernanceRevocationStatus:
            return GovernanceRevocationStatus("https://gov.example", False, False, 2_000)

    options = {
        "token": token,
        "expected_issuer": "https://gov.example/governance",
        "expected_audience": "https://seller.example/sales",
        "authenticated_caller": "https://seller.example/sales",
        "expected_task": "update_media_buy",
        "payload": payload,
        "actual_commitment": build_governance_commitment(1, "USD"),
        "jwks": StaticJwksResolver({"keys": [public_jwk]}),
        "replay_store": InMemoryGovernanceReplayStore(),
        "expected_phase": "modification",
        "expected_subject": "opaque-action",
        "revocation_resolver": FreshRevocationResolver(),
        "now": lambda: 1_000,
    }

    mismatch = await verify_governance_authorization(
        **options,
        expected_media_buy_id="mb-2",
    )
    assert not mismatch.ok and mismatch.error == "governance_token_not_applicable"
    accepted = await verify_governance_authorization(
        **options,
        expected_media_buy_id="mb-1",
    )
    assert accepted.ok


@pytest.mark.asyncio
async def test_server_middleware_verifies_before_side_effect_and_replays_cached_result() -> None:
    signed = VECTORS["signed_jws"]
    defaults = signed["verification_defaults"]
    case = signed["cases"][0]
    public_jwk = {
        key: value
        for key, value in signed["test_key"].items()
        if key != "_private_d_for_test_only" and not key.startswith("$")
    }
    middleware = make_governance_enforcement_middleware(
        tasks={"create_media_buy"},
        expected_audience=defaults["expected_audience"],
        resolve_issuer_jwks=lambda issuer, _task, _params, _context: (
            StaticJwksResolver({"keys": [public_jwk]})
            if issuer == defaults["expected_issuer"]
            else None
        ),
        idempotency_store=IdempotencyStore(MemoryBackend(), ttl_seconds=3600),
        replay_store=InMemoryGovernanceReplayStore(),
        resolve_enforcement=enforcement_resolver(
            required=True, commitment=defaults["actual_commitment"]
        ),
        now=lambda: defaults["now"],
        clock_skew_seconds=defaults["clock_skew_seconds"],
    )
    called = False

    async def call_next() -> dict[str, str]:
        nonlocal called
        called = True
        return {"status": "committed"}

    params = {**defaults["payload"], "governance_context": case["compact_jws"]}
    context = ToolContext(caller_identity=defaults["authenticated_caller"])
    assert await middleware("create_media_buy", params, context, call_next) == {
        "status": "committed"
    }
    assert called

    called = False
    assert await middleware("create_media_buy", params, context, call_next) == {
        "status": "committed",
        "replayed": True,
    }
    assert not called


@pytest.mark.asyncio
async def test_middleware_verifies_supplied_token_on_exempt_operation() -> None:
    called = False
    middleware = make_governance_enforcement_middleware(
        tasks={"update_media_buy"},
        expected_audience="https://seller.example",
        resolve_issuer_jwks=lambda *_args: None,
        idempotency_store=IdempotencyStore(MemoryBackend(), ttl_seconds=3600),
        replay_store=InMemoryGovernanceReplayStore(),
        resolve_enforcement=enforcement_resolver(
            required=False, commitment=build_governance_commitment(0, "USD")
        ),
    )

    async def call_next() -> dict[str, str]:
        nonlocal called
        called = True
        return {"status": "committed"}

    with pytest.raises(PermissionDeniedError):
        await middleware(
            "update_media_buy",
            {"governance_context": "invalid.token.value"},
            ToolContext(caller_identity="https://buyer.example"),
            call_next,
        )
    assert not called


@pytest.mark.asyncio
async def test_middleware_idempotency_is_bound_to_skill() -> None:
    middleware = make_governance_enforcement_middleware(
        tasks={"task_a", "task_b"},
        expected_audience="https://seller.example",
        resolve_issuer_jwks=lambda *_args: None,
        idempotency_store=IdempotencyStore(MemoryBackend(), ttl_seconds=3600),
        replay_store=InMemoryGovernanceReplayStore(),
        resolve_enforcement=enforcement_resolver(
            required=False, commitment=build_governance_commitment(0, "USD")
        ),
    )
    params = {"idempotency_key": "same-request-key-1"}
    context = ToolContext(caller_identity="https://buyer.example")

    async def task_a() -> dict[str, str]:
        return {"from": "a"}

    async def task_b() -> dict[str, str]:
        return {"from": "b"}

    assert await middleware("task_a", params, context, task_a) == {"from": "a"}
    with pytest.raises(IdempotencyConflictError, match="idempotency_key reused"):
        await middleware("task_b", params, context, task_b)


def test_intent_and_execution_builders_keep_plan_on_buyer_side() -> None:
    intent = build_governance_intent_request(
        plan_id="plan-1",
        caller="https://buyer.example",
        target_agent="https://seller.example",
        tool="update_media_buy",
        payload={"media_buy_id": "mb-1", "budget": 12},
        proposed_commitment=build_governance_commitment(2, "USD"),
    )
    assert str(intent.target_agent) == "https://seller.example/"
    assert intent.payload == {"media_buy_id": "mb-1", "budget": 12}
    assert intent.governance_context is None

    execution = build_governance_execution_request(
        caller="https://seller.example",
        governance_context="header.payload.signature",
        planned_delivery={"media_buy_id": "mb-1", "total_budget": 12, "currency": "USD"},
        phase="modification",
        execution_commitment=build_governance_commitment(2, "USD"),
    )
    assert execution.plan_id is None
    assert execution.payload is None
    assert execution.planned_delivery is not None
    assert execution.planned_delivery.media_buy_id == "mb-1"
    assert validate_governance_request(intent) == "intent"
    assert validate_governance_request(execution) == "execution"

    with pytest.raises(ValueError, match="mix intent and execution"):
        validate_governance_request(
            {
                "caller": "https://buyer.example",
                "plan_id": "plan-1",
                "target_agent": "https://seller.example",
                "tool": "create_media_buy",
                "payload": {},
                "governance_context": "header.payload.signature",
                "planned_delivery": {"total_budget": 1, "currency": "USD"},
            }
        )
    with pytest.raises(ValueError, match="runtime_attestations"):
        validate_governance_request(
            {
                "caller": "https://buyer.example",
                "plan_id": "plan-1",
                "target_agent": "https://seller.example",
                "tool": "create_media_buy",
                "payload": {},
                "runtime_attestations": [{"credential": "opaque"}],
            }
        )
    assert (
        governance_request_check_type(
            {
                "adcp_version": "3.1-beta.4",
                "caller": "https://seller.example",
                "governance_context": "header.payload.signature",
                "phase": "purchase",
                "planned_delivery": {"total_budget": 1, "currency": "USD"},
            }
        )
        is None
    )
    modern_incomplete = {
        "adcp_version": "3.2-beta.5",
        "caller": "https://buyer.example",
        "plan_id": "plan-1",
        "tool": "create_media_buy",
        "payload": {},
    }
    assert governance_request_check_type(modern_incomplete) == "intent"
    with pytest.raises(ValueError, match="target_agent"):
        validate_governance_request(modern_incomplete)
    with pytest.raises(ValueError, match="select an intent or execution"):
        governance_request_check_type(
            {
                "adcp_version": "3.2-beta.5",
                "plan_id": "plan-1",
                "caller": "https://buyer.example",
            }
        )


def test_outcome_builder_binds_terminal_result_to_approved_check() -> None:
    outcome = build_governance_outcome_request(
        plan_id="plan-1",
        check_id="check-1",
        idempotency_key="outcome-request-1",
        outcome="completed",
        governance_context="header.payload.signature",
        seller_response={"seller_reference": "mb-1", "committed_budget": 12},
    )

    assert outcome.plan_id == "plan-1"
    assert outcome.check_id == "check-1"
    assert outcome.seller_response is not None
    assert outcome.seller_response.seller_reference == "mb-1"
    assert validate_governance_outcome_request(outcome) is outcome

    with pytest.raises(TypeError, match="exactly one"):
        build_governance_outcome_request(
            plan_id="plan-1",
            check_id="check-1",
            idempotency_key="outcome-request-2",
            outcome="failed",
            governance_context="header.payload.signature",
            seller_response={"seller_reference": "mb-1"},
            error={"code": "DECLINED"},
        )
    with pytest.raises(ValueError, match="exactly"):
        validate_governance_outcome_request(
            {
                "plan_id": "plan-1",
                "check_id": "check-1",
                "idempotency_key": "outcome-request-3",
                "outcome": "completed",
                "governance_context": "header.payload.signature",
                "seller_response": {},
                "error": {"code": "DECLINED"},
            }
        )
    legacy_delivery = {
        "plan_id": "plan-1",
        "idempotency_key": "legacy-outcome-1",
        "outcome": "delivery",
        "governance_context": "legacy-context",
        "delivery": {"source": "buyer_measurement"},
    }
    with pytest.raises(ValueError, match="check_id"):
        validate_governance_outcome_request(legacy_delivery)
    assert (
        validate_governance_outcome_request(legacy_delivery, allow_legacy_delivery=True)
        is legacy_delivery
    )


def test_outcome_hash_excludes_retry_metadata_but_keeps_authorization() -> None:
    payload = {
        "plan_id": "plan-1",
        "check_id": "check-1",
        "idempotency_key": "outcome-request-1",
        "outcome": "completed",
        "governance_context": "authorization-1",
        "seller_response": {"seller_reference": "mb-1"},
        "context": {"trace_id": "trace-1"},
    }
    expected = compute_governance_outcome_hash(payload)

    assert (
        compute_governance_outcome_hash(
            {**payload, "idempotency_key": "outcome-request-2", "context": {"trace_id": "trace-2"}}
        )
        == expected
    )
    assert (
        compute_governance_outcome_hash({**payload, "governance_context": "authorization-2"})
        != expected
    )


def test_modern_verdict_normalization_fails_closed() -> None:
    approved = normalize_governance_verdict(
        {
            "check_id": "check-1",
            "check_type": "intent",
            "verdict": "approved",
            "explanation": "ok",
            "governance_context": "header.payload.signature",
            "expires_at": "2026-08-24T06:00:00Z",
        }
    )
    assert approved is not None and approved.verdict == "approved"
    assert (
        normalize_governance_verdict(
            {
                "check_id": "check-attestation",
                "check_type": "intent",
                "verdict": "approved",
                "explanation": "ok",
                "governance_context": "header.payload.signature",
                "expires_at": "2026-08-24T06:00:00Z",
                "runtime_attestation_evaluations": [],
            }
        )
        is None
    )
    assert (
        normalize_governance_verdict(
            {
                "check_id": "check-2",
                "check_type": "intent",
                "verdict": "conditions",
                "explanation": "change budget",
                "conditions": [{"field": "payload.budget", "reason": "too high"}],
                "consultation_context": "consult-1",
                "governance_context": "must-not-leak",
            }
        )
        is None
    )
    assert (
        normalize_governance_verdict(
            {
                "check_id": "check-3",
                "check_type": "execution",
                "verdict": "conditions",
                "explanation": "invalid at execution",
                "conditions": [{"field": "payload.budget", "reason": "too high"}],
                "consultation_context": "consult-1",
            }
        )
        is None
    )


def test_governance_enforcement_capabilities_require_feature_and_unique_task() -> None:
    capabilities = {
        "experimental_features": ["governance.campaign"],
        "adcp": {
            "governance_enforcement": {
                "tasks": [
                    {
                        "task": "create_media_buy",
                        "modes": ["signed_context", "online_execution_check"],
                    }
                ]
            }
        },
    }
    assert target_declares_governance_enforcement(capabilities, "create_media_buy")
    assert get_governance_enforcement_tasks(capabilities)[0].task == "create_media_buy"
    capabilities["adcp"]["governance_enforcement"]["tasks"].append(  # type: ignore[index]
        {"task": "create_media_buy", "modes": ["signed_context"]}
    )
    with pytest.raises(ValueError, match="duplicate"):
        get_governance_enforcement_tasks(capabilities)

    online_only = {
        "experimental_features": ["governance.campaign"],
        "adcp": {
            "governance_enforcement": {
                "tasks": [
                    {
                        "task": "create_media_buy",
                        "modes": ["signed_context", "online_execution_check"],
                    }
                ]
            }
        },
    }
    assert target_declares_governance_enforcement(
        online_only, "create_media_buy", "online_execution_check"
    )
    online_only["adcp"]["governance_enforcement"]["tasks"][0]["modes"] = ["online_execution_check"]
    with pytest.raises(ValueError, match="signed_context"):
        get_governance_enforcement_tasks(online_only)
    online_only["adcp"]["governance_enforcement"]["tasks"][0]["modes"] = ["unknown"]
    with pytest.raises(ValueError, match="mode"):
        get_governance_enforcement_tasks(online_only)


@pytest.mark.parametrize(
    ("task", "payload", "expected"),
    [
        ("activate_signal", {"action": "deactivate"}, False),
        ("build_creative", {"mode": "estimate"}, False),
        ("update_media_buy", {"media_buy_id": "mb-1", "paused": True}, False),
        ("update_media_buy", {"media_buy_id": "mb-1", "paused": False}, True),
        ("update_media_buy", {"media_buy_id": "mb-1", "budget": 10}, None),
    ],
)
def test_stateless_governance_applicability(
    task: str, payload: dict[str, Any], expected: bool | None
) -> None:
    assert stateless_governance_applicability(task, payload) is expected
