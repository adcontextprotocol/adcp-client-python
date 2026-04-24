"""Tests for the buyer-side preset for raw-httpx adapters.

``install_signing_event_hook`` mirrors what ``ADCPClient`` does
internally — for adapters that don't use the high-level client. Each
test goes outbound through the hook and verifies the produced
signature with the RFC 9421 verifier, the same end-to-end shape used
by the existing ``test_autosign_hook.py`` suite.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from adcp.signing import (
    SigningConfig,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    install_signing_event_hook,
    private_key_from_jwk,
    signing_operation,
    verify_request_signature,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    CoversContentDigest,
    RequestSigning,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
ED25519_KEY = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")


def _config() -> SigningConfig:
    return SigningConfig(
        private_key=private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only"),
        key_id=ED25519_KEY["kid"],
        alg="ed25519",
    )


def _capability(
    *,
    required: list[str] | None = None,
    warn: list[str] | None = None,
    supported_for: list[str] | None = None,
    covers: CoversContentDigest = CoversContentDigest.either,
    signing_supported: bool = True,
) -> RequestSigning:
    return RequestSigning(
        supported=signing_supported,
        covers_content_digest=covers,
        required_for=required or [],
        warn_for=warn or [],
        supported_for=supported_for,
    )


def _verify(
    request: httpx.Request,
    body: bytes,
    *,
    operation: str,
    covers_policy: str = "either",
    required_for: frozenset[str] = frozenset(),
) -> None:
    jwks_resolver = StaticJwksResolver({"keys": [ED25519_KEY]})
    options = VerifyOptions(
        now=float(int(time.time())),
        capability=VerifierCapability(
            covers_content_digest=covers_policy,  # type: ignore[arg-type]
            required_for=required_for,
        ),
        operation=operation,
        jwks_resolver=jwks_resolver,
    )
    verify_request_signature(
        method=request.method,
        url=str(request.url),
        headers=dict(request.headers),
        body=body,
        options=options,
    )


@pytest.mark.asyncio
async def test_signs_required_for_operation() -> None:
    body = b'{"plan_id":"p1"}'
    request = httpx.Request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={"Content-Type": "application/json"},
        content=body,
    )

    capability = _capability(required=["create_media_buy"])
    client = httpx.AsyncClient()
    install_signing_event_hook(client, signing=_config(), seller_capability=capability)
    [hook] = client.event_hooks["request"]

    with signing_operation("create_media_buy"):
        await hook(request)

    assert "Signature" in request.headers
    assert "Signature-Input" in request.headers
    _verify(
        request,
        body,
        operation="create_media_buy",
        required_for=frozenset({"create_media_buy"}),
    )


@pytest.mark.asyncio
async def test_skips_unsigned_operation_not_in_any_list() -> None:
    request = httpx.Request(
        method="POST",
        url="https://seller.example.com/adcp/get_products",
        headers={"Content-Type": "application/json"},
        content=b"{}",
    )

    capability = _capability(required=["create_media_buy"])
    client = httpx.AsyncClient()
    install_signing_event_hook(client, signing=_config(), seller_capability=capability)
    [hook] = client.event_hooks["request"]

    with signing_operation("get_products"):
        await hook(request)

    assert "Signature" not in request.headers


@pytest.mark.asyncio
async def test_skips_when_current_operation_unset() -> None:
    """No ContextVar → out-of-band call (health check, capability prefetch)."""
    request = httpx.Request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={"Content-Type": "application/json"},
        content=b"{}",
    )

    capability = _capability(required=["create_media_buy"])
    client = httpx.AsyncClient()
    install_signing_event_hook(client, signing=_config(), seller_capability=capability)
    [hook] = client.event_hooks["request"]

    # No `signing_operation` block — current_operation stays None.
    await hook(request)

    assert "Signature" not in request.headers


@pytest.mark.asyncio
async def test_skips_get_adcp_capabilities() -> None:
    """Bootstrap carve-out: signing the capability prefetch would recurse."""
    request = httpx.Request(
        method="POST",
        url="https://seller.example.com/adcp/get_adcp_capabilities",
        headers={"Content-Type": "application/json"},
        content=b"{}",
    )

    capability = _capability(required=["get_adcp_capabilities"])
    client = httpx.AsyncClient()
    install_signing_event_hook(client, signing=_config(), seller_capability=capability)
    [hook] = client.event_hooks["request"]

    with signing_operation("get_adcp_capabilities"):
        await hook(request)

    assert "Signature" not in request.headers


@pytest.mark.asyncio
async def test_supports_async_capability_provider() -> None:
    """A provider that returns an awaitable resolves correctly."""
    body = b'{"x":1}'
    request = httpx.Request(
        method="POST",
        url="https://seller.example.com/adcp/sync_creatives",
        headers={"Content-Type": "application/json"},
        content=body,
    )

    capability = _capability(supported_for=["sync_creatives"])

    async def provider() -> RequestSigning | None:
        return capability

    client = httpx.AsyncClient()
    install_signing_event_hook(client, signing=_config(), capability_provider=provider)
    [hook] = client.event_hooks["request"]

    with signing_operation("sync_creatives"):
        await hook(request)

    assert "Signature" in request.headers


@pytest.mark.asyncio
async def test_supports_sync_capability_provider() -> None:
    capability = _capability(required=["create_media_buy"])

    def provider() -> RequestSigning | None:
        return capability

    body = b'{"plan_id":"p1"}'
    request = httpx.Request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={"Content-Type": "application/json"},
        content=body,
    )

    client = httpx.AsyncClient()
    install_signing_event_hook(client, signing=_config(), capability_provider=provider)
    [hook] = client.event_hooks["request"]

    with signing_operation("create_media_buy"):
        await hook(request)

    assert "Signature" in request.headers


def test_requires_exactly_one_of_capability_or_provider() -> None:
    config = _config()
    client = httpx.AsyncClient()

    with pytest.raises(ValueError, match="exactly one"):
        install_signing_event_hook(client, signing=config)

    capability = _capability()

    def provider() -> RequestSigning | None:
        return capability

    with pytest.raises(ValueError, match="exactly one"):
        install_signing_event_hook(
            client,
            signing=config,
            seller_capability=capability,
            capability_provider=provider,
        )


def test_signing_operation_resets_context_var_on_exit() -> None:
    from adcp.signing.autosign import current_operation

    assert current_operation.get() is None
    with signing_operation("create_media_buy"):
        assert current_operation.get() == "create_media_buy"
    assert current_operation.get() is None


def test_signing_operation_resets_on_exception() -> None:
    from adcp.signing.autosign import current_operation

    assert current_operation.get() is None
    with pytest.raises(RuntimeError):
        with signing_operation("create_media_buy"):
            raise RuntimeError("boom")
    assert current_operation.get() is None


@pytest.mark.asyncio
async def test_appends_to_existing_event_hooks() -> None:
    """Pre-existing request hooks are preserved; the signer is appended."""
    pre_existing_called: list[Any] = []

    async def existing_hook(_request: httpx.Request) -> None:
        pre_existing_called.append(True)

    client = httpx.AsyncClient(event_hooks={"request": [existing_hook]})
    install_signing_event_hook(
        client,
        signing=_config(),
        seller_capability=_capability(required=["create_media_buy"]),
    )

    request = httpx.Request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={"Content-Type": "application/json"},
        content=b"{}",
    )

    hooks = client.event_hooks["request"]
    assert len(hooks) == 2
    with signing_operation("create_media_buy"):
        for hook in hooks:
            await hook(request)

    assert pre_existing_called == [True]
    assert "Signature" in request.headers
