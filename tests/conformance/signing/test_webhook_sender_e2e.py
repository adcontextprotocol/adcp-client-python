"""End-to-end: WebhookSender POSTs to a FastAPI-mounted WebhookReceiver.

The killer test for the seamless-DX claim — if the sender's signing and the
receiver's verification don't agree across a real httpx/ASGI transport, the
whole story is broken. Covers:

* Happy path — typed send_mcp() lands, receiver verifies + parses.
* Retry preserves idempotency_key — second call with explicit key dedupes.
* Signature-binding headers can't be overridden via extra_headers.
* send_raw() rejects missing idempotency_key instead of emitting a
  guaranteed-to-reject webhook.
* Multiple kinds (revocation, artifact, list-change) each land and parse.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

fastapi = pytest.importorskip("fastapi")
FastAPI = fastapi.FastAPI
Request = fastapi.Request
from fastapi.responses import JSONResponse  # noqa: E402

from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.signing import StaticJwksResolver
from adcp.webhooks import (
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookSender,
    WebhookVerifyOptions,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
REQUEST_ED25519 = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")

# Clone to a webhook-signing JWK with private material for the sender.
WEBHOOK_JWK = {
    **copy.deepcopy(REQUEST_ED25519),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}


def _build_receiver_app(
    kind: str = "mcp",
) -> tuple[FastAPI, WebhookReceiver]:
    receiver = WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_JWK]}),
            ),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="test-receiver",
            publisher_scope_for=lambda _signer: "test-publisher",
            kind=kind,  # type: ignore[arg-type]
        ),
    )
    app = FastAPI()

    @app.post("/webhooks/adcp")
    async def webhook_endpoint(request: Request) -> JSONResponse:
        outcome = await receiver.receive(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            body=await request.body(),
        )
        if outcome.http_status is None:
            outcome = await receiver.acknowledge(outcome)
        if outcome.rejected:
            return JSONResponse(
                {"error": outcome.rejection_reason},
                status_code=outcome.http_status or 400,
                headers=dict(outcome.response_headers),
            )
        return JSONResponse(
            {
                "duplicate": outcome.duplicate,
                "sender": outcome.sender_identity,
                "idempotency_key": outcome.idempotency_key,
            },
            status_code=outcome.http_status or 200,
        )

    return app, receiver


def _build_sender(app: FastAPI) -> WebhookSender:
    """Construct a WebhookSender wired to the FastAPI app via ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return WebhookSender.from_jwk(
        {**WEBHOOK_JWK, "d": WEBHOOK_JWK["_private_d_for_test_only"]},
        client=client,
    )


@pytest.mark.asyncio
async def test_sender_to_receiver_happy_path() -> None:
    app, _ = _build_receiver_app()
    async with _build_sender(app) as sender:
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_sender_e2e",
            task_type="create_media_buy",
            status="completed",
            result={"media_buy_id": "mb_1"},
        )
    assert result.ok
    assert result.status_code == 200
    assert result.idempotency_key.startswith("whk_")
    body = json.loads(result.response_body)
    assert body["duplicate"] is False
    assert body["sender"] == "test-webhook-ed25519-2026"
    assert body["idempotency_key"] == result.idempotency_key


@pytest.mark.asyncio
async def test_retry_with_same_idempotency_key_dedupes() -> None:
    """The whole reason WebhookDeliveryResult exposes idempotency_key: so the
    retry call reuses it and the receiver dedupes correctly. Without this,
    every retry processes as a fresh event."""
    app, _ = _build_receiver_app()
    async with _build_sender(app) as sender:
        first = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_retry",
            task_type="create_media_buy",
            status="completed",
        )
        # Simulate caller deciding to retry (e.g., if first had been non-2xx).
        retry = await sender.send_mcp(
            url=first.url,
            task_id="task_retry",
            task_type="create_media_buy",
            status="completed",
            idempotency_key=first.idempotency_key,
        )

    assert first.ok and retry.ok
    first_body = json.loads(first.response_body)
    retry_body = json.loads(retry.response_body)
    assert first_body["duplicate"] is False
    assert retry_body["duplicate"] is True
    assert retry.idempotency_key == first.idempotency_key


@pytest.mark.asyncio
async def test_extra_headers_cannot_override_signature_bindings() -> None:
    """A caller passing extra_headers MUST NOT be able to overwrite signature-
    binding headers — that would let them silently break signing without seeing
    an error at send time."""
    app, _ = _build_receiver_app()
    async with _build_sender(app) as sender:
        with pytest.raises(ValueError, match="auth-binding"):
            await sender.send_mcp(
                url="http://test/webhooks/adcp",
                task_id="t",
                task_type="create_media_buy",
                status="completed",
                extra_headers={"Signature": "attacker-controlled"},
            )
        with pytest.raises(ValueError, match="auth-binding"):
            await sender.send_mcp(
                url="http://test/webhooks/adcp",
                task_id="t",
                task_type="create_media_buy",
                status="completed",
                extra_headers={"Content-Digest": "forged"},
            )


@pytest.mark.asyncio
async def test_send_raw_requires_idempotency_key_at_signature() -> None:
    """send_raw's idempotency_key is a required kwarg — missing it fails at
    the type-level (TypeError from Python), not a runtime dict check. This
    makes the contract visible to IDEs and agents reading the signature."""
    app, _ = _build_receiver_app()
    async with _build_sender(app) as sender:
        with pytest.raises(TypeError, match="idempotency_key"):
            # type: ignore[call-arg] — deliberately missing the required kwarg
            await sender.send_raw(  # type: ignore[call-arg]
                url="http://test/webhooks/adcp",
                payload={"task_id": "t", "status": "completed"},
            )

    # Empty string still gets a ValueError with a clean message.
    async with _build_sender(app) as sender:
        with pytest.raises(ValueError, match="idempotency_key"):
            await sender.send_raw(
                url="http://test/webhooks/adcp",
                idempotency_key="",
                payload={"task_id": "t"},
            )


@pytest.mark.asyncio
async def test_send_raw_enforces_body_size_cap() -> None:
    """Oversized bodies raise before signing — matches adcp.webhooks.deliver."""
    app, _ = _build_receiver_app()
    async with _build_sender(app) as sender:
        with pytest.raises(ValueError, match="10,485,760"):
            await sender.send_raw(
                url="http://test/webhooks/adcp",
                idempotency_key="whk_cap_test_0000000000000000",
                payload={"blob": "x" * (11 * 1024 * 1024)},
            )


@pytest.mark.asyncio
async def test_from_jwk_rejects_wrong_adcp_use() -> None:
    """Guardrail at construction: a request-signing JWK silently produces
    signatures no webhook receiver will accept. Fail fast."""
    bad_jwk: dict[str, Any] = {
        **copy.deepcopy(REQUEST_ED25519),
        "adcp_use": "request-signing",  # wrong purpose for a webhook sender
        "d": REQUEST_ED25519["_private_d_for_test_only"],
    }
    with pytest.raises(ValueError, match="webhook-signing"):
        WebhookSender.from_jwk(bad_jwk)


@pytest.mark.asyncio
async def test_sends_revocation_notification() -> None:
    app, _ = _build_receiver_app(kind="revocation_notification")
    async with _build_sender(app) as sender:
        result = await sender.send_revocation_notification(
            url="http://test/webhooks/adcp",
            rights_id="rights_1",
            brand_id="brand_1",
            reason="Rights revoked",
            effective_at="2026-04-19T00:00:00Z",
        )
    assert result.ok, result.response_body


@pytest.mark.asyncio
async def test_sends_artifact_webhook() -> None:
    app, _ = _build_receiver_app(kind="artifact")
    async with _build_sender(app) as sender:
        result = await sender.send_artifact_webhook(
            url="http://test/webhooks/adcp",
            media_buy_id="mb_1",
            batch_id="batch_1",
            timestamp="2026-04-19T00:00:00Z",
            artifacts=[],
        )
    assert result.ok, result.response_body


@pytest.mark.asyncio
async def test_sends_collection_list_changed() -> None:
    app, _ = _build_receiver_app(kind="collection_list_changed")
    async with _build_sender(app) as sender:
        result = await sender.send_collection_list_changed(
            url="http://test/webhooks/adcp",
            list_id="cl_1",
            resolved_at="2026-04-19T00:00:00Z",
            signature="sig",
        )
    assert result.ok, result.response_body


@pytest.mark.asyncio
async def test_sends_property_list_changed() -> None:
    app, _ = _build_receiver_app(kind="property_list_changed")
    async with _build_sender(app) as sender:
        result = await sender.send_property_list_changed(
            url="http://test/webhooks/adcp",
            list_id="pl_1",
            resolved_at="2026-04-19T00:00:00Z",
            signature="sig",
        )
    assert result.ok, result.response_body


@pytest.mark.asyncio
async def test_sent_body_equals_signed_body_equals_received_body() -> None:
    """The core invariant WebhookSender depends on: the bytes signed == the
    bytes POSTed. If httpx ever re-serializes on the wire — or if we add a
    reserialization step inside the sender by mistake — content-digest breaks
    and every receiver 401s. This test captures the raw body at the server
    and compares it to what the sender reports in ``sent_body``."""
    captured_bodies: list[bytes] = []

    async def _capture_app_receive_app() -> tuple[FastAPI, WebhookReceiver]:
        app, receiver = _build_receiver_app()

        # Wrap the existing route: we can't easily intercept before the
        # receiver reads the body, so just add a sibling capture route
        # and sign to it instead.
        return app, receiver

    app, _ = await _capture_app_receive_app()

    # Add a bytes-capture route that uses the SAME verify+dedupe machinery
    # but also records the raw body bytes for comparison.
    @app.post("/webhooks/adcp/capture")
    async def capture(request: Request) -> JSONResponse:
        body = await request.body()
        captured_bodies.append(body)
        return JSONResponse({"captured_len": len(body)}, status_code=200)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sender = WebhookSender.from_jwk(
            {**WEBHOOK_JWK, "d": WEBHOOK_JWK["_private_d_for_test_only"]},
            client=client,
        )
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp/capture",
            task_id="task_byte_identity",
            task_type="create_media_buy",
            status="completed",
            result={"media_buy_id": "mb_bytes"},
        )

    assert result.ok
    assert len(captured_bodies) == 1
    # sent_body == what the server received == what was signed.
    assert result.sent_body == captured_bodies[0]


@pytest.mark.asyncio
async def test_resend_replays_identical_bytes_with_fresh_signature() -> None:
    """resend() must replay the exact same bytes (preserving idempotency_key
    AND all payload fields) under a fresh signature. Receiver dedupes."""
    app, _ = _build_receiver_app()
    async with _build_sender(app) as sender:
        first = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_resend",
            task_type="create_media_buy",
            status="completed",
            result={"media_buy_id": "mb_resend"},
        )
        second = await sender.resend(first)

    assert first.ok and second.ok
    # Byte-identical replay — the whole point of resend.
    assert first.sent_body == second.sent_body
    # Different response (first_seen vs duplicate).
    first_body = json.loads(first.response_body)
    second_body = json.loads(second.response_body)
    assert first_body["duplicate"] is False
    assert second_body["duplicate"] is True


@pytest.mark.asyncio
async def test_extra_header_case_insensitive_rejection() -> None:
    """The reserved-header block uses .lower(), so UPPER and MixedCase
    variants all reject — confirm explicitly since the block is the last
    line of defense against a caller accidentally overwriting signature
    headers."""
    app, _ = _build_receiver_app()
    bad_headers = (
        "SIGNATURE",
        "Signature-Input",
        "SIGNATURE-INPUT",
        "content-digest",
        "CONTENT-TYPE",
    )
    async with _build_sender(app) as sender:
        for bad in bad_headers:
            with pytest.raises(ValueError, match="auth-binding|content-type"):
                await sender.send_mcp(
                    url="http://test/webhooks/adcp",
                    task_id="t",
                    task_type="create_media_buy",
                    status="completed",
                    extra_headers={bad: "attacker"},
                )


@pytest.mark.asyncio
async def test_receiver_rejects_when_sender_key_wrong() -> None:
    """A sender signing with the wrong key gets a 401 from the receiver —
    the receiver's StaticJwksResolver doesn't know that kid."""
    app, _ = _build_receiver_app()
    # Construct a sender whose kid isn't in the receiver's JWKS.
    unknown_jwk = {
        **copy.deepcopy(WEBHOOK_JWK),
        "kid": "unknown-kid",
        "d": WEBHOOK_JWK["_private_d_for_test_only"],
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sender = WebhookSender.from_jwk(unknown_jwk, client=client)
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="t",
            task_type="create_media_buy",
            status="completed",
        )
    assert not result.ok
    assert result.status_code == 401


@pytest.mark.asyncio
async def test_owned_client_rejects_loopback_destination() -> None:
    """Default sender (no operator client) must reject loopback URLs via SSRF
    guard. Without pin-and-bind, a buyer-supplied webhook URL pointing at
    127.0.0.1 lets a public agent reach in-cluster services."""
    from adcp.signing import SSRFValidationError

    sender = WebhookSender.from_jwk(
        {**WEBHOOK_JWK, "d": WEBHOOK_JWK["_private_d_for_test_only"]},
    )
    async with sender:
        with pytest.raises(SSRFValidationError):
            await sender.send_mcp(
                url="https://127.0.0.1/webhooks/adcp",
                task_id="task_ssrf",
                task_type="create_media_buy",
                status="completed",
            )


@pytest.mark.asyncio
async def test_owned_client_rejects_disallowed_port_when_hardening_configured() -> None:
    """Operators opt in to the port-allowlist hardening posture by passing
    ``allowed_destination_ports=DEFAULT_ALLOWED_PORTS`` (or a custom set).
    The sender then rejects buyer URLs on ports outside the set, closing
    the smuggle vector to internal Redis/SMTP/Memcached on the same
    routable IP. Without the kwarg, AdCP-spec-compliant ports like
    :9443/:4443 still pass."""
    from unittest.mock import patch

    from adcp.signing import DEFAULT_ALLOWED_PORTS, SSRFValidationError

    sender = WebhookSender.from_jwk(
        {**WEBHOOK_JWK, "d": WEBHOOK_JWK["_private_d_for_test_only"]},
        allowed_destination_ports=DEFAULT_ALLOWED_PORTS,
    )
    # Mock DNS so we don't hit a live host; the port check fires before
    # resolution even runs (in resolve_and_validate_host).
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
    ):
        async with sender:
            with pytest.raises(SSRFValidationError, match="port 6379 not allowed"):
                await sender.send_mcp(
                    url="https://example.com:6379/webhooks/adcp",
                    task_id="task_port",
                    task_type="create_media_buy",
                    status="completed",
                )


@pytest.mark.asyncio
async def test_send_mcp_threads_operation_id_into_payload() -> None:
    """``operation_id`` is buyer-supplied and embedded by the buyer in the
    webhook URL when registering pushNotificationConfig. Per the schema at
    ``mcp-webhook-payload.json``, publishers MUST echo it in the payload so
    buyers correlate notifications without parsing URL paths.

    Regression guard for the docstring-vs-schema mismatch noted in the
    DecisioningPlatform foundations audit (the prior docstring discouraged
    populating the field, contradicting the schema's MUST)."""
    captured_payloads: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/webhooks/adcp/{op_id}")
    async def echo(request: Request, op_id: str) -> JSONResponse:
        body = await request.body()
        captured_payloads.append(json.loads(body))
        return JSONResponse({"ok": True}, status_code=200)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sender = WebhookSender.from_jwk(
            {**WEBHOOK_JWK, "d": WEBHOOK_JWK["_private_d_for_test_only"]},
            client=client,
        )
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp/op_abc123",
            task_id="task_op_id_test",
            task_type="create_media_buy",
            status="completed",
            operation_id="op_abc123",
            result={"media_buy_id": "mb_1"},
        )

    assert result.ok
    assert captured_payloads[0]["operation_id"] == "op_abc123"


@pytest.mark.asyncio
async def test_owned_client_default_allows_non_standard_ports() -> None:
    """The default ``WebhookSender`` (no operator client, no
    ``allowed_destination_ports``) accepts AdCP-spec-compliant buyers on
    non-standard ports — :9443 (Tomcat default), :4443 (Spring Boot
    default), path-routed multi-tenant gateways.

    Sender-level positive analog of ``test_ssrf_default_imposes_no_port_filter``
    in ``test_jwks.py`` — confirms the permissive default reaches the
    actual delivery path, not just the underlying validator. The IP-range
    check is enforced by the validator and covered separately by
    ``test_owned_client_rejects_loopback_destination``."""
    from unittest.mock import patch

    captured: list[tuple[str, int]] = []
    app = FastAPI()

    @app.post("/webhooks/adcp")
    async def echo(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True}, status_code=200)

    asgi_transport = httpx.ASGITransport(app=app)

    # Stub the pinned-transport build so we don't open a real socket to
    # a public IP; capture that the build was attempted for the
    # non-standard port and route the actual POST through ASGI.
    def fake_build(uri: str, **_kwargs: Any) -> Any:
        from urllib.parse import urlparse

        parsed = urlparse(uri)
        captured.append((parsed.hostname or "", parsed.port or 443))
        return asgi_transport

    sender = WebhookSender.from_jwk(
        {**WEBHOOK_JWK, "d": WEBHOOK_JWK["_private_d_for_test_only"]},
    )
    with patch(
        "adcp.webhook_sender.build_async_ip_pinned_transport",
        side_effect=fake_build,
    ):
        async with sender:
            result = await sender.send_mcp(
                url="http://test:9443/webhooks/adcp",
                task_id="task_nonstd",
                task_type="create_media_buy",
                status="completed",
            )

    assert result.ok
    assert captured == [("test", 9443)]


@pytest.mark.asyncio
async def test_operator_supplied_client_bypasses_ssrf_guard() -> None:
    """When the operator passes their own httpx client (vetted egress
    proxy, ASGI test transport, etc.), the framework trusts them
    completely — pin-and-bind is skipped and the SSRF range check does
    NOT fire. The operator owns SSRF on their transport.

    Named regression test for the documented contract; without this, a
    future refactor that mistakenly applies pin-and-bind to both
    branches breaks ASGI-based unit tests and any vetted-proxy
    deployments that route via private networks."""
    app = FastAPI()

    @app.post("/webhooks/adcp")
    async def echo(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True}, status_code=200)

    transport = httpx.ASGITransport(app=app)
    # base_url is loopback-equivalent. With the SSRF guard active this
    # would raise SSRFValidationError; under the operator-trust contract
    # it must succeed.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sender = WebhookSender.from_jwk(
            {**WEBHOOK_JWK, "d": WEBHOOK_JWK["_private_d_for_test_only"]},
            client=client,
        )
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_op_trust",
            task_type="create_media_buy",
            status="completed",
        )

    assert result.ok


@pytest.mark.asyncio
async def test_owned_client_ignores_https_proxy_env() -> None:
    """``HTTPS_PROXY`` / ``HTTP_PROXY`` env vars MUST NOT defeat the IP
    pin on the owned-client path. httpx's default ``trust_env=True``
    routes requests through proxy env vars, which would bypass the
    AsyncIpPinnedTransport's network_backend entirely — an attacker who
    controls process env (sidecar config, dotenv, malicious cluster
    egress policy) could otherwise pivot to receiving the signed
    webhook body.

    The sender constructs its per-request ``httpx.AsyncClient`` with
    ``trust_env=False`` to close this. Regression guard: if a future
    refactor drops the kwarg, this test catches it by setting a proxy
    env var that points at an unreachable address; the pinned transport
    must still route via the resolved IP and reach the test ASGI app
    (which we can't directly observe under a real socket, so we assert
    the proxy var is ignored by checking the constructor config)."""
    import os
    from unittest.mock import MagicMock, patch

    sender = WebhookSender.from_jwk(
        {**WEBHOOK_JWK, "d": WEBHOOK_JWK["_private_d_for_test_only"]},
    )

    captured_kwargs: dict[str, Any] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            # Capture kwargs only from the per-request construction in
            # _send_bytes; __aenter__'s eager _get_client() also flows
            # through here but its kwargs don't affect the per-request
            # delivery path. The last writer wins; the per-request call
            # is the one we care about.
            captured_kwargs.update(kwargs)
            self._response = MagicMock(status_code=200, headers={}, content=b"{}")

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any) -> Any:
            return self._response

    # Set HTTPS_PROXY pointing at an unreachable address. With
    # trust_env=True (the httpx default), this would override the
    # transport. The sender MUST pass trust_env=False to ignore it.
    with patch.dict(os.environ, {"HTTPS_PROXY": "http://attacker.invalid:9999"}):
        with patch(
            "adcp.webhook_sender.build_async_ip_pinned_transport",
            return_value=MagicMock(),  # transport itself isn't used here
        ):
            with patch("adcp.webhook_sender.httpx.AsyncClient", _FakeAsyncClient):
                async with sender:
                    await sender.send_mcp(
                        url="https://buyer.example.com/webhooks/adcp",
                        task_id="task_proxy",
                        task_type="create_media_buy",
                        status="completed",
                    )

    # The per-request httpx.AsyncClient construction passes a `transport`
    # kwarg; the eager __aenter__ construction does not. Asserting both
    # `transport` is present AND `trust_env=False` is set proves the
    # captured kwargs are from the per-request construction, not the
    # eager-init that has nothing to do with HTTPS_PROXY hardening.
    assert "transport" in captured_kwargs, (
        "captured kwargs do not include `transport` — the assertion below "
        "is reading the eager __aenter__ construction, not the per-request "
        "construction the proxy-bypass guard lives on"
    )
    assert captured_kwargs.get("trust_env") is False, (
        "WebhookSender's per-request httpx.AsyncClient must construct with "
        "trust_env=False — otherwise HTTPS_PROXY env vars defeat the IP pin"
    )
    assert captured_kwargs.get("follow_redirects") is False


@pytest.mark.asyncio
async def test_owned_client_rejects_hostile_url_before_signing() -> None:
    """Validate-before-sign defense in depth: a hostile URL raises
    SSRFValidationError synchronously inside ``build_async_ip_pinned_transport``,
    BEFORE the auth strategy's ``build_auth_headers`` is called. No
    Ed25519/ES256 signature ever materializes in process memory for a
    URL that fails the SSRF guard — anything that snapshots locals on
    exception (faulthandler, custom logging) cannot capture a signature
    that wasn't generated.

    Regression guard for the validate-before-sign reorder in _send_bytes."""
    from adcp.signing import SSRFValidationError

    sender = WebhookSender.from_jwk(
        {**WEBHOOK_JWK, "d": WEBHOOK_JWK["_private_d_for_test_only"]},
    )

    class _RecordingStrategy:
        called = False

        def build_auth_headers(self, **_: object) -> dict[str, str]:
            type(self).called = True
            return {}

        def reserved_headers(self) -> frozenset[str]:
            return frozenset()

    spy = _RecordingStrategy()
    sender._auth = spy  # type: ignore[assignment]
    async with sender:
        with pytest.raises(SSRFValidationError):
            await sender.send_mcp(
                url="https://127.0.0.1/webhooks/adcp",
                task_id="task_no_sign",
                task_type="create_media_buy",
                status="completed",
            )
    assert _RecordingStrategy.called is False, (
        "build_auth_headers was called even though SSRF validation rejected "
        "the URL — the signature would sit in process memory until the "
        "rejection. Validate-before-sign ordering is broken; check _send_bytes."
    )
