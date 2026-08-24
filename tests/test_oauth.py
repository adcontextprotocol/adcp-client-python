"""Buyer-side OAuth discovery, PKCE, state, and exchange security tests."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import time
import zlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

import adcp
import adcp.oauth as oauth_module
from adcp.oauth import (
    InMemoryPendingOAuthFlowStore,
    OAuthAuthorizationError,
    OAuthAuthorizationServerMetadata,
    OAuthDiscoveryError,
    OAuthFlowStoreError,
    OAuthIssuerBinding,
    OAuthTokenExchangeError,
    PendingOAuthAuthorization,
    complete_oauth_authorization,
    discover_oauth_metadata,
    start_oauth_authorization,
)


def test_oauth_public_api_is_exported_from_module_and_package() -> None:
    expected = {
        "InMemoryPendingOAuthFlowStore",
        "OAuthAuthorizationError",
        "OAuthAuthorizationRequest",
        "OAuthAuthorizationServerMetadata",
        "OAuthClientError",
        "OAuthDiscoveryError",
        "OAuthFlowStoreError",
        "OAuthIssuerBinding",
        "OAuthTokenExchangeError",
        "OAuthTokenSet",
        "PendingOAuthAuthorization",
        "PendingOAuthFlowStore",
        "complete_oauth_authorization",
        "discover_oauth_metadata",
        "start_oauth_authorization",
    }
    assert set(oauth_module.__all__) == expected
    for name in expected:
        assert name in adcp.__all__
        assert getattr(adcp, name) is getattr(oauth_module, name)


def _metadata(**overrides: Any) -> OAuthAuthorizationServerMetadata:
    values: dict[str, Any] = {
        "issuer": "https://auth.example/tenant",
        "authorization_endpoint": "https://auth.example/authorize",
        "token_endpoint": "https://tokens.example/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["openid", "media.buy"],
        "authorization_response_iss_parameter_supported": True,
    }
    values.update(overrides)
    return OAuthAuthorizationServerMetadata.model_validate(values)


def _metadata_json(**overrides: Any) -> dict[str, Any]:
    metadata = _metadata(**overrides)
    return metadata.model_dump(mode="json")


def _mock_transport(handler: Any) -> Any:
    return patch(
        "adcp.oauth.build_async_ip_pinned_transport",
        return_value=httpx.MockTransport(handler),
    )


class _TrackingStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False
        self.iterated = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.iterated = True
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_discovery_inserts_well_known_before_issuer_path_and_matches_exact_issuer() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_metadata_json())

    with _mock_transport(handler):
        metadata = await discover_oauth_metadata("https://auth.example/tenant")

    assert metadata.issuer == "https://auth.example/tenant"
    assert str(captured[0].url) == (
        "https://auth.example/.well-known/oauth-authorization-server/tenant"
    )
    assert captured[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_discovery_preserves_trailing_slash_and_has_no_root_fallback() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(404)

    with _mock_transport(handler):
        with pytest.raises(OAuthDiscoveryError) as caught:
            await discover_oauth_metadata("https://auth.example/tenant/")

    assert caught.value.code == "http_error"
    assert urls == ["https://auth.example/.well-known/oauth-authorization-server/tenant"]


@pytest.mark.asyncio
async def test_discovery_strips_root_issuer_slash_only_for_well_known_path() -> None:
    captured: list[str] = []
    body = _metadata_json(
        issuer="https://auth.example/",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json=body)

    with _mock_transport(handler):
        result = await discover_oauth_metadata("https://auth.example/")
    assert captured == ["https://auth.example/.well-known/oauth-authorization-server"]
    assert result.issuer == "https://auth.example/"


@pytest.mark.asyncio
async def test_discovery_rejects_byte_different_issuer() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_metadata_json(issuer="https://auth.example/tenant/"))

    with _mock_transport(handler):
        with pytest.raises(OAuthDiscoveryError) as caught:
            await discover_oauth_metadata("https://auth.example/tenant")
    assert caught.value.code == "issuer_mismatch"


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"response_types_supported": ["token"]}, "authorization_code_unsupported"),
        ({"grant_types_supported": ["refresh_token"]}, "authorization_code_unsupported"),
        (
            {"token_endpoint_auth_methods_supported": ["client_secret_basic"]},
            "public_client_unsupported",
        ),
        ({"code_challenge_methods_supported": ["plain"]}, "s256_unsupported"),
    ],
)
@pytest.mark.asyncio
async def test_discovery_requires_public_authorization_code_s256_metadata(
    override: dict[str, Any], code: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_metadata_json(**override))

    with _mock_transport(handler):
        with pytest.raises(OAuthDiscoveryError) as caught:
            await discover_oauth_metadata("https://auth.example/tenant")
    assert caught.value.code == code


@pytest.mark.parametrize(
    "field,value",
    [
        ("authorization_endpoint", "https://user:password@auth.example/authorize"),
        ("token_endpoint", "https://auth.example/token#fragment"),
        ("authorization_endpoint", "https://auth.example/authorize?state=fixed"),
        ("authorization_endpoint", "https://auth.example/authorize?a=1&a=2"),
    ],
)
def test_metadata_rejects_unsafe_endpoint_shapes(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        _metadata(**{field: value})


def test_metadata_accepts_response_type_combinations_but_validates_scopes() -> None:
    metadata = _metadata(response_types_supported=["code", "code token"])
    assert metadata.response_types_supported == ("code", "code token")
    for invalid_scope in ('bad"scope', "bad\\scope", "line\nscope", "two scopes"):
        with pytest.raises(ValueError):
            _metadata(scopes_supported=[invalid_scope])


@pytest.mark.asyncio
async def test_discovery_rejects_redirect_encoded_and_oversized_responses() -> None:
    responses = [
        httpx.Response(302, headers={"location": "https://other.example/metadata"}),
        httpx.Response(
            200,
            content=gzip.compress(json.dumps(_metadata_json()).encode()),
            headers={"content-encoding": "gzip"},
        ),
        httpx.Response(200, content=b"{" + b"x" * (64 * 1024)),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    with _mock_transport(handler):
        for expected in ("http_error", "invalid_response", "invalid_response"):
            with pytest.raises(OAuthDiscoveryError) as caught:
                await discover_oauth_metadata("https://auth.example/tenant")
            assert caught.value.code == expected


@pytest.mark.asyncio
async def test_discovery_closes_stream_on_invalid_body() -> None:
    stream = _TrackingStream(b"not-json")
    with _mock_transport(lambda _request: httpx.Response(200, stream=stream)):
        with pytest.raises(OAuthDiscoveryError):
            await discover_oauth_metadata("https://auth.example/tenant")
    assert stream.closed


@pytest.mark.asyncio
async def test_discovery_rejects_compression_before_reading_a_bomb() -> None:
    stream = _TrackingStream(gzip.compress(b"x" * (5 * 1024 * 1024)))
    response = httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
    with _mock_transport(lambda _request: response):
        with pytest.raises(OAuthDiscoveryError) as caught:
            await discover_oauth_metadata("https://auth.example/tenant")
    assert caught.value.code == "invalid_response"
    assert not stream.iterated
    assert stream.closed


@pytest.mark.parametrize(
    "issuer",
    [
        "http://auth.example/tenant",
        "https://user:password@auth.example/tenant",
        "https://auth.example/tenant?query=1",
        "https://auth.example/tenant#fragment",
    ],
)
@pytest.mark.asyncio
async def test_discovery_rejects_insecure_or_ambiguous_issuer(issuer: str) -> None:
    with pytest.raises(OAuthDiscoveryError) as caught:
        await discover_oauth_metadata(issuer)
    assert caught.value.code == "invalid_issuer"


@pytest.mark.asyncio
async def test_loopback_http_requires_explicit_flag_and_literal_address() -> None:
    body = _metadata_json(
        issuer="http://127.0.0.1:8765/tenant",
        authorization_endpoint="http://127.0.0.1:8765/authorize",
        token_endpoint="http://127.0.0.1:8765/token",
    )
    with pytest.raises(OAuthDiscoveryError):
        await discover_oauth_metadata("http://127.0.0.1:8765/tenant")
    with pytest.raises(OAuthDiscoveryError):
        await discover_oauth_metadata("http://localhost:8765/tenant", allow_loopback_http=True)

    with _mock_transport(lambda _request: httpx.Response(200, json=body)):
        result = await discover_oauth_metadata(
            "http://127.0.0.1:8765/tenant", allow_loopback_http=True
        )
    assert result.issuer == "http://127.0.0.1:8765/tenant"


@pytest.mark.asyncio
async def test_https_discovery_still_rejects_private_and_metadata_addresses() -> None:
    for issuer in ("https://127.0.0.1/tenant", "https://169.254.169.254/tenant"):
        with pytest.raises(OAuthDiscoveryError) as caught:
            await discover_oauth_metadata(issuer)
        assert caught.value.code == "network_error"


@pytest.mark.asyncio
async def test_start_stores_verifier_but_public_result_does_not_expose_it() -> None:
    store = InMemoryPendingOAuthFlowStore()
    result = await start_oauth_authorization(
        _metadata(),
        client_id="buyer-public",
        redirect_uri="https://buyer.example/oauth/callback",
        store=store,
        issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
        scopes=["openid", "media.buy"],
        resource="https://seller.example/mcp",
    )

    assert "verifier" not in repr(result).lower()
    assert "verifier" not in result.model_dump()
    query = parse_qs(urlsplit(result.authorization_url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["buyer-public"]
    assert query["redirect_uri"] == ["https://buyer.example/oauth/callback"]
    assert query["scope"] == ["openid media.buy"]
    assert query["resource"] == ["https://seller.example/mcp"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [result.state]

    pending = await store.consume(result.state)
    assert pending is not None
    verifier = pending.code_verifier.get_secret_value()
    assert 43 <= len(verifier) <= 128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert query["code_challenge"] == [challenge.decode()]
    assert verifier not in repr(pending)
    assert isinstance(pending.model_dump()["code_verifier"], SecretStr)


@pytest.mark.asyncio
async def test_start_rejects_invalid_client_scope_and_unapproved_loopback_redirect() -> None:
    store = InMemoryPendingOAuthFlowStore()
    common = {
        "metadata": _metadata(),
        "redirect_uri": "https://buyer.example/oauth/callback",
        "store": store,
        "issuer_binding": OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
    }
    with pytest.raises(ValueError, match="client_id"):
        await start_oauth_authorization(client_id="buyer\nclient", **common)
    with pytest.raises(ValueError, match="scope-token"):
        await start_oauth_authorization(client_id="buyer", scopes=['bad"scope'], **common)
    with pytest.raises(ValueError, match="redirect_uri"):
        await start_oauth_authorization(
            _metadata(),
            client_id="buyer",
            redirect_uri="http://127.0.0.1:8765/callback",
            store=store,
            issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
        )


@pytest.mark.asyncio
async def test_start_requires_explicit_supported_mixup_binding() -> None:
    store = InMemoryPendingOAuthFlowStore()
    with pytest.raises(ValueError, match="RFC 9207"):
        await start_oauth_authorization(
            _metadata(authorization_response_iss_parameter_supported=False),
            client_id="buyer-public",
            redirect_uri="https://buyer.example/oauth/callback",
            store=store,
            issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
        )

    result = await start_oauth_authorization(
        _metadata(authorization_response_iss_parameter_supported=False),
        client_id="buyer-public",
        redirect_uri="https://buyer.example/oauth/issuer-specific-callback",
        store=store,
        issuer_binding=OAuthIssuerBinding.DISTINCT_REDIRECT_URI,
    )
    assert result.state


@pytest.mark.asyncio
async def test_start_revalidates_metadata_transport_security() -> None:
    store = InMemoryPendingOAuthFlowStore()
    metadata = _metadata(
        authorization_endpoint="http://127.0.0.1:8765/authorize",
        token_endpoint="http://127.0.0.1:8765/token",
    )
    with pytest.raises(OAuthDiscoveryError) as caught:
        await start_oauth_authorization(
            metadata,
            client_id="buyer-public",
            redirect_uri="https://buyer.example/oauth/callback",
            store=store,
            issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
        )
    assert caught.value.code == "insecure_endpoint"

    request = await start_oauth_authorization(
        metadata,
        client_id="buyer-public",
        redirect_uri="https://buyer.example/oauth/callback",
        store=store,
        issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
        allow_loopback_http=True,
    )
    assert request.authorization_url.startswith("http://127.0.0.1:8765/")


def _pending(
    state: str,
    *,
    now: datetime,
    expires_at: datetime | None = None,
) -> PendingOAuthAuthorization:
    return PendingOAuthAuthorization(
        state=state,
        code_verifier=SecretStr("v" * 64),
        issuer="https://auth.example/tenant",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://tokens.example/token",
        client_id="buyer-public",
        redirect_uri="https://buyer.example/oauth/callback",
        scope="openid",
        resource="https://seller.example/mcp",
        issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
        created_at=now,
        expires_at=expires_at or now + timedelta(minutes=10),
    )


@pytest.mark.asyncio
async def test_store_collision_expiry_capacity_and_atomic_double_consume() -> None:
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    store = InMemoryPendingOAuthFlowStore(capacity=1, clock=lambda: now)
    state = "s" * 43
    assert await store.insert_if_absent(_pending(state, now=now))
    assert not await store.insert_if_absent(_pending(state, now=now))
    with pytest.raises(OAuthFlowStoreError) as capacity:
        await store.insert_if_absent(_pending("t" * 43, now=now))
    assert capacity.value.code == "capacity_exceeded"

    first, second = await asyncio.gather(store.consume(state), store.consume(state))
    assert sum(item is not None for item in (first, second)) == 1
    assert await store.insert_if_absent(
        _pending("e" * 43, now=now, expires_at=now + timedelta(seconds=1))
    )
    now += timedelta(seconds=2)
    assert await store.consume("e" * 43) is None


async def _started_flow(
    *,
    issuer_binding: OAuthIssuerBinding = OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
) -> tuple[InMemoryPendingOAuthFlowStore, str]:
    store = InMemoryPendingOAuthFlowStore()
    request = await start_oauth_authorization(
        _metadata(),
        client_id="buyer-public",
        redirect_uri="https://buyer.example/oauth/callback",
        store=store,
        issuer_binding=issuer_binding,
        resource="https://seller.example/mcp",
    )
    return store, request.state


@pytest.mark.asyncio
async def test_state_mismatch_does_not_consume_but_issuer_mismatch_does() -> None:
    store, state = await _started_flow()
    with pytest.raises(OAuthAuthorizationError) as mismatch:
        await complete_oauth_authorization(
            code="code",
            callback_state=state,
            expected_state="x" * 43,
            callback_issuer="https://auth.example/tenant",
            store=store,
        )
    assert mismatch.value.code == "state_mismatch"

    with pytest.raises(OAuthAuthorizationError) as issuer:
        await complete_oauth_authorization(
            code="code",
            callback_state=state,
            expected_state=state,
            callback_issuer="https://other.example/tenant",
            store=store,
        )
    assert issuer.value.code == "issuer_mismatch"
    with pytest.raises(OAuthAuthorizationError) as replay:
        await complete_oauth_authorization(
            code="code",
            callback_state=state,
            expected_state=state,
            callback_issuer="https://auth.example/tenant",
            store=store,
        )
    assert replay.value.code == "flow_not_found"


@pytest.mark.asyncio
async def test_missing_rfc9207_issuer_consumes_and_unicode_state_fails_cleanly() -> None:
    store, state = await _started_flow()
    with pytest.raises(OAuthAuthorizationError) as invalid_state:
        await complete_oauth_authorization(
            code="code",
            callback_state="é" * 43,
            expected_state="é" * 43,
            callback_issuer="https://auth.example/tenant",
            store=store,
        )
    assert invalid_state.value.code == "state_mismatch"
    assert await store.consume(state) is not None

    store, state = await _started_flow()
    with pytest.raises(OAuthAuthorizationError) as missing_issuer:
        await complete_oauth_authorization(
            code="code",
            callback_state=state,
            expected_state=state,
            store=store,
        )
    assert missing_issuer.value.code == "issuer_mismatch"
    assert await store.consume(state) is None


@pytest.mark.asyncio
async def test_distinct_redirect_binding_does_not_require_callback_issuer() -> None:
    store, state = await _started_flow(issuer_binding=OAuthIssuerBinding.DISTINCT_REDIRECT_URI)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "access", "token_type": "Bearer"})

    with _mock_transport(handler):
        token = await complete_oauth_authorization(
            code="code",
            callback_state=state,
            expected_state=state,
            store=store,
        )
    assert token.access_token.get_secret_value() == "access"


@pytest.mark.asyncio
async def test_distinct_redirect_binding_still_rejects_a_supplied_wrong_issuer() -> None:
    store, state = await _started_flow(issuer_binding=OAuthIssuerBinding.DISTINCT_REDIRECT_URI)
    with pytest.raises(OAuthAuthorizationError) as caught:
        await complete_oauth_authorization(
            code="code",
            callback_state=state,
            expected_state=state,
            callback_issuer="https://wrong.example/tenant",
            store=store,
        )
    assert caught.value.code == "issuer_mismatch"
    assert await store.consume(state) is None


@pytest.mark.asyncio
async def test_loopback_network_authority_is_persisted_from_start_to_completion() -> None:
    metadata = _metadata(
        issuer="http://127.0.0.1:8765/tenant",
        authorization_endpoint="http://127.0.0.1:8765/authorize",
        token_endpoint="http://127.0.0.1:8765/token",
    )
    store = InMemoryPendingOAuthFlowStore()
    request = await start_oauth_authorization(
        metadata,
        client_id="buyer-public",
        redirect_uri="http://127.0.0.1:9999/callback",
        store=store,
        issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
        allow_loopback_http=True,
    )
    with _mock_transport(
        lambda _request: httpx.Response(
            200, json={"access_token": "access", "token_type": "Bearer"}
        )
    ):
        token = await complete_oauth_authorization(
            code="code",
            callback_state=request.state,
            expected_state=request.state,
            callback_issuer=metadata.issuer,
            store=store,
        )
    assert token.access_token.get_secret_value() == "access"


@pytest.mark.asyncio
async def test_callback_error_is_sanitized_and_consumed() -> None:
    store, state = await _started_flow()
    with pytest.raises(OAuthAuthorizationError) as caught:
        await complete_oauth_authorization(
            code=None,
            callback_state=state,
            expected_state=state,
            callback_issuer="https://auth.example/tenant",
            callback_error="access_denied",
            store=store,
        )
    assert caught.value.oauth_error == "access_denied"
    assert state not in str(caught.value)
    assert await store.consume(state) is None


@pytest.mark.asyncio
async def test_token_exchange_uses_only_persisted_fields_and_returns_secret_types() -> None:
    store, state = await _started_flow()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "id_token": "id-secret",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid",
                "unknown": "discarded",
            },
        )

    with _mock_transport(handler) as transport_factory:
        tokens = await complete_oauth_authorization(
            code="authorization-code",
            callback_state=state,
            expected_state=state,
            callback_issuer="https://auth.example/tenant",
            store=store,
        )

    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "https://tokens.example/token"
    assert "authorization" not in request.headers
    form = parse_qs(request.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["client_id"] == ["buyer-public"]
    assert form["code"] == ["authorization-code"]
    assert form["redirect_uri"] == ["https://buyer.example/oauth/callback"]
    assert form["resource"] == ["https://seller.example/mcp"]
    assert form["code_verifier"] and 43 <= len(form["code_verifier"][0]) <= 128
    assert tokens.access_token.get_secret_value() == "access-secret"
    assert "access-secret" not in repr(tokens)
    assert "refresh-secret" not in repr(tokens)
    assert "id-secret" not in repr(tokens)
    assert "unknown" not in tokens.model_dump()
    assert transport_factory.call_args.kwargs["allowed_ports"] is None


@pytest.mark.asyncio
async def test_token_error_never_leaks_remote_prose_or_token_shaped_values() -> None:
    store, state = await _started_flow()
    secret = "sk_live_sensitive_value"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"retry-after": "17"},
            json={
                "error": "invalid_grant",
                "error_description": f"bad code {secret}",
                "access_token": secret,
            },
        )

    with _mock_transport(handler):
        with pytest.raises(OAuthTokenExchangeError) as caught:
            await complete_oauth_authorization(
                code="authorization-code",
                callback_state=state,
                expected_state=state,
                callback_issuer="https://auth.example/tenant",
                store=store,
            )
    assert caught.value.oauth_error == "invalid_grant"
    assert caught.value.retry_after_seconds == 17
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert await store.consume(state) is None


@pytest.mark.parametrize(
    "body",
    [
        {"access_token": "access\r\nX-Evil: yes", "token_type": "Bearer"},
        {"access_token": "access", "token_type": "Bearer\r\nX-Evil: yes"},
        {"access_token": "access", "token_type": "Bearer", "scope": "line\nscope"},
        {"access_token": "access", "token_type": "Bearer", "scope": 'bad"scope'},
        {"access_token": "access", "token_type": "Bearer", "scope": "bad\\scope"},
    ],
)
@pytest.mark.asyncio
async def test_token_projection_rejects_control_or_non_scope_public_fields(
    body: dict[str, str],
) -> None:
    store, state = await _started_flow()
    with _mock_transport(lambda _request: httpx.Response(200, json=body)):
        with pytest.raises(OAuthTokenExchangeError) as caught:
            await complete_oauth_authorization(
                code="authorization-code",
                callback_state=state,
                expected_state=state,
                callback_issuer="https://auth.example/tenant",
                store=store,
            )
    assert caught.value.code == "invalid_token_response"


@pytest.mark.asyncio
async def test_resource_indicator_error_is_preserved_as_safe_code() -> None:
    store, state = await _started_flow()
    with _mock_transport(lambda _request: httpx.Response(400, json={"error": "invalid_target"})):
        with pytest.raises(OAuthTokenExchangeError) as caught:
            await complete_oauth_authorization(
                code="authorization-code",
                callback_state=state,
                expected_state=state,
                callback_issuer="https://auth.example/tenant",
                store=store,
            )
    assert caught.value.oauth_error == "invalid_target"


@pytest.mark.asyncio
async def test_oversized_token_error_preserves_status_retry_and_closes_stream() -> None:
    store, state = await _started_flow()
    stream = _TrackingStream(b"{" + b"x" * (64 * 1024))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "9"}, stream=stream)

    with _mock_transport(handler):
        with pytest.raises(OAuthTokenExchangeError) as caught:
            await complete_oauth_authorization(
                code="authorization-code",
                callback_state=state,
                expected_state=state,
                callback_issuer="https://auth.example/tenant",
                store=store,
            )
    assert caught.value.code == "http_error"
    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds == 9
    assert stream.closed


@pytest.mark.asyncio
async def test_token_malformed_encoded_and_oversized_bodies_fail_closed() -> None:
    responses = [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(
            200,
            content=zlib.compress(b'{"access_token":"x","token_type":"Bearer"}'),
            headers={"content-encoding": "deflate"},
        ),
        httpx.Response(200, content=b"{" + b"x" * (64 * 1024)),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    with _mock_transport(handler):
        for _ in range(3):
            store, state = await _started_flow()
            with pytest.raises(OAuthTokenExchangeError) as caught:
                await complete_oauth_authorization(
                    code="authorization-code",
                    callback_state=state,
                    expected_state=state,
                    callback_issuer="https://auth.example/tenant",
                    store=store,
                )
            assert caught.value.code == "invalid_response"


@pytest.mark.asyncio
async def test_token_exchange_revalidates_private_endpoint_before_connecting() -> None:
    store = InMemoryPendingOAuthFlowStore()
    request = await start_oauth_authorization(
        _metadata(token_endpoint="https://127.0.0.1/token"),
        client_id="buyer-public",
        redirect_uri="https://buyer.example/oauth/callback",
        store=store,
        issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
    )
    with pytest.raises(OAuthTokenExchangeError) as caught:
        await complete_oauth_authorization(
            code="authorization-code",
            callback_state=request.state,
            expected_state=request.state,
            callback_issuer="https://auth.example/tenant",
            store=store,
        )
    assert caught.value.code == "network_error"


@pytest.mark.asyncio
async def test_discovery_deadline_covers_dns_without_blocking_event_loop() -> None:
    ticks: list[float] = []

    def slow_transport(*_args: Any, **_kwargs: Any) -> httpx.MockTransport:
        time.sleep(0.25)
        return httpx.MockTransport(lambda _request: httpx.Response(500))

    async def sibling() -> None:
        await asyncio.sleep(0.02)
        ticks.append(asyncio.get_running_loop().time())

    started = asyncio.get_running_loop().time()
    with patch("adcp.oauth.build_async_ip_pinned_transport", side_effect=slow_transport):
        sibling_task = asyncio.create_task(sibling())
        with pytest.raises(OAuthDiscoveryError) as caught:
            await discover_oauth_metadata("https://auth.example/tenant", timeout=0.05)
        await sibling_task
    assert caught.value.code == "timeout"
    assert ticks[0] - started < 0.1


@pytest.mark.asyncio
async def test_token_deadline_covers_dns_and_consumes_flow() -> None:
    store, state = await _started_flow()

    def slow_transport(*_args: Any, **_kwargs: Any) -> httpx.MockTransport:
        time.sleep(0.25)
        return httpx.MockTransport(lambda _request: httpx.Response(500))

    with patch("adcp.oauth.build_async_ip_pinned_transport", side_effect=slow_transport):
        with pytest.raises(OAuthTokenExchangeError) as caught:
            await complete_oauth_authorization(
                code="authorization-code",
                callback_state=state,
                expected_state=state,
                callback_issuer="https://auth.example/tenant",
                store=store,
                timeout=0.05,
            )
    assert caught.value.code == "timeout"
    assert await store.consume(state) is None


@pytest.mark.asyncio
async def test_deep_json_is_sanitized_for_discovery_and_token_exchange() -> None:
    nested = ("[" * 1500 + "0" + "]" * 1500).encode()
    with _mock_transport(lambda _request: httpx.Response(200, content=nested)):
        with pytest.raises(OAuthDiscoveryError) as discovery:
            await discover_oauth_metadata("https://auth.example/tenant")
    assert discovery.value.code == "invalid_response"

    store, state = await _started_flow()
    with _mock_transport(lambda _request: httpx.Response(200, content=nested)):
        with pytest.raises(OAuthTokenExchangeError) as token:
            await complete_oauth_authorization(
                code="authorization-code",
                callback_state=state,
                expected_state=state,
                callback_issuer="https://auth.example/tenant",
                store=store,
            )
    assert token.value.code == "invalid_response"


@pytest.mark.asyncio
async def test_completion_independently_rejects_expired_external_store_record() -> None:
    now = datetime.now(timezone.utc)
    pending = _pending(
        "s" * 43, now=now - timedelta(minutes=2), expires_at=now - timedelta(minutes=1)
    )

    class StaleStore:
        async def insert_if_absent(self, _pending: PendingOAuthAuthorization) -> bool:
            return True

        async def consume(self, _state: str) -> PendingOAuthAuthorization | None:
            return pending

    with pytest.raises(OAuthAuthorizationError) as caught:
        await complete_oauth_authorization(
            code="authorization-code",
            callback_state=pending.state,
            expected_state=pending.state,
            callback_issuer=pending.issuer,
            store=StaleStore(),
        )
    assert caught.value.code == "flow_expired"


@pytest.mark.asyncio
async def test_start_store_and_completion_share_an_injectable_clock() -> None:
    now = datetime(2020, 1, 1, tzinfo=timezone.utc)
    store = InMemoryPendingOAuthFlowStore(clock=lambda: now)
    request = await start_oauth_authorization(
        _metadata(),
        client_id="buyer-public",
        redirect_uri="https://buyer.example/oauth/callback",
        store=store,
        issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
        clock=lambda: now,
    )
    with _mock_transport(
        lambda _request: httpx.Response(
            200, json={"access_token": "access", "token_type": "Bearer"}
        )
    ):
        tokens = await complete_oauth_authorization(
            code="authorization-code",
            callback_state=request.state,
            expected_state=request.state,
            callback_issuer="https://auth.example/tenant",
            store=store,
            clock=lambda: now,
        )
    assert tokens.access_token.get_secret_value() == "access"


@pytest.mark.asyncio
async def test_cancellation_during_token_exchange_does_not_restore_flow() -> None:
    store, state = await _started_flow()

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with _mock_transport(handler):
        task = asyncio.create_task(
            complete_oauth_authorization(
                code="authorization-code",
                callback_state=state,
                expected_state=state,
                callback_issuer="https://auth.example/tenant",
                store=store,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await store.consume(state) is None


@pytest.mark.asyncio
async def test_two_concurrent_completions_exchange_only_once() -> None:
    store, state = await _started_flow()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": "access", "token_type": "Bearer"})

    async def complete() -> object:
        try:
            return await complete_oauth_authorization(
                code="authorization-code",
                callback_state=state,
                expected_state=state,
                callback_issuer="https://auth.example/tenant",
                store=store,
            )
        except OAuthAuthorizationError as exc:
            return exc

    with _mock_transport(handler):
        results = await asyncio.gather(complete(), complete())
    assert calls == 1
    assert sum(isinstance(result, OAuthAuthorizationError) for result in results) == 1
