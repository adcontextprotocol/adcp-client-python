"""Established WBA identities use AdCP authorization errors on both transports."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.types import CallToolResult

from adcp.decisioning.registry import BuyerAgent
from adcp.decisioning.trust_grants import TrustGrantClaim, TrustGrantGate
from adcp.decisioning.types import Account, AdcpError
from adcp.protocols.mcp import extract_adcp_error
from adcp.server import ADCPHandler, RequestSignatureVerification
from adcp.server.serve import _build_mcp_and_a2a_app
from adcp.signing import (
    OriginBindings,
    StaticSignerKeys,
    TrustGrantEvaluator,
    TrustRecordEvaluator,
    TrustResponse,
    public_directory_key,
    sign_wba_request,
    trust_record_url,
)

ORIGIN = "https://buyer.platform.com"
CLAIM = TrustGrantClaim("brand.com", "platform.com")
BUYER = BuyerAgent(agent_url=ORIGIN, display_name="Buyer", status="active")


def grant_gate(failure: str) -> tuple[TrustGrantGate, TrustRecordEvaluator]:
    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        if url == trust_record_url(ORIGIN):
            document: Any = {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}
        elif failure == "offline":
            raise OSError("brand trust record offline")
        elif failure == "invalid":
            return TrustResponse(b"not JSON")
        else:
            document = {"grants": []}
        return TrustResponse(json.dumps(document).encode())

    records = TrustRecordEvaluator(fetcher=fetch)
    return TrustGrantGate(TrustGrantEvaluator(records), lambda account, agent: CLAIM), records


def changing_grant_gate(
    *, initial: str, grant_valid_until: str | None = None
) -> tuple[TrustGrantGate, list[str], list[float]]:
    mode, now = [initial], [1000.0]

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        if url == trust_record_url(ORIGIN):
            document = {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}
        elif mode[0] == "offline":
            raise OSError("brand trust record offline")
        else:
            document = {
                "exclusive_grants": True,
                "grants": [
                    {
                        "grantee": "platform.com" if mode[0] == "allowed" else "other.com",
                        "scopes": ["adcp:media_buying"],
                    }
                ],
            }
            if grant_valid_until is not None:
                document["grants"][0]["valid_until"] = grant_valid_until
        return TrustResponse(json.dumps(document).encode())

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    gate = TrustGrantGate(
        TrustGrantEvaluator(records),
        lambda account, agent: account.metadata["claim"],
    )
    return gate, mode, now


async def require_existing_claim(
    gate: TrustGrantGate, claim: TrustGrantClaim = CLAIM, buyer: BuyerAgent = BUYER
) -> None:
    await gate.require_account(
        Account(id="existing", metadata={"claim": claim}),
        buyer,
        operation="get_products",
    )


@pytest.mark.parametrize("initial", ["allowed", "denied"])
async def test_known_grant_denial_never_becomes_authority_during_outage(initial: str) -> None:
    gate, mode, now = changing_grant_gate(initial=initial)
    if initial == "allowed":
        await require_existing_claim(gate)
        mode[0], now[0] = "denied", now[0] + 61
    with pytest.raises(AdcpError) as denied:
        await require_existing_claim(gate)
    assert denied.value.details == {"scope": "agent", "reason": "grant_absent"}
    mode[0], now[0] = "offline", now[0] + 61
    with pytest.raises(AdcpError) as outage:
        await require_existing_claim(gate)
    assert outage.value.code == "PERMISSION_DENIED"
    assert outage.value.details == denied.value.details


async def test_fresh_restored_grant_clears_previously_observed_denial() -> None:
    gate, mode, now = changing_grant_gate(initial="denied")
    with pytest.raises(AdcpError, match="PERMISSION_DENIED"):
        await require_existing_claim(gate)
    mode[0], now[0] = "allowed", now[0] + 61
    await require_existing_claim(gate)
    mode[0], now[0] = "offline", now[0] + 61
    await require_existing_claim(gate)


@pytest.mark.parametrize("grace_offset,allowed", [(239, True), (240, False), (241, False)])
async def test_positive_claim_outage_continuity_has_a_fixed_grace_deadline(
    grace_offset: int, allowed: bool
) -> None:
    gate, mode, now = changing_grant_gate(initial="allowed")
    await require_existing_claim(gate)
    mode[0], now[0] = "offline", 1061
    await require_existing_claim(gate)
    # Freshness expires at 1060; an outage read never refreshes the
    # outage-continuation deadline (four polling intervals: 1300).
    now[0] = 1060 + grace_offset
    if allowed:
        await require_existing_claim(gate)
    else:
        with pytest.raises(AdcpError, match="SERVICE_UNAVAILABLE"):
            await require_existing_claim(gate)


async def test_initial_trust_outage_cannot_create_claim_authority() -> None:
    gate, _ = grant_gate("offline")
    with pytest.raises(AdcpError, match="SERVICE_UNAVAILABLE"):
        await gate.require_account(Account(id="existing"), BUYER, operation="get_products")


async def test_outage_grace_never_extends_an_explicit_grant_expiry() -> None:
    gate, mode, now = changing_grant_gate(
        initial="allowed", grant_valid_until="1970-01-01T00:17:50Z"
    )
    await require_existing_claim(gate)
    mode[0], now[0] = "offline", 1061
    await require_existing_claim(gate)
    # The last grant read has expired on its own window: it is absent.
    now[0] = 1070
    with pytest.raises(AdcpError) as expired:
        await require_existing_claim(gate)
    assert expired.value.code == "PERMISSION_DENIED"
    assert expired.value.details == {"scope": "agent", "reason": "grant_absent"}


async def test_claim_aliases_and_country_order_cannot_bypass_known_denial() -> None:
    gate, mode, now = changing_grant_gate(initial="denied")
    claim = TrustGrantClaim("brand.com", "platform.com", brand_id="brand-1", countries=("US", "CA"))
    with pytest.raises(AdcpError, match="PERMISSION_DENIED"):
        await require_existing_claim(gate, claim)
    mode[0], now[0] = "offline", now[0] + 61
    alias_claim = TrustGrantClaim(
        "https://Ads.BRAND.com/path",
        "https://Agency.PLATFORM.com",
        brand_id="brand-1",
        countries=("CA", "US", "US"),
    )
    alias_buyer = BuyerAgent(
        agent_url="https://BUYER.PLATFORM.com:443/other", display_name="Buyer", status="active"
    )
    with pytest.raises(AdcpError, match="PERMISSION_DENIED"):
        await require_existing_claim(gate, alias_claim, alias_buyer)


@pytest.mark.parametrize("failure", ["offline", "invalid", "grant_absent"])
async def test_grant_error_codes_separate_outage_from_authorization(failure: str) -> None:
    gate, _ = grant_gate(failure)
    with pytest.raises(AdcpError) as rejected:
        await gate.require_claim(CLAIM, BUYER)
    error = rejected.value
    if failure in {"offline", "invalid"}:
        # An unreadable or invalid grantor record is an outage, never grant_absent.
        assert error.code == "SERVICE_UNAVAILABLE"
        assert error.recovery == "transient"
        assert not error.details
    else:
        assert error.code == "PERMISSION_DENIED"
        assert error.recovery == "correctable"
        assert error.details == {"scope": "agent", "reason": "grant_absent"}


@pytest.mark.parametrize(
    "operation,params",
    [
        ("get_products", None),
        ("get_media_buys", None),
        ("sync_creatives", None),
        ("provide_performance_feedback", None),
        ("control_media_buy", {"canceled": True}),
        ("update_media_buy", {"paused": True}),
    ],
)
async def test_bound_existing_account_nonspend_continues_on_true_outage(
    operation: str, params: dict[str, Any] | None
) -> None:
    gate, mode, now = changing_grant_gate(initial="allowed")
    account = Account(id="existing", metadata={"claim": CLAIM})
    for state in ("allowed", "offline"):
        mode[0] = state
        await gate.require_account(account, BUYER, operation=operation, operation_params=params)
        now[0] += 61


@pytest.mark.parametrize("operation", ["sync_accounts", "create_media_buy", "unknown_operation"])
async def test_provisioning_spend_and_unknown_operations_require_fresh_authority(
    operation: str,
) -> None:
    gate, _ = grant_gate("offline")
    with pytest.raises(AdcpError) as rejected:
        await gate.require_account(Account(id="existing"), BUYER, operation=operation)
    # An unknown task also requires its own explicit grant scope.
    assert rejected.value.code == (
        "PERMISSION_DENIED" if operation == "unknown_operation" else "SERVICE_UNAVAILABLE"
    )


async def test_new_account_declaration_never_uses_cached_binding_to_create_authority() -> None:
    gate, _ = grant_gate("offline")
    with pytest.raises(AdcpError) as rejected:
        await gate.require_declaration(
            {"brand": {"domain": "brand.com"}, "operator": "platform.com"}, BUYER
        )
    assert rejected.value.code == "SERVICE_UNAVAILABLE"


@pytest.mark.parametrize(
    "params",
    [
        {"paused": False},
        {"total_budget": 1000},
        {"daily_budget_cap": None},
        {"packages": [{"budget": 500}]},
    ],
)
async def test_resume_and_financial_changes_require_fresh_grant_authority(
    params: dict[str, Any],
) -> None:
    gate, _ = grant_gate("offline")
    with pytest.raises(AdcpError, match="SERVICE_UNAVAILABLE"):
        await gate.require_account(
            Account(id="existing"),
            BUYER,
            operation="update_media_buy",
            operation_params=params,
        )


async def test_seller_spend_classification_is_authoritative_for_nonspend_default() -> None:
    gate, _ = grant_gate("offline")
    with pytest.raises(AdcpError, match="SERVICE_UNAVAILABLE"):
        await gate.require_account(
            Account(id="existing"),
            BUYER,
            operation="sync_creatives",
            spend_committing=True,
        )


async def test_handler_preserves_explicit_financial_null_for_spend_classification() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from pydantic import BaseModel

    from adcp.decisioning import (
        AuthInfo,
        DecisioningCapabilities,
        DecisioningPlatform,
        HttpSigCredential,
        InMemoryTaskRegistry,
        SingletonAccounts,
        signing_only_registry,
    )
    from adcp.decisioning.handler import PlatformHandler
    from adcp.server import ToolContext

    class Change(BaseModel):
        daily_budget_cap: int | None = None

    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="existing")

    async def lookup(origin: str) -> BuyerAgent:
        return BUYER

    context = ToolContext(
        metadata={
            "adcp.auth_info": AuthInfo(
                kind="http_sig",
                credential=HttpSigCredential(
                    kind="http_sig", keyid="kid", agent_url=ORIGIN, verified_at=1000
                ),
                extra={"signing_profile": "web-bot-auth", "principal_id": "approved-principal"},
            )
        }
    )
    gate, _ = grant_gate("offline")
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            Platform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            buyer_agent_registry=signing_only_registry(lookup),
            trust_grant_gate=gate,
        )
        with pytest.raises(AdcpError, match="SERVICE_UNAVAILABLE"):
            await handler._resolve_account(
                None,
                context,
                tool_name="update_media_buy",
                request_params=Change(daily_budget_cap=None),
            )


async def test_read_without_a_prior_record_cannot_continue_through_an_outage() -> None:
    gate, _ = grant_gate("offline")
    with pytest.raises(AdcpError, match="SERVICE_UNAVAILABLE"):
        await gate.require_account(Account(id="existing"), BUYER, operation="get_products")


async def test_unestablished_agent_keeps_oracle_clamp_even_during_outage() -> None:
    gate, _ = grant_gate("offline")
    with pytest.raises(AdcpError) as rejected:
        await gate.require_claim(CLAIM, None)
    assert rejected.value.code == "PERMISSION_DENIED"
    assert not rejected.value.details


@pytest.mark.parametrize("failure", ["invalid", "grant_absent"])
async def test_cached_binding_does_not_override_missing_or_invalid_grants(failure: str) -> None:
    gate, _ = grant_gate(failure)
    with pytest.raises(AdcpError) as rejected:
        await gate.require_account(Account(id="existing"), BUYER, operation="get_products")
    assert rejected.value.code == (
        "SERVICE_UNAVAILABLE" if failure == "invalid" else "PERMISSION_DENIED"
    )


@pytest.mark.parametrize("transport", ["mcp", "a2a"])
@pytest.mark.parametrize("failure", ["offline", "invalid", "grant_absent"])
async def test_valid_signed_authorization_failure_reaches_standard_client_error_envelope(
    transport: str, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADCP_SANDBOX", "1")
    gate, records = grant_gate(failure)
    private = Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    bindings = OriginBindings()
    bindings.bind(ORIGIN, "approved-principal")

    class Handler(ADCPHandler[Any]):
        async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
            assert context.metadata["adcp.auth_info"].extra["principal_id"] == "approved-principal"
            await gate.require_claim(CLAIM, BUYER)
            raise AssertionError("authorization failure must prevent handler side effects")

    async def not_revoked(origin: str, keyid: str) -> bool:
        return False

    app = _build_mcp_and_a2a_app(
        Handler(),
        name="authorization-test",
        port=0,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        advertise_all=True,
        stateless_http=True,
        allowed_hosts=["localhost"],
        request_signature_verification=RequestSignatureVerification(
            signer_keys=StaticSignerKeys({ORIGIN: {"keys": [jwk]}}),
            enable_web_bot_auth=True,
            origin_bindings=bindings,
            trust_records=records,
            wba_revocation_checker=not_revoked,
        ),
    )
    if transport == "mcp":
        path = "/mcp"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_products", "arguments": {}},
        }
    else:
        path = "/"
        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "m1",
                    "role": "user",
                    "parts": [
                        {"kind": "data", "data": {"skill": "get_products", "parameters": {}}}
                    ],
                }
            },
        }
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    headers.update(
        sign_wba_request(
            method="POST",
            url="http://localhost" + path,
            headers=headers,
            body=body,
            private_key=private,
            agent_origin=ORIGIN,
        )
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client:
            response = await client.post(path, content=body, headers=headers)
    response.raise_for_status()
    assert "accept-signature" not in response.headers
    wire = response.json()["result"]
    if transport == "mcp":
        decoded = CallToolResult.model_validate(wire)
        assert decoded.is_error
        error = extract_adcp_error(decoded)
    else:
        assert wire["status"]["state"] == "failed"
        error = next(
            part["data"]["adcp_error"]
            for artifact in wire["artifacts"]
            for part in artifact["parts"]
            if part.get("kind") == "data" and "adcp_error" in part.get("data", {})
        )
    assert error is not None
    outage = failure in {"offline", "invalid"}
    assert error["code"] == ("SERVICE_UNAVAILABLE" if outage else "PERMISSION_DENIED")
    assert error["recovery"] == ("transient" if outage else "correctable")
    assert error.get("details", {}) == (
        {"scope": "agent", "reason": "grant_absent"} if failure == "grant_absent" else {}
    )
    await assert_public_typed_error(transport, wire, error, monkeypatch)


async def assert_public_typed_error(
    transport: str, wire: dict[str, Any], error: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Feed the actual wire error through the public typed client too. Returning
    # HTTP 200 is insufficient if typed parsing drops the retry/code metadata.
    from unittest.mock import AsyncMock

    from adcp import ADCPClient, AgentConfig
    from adcp.types import GetProductsRequest

    if transport == "mcp":
        decoded = CallToolResult.model_validate(wire)

        class Session:
            async def call_tool(
                self, name: str, arguments: dict[str, Any], **kwargs: Any
            ) -> CallToolResult:
                assert name == "get_products"
                return decoded

        typed_client = ADCPClient.from_mcp_client(Session(), agent_id="seller")
    else:
        from a2a import types as pb
        from google.protobuf.json_format import ParseDict
        from google.protobuf.struct_pb2 import Value

        typed_client = ADCPClient(
            AgentConfig(id="seller", agent_uri="http://localhost", protocol="a2a")
        )
        # The HTTP binding uses the legacy JSON shape; the A2A SDK passes
        # protobuf tasks to our adapter after decoding that binding.
        parts = [
            pb.Part(data=ParseDict(part["data"], Value()))
            for artifact in wire["artifacts"]
            for part in artifact["parts"]
            if part.get("kind") == "data"
        ]
        task = pb.Task(
            status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_FAILED),
            artifacts=[pb.Artifact(parts=parts)],
        )
        raw_result = typed_client.adapter._process_task_response(task, None)
        monkeypatch.setattr(
            typed_client.adapter, "get_products", AsyncMock(return_value=raw_result)
        )
    typed_result = await typed_client.get_products(
        GetProductsRequest(buying_mode="brief", brief="Any")
    )
    assert not typed_result.success
    assert typed_result.adcp_error == error
    assert typed_result.adcp_error["recovery"] == error["recovery"]


@pytest.mark.parametrize("transport", ["mcp", "a2a"])
@pytest.mark.parametrize(
    "signer_failure",
    [
        "revoked",
        "directory_offline",
        "directory_503",
        "directory_429",
        "directory_404",
        "replay_outage",
        "revocation_offline",
    ],
)
async def test_signer_failures_never_authenticate_or_reach_business_effects(
    transport: str, signer_failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DR-0023: the signature path answers with WBA statuses only, never 503.

    A revoked key is 403 without Accept-Signature; a directory that cannot be
    resolved leaves the request unverified (403 with Accept-Signature); both
    carry the generic authentication error. A replay-store outage is an
    application-layer SERVICE_UNAVAILABLE. An origin whose revocation list
    cannot be read is still identity-established: unbound, it gets what an
    anonymous caller gets.
    """
    from adcp.server import ToolContext
    from adcp.server.auth import BearerTokenAuth, current_principal
    from adcp.server.signed_requests import current_verified_signer, verified_signer_for_request
    from adcp.signing import (
        DirectoryResponse,
        DiscoveredSignerKeys,
        InMemoryReplayStore,
        OriginRevocationChecker,
    )
    from adcp.signing.wba import GENERIC_AUTHENTICATION_ERROR

    monkeypatch.setenv("ADCP_SANDBOX", "1")
    private = Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    effects: list[str] = []
    observations: list[str] = []

    class Handler(ADCPHandler[Any]):
        async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
            effects.append("handler")
            return {"products": []}

    async def validate_token(token: str) -> None:
        effects.append("bearer lookup")
        return None

    async def business_middleware(skill: str, params: Any, context: Any, call_next: Any) -> Any:
        effects.append("business middleware")
        return await call_next()

    def context_factory(metadata: Any) -> ToolContext:
        observations.append(metadata.transport)
        assert current_verified_signer.get() is None
        assert current_principal.get() is None
        assert verified_signer_for_request(metadata.request_context) is None
        scope = getattr(metadata.request_context, "scope", None)
        if scope is not None:
            assert not getattr(scope.get("user"), "is_authenticated", False)
        return ToolContext()

    async def revoked(origin: str, keyid: str) -> bool:
        assert origin == ORIGIN and keyid == jwk["kid"]
        return signer_failure == "revoked"

    async def directory_fetch(url: str, *, allow_private: bool = False) -> DirectoryResponse:
        if signer_failure == "directory_offline":
            raise OSError("directory offline")
        return DirectoryResponse(b"", {}, status=int(signer_failure.rpartition("_")[2]))

    async def revocation_fetch(url: str, *, allow_private: bool = False) -> Any:
        raise OSError("revocation list unavailable past grace")

    class BrokenReplayStore(InMemoryReplayStore):
        def claim(self, keyid: str, nonce: str, ttl_seconds: float) -> Any:
            raise RuntimeError("replay store offline")

    signer_keys = (
        DiscoveredSignerKeys(fetcher=directory_fetch)
        if signer_failure.startswith("directory_")
        else StaticSignerKeys({ORIGIN: {"keys": [jwk]}})
    )
    revocation_checker = (
        OriginRevocationChecker(signer_keys, fetcher=revocation_fetch)
        if signer_failure == "revocation_offline"
        else revoked
    )

    config = RequestSignatureVerification(
        signer_keys=signer_keys,
        enable_web_bot_auth=True,
        wba_revocation_checker=revocation_checker,
        replay_store=(
            BrokenReplayStore() if signer_failure == "replay_outage" else InMemoryReplayStore()
        ),
    )
    app = _build_mcp_and_a2a_app(
        Handler(),
        name="revoked-test",
        port=0,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        advertise_all=True,
        stateless_http=True,
        allowed_hosts=["localhost"],
        auth=BearerTokenAuth(validate_token=validate_token),
        context_factory=context_factory,
        middleware=[business_middleware],
        request_signature_verification=config,
    )
    path = "/mcp" if transport == "mcp" else "/"
    params = (
        {"name": "get_products", "arguments": {}}
        if transport == "mcp"
        else {
            "message": {
                "messageId": "revoked-m1",
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "get_products", "parameters": {}}}],
            }
        }
    )
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call" if transport == "mcp" else "message/send",
            "params": params,
        }
    ).encode()
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    headers.update(
        sign_wba_request(
            method="POST",
            url="http://localhost" + path,
            headers=headers,
            body=body,
            private_key=private,
            agent_origin=ORIGIN,
        )
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client:
            response = await client.post(path, content=body, headers=headers)
            # Error-only admission must not survive to an unrelated unsigned call.
            unsigned = await client.post(
                path,
                content=body,
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
    assert unsigned.status_code == 401
    assert not effects
    assert current_verified_signer.get() is None
    assert current_principal.get() is None
    if signer_failure == "revoked" or signer_failure.startswith("directory_"):
        assert response.status_code == 403
        assert response.json() == GENERIC_AUTHENTICATION_ERROR
        # Re-sign on a discovery failure; stop using a revoked key.
        assert ("accept-signature" in response.headers) == (signer_failure != "revoked")
        assert not observations
        return
    assert "accept-signature" not in response.headers
    if signer_failure == "revocation_offline":
        # Identity-established and unbound: what an anonymous caller gets,
        # here the bearer challenge, with no business effect.
        assert response.status_code == 401
        assert not observations
        return
    response.raise_for_status()
    assert observations == [transport]
    wire = response.json()["result"]
    if transport == "mcp":
        decoded = CallToolResult.model_validate(wire)
        assert decoded.is_error
        error = extract_adcp_error(decoded)
    else:
        assert wire["status"]["state"] == "failed"
        error = next(
            part["data"]["adcp_error"]
            for artifact in wire["artifacts"]
            for part in artifact["parts"]
            if part.get("kind") == "data"
        )
    assert error is not None and error["code"] == "SERVICE_UNAVAILABLE"
    assert error["recovery"] == "transient"
    assert not error.get("details")
    await assert_public_typed_error(transport, wire, error, monkeypatch)
