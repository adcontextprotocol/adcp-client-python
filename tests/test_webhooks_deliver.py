"""Tests for adcp.webhooks.deliver() — one-shot legacy-auth webhook dispatch.

The helper collapses the seller's six-step boilerplate (build, serialize,
sign, merge headers, POST, token-echo) into one call. The load-bearing
property every test defends is that the bytes we *sign* and the bytes we
*POST* are identical — the serialization-format drift that plagued the
hand-rolled path is structurally prevented here.

All tests suppress the ``DeprecationWarning`` that fires on first legacy-
auth use — it's asserted separately in ``test_deprecation_warning_fires``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from a2a.types import TaskState  # TaskState is the proto enum; still exported

from adcp.types.generated_poc.core.push_notification_config import (
    Authentication as PNAuthentication,
)
from adcp.types.generated_poc.core.push_notification_config import (
    PushNotificationConfig,
)
from adcp.types.generated_poc.core.reporting_webhook import Authentication as RWAuth
from adcp.types.generated_poc.core.reporting_webhook import (
    ReportingFrequency,
    ReportingWebhook,
)
from adcp.webhooks import (
    create_mcp_webhook_payload,
    deliver,
)
from tests.a2a_compat_shim import Artifact, DataPart, Part, Task, TaskStatus

# Global DeprecationWarning filter — legacy auth always warns; silence here
# and assert the warning once in its own dedicated test. The filter strips
# the module qualifier so it catches warnings emitted via stacklevel that
# appear to originate from test-file frames.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# Shared secret padded past the 32-char floor the auth schema enforces.
_SECRET = "s" * 40
_BEARER_TOKEN = "b" * 40
# PushNotificationConfig.token has min_length=16.
_PUSH_TOKEN = "push-token-01234567"
_TOKEN_FIELD = "push_notification_token"


async def _capture_client(
    handler: Any = None,
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """Build an AsyncClient backed by a MockTransport; capture every request."""
    captured: list[httpx.Request] = []

    def _default_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202, json={"ok": True})

    transport = httpx.MockTransport(handler or _default_handler)
    return httpx.AsyncClient(transport=transport), captured


def _mcp_payload() -> dict[str, Any]:
    return create_mcp_webhook_payload(
        task_id="task_123",
        task_type="create_media_buy",
        status="completed",
        result={"media_buy_id": "mb_1"},
        timestamp=None,  # deterministic output tested separately
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
    )


async def test_bearer_auth_adds_authorization_header() -> None:
    """schemes=[Bearer] → Authorization: Bearer <credentials>."""
    client, captured = await _capture_client()
    config = ReportingWebhook(
        url="https://buyer.example/webhooks/report",
        authentication=RWAuth(schemes=["Bearer"], credentials=_BEARER_TOKEN),
        reporting_frequency=ReportingFrequency.daily,
    )

    async with client:
        response = await deliver(config, _mcp_payload(), client=client)

    assert response.status_code == 202
    assert len(captured) == 1
    sent = captured[0]
    assert sent.headers["authorization"] == f"Bearer {_BEARER_TOKEN}"
    assert "x-adcp-signature" not in sent.headers


async def test_hmac_auth_signs_posted_bytes() -> None:
    """schemes=[HMAC-SHA256] → signature over the exact bytes that hit the wire."""
    client, captured = await _capture_client()
    config = PushNotificationConfig(
        url="https://buyer.example/webhooks/mb",
        authentication=PNAuthentication(schemes=["HMAC-SHA256"], credentials=_SECRET),
    )

    async with client:
        await deliver(config, _mcp_payload(), client=client)

    sent = captured[0]
    assert "x-adcp-signature" in sent.headers
    assert "x-adcp-timestamp" in sent.headers

    # Recompute the signature over the exact bytes httpx posted — if the
    # helper re-serialized anywhere after signing, this fails.
    sig_header = sent.headers["x-adcp-signature"]
    timestamp = sent.headers["x-adcp-timestamp"]
    assert sig_header.startswith("sha256=")
    expected = hmac.new(
        _SECRET.encode(),
        f"{timestamp}.".encode() + sent.content,
        hashlib.sha256,
    ).hexdigest()
    assert sig_header == f"sha256={expected}"


async def test_token_echo_opt_in_mcp() -> None:
    """token_field= echoes config.token into the MCP body at that key."""
    client, captured = await _capture_client()
    config = PushNotificationConfig(
        url="https://buyer.example/webhooks/mb",
        token=_PUSH_TOKEN,
        authentication=PNAuthentication(schemes=["Bearer"], credentials=_BEARER_TOKEN),
    )

    async with client:
        await deliver(config, _mcp_payload(), client=client, token_field=_TOKEN_FIELD)

    body = json.loads(captured[0].content)
    assert body[_TOKEN_FIELD] == _PUSH_TOKEN


async def test_token_echo_opt_in_a2a_metadata() -> None:
    """For Task payloads, opt-in echo attaches the token under metadata —
    Task top-level fields are strictly typed so arbitrary keys can only go
    into the metadata bag."""
    client, captured = await _capture_client()
    task = Task(
        id="t1",
        context_id="c1",
        status=TaskStatus(state=TaskState.completed),
        artifacts=[
            Artifact(
                artifact_id="a1",
                parts=[Part(root=DataPart(data={"media_buy_id": "mb_1"}))],
            )
        ],
    )
    config = PushNotificationConfig(
        url="https://buyer.example/webhooks/mb",
        token=_PUSH_TOKEN,
        authentication=PNAuthentication(schemes=["Bearer"], credentials=_BEARER_TOKEN),
    )

    async with client:
        await deliver(config, task, client=client, token_field=_TOKEN_FIELD)

    body = json.loads(captured[0].content)
    assert body["metadata"][_TOKEN_FIELD] == _PUSH_TOKEN
    # The A2A Task's native shape survived serialization.
    assert body["artifacts"][0]["artifactId"] == "a1"
    assert body["status"]["state"] == "completed"


async def test_token_echo_default_is_opt_in_only() -> None:
    """Without token_field=, a token on the config does NOT land in the body.

    The AdCP legacy schema says the token is echoed but doesn't name the
    wire field — so the caller must pick one explicitly. Default is silence."""
    client, captured = await _capture_client()
    config = PushNotificationConfig(
        url="https://buyer.example/webhooks/mb",
        token=_PUSH_TOKEN,
        authentication=PNAuthentication(schemes=["Bearer"], credentials=_BEARER_TOKEN),
    )

    async with client:
        await deliver(config, _mcp_payload(), client=client)

    body = json.loads(captured[0].content)
    assert "push_notification_token" not in body
    assert _PUSH_TOKEN not in body.values()


async def test_retry_produces_byte_identical_body() -> None:
    """Two deliver() calls with the same payload produce byte-identical bodies.

    Load-bearing property: retries replay the same bytes, so receivers dedupe
    by ``idempotency_key`` on a payload that looks exactly like the first
    attempt. If serialization weren't deterministic here, retried webhooks
    would appear as distinct events."""
    client, captured = await _capture_client()
    config = ReportingWebhook(
        url="https://buyer.example/webhooks/report",
        authentication=RWAuth(schemes=["Bearer"], credentials=_BEARER_TOKEN),
        reporting_frequency=ReportingFrequency.daily,
    )
    payload = _mcp_payload()

    async with client:
        await deliver(config, payload, client=client)
        await deliver(config, payload, client=client)

    assert captured[0].content == captured[1].content


async def test_accepts_dict_config() -> None:
    """Sellers reading PushNotificationConfig from a raw request dict shouldn't
    need to round-trip through the Pydantic model."""
    client, captured = await _capture_client()
    config = {
        "url": "https://buyer.example/webhooks/mb",
        "authentication": {"schemes": ["Bearer"], "credentials": _BEARER_TOKEN},
    }

    async with client:
        response = await deliver(config, _mcp_payload(), client=client)

    assert response.status_code == 202
    assert captured[0].headers["authorization"] == f"Bearer {_BEARER_TOKEN}"


async def test_no_authentication_raises_pointing_to_websocksender() -> None:
    """Absent ``authentication`` is a spec violation (push-notification-config
    says the seller MUST sign with RFC 9421). The helper refuses rather than
    silently posting unsigned."""
    client, _ = await _capture_client()
    config = {"url": "https://buyer.example/webhooks/mb"}

    async with client:
        with pytest.raises(ValueError, match="WebhookSender"):
            await deliver(config, _mcp_payload(), client=client)


async def test_unknown_auth_scheme_raises() -> None:
    """Schemes outside the legacy set fail loudly — silent no-op would hand
    the caller an unsigned POST under the illusion of having signed it."""
    client, _ = await _capture_client()
    config = {
        "url": "https://buyer.example/webhooks/mb",
        "authentication": {"schemes": ["Digest"], "credentials": "c" * 40},
    }
    async with client:
        with pytest.raises(ValueError, match="unknown authentication scheme"):
            await deliver(config, _mcp_payload(), client=client)


async def test_hmac_without_credentials_raises() -> None:
    """HMAC-SHA256 without credentials is not a valid config; refuse."""
    client, _ = await _capture_client()
    config = {
        "url": "https://buyer.example/webhooks/mb",
        "authentication": {"schemes": ["HMAC-SHA256"]},
    }
    async with client:
        with pytest.raises(ValueError, match="credentials"):
            await deliver(config, _mcp_payload(), client=client)


async def test_missing_url_raises() -> None:
    """A config without ``url`` is unusable — raise at the boundary, not
    inside httpx."""
    client, _ = await _capture_client()
    config = {"authentication": {"schemes": ["Bearer"], "credentials": _BEARER_TOKEN}}
    async with client:
        with pytest.raises(ValueError, match="url"):
            await deliver(config, _mcp_payload(), client=client)


async def test_http_url_rejected() -> None:
    """HTTP would expose the body, token, and Authorization in transit."""
    client, _ = await _capture_client()
    config = {
        "url": "http://buyer.example/webhooks/mb",
        "authentication": {"schemes": ["Bearer"], "credentials": _BEARER_TOKEN},
    }
    async with client:
        with pytest.raises(ValueError, match="https"):
            await deliver(config, _mcp_payload(), client=client)


async def test_crlf_in_credentials_rejected() -> None:
    """CRLF in credentials could smuggle headers past receivers that don't
    enforce RFC-compliant header parsing."""
    client, _ = await _capture_client()
    bad = "x" * 30 + "\r\nX-Admin: true"
    config = {
        "url": "https://buyer.example/webhooks/mb",
        "authentication": {"schemes": ["Bearer"], "credentials": bad},
    }
    async with client:
        with pytest.raises(ValueError, match="control characters"):
            await deliver(config, _mcp_payload(), client=client)


async def test_extra_headers_merge_but_reserved_are_rejected() -> None:
    """Custom headers merge; auth, content, signature headers stay sender-owned."""
    client, captured = await _capture_client()
    config = ReportingWebhook(
        url="https://buyer.example/webhooks/report",
        authentication=RWAuth(schemes=["Bearer"], credentials=_BEARER_TOKEN),
        reporting_frequency=ReportingFrequency.daily,
    )

    async with client:
        await deliver(
            config,
            _mcp_payload(),
            client=client,
            extra_headers={"X-Trace-Id": "trace-abc"},
        )
    assert captured[0].headers["x-trace-id"] == "trace-abc"

    # Every reserved header name must be rejected with a class-appropriate
    # message. The mistake categories differ — someone passing Authorization
    # usually doesn't know about config.authentication; someone passing
    # Signature-Input is debugging and needs pointed at WebhookSender.
    reserved_samples = {
        "Authorization": "config.authentication",
        "Content-Type": "application/json",
        "Content-Digest": "WebhookSender",
        "Signature": "WebhookSender",
        "Signature-Input": "WebhookSender",
        "X-AdCP-Signature": "HMAC-SHA256",
        "X-AdCP-Timestamp": "HMAC-SHA256",
        "Host": "sender-owned",
        "Content-Length": "sender-owned",
    }
    for header, expected_phrase in reserved_samples.items():
        override_client, _ = await _capture_client()
        async with override_client:
            with pytest.raises(ValueError, match=expected_phrase):
                await deliver(
                    config,
                    _mcp_payload(),
                    client=override_client,
                    extra_headers={header: "attacker"},
                )


async def test_timeout_seconds_with_client_raises() -> None:
    """Configuring timeout on a helper-owned client and a shared client at
    the same time is ambiguous — force the caller to pick."""
    client, _ = await _capture_client()
    config = ReportingWebhook(
        url="https://buyer.example/webhooks/report",
        authentication=RWAuth(schemes=["Bearer"], credentials=_BEARER_TOKEN),
        reporting_frequency=ReportingFrequency.daily,
    )
    async with client:
        with pytest.raises(ValueError, match="timeout_seconds"):
            await deliver(config, _mcp_payload(), client=client, timeout_seconds=5.0)


async def test_url_with_embedded_userinfo_rejected() -> None:
    """URLs like https://user:pass@host/ get logged by every HTTP intermediary.
    The helper forces credentials into config.authentication where the
    signing path keeps them out of URL-shaped logs."""
    client, _ = await _capture_client()
    config = {
        "url": "https://user:secret@buyer.example/webhooks/mb",
        "authentication": {"schemes": ["Bearer"], "credentials": _BEARER_TOKEN},
    }
    async with client:
        with pytest.raises(ValueError, match="userinfo"):
            await deliver(config, _mcp_payload(), client=client)


async def test_body_size_cap_enforced() -> None:
    """Oversize bodies raise with an actionable message before a 10MB POST
    that the receiver would reject at the reverse proxy anyway."""
    client, _ = await _capture_client()
    config = {
        "url": "https://buyer.example/webhooks/mb",
        "authentication": {"schemes": ["Bearer"], "credentials": _BEARER_TOKEN},
    }
    # One oversized field in the payload — 11MB of 'x'.
    oversize = _mcp_payload()
    oversize["result"] = {"blob": "x" * (11 * 1024 * 1024)}
    async with client:
        with pytest.raises(ValueError, match="10,485,760"):
            await deliver(config, oversize, client=client)


async def test_extra_headers_count_cap() -> None:
    """A caller iterating a large container into extra_headers shouldn't
    produce an unbounded header block."""
    client, _ = await _capture_client()
    config = {
        "url": "https://buyer.example/webhooks/mb",
        "authentication": {"schemes": ["Bearer"], "credentials": _BEARER_TOKEN},
    }
    headers = {f"X-Tag-{i}": str(i) for i in range(65)}
    async with client:
        with pytest.raises(ValueError, match="extra_headers has 65 entries"):
            await deliver(config, _mcp_payload(), client=client, extra_headers=headers)


async def test_authentication_wrong_type_raises() -> None:
    """config.authentication must be a mapping — a bare string (common mistake
    when a seller writes ``authentication='Bearer xxx'``) fails cleanly rather
    than raising AttributeError deep in the helper."""
    client, _ = await _capture_client()
    config = {
        "url": "https://buyer.example/webhooks/mb",
        "authentication": "Bearer " + _BEARER_TOKEN,
    }
    async with client:
        with pytest.raises(ValueError, match="authentication must be"):
            await deliver(config, _mcp_payload(), client=client)


async def test_retry_requires_caller_to_not_mutate_payload() -> None:
    """Byte-identity of retries rests on the *caller* not mutating the
    payload between calls. This test documents the contract by showing
    what breaks when the caller refreshes a timestamp mid-retry.

    It is NOT a bug in the helper — the docstring flags the contract —
    but a test that exercises the failure mode makes the contract concrete."""
    client, captured = await _capture_client()
    config = {
        "url": "https://buyer.example/webhooks/mb",
        "authentication": {"schemes": ["Bearer"], "credentials": _BEARER_TOKEN},
    }
    payload = _mcp_payload()
    async with client:
        await deliver(config, payload, client=client)
        # Caller mutates — retry bytes now differ.
        payload["timestamp"] = "2026-01-01T00:00:00Z"
        await deliver(config, payload, client=client)

    assert captured[0].content != captured[1].content


async def test_signed_bytes_match_posted_bytes() -> None:
    """The helper must POST via ``content=`` — ``json=`` would re-serialize
    and break the signature invariant. Compare body bytes to a byte-exact
    expected serialization."""
    client, captured = await _capture_client()
    config = PushNotificationConfig(
        url="https://buyer.example/webhooks/mb",
        authentication=PNAuthentication(schemes=["HMAC-SHA256"], credentials=_SECRET),
    )
    payload = _mcp_payload()

    async with client:
        await deliver(config, payload, client=client)

    # Compact separators — deliver() pins the canonical on-wire form from
    # adcontextprotocol/adcp#2478 so signer and wire bytes can never drift.
    expected_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert captured[0].content == expected_body


async def test_deprecation_warning_fires_for_legacy_auth() -> None:
    """Mirrors the receiver-side :class:`LegacyWebhookHmacError` deprecation
    warning — senders must know to migrate to 9421 before 4.0 removes it."""
    # Reset the module-level "already warned" flag so this test is hermetic.
    import adcp.webhooks as webhooks_module

    webhooks_module._AUTH_DEPRECATION_WARNED = False

    client, _ = await _capture_client()
    config = ReportingWebhook(
        url="https://buyer.example/webhooks/report",
        authentication=RWAuth(schemes=["Bearer"], credentials=_BEARER_TOKEN),
        reporting_frequency=ReportingFrequency.daily,
    )

    async with client:
        with pytest.warns(DeprecationWarning, match="AdCP 4.0"):
            await deliver(config, _mcp_payload(), client=client)


# -- Outbound wire-normalization: 1.0 proto enums → 0.3 spec strings -----


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """Small helper — call the private normalizer directly on a dict so
    the tests below don't need to stand up a full webhook dispatch."""
    from adcp.webhooks import _normalize_a2a_task_state_to_v03

    _normalize_a2a_task_state_to_v03(payload)
    return payload


def test_normalize_rewrites_status_state_to_0_3_lowercase() -> None:
    out = _normalize({"status": {"state": "TASK_STATE_COMPLETED"}})
    assert out["status"]["state"] == "completed"


def test_normalize_rewrites_status_message_role() -> None:
    out = _normalize(
        {
            "status": {
                "state": "TASK_STATE_INPUT_REQUIRED",
                "message": {"role": "ROLE_AGENT"},
            }
        }
    )
    assert out["status"]["state"] == "input-required"
    assert out["status"]["message"]["role"] == "agent"


def test_normalize_walks_task_history_roles() -> None:
    """Regression: ``Task.history[]`` carries Messages whose ``role``
    field serializes SCREAMING_SNAKE. A handroll of a Task envelope
    that populates history (proxied from another source) must have
    every role flipped, not just the top-level / status.message."""
    out = _normalize(
        {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "history": [
                {"role": "ROLE_USER", "parts": [{"text": "first"}]},
                {"role": "ROLE_AGENT", "parts": [{"text": "second"}]},
                "not-a-message",  # heterogeneous entries must be tolerated
            ],
        }
    )
    assert out["status"]["state"] == "completed"
    assert out["history"][0]["role"] == "user"
    assert out["history"][1]["role"] == "agent"
    assert out["history"][2] == "not-a-message"


def test_normalize_passthrough_for_unknown_enum_prefixes() -> None:
    """Non-enum values that happen not to start with the proto
    prefixes must survive unchanged — guards against accidental
    mutation of user-supplied data."""
    out = _normalize(
        {
            "status": {"state": "completed", "message": {"role": "user"}},
            "role": "user",
        }
    )
    assert out["status"]["state"] == "completed"
    assert out["status"]["message"]["role"] == "user"
    assert out["role"] == "user"


# -- SSRF guard regression tests (parity with WebhookSender) ---------------


@pytest.mark.asyncio
async def test_deliver_owned_client_rejects_loopback_destination() -> None:
    """The legacy ``deliver()`` helper now wires the same per-request
    IP-pinned transport that ``WebhookSender._send_bytes`` uses (see #299).
    A buyer-supplied URL pointing at 127.0.0.1 must reject before the
    POST hits a real socket — the previous unguarded ``httpx.AsyncClient``
    would have delivered to the loopback address.

    Operator-supplied clients (``client=...``) skip the SSRF guard by
    design; they own egress policy on their transport."""
    from adcp.signing import SSRFValidationError

    config = PushNotificationConfig(
        url="https://127.0.0.1/webhooks/adcp",
        authentication=PNAuthentication(schemes=["Bearer"], credentials=_BEARER_TOKEN),
    )
    payload = create_mcp_webhook_payload(
        task_id="task_ssrf",
        task_type="create_media_buy",
        status="completed",
    )

    with pytest.raises(SSRFValidationError):
        await deliver(config, payload)


@pytest.mark.asyncio
async def test_deliver_allow_private_dev_escape_hatch() -> None:
    """Adopters with dev/CI fixtures posting to internal endpoints
    pass ``allow_private=True`` to disable the IP-range check. Pin-and-
    bind still applies (URL gets resolved + connection pinned), and the
    rest of the contract stands — bytes signed == bytes posted."""
    from unittest.mock import patch

    config = PushNotificationConfig(
        url="https://10.0.0.1/webhooks/adcp",
        authentication=PNAuthentication(schemes=["Bearer"], credentials=_BEARER_TOKEN),
    )
    payload = create_mcp_webhook_payload(
        task_id="task_priv",
        task_type="create_media_buy",
        status="completed",
    )

    # Stub the pinned-transport build with a transport that actually
    # accepts a body without opening a socket. We're testing the kwarg
    # plumbing, not the network round-trip.
    captured: dict[str, Any] = {}

    def fake_build(uri: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        captured["uri"] = uri
        return httpx.MockTransport(lambda _req: httpx.Response(200))

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        side_effect=fake_build,
    ):
        response = await deliver(config, payload, allow_private=True)

    assert response.status_code == 200
    assert captured["allow_private"] is True
    assert captured["uri"] == "https://10.0.0.1/webhooks/adcp"


@pytest.mark.asyncio
async def test_deliver_operator_supplied_client_skips_ssrf_guard() -> None:
    """When the operator provides their own ``httpx.AsyncClient``,
    ``deliver()`` does NOT build a pinned transport — the operator owns
    SSRF on their transport (vetted egress proxy, ASGI test transport).
    A request that would fail the SSRF range check under the owned-client
    path (resolves to a private/loopback IP) succeeds via the operator's
    MockTransport without any DNS lookup."""
    # https:// is required by the scheme check; MockTransport captures
    # the request without a real socket so SSRF range never fires.
    config = PushNotificationConfig(
        url="https://operator-trusted.test/webhooks/adcp",
        authentication=PNAuthentication(schemes=["Bearer"], credentials=_BEARER_TOKEN),
    )
    payload = create_mcp_webhook_payload(
        task_id="task_op",
        task_type="create_media_buy",
        status="completed",
    )

    transport = httpx.MockTransport(lambda _req: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        response = await deliver(config, payload, client=client)

    assert response.status_code == 200
