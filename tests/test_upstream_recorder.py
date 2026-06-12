"""Tests for the reference UpstreamRecorder testing middleware.

Exercises the recorder over real httpx traffic via ``httpx.MockTransport``
(no live network), asserting it captures calls through the async event
hooks, scopes records per principal, redacts secrets at record time, and
shapes data for the ``query_upstream_traffic`` conformance scenario.

The recorded-call shape is validated against the inline ``recorded_calls[]``
item subschema from the rc.9 ``comply-test-controller-response.json`` cache
so the test tracks the spec, not the implementation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest

from adcp.testing import (
    RecordedCall,
    UpstreamRecorder,
    UpstreamRecorderScopeError,
    UpstreamTrafficResult,
)
from adcp.testing.upstream_recorder import SECRET_KEY_PATTERN

PRINCIPAL = "agent.example.com"
UPLOAD_URL = "https://api.example.com/v2/audience/upload"

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "cache"
    / "3.1.0-rc.13"
    / "compliance"
    / "comply-test-controller-response.json"
)


def _recorded_call_item_schema() -> dict[str, Any]:
    """The inline ``recorded_calls[]`` item subschema (no external $refs)."""
    doc = json.loads(_SCHEMA_PATH.read_text())
    branch = next(b for b in doc["oneOf"] if b.get("title") == "UpstreamTrafficSuccess")
    return branch["properties"]["recorded_calls"]["items"]


def _mock_handler(status: int = 200) -> httpx.MockTransport:
    """A MockTransport that echoes a fixed JSON response for any request."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"ok": True})

    return httpx.MockTransport(handle)


async def _client_with(recorder: UpstreamRecorder, status: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_mock_handler(status),
        event_hooks=recorder.httpx_hooks(),
    )


# -- capture via httpx event hooks ----------------------------------------


async def test_records_outbound_call_via_event_hooks() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={"audience": "acme-user-0001"})

    result = recorder.query(principal=PRINCIPAL)
    assert result.total_count == 1
    call = result.recorded_calls[0]
    assert call.method == "POST"
    assert call.url == UPLOAD_URL
    assert call.host == "api.example.com"
    assert call.path == "/v2/audience/upload"
    assert call.status_code == 200
    assert call.payload == {"audience": "acme-user-0001"}


async def test_captures_status_code_from_response() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder, status=503) as client:
            await client.get(UPLOAD_URL)
    call = recorder.query(principal=PRINCIPAL).recorded_calls[0]
    assert call.status_code == 503


async def test_endpoint_is_method_space_url() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={})
    call = recorder.query(principal=PRINCIPAL).recorded_calls[0]
    assert call.endpoint == f"POST {UPLOAD_URL}"


# -- caller scoping --------------------------------------------------------


async def test_query_returns_only_requested_principal() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async("agent.a"):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={"who": "a"})
    async with recorder.principal_scope_async("agent.b"):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={"who": "b"})

    result_a = recorder.query(principal="agent.a")
    assert result_a.total_count == 1
    assert result_a.recorded_calls[0].payload == {"who": "a"}

    result_b = recorder.query(principal="agent.b")
    assert result_b.total_count == 1
    assert result_b.recorded_calls[0].payload == {"who": "b"}


async def test_unknown_principal_returns_empty() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={})
    result = recorder.query(principal="agent.other")
    assert result.total_count == 0
    assert result.recorded_calls == ()


async def test_call_outside_scope_is_dropped() -> None:
    recorder = UpstreamRecorder()
    # No principal scope bound — the call is unattributable, fail-closed.
    async with await _client_with(recorder) as client:
        await client.post(UPLOAD_URL, json={})
    assert recorder.debug() == {}


def test_empty_principal_in_scope_raises() -> None:
    recorder = UpstreamRecorder()
    with pytest.raises(UpstreamRecorderScopeError):
        with recorder.principal_scope(""):
            pass


def test_empty_principal_in_query_raises() -> None:
    recorder = UpstreamRecorder()
    with pytest.raises(UpstreamRecorderScopeError):
        recorder.query(principal="")


# -- secret redaction at record time --------------------------------------


async def test_redacts_secret_keys_in_json_body() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(
                UPLOAD_URL,
                json={
                    "api_key": "sk-live-12345",
                    "nested": {"refresh_token": "rt-abc", "audience": "acme-user-0002"},
                    "tokens_kept": "this-key-does-not-match",
                },
            )
    call = recorder.query(principal=PRINCIPAL).recorded_calls[0]
    assert call.payload["api_key"] == "[redacted]"
    assert call.payload["nested"]["refresh_token"] == "[redacted]"
    # Non-secret values survive — including the load-bearing identifier.
    assert call.payload["nested"]["audience"] == "acme-user-0002"
    assert call.payload["tokens_kept"] == "this-key-does-not-match"


async def test_redacts_authorization_header_record() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(
                UPLOAD_URL,
                json={"audience": "acme-user-0003"},
                headers={"Authorization": "Bearer secret-token"},
            )
    # Header redaction is exercised; the wire shape carries the body, which
    # must still round-trip its non-secret identifier.
    call = recorder.query(principal=PRINCIPAL).recorded_calls[0]
    assert call.payload == {"audience": "acme-user-0003"}


def test_secret_key_pattern_matches_spec_keys() -> None:
    for key in (
        "authorization",
        "Authorization",
        "credential",
        "credentials",
        "token",
        "api_key",
        "api-key",
        "apikey",
        "password",
        "secret",
        "client_secret",
        "refresh_token",
        "access_token",
        "bearer",
        "session_token",
        "offering_token",
        "cookie",
        "set_cookie",
        "set-cookie",
    ):
        assert SECRET_KEY_PATTERN.match(key), key
    for key in ("correlation_id", "audience", "tokens", "secretive"):
        assert not SECRET_KEY_PATTERN.match(key), key


@pytest.mark.parametrize("secret_param", ["api_key", "access_token", "X-Amz-Signature"])
async def test_redacts_secret_url_query_params(secret_param: str) -> None:
    recorder = UpstreamRecorder()
    url = (
        f"https://api.example.com/v2/audience/upload"
        f"?{secret_param}=sk-live-99999&audience_id=acme-0007"
    )
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(url, json={})

    call = recorder.query(principal=PRINCIPAL).recorded_calls[0]
    # Secret redacted in both url and the derived endpoint; non-secret kept.
    assert "sk-live-99999" not in call.url
    assert "sk-live-99999" not in call.endpoint
    assert f"{secret_param}=%5Bredacted%5D" in call.url
    assert "audience_id=acme-0007" in call.url
    assert call.endpoint == f"POST {call.url}"


async def test_redacts_secret_keys_in_form_urlencoded_body() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(
                UPLOAD_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": "rt-eyJsecret",
                    "client_secret": "cs-abc-secret",
                    "audience": "acme-user-0008",
                },
            )

    call = recorder.query(principal=PRINCIPAL).recorded_calls[0]
    assert "rt-eyJsecret" not in call.payload
    assert "cs-abc-secret" not in call.payload
    assert "refresh_token=%5Bredacted%5D" in call.payload
    assert "client_secret=%5Bredacted%5D" in call.payload
    # Non-secret keys survive, including the value used for grant_type.
    assert "grant_type=refresh_token" in call.payload
    assert "audience=acme-user-0008" in call.payload
    # payload_length tracks the emitted (redacted) bytes.
    assert call.payload_length == len(call.payload.encode("utf-8"))


# -- query filtering -------------------------------------------------------


async def test_since_timestamp_filters_older_calls() -> None:
    recorder = UpstreamRecorder()
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    recorder.record(
        method="POST",
        url=UPLOAD_URL,
        content_type="application/json",
        payload={"n": 1},
        timestamp=base,
        principal=PRINCIPAL,
    )
    recorder.record(
        method="POST",
        url=UPLOAD_URL,
        content_type="application/json",
        payload={"n": 2},
        timestamp=base + timedelta(minutes=5),
        principal=PRINCIPAL,
    )
    result = recorder.query(principal=PRINCIPAL, since_timestamp=base + timedelta(minutes=1))
    assert result.total_count == 1
    assert result.recorded_calls[0].payload == {"n": 2}


async def test_endpoint_pattern_wildcard_matches_path() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={})
            await client.post("https://api.example.com/v2/events/log", json={})

    matched = recorder.query(principal=PRINCIPAL, endpoint_pattern="POST */audience/upload")
    assert matched.total_count == 1
    assert matched.recorded_calls[0].url == UPLOAD_URL

    all_posts = recorder.query(principal=PRINCIPAL, endpoint_pattern="POST *")
    assert all_posts.total_count == 2


async def test_endpoint_pattern_is_anchored() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={})
    # Substring without wildcards must not match an anchored pattern.
    result = recorder.query(principal=PRINCIPAL, endpoint_pattern="audience/upload")
    assert result.total_count == 0


async def test_limit_truncates_and_reports_total() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            for i in range(5):
                await client.post(UPLOAD_URL, json={"i": i})
    result = recorder.query(principal=PRINCIPAL, limit=2)
    assert len(result.recorded_calls) == 2
    assert result.total_count == 5
    assert result.truncated is True


async def test_results_ordered_by_timestamp_ascending() -> None:
    recorder = UpstreamRecorder()
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    for offset in (10, 0, 5):
        recorder.record(
            method="GET",
            url=UPLOAD_URL,
            content_type="application/json",
            payload={"offset": offset},
            timestamp=base + timedelta(minutes=offset),
            principal=PRINCIPAL,
        )
    result = recorder.query(principal=PRINCIPAL)
    offsets = [c.payload["offset"] for c in result.recorded_calls]
    assert offsets == [0, 5, 10]


def test_invalid_limit_raises() -> None:
    recorder = UpstreamRecorder()
    with pytest.raises(ValueError):
        recorder.query(principal=PRINCIPAL, limit=0)


# -- query_upstream_traffic response shaping ------------------------------


async def test_recorded_call_dict_validates_against_spec_schema() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={"audience": "acme-user-0004"})

    item = recorder.query(principal=PRINCIPAL).recorded_calls[0].to_recorded_call_dict()
    # Validates against the inline recorded_calls[] item subschema, including
    # the raw/digest oneOf discriminator and required fields.
    jsonschema.validate(item, _recorded_call_item_schema())
    assert item["attestation_mode"] == "raw"
    # payload_length is the post-redaction UTF-8 byte length of the body.
    assert item["payload_length"] == len(
        json.dumps(item["payload"], separators=(",", ":")).encode("utf-8")
    )


async def test_response_dict_has_required_top_level_fields() -> None:
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={"audience": "acme-user-0005"})

    response = recorder.query(principal=PRINCIPAL).to_response_dict()
    for required in ("success", "recorded_calls", "total_count", "since_timestamp"):
        assert required in response
    assert response["success"] is True
    assert isinstance(response["recorded_calls"], list)
    # since_timestamp is ISO 8601.
    datetime.fromisoformat(response["since_timestamp"])


async def test_purpose_classifier_tags_calls() -> None:
    def classify(call: RecordedCall) -> str | None:
        return "platform_primary" if "/audience/upload" in call.path else "other"

    recorder = UpstreamRecorder(purpose=classify)
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(UPLOAD_URL, json={})
    item = recorder.query(principal=PRINCIPAL).recorded_calls[0].to_recorded_call_dict()
    assert item["purpose"] == "platform_primary"
    jsonschema.validate(item, _recorded_call_item_schema())


# -- payload truncation ----------------------------------------------------


def test_oversized_string_payload_is_truncated() -> None:
    recorder = UpstreamRecorder(max_payload_bytes=32)
    recorder.record(
        method="POST",
        url=UPLOAD_URL,
        content_type="text/plain",
        payload="x" * 100,
        principal=PRINCIPAL,
    )
    call = recorder.query(principal=PRINCIPAL).recorded_calls[0]
    assert call.payload.endswith("[…truncated]")
    # payload_length is the UTF-8 byte length of the EMITTED (truncated)
    # value, not the original, and the emitted value stays within the cap.
    assert call.payload_length == len(call.payload.encode("utf-8"))
    assert call.payload_length <= 32


async def test_truncated_payload_validates_against_spec_schema() -> None:
    # Truncate against the schema's payload maxLength (65536) so the emitted
    # value is exercised against the real bound, not a tiny synthetic cap.
    recorder = UpstreamRecorder()
    async with recorder.principal_scope_async(PRINCIPAL):
        async with await _client_with(recorder) as client:
            await client.post(
                UPLOAD_URL,
                content="y" * 70000,
                headers={"content-type": "text/plain"},
            )
    item = recorder.query(principal=PRINCIPAL).recorded_calls[0].to_recorded_call_dict()
    assert item["payload"].endswith("[…truncated]")
    assert len(item["payload"]) <= 65536
    assert item["payload_length"] == len(item["payload"].encode("utf-8"))
    jsonschema.validate(item, _recorded_call_item_schema())


def test_record_returns_none_when_unscoped() -> None:
    recorder = UpstreamRecorder()
    result = recorder.record(method="GET", url=UPLOAD_URL, content_type="application/json")
    assert result is None


def test_result_type_is_upstream_traffic_result() -> None:
    recorder = UpstreamRecorder()
    assert isinstance(recorder.query(principal=PRINCIPAL), UpstreamTrafficResult)
