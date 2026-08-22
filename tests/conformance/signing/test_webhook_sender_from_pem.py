"""Round-trip: adcp-keygen-produced PEM + public JWK glue together via from_pem.

The DX contract this closes: ``adcp-keygen --purpose webhook-signing`` writes
a PEM and prints the PUBLIC JWK. The PEM has the private key material but no
``d`` scalar for :meth:`WebhookSender.from_jwk`. ``from_pem`` is the companion
that takes the same PEM, binds the kid, and returns a sender whose signatures
verify against the printed JWK.
"""

from __future__ import annotations

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
from adcp.signing.keygen import generate_ed25519, generate_es256
from adcp.webhooks import (
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookSender,
    WebhookVerifyOptions,
)


def _build_receiver_app(jwk: dict[str, Any]) -> FastAPI:
    receiver = WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [jwk]}),
            ),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="test-receiver",
            publisher_scope_for=lambda _signer: "test-publisher",
            kind="mcp",
        ),
    )
    app = FastAPI()

    @app.post("/webhooks/adcp")
    async def endpoint(request: Request) -> JSONResponse:
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
            )
        return JSONResponse(
            {
                "duplicate": outcome.duplicate,
                "sender": outcome.sender_identity,
            },
            status_code=outcome.http_status or 200,
        )

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("generator", "alg"),
    [(generate_ed25519, "ed25519"), (generate_es256, "es256")],
)
async def test_from_pem_signatures_verify_against_public_jwk(
    tmp_path: Path, generator: Any, alg: str
) -> None:
    """The core DX loop: keygen writes a PEM, publishes the JWK, from_pem
    rehydrates the PEM into a sender, and the sender's signatures verify
    against that same JWK. No private-key-material-in-JWK roundabout."""
    kid = f"webhook-{alg}-kid"
    pem_bytes, public_jwk = generator(kid=kid, adcp_use="webhook-signing")
    pem_path = tmp_path / "webhook-key.pem"
    pem_path.write_bytes(pem_bytes)

    app = _build_receiver_app(public_jwk)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sender = WebhookSender.from_pem(pem_path, key_id=kid, alg=alg, client=client)
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_from_pem",
            task_type="create_media_buy",
            status="completed",
            result={"media_buy_id": "mb_1"},
        )

    assert result.ok, result.response_body
    body = json.loads(result.response_body)
    assert body["sender"] == kid
    assert body["duplicate"] is False


@pytest.mark.asyncio
async def test_from_pem_accepts_raw_bytes() -> None:
    """The pem_path kwarg accepts bytes directly — useful for callers who
    load the PEM from a secrets store (Vault, AWS Secrets Manager) and
    never touch the filesystem."""
    kid = "webhook-bytes-kid"
    pem_bytes, public_jwk = generate_ed25519(kid=kid, adcp_use="webhook-signing")

    app = _build_receiver_app(public_jwk)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sender = WebhookSender.from_pem(pem_bytes, key_id=kid, client=client)
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_bytes",
            task_type="create_media_buy",
            status="completed",
        )
    assert result.ok, result.response_body


@pytest.mark.asyncio
async def test_from_pem_encrypted_pem_with_passphrase(tmp_path: Path) -> None:
    """Round-trip the ``adcp-keygen --encrypt`` output: an encrypted PEM,
    loaded by from_pem with the matching passphrase, signs webhooks the
    published JWK verifies."""
    kid = "webhook-enc-kid"
    passphrase = b"correct horse battery staple"
    pem_bytes, public_jwk = generate_ed25519(
        kid=kid, passphrase=passphrase, adcp_use="webhook-signing"
    )
    assert b"ENCRYPTED" in pem_bytes
    pem_path = tmp_path / "enc.pem"
    pem_path.write_bytes(pem_bytes)

    app = _build_receiver_app(public_jwk)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sender = WebhookSender.from_pem(pem_path, key_id=kid, passphrase=passphrase, client=client)
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_enc",
            task_type="create_media_buy",
            status="completed",
        )
    assert result.ok, result.response_body


def test_from_pem_wrong_passphrase_raises_clear_error(tmp_path: Path) -> None:
    """Passing the wrong passphrase must surface an error at construction
    time rather than producing a silently-broken sender."""
    pem_bytes, _ = generate_ed25519(kid="kid", passphrase=b"right", adcp_use="webhook-signing")
    pem_path = tmp_path / "enc.pem"
    pem_path.write_bytes(pem_bytes)

    with pytest.raises(ValueError):
        WebhookSender.from_pem(pem_path, key_id="kid", passphrase=b"wrong")


def test_from_pem_missing_passphrase_on_encrypted_pem(tmp_path: Path) -> None:
    """Cryptography raises TypeError when we omit the passphrase on an
    encrypted PEM — surfaced unmodified so the caller sees exactly what
    went wrong."""
    pem_bytes, _ = generate_ed25519(kid="kid", passphrase=b"phrase", adcp_use="webhook-signing")
    pem_path = tmp_path / "enc.pem"
    pem_path.write_bytes(pem_bytes)

    with pytest.raises(TypeError):
        WebhookSender.from_pem(pem_path, key_id="kid")


def test_from_pem_rejects_unsupported_alg(tmp_path: Path) -> None:
    pem_bytes, _ = generate_ed25519(kid="kid", adcp_use="webhook-signing")
    pem_path = tmp_path / "k.pem"
    pem_path.write_bytes(pem_bytes)

    with pytest.raises(ValueError, match="unsupported alg"):
        WebhookSender.from_pem(pem_path, key_id="kid", alg="rsa")


def test_from_pem_detects_alg_pem_mismatch(tmp_path: Path) -> None:
    """An ed25519 PEM declared as es256 (or vice versa) would produce
    signatures no verifier can validate. from_pem detects the mismatch
    at construction and raises with a remediation hint."""
    ed_pem, _ = generate_ed25519(kid="kid", adcp_use="webhook-signing")
    ed_path = tmp_path / "ed.pem"
    ed_path.write_bytes(ed_pem)
    with pytest.raises(ValueError, match="PEM holds"):
        WebhookSender.from_pem(ed_path, key_id="kid", alg="es256")

    es_pem, _ = generate_es256(kid="kid", adcp_use="webhook-signing")
    es_path = tmp_path / "es.pem"
    es_path.write_bytes(es_pem)
    with pytest.raises(ValueError, match="PEM holds"):
        WebhookSender.from_pem(es_path, key_id="kid", alg="ed25519")
