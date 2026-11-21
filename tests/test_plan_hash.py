"""``compute_plan_hash`` and the as-supplied request on governance handlers (#1852).

``plan_hash`` is defined over one ``sync_plans`` plan exactly as the buyer
supplied it. These tests pin the helper against the spec's canonicalization
rules and check that the dispatcher hands governance handlers the request
before any default, hook or conversion touches it.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.governance import (
    PLAN_HASH_EXCLUDED_FIELDS,
    compute_governed_payload_hash,
    compute_plan_hash,
    issue_governance_authorization,
)
from adcp.server import GovernanceHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.signing.crypto import b64url_decode
from adcp.types import SyncPlansRequest
from adcp.validation.client_hooks import SERVER_DEFAULT_VALIDATION

# The plan-hash vectors ship with the pinned compliance bundle.
BUNDLE_VERSION = files("adcp").joinpath("ADCP_VERSION").read_text(encoding="utf-8").strip()
VECTOR_DIR = files("adcp").joinpath("_compliance", BUNDLE_VERSION, "test-vectors", "plan-hash")
VECTORS = [
    json.loads(entry.read_text(encoding="utf-8")) | {"_file": entry.name}
    for entry in sorted(VECTOR_DIR.iterdir(), key=lambda entry: entry.name)
    if entry.name.endswith(".json")
]

MINIMAL_PLAN: dict[str, Any] = {
    "plan_id": "plan_minimal_2026",
    "brand": {"domain": "example.com"},
    "objectives": "Drive awareness for Q1 launch.",
    "budget": {"total": 100000, "currency": "USD", "reallocation_threshold": 5000},
    "flight": {"start": "2026-04-01T00:00:00Z", "end": "2026-06-30T00:00:00Z"},
}
MINIMAL_PLAN_HASH = "oR0jFDEtzcwgPbNf-Ofd_fZHYfAyD1TRbzGOFBVCG-c"


def _with(**changes: Any) -> dict[str, Any]:
    plan = copy.deepcopy(MINIMAL_PLAN)
    plan.update(changes)
    return plan


# ---------------------------------------------------------------------------
# Spec vectors
# ---------------------------------------------------------------------------


def test_packaged_plan_hash_vectors_are_present() -> None:
    assert len(VECTORS) == 11


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["_file"])
def test_spec_plan_hash_vectors(vector: dict[str, Any]) -> None:
    expected = vector["expected"]
    plan_hash = compute_plan_hash(vector["plan_as_supplied"])
    assert plan_hash == expected["plan_hash"]
    assert b64url_decode(plan_hash).hex() == expected["sha256_hex"]
    assert hashlib.sha256(expected["jcs_bytes"].encode("utf-8")).hexdigest() == (
        expected["sha256_hex"]
    )


# ---------------------------------------------------------------------------
# Canonicalization rules
# ---------------------------------------------------------------------------


def test_output_is_unpadded_base64url_of_a_sha256_digest() -> None:
    plan_hash = compute_plan_hash(MINIMAL_PLAN)
    assert plan_hash == MINIMAL_PLAN_HASH
    assert "=" not in plan_hash and "+" not in plan_hash and "/" not in plan_hash
    assert len(b64url_decode(plan_hash)) == 32


def test_key_order_does_not_affect_the_hash() -> None:
    reordered = {key: MINIMAL_PLAN[key] for key in reversed(list(MINIMAL_PLAN))}
    reordered["budget"] = dict(reversed(list(MINIMAL_PLAN["budget"].items())))
    assert compute_plan_hash(reordered) == MINIMAL_PLAN_HASH


def test_array_order_is_preserved() -> None:
    first = _with(policy_categories=["alcohol", "gambling"])
    second = _with(policy_categories=["gambling", "alcohol"])
    assert compute_plan_hash(first) != compute_plan_hash(second)


def test_numbers_follow_rfc8785_formatting() -> None:
    integral_float = _with(budget={**MINIMAL_PLAN["budget"], "total": 100000.0})
    exponent = _with(budget={**MINIMAL_PLAN["budget"], "total": 1e5})
    assert compute_plan_hash(integral_float) == MINIMAL_PLAN_HASH
    assert compute_plan_hash(exponent) == MINIMAL_PLAN_HASH
    # A value that differs only in the last representable digit is distinct.
    fractional = _with(budget={**MINIMAL_PLAN["budget"], "total": 100000.00000000001})
    assert compute_plan_hash(fractional) != MINIMAL_PLAN_HASH


def test_omitted_false_and_null_human_review_required_are_distinct() -> None:
    omitted = compute_plan_hash(MINIMAL_PLAN)
    explicit_false = compute_plan_hash(_with(human_review_required=False))
    explicit_null = compute_plan_hash(_with(human_review_required=None))
    assert omitted == MINIMAL_PLAN_HASH
    assert len({omitted, explicit_false, explicit_null}) == 3


def test_schema_defaults_are_not_synthesized() -> None:
    defaulted = _with(budget={**MINIMAL_PLAN["budget"], "accounting_mode": "gross_commitment"})
    assert compute_plan_hash(defaulted) != MINIMAL_PLAN_HASH


def test_parsed_model_dump_is_not_a_valid_preimage() -> None:
    request = SyncPlansRequest.model_validate(
        {"idempotency_key": "sync-plans-0000000001", "plans": [MINIMAL_PLAN]}
    )
    dumped = request.model_dump(mode="json")["plans"][0]
    # Parsing fills budget.accounting_mode (and human_review_required on 3.2),
    # which is exactly why handlers need the as-supplied plan.
    assert dumped["budget"]["accounting_mode"] == "gross_commitment"
    assert compute_plan_hash(dumped) != MINIMAL_PLAN_HASH


def test_ext_and_unknown_fields_are_in_the_preimage() -> None:
    with_ext = compute_plan_hash(_with(ext={"trace_id": "a"}))
    rotated_ext = compute_plan_hash(_with(ext={"trace_id": "b"}))
    unknown = compute_plan_hash(_with(human_override={"reason": "future field"}))
    assert len({MINIMAL_PLAN_HASH, with_ext, rotated_ext, unknown}) == 4


def test_only_the_top_level_bookkeeping_fields_are_stripped() -> None:
    assert PLAN_HASH_EXCLUDED_FIELDS == {
        "version",
        "status",
        "syncedAt",
        "revisionHistory",
        "committedBudget",
        "committedByType",
    }
    bookkept = _with(**{name: "x" for name in PLAN_HASH_EXCLUDED_FIELDS})
    assert compute_plan_hash(bookkept) == MINIMAL_PLAN_HASH
    # Nested fields with the same names are business data, not bookkeeping.
    nested = _with(ext={"status": "draft", "version": 2})
    assert compute_plan_hash(nested) != compute_plan_hash(_with(ext={}))
    # Anything that merely looks like bookkeeping stays in (fail-safe inclusion).
    assert compute_plan_hash(_with(updatedAt="2026-01-01T00:00:00Z")) != MINIMAL_PLAN_HASH


def test_unicode_is_not_normalized() -> None:
    nfc = _with(objectives="Café")
    nfd = _with(objectives="Café")
    assert compute_plan_hash(nfc) != compute_plan_hash(nfd)


def test_input_mapping_is_not_mutated() -> None:
    plan = _with(version=3, ext={"trace_id": "a"})
    snapshot = copy.deepcopy(plan)
    compute_plan_hash(plan)
    assert plan == snapshot


def test_rejects_parsed_models_and_non_mappings() -> None:
    request = SyncPlansRequest.model_validate(
        {"idempotency_key": "sync-plans-0000000001", "plans": [MINIMAL_PLAN]}
    )
    with pytest.raises(TypeError, match="as supplied"):
        compute_plan_hash(request.plans[0])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="mapping"):
        compute_plan_hash([MINIMAL_PLAN])  # type: ignore[arg-type]


def test_rejects_values_rfc8785_cannot_canonicalize() -> None:
    with pytest.raises(ValueError):
        compute_plan_hash(_with(budget={**MINIMAL_PLAN["budget"], "total": float("nan")}))
    with pytest.raises(ValueError):
        compute_plan_hash(_with(ext={"big": 2**60}))


# ---------------------------------------------------------------------------
# issue_governance_authorization(plan=...)
# ---------------------------------------------------------------------------

_SIGNING_KEY = Ed25519PrivateKey.generate()


def _decode_claims(token: str) -> dict[str, Any]:
    return json.loads(b64url_decode(token.split(".")[1]))


def _issue(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "private_key": _SIGNING_KEY,
        "key_id": "gov-key",
        "alg": "EdDSA",
        "issuer": "https://governance.example",
        "subject": "opaque-action",
        "audience": "https://seller.example/adcp",
        "caller": "https://buyer.example",
        "check_id": "chk_1",
        "task": "create_media_buy",
        "payload": {"idempotency_key": "k" * 16},
        "authorized_commitment": {"amount": 10, "currency": "USD"},
        "issued_at": 1_800_000_000,
    }
    kwargs.update(overrides)
    return issue_governance_authorization(**kwargs)


def test_issue_governance_authorization_hashes_a_supplied_plan() -> None:
    token = _issue(plan=_with(version=9))
    assert _decode_claims(token)["plan_hash"] == MINIMAL_PLAN_HASH


def test_issue_governance_authorization_still_accepts_a_precomputed_hash() -> None:
    token = _issue(plan_hash="opaque-plan-hash")
    assert _decode_claims(token)["plan_hash"] == "opaque-plan-hash"


@pytest.mark.parametrize(
    "overrides", [{}, {"plan": MINIMAL_PLAN, "plan_hash": MINIMAL_PLAN_HASH}], ids=["none", "both"]
)
def test_issue_governance_authorization_needs_exactly_one_plan_input(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(TypeError, match="exactly one"):
        _issue(**overrides)


# ---------------------------------------------------------------------------
# Handlers receive the as-supplied request
# ---------------------------------------------------------------------------


def _sync_plans_params(**extra: Any) -> dict[str, Any]:
    return {
        "adcp_version": "3.2",
        "idempotency_key": "sync-plans-0000000001",
        "plans": [copy.deepcopy(MINIMAL_PLAN)],
        **extra,
    }


class _RecordedError(Exception):
    pass


class _RecordingGovernanceHandler(GovernanceHandler[ToolContext]):
    def __init__(self) -> None:
        super().__init__()
        self.seen: dict[str, Any] = {}

    async def handle_sync_plans(self, request, context=None):  # type: ignore[no-untyped-def]
        assert context is not None
        self.seen = {"request": request, "raw": context.raw_request}
        if context.raw_request is not None:
            # A handler mutating its copy must not disturb anything else.
            context.raw_request["plans"][0]["objectives"] = "mutated by handler"
        return {"plans": [{"plan_id": "plan_minimal_2026", "status": "active", "version": 1}]}

    async def handle_check_governance(self, request, context=None):  # type: ignore[no-untyped-def]
        assert context is not None
        self.seen = {"request": request, "raw": context.raw_request}
        # Only the handler's inputs matter here; stop before building a verdict.
        raise _RecordedError

    async def handle_get_creative_features(self, request, context=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def handle_report_plan_outcome(self, request, context=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def handle_get_plan_audit_logs(self, request, context=None):  # type: ignore[no-untyped-def]
        assert context is not None
        self.seen = {"raw": context.raw_request}
        return {"plans": []}

    async def handle_create_property_list(self, request, context=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def handle_get_property_list(self, request, context=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def handle_list_property_lists(self, request, context=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def handle_update_property_list(self, request, context=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def handle_delete_property_list(self, request, context=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.mark.asyncio
async def test_sync_plans_handler_receives_the_plan_as_supplied() -> None:
    handler = _RecordingGovernanceHandler()
    call = create_tool_caller(handler, "sync_plans", validation=SERVER_DEFAULT_VALIDATION)
    params = _sync_plans_params()
    sent = copy.deepcopy(params)

    await call(params, ToolContext())

    raw = handler.seen["raw"]
    raw["plans"][0]["objectives"] = MINIMAL_PLAN["objectives"]  # undo the handler's edit
    assert raw == sent
    assert compute_plan_hash(raw["plans"][0]) == MINIMAL_PLAN_HASH
    # The parsed request still carries defaults and does not hash the same.
    parsed = handler.seen["request"].model_dump(mode="json")["plans"][0]
    assert compute_plan_hash(parsed) != MINIMAL_PLAN_HASH
    # The handler got a private copy; the transport's dict is untouched.
    assert params == sent


@pytest.mark.asyncio
async def test_raw_request_is_captured_before_pre_validation_hooks() -> None:
    handler = _RecordingGovernanceHandler()

    def rewrite(_tool: str, args: dict[str, Any]) -> dict[str, Any]:
        args["plans"] = [{**args["plans"][0], "objectives": "rewritten by hook"}]
        return args

    call = create_tool_caller(
        handler,
        "sync_plans",
        validation=SERVER_DEFAULT_VALIDATION,
        pre_validation_hook=rewrite,
    )

    await call(_sync_plans_params(), ToolContext())

    assert handler.seen["request"].plans[0].objectives == "rewritten by hook"
    raw = handler.seen["raw"]
    assert raw["plans"][0]["objectives"] == "mutated by handler"
    raw["plans"][0]["objectives"] = MINIMAL_PLAN["objectives"]
    assert compute_plan_hash(raw["plans"][0]) == MINIMAL_PLAN_HASH


@pytest.mark.asyncio
async def test_check_governance_handler_receives_the_payload_as_supplied() -> None:
    handler = _RecordingGovernanceHandler()
    call = create_tool_caller(handler, "check_governance", validation=SERVER_DEFAULT_VALIDATION)
    payload = {
        "idempotency_key": "create-media-buy-0001",
        "brand": {"domain": "example.com"},
        "start_time": "2026-04-01T00:00:00Z",
        "end_time": "2026-06-30T00:00:00Z",
        "packages": [],
        "ext": {"order": [3, 1, 2]},
    }
    params = {
        "adcp_version": "3.2",
        "plan_id": "plan_minimal_2026",
        "caller": "https://buyer.example",
        "target_agent": "https://seller.example/adcp",
        "tool": "create_media_buy",
        "payload": copy.deepcopy(payload),
        "proposed_commitment": {"amount": 10, "currency": "USD"},
    }

    with pytest.raises(_RecordedError):
        await call(params, ToolContext())

    raw = handler.seen["raw"]
    assert raw["payload"] == payload
    assert compute_governed_payload_hash(raw["payload"]) == compute_governed_payload_hash(payload)


@pytest.mark.asyncio
async def test_other_tools_and_reused_contexts_get_no_raw_request() -> None:
    handler = _RecordingGovernanceHandler()
    context = ToolContext()
    sync = create_tool_caller(handler, "sync_plans", validation=SERVER_DEFAULT_VALIDATION)
    audit = create_tool_caller(handler, "get_plan_audit_logs", validation=SERVER_DEFAULT_VALIDATION)

    await sync(_sync_plans_params(), context)
    assert context.raw_request is not None
    await audit({"adcp_version": "3.2", "plan_ids": ["plan_minimal_2026"]}, context)

    assert handler.seen["raw"] is None
    assert context.raw_request is None


def test_tool_context_raw_request_defaults_to_none_and_stays_out_of_repr() -> None:
    assert ToolContext().raw_request is None
    context = ToolContext(raw_request={"plans": [{"objectives": "confidential"}]})
    assert "confidential" not in repr(context)


@pytest.mark.asyncio
async def test_decisioning_governance_platform_receives_the_plan_as_supplied() -> None:
    seen: dict[str, Any] = {}

    class _GovernanceAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-spend-authority"],
            governance_aware=True,
        )
        accounts = SingletonAccounts(account_id="gov")

        def sync_plans(self, req, ctx):  # type: ignore[no-untyped-def]
            seen["raw"] = ctx.raw_request
            return {"plans": [{"plan_id": "plan_minimal_2026", "status": "active", "version": 1}]}

        def check_governance(self, req, ctx):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def report_plan_outcome(self, req, ctx):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def get_plan_audit_logs(self, req, ctx):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            _GovernanceAgent(), executor=executor, registry=InMemoryTaskRegistry()
        )
        call = create_tool_caller(handler, "sync_plans", validation=SERVER_DEFAULT_VALIDATION)
        await call(_sync_plans_params(), ToolContext())

    assert seen["raw"]["plans"][0] == MINIMAL_PLAN
    assert compute_plan_hash(seen["raw"]["plans"][0]) == MINIMAL_PLAN_HASH


# ---------------------------------------------------------------------------
# Through the real transports
# ---------------------------------------------------------------------------

#: Vector 008 mixes integers (``total``, ``amount``) and fractions
#: (``max_pct``). Over A2A a DataPart rides a protobuf ``Struct``, which turns
#: every number into a double before the SDK restores the integral ones.
NUMERIC_VECTOR = next(v for v in VECTORS if v["_file"].startswith("008-"))


class _TransportGovernanceHandler(_RecordingGovernanceHandler):
    async def handle_sync_plans(self, request, context=None):  # type: ignore[no-untyped-def]
        assert context is not None
        self.seen = {"request": request, "raw": context.raw_request}
        return {"plans": [{"plan_id": request.plans[0].plan_id, "status": "active", "version": 1}]}


async def _rewrite_objectives(skill_name, params, context, call_next):  # type: ignore[no-untyped-def]
    # Edits the shared params dict in place, as a careless middleware might.
    if skill_name == "sync_plans":
        params["plans"][0]["objectives"] = "rewritten by middleware"
    return await call_next()


def _transport_client(
    handler: GovernanceHandler[Any], *, middleware: list[Any] | None
) -> Iterator[Any]:
    from starlette.testclient import TestClient

    from adcp.server.serve import _build_mcp_and_a2a_app

    app = _build_mcp_and_a2a_app(
        handler,
        name="plan-hash",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        stateless_http=True,
        allowed_hosts=["testserver"],
        middleware=middleware,
    )
    with TestClient(app, base_url="http://localhost:3001") as client:
        yield client


def _send(client: Any, transport: str, params: dict[str, Any]) -> None:
    if transport == "mcp":
        response = client.post(
            "/mcp/",
            headers={"accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "sync_plans", "arguments": params},
            },
        )
        assert response.status_code == 200, response.text
        assert not response.json()["result"].get("isError"), response.text
        return
    invocation = {"skill": "sync_plans", "parameters": params}
    if transport == "a2a-v0.3":
        headers: dict[str, str] = {}
        method, role, part = "message/send", "user", {"kind": "data", "data": invocation}
    else:
        headers = {"A2A-Version": "1.0"}
        method, role, part = "SendMessage", "ROLE_USER", {"data": invocation}
    response = client.post(
        "/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"message": {"messageId": f"m-{transport}", "role": role, "parts": [part]}},
        },
    )
    assert response.status_code == 200, response.text
    assert "error" not in response.json(), response.text


@pytest.mark.parametrize("with_middleware", [False, True], ids=["plain", "mutating-middleware"])
@pytest.mark.parametrize("transport", ["mcp", "a2a-v0.3", "a2a-v1.0"])
def test_transports_deliver_the_plan_as_supplied(transport: str, with_middleware: bool) -> None:
    handler = _TransportGovernanceHandler()
    plan = copy.deepcopy(NUMERIC_VECTOR["plan_as_supplied"])
    params = {"adcp_version": "3.2", "idempotency_key": "sync-plans-0000000002", "plans": [plan]}

    for client in _transport_client(
        handler, middleware=[_rewrite_objectives] if with_middleware else None
    ):
        _send(client, transport, params)

    raw_plan = handler.seen["raw"]["plans"][0]
    # Integers come back as integers and fractions as the same doubles, so
    # the as-supplied plan reproduces the spec's expected hash bit-exactly.
    assert json.dumps(raw_plan, sort_keys=True) == json.dumps(plan, sort_keys=True)
    assert compute_plan_hash(raw_plan) == NUMERIC_VECTOR["expected"]["plan_hash"]
    expected_objectives = "rewritten by middleware" if with_middleware else plan["objectives"]
    # Middleware rewrites still reach the parsed request; raw_request is the
    # buyer's copy, taken before the middleware chain ran.
    assert handler.seen["request"].plans[0].objectives == expected_objectives
