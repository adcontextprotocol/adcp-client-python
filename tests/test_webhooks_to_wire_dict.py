"""Tests for ``adcp.webhooks.to_wire_dict``.

The seam exists so adopters wrapping ``create_a2a_webhook_payload`` and
``create_mcp_webhook_payload`` can serialize either return shape with one
call. The load-bearing properties:

* a2a protobuf round-trips to camelCase keys (``id``, ``contextId``,
  ``artifactId``) so external A2A receivers see the on-wire shape.
* MCP payloads dump to snake_case keys per the MCP webhook schema
  (``task_id``, ``task_type``).
* Pydantic models dump to JSON-mode dicts so sub-models serialize too.
* Plain dicts (legacy / hand-built) pass through verbatim.
* Unsupported types raise ``TypeError`` at the seam — silent fallthrough
  to ``str(payload)`` or similar would mask integration bugs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adcp.types import GeneratedTaskStatus
from adcp.types.generated_poc.core.mcp_webhook_payload import McpWebhookPayload
from adcp.webhooks import (
    create_a2a_webhook_payload,
    create_mcp_webhook_payload,
    to_wire_dict,
)


def test_a2a_task_round_trips_to_camelcase_wire_keys() -> None:
    """Terminated A2A status returns a Task → camelCase wire keys."""
    payload = create_a2a_webhook_payload(
        task_id="task_123",
        status=GeneratedTaskStatus.completed,
        context_id="ctx_456",
        result={"media_buy_id": "mb_1"},
        timestamp=datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc),
    )

    wire = to_wire_dict(payload)

    assert wire["id"] == "task_123"
    assert wire["contextId"] == "ctx_456"
    assert wire["status"]["state"] == "completed"
    assert wire["artifacts"][0]["artifactId"] == "task_123_result"
    # Inner DataPart preserves the AdCP response payload verbatim.
    assert wire["artifacts"][0]["parts"][0]["data"] == {"media_buy_id": "mb_1"}


def test_a2a_status_update_event_round_trips_to_camelcase_wire_keys() -> None:
    """Intermediate A2A status returns a TaskStatusUpdateEvent."""
    payload = create_a2a_webhook_payload(
        task_id="task_789",
        status=GeneratedTaskStatus.working,
        context_id="ctx_789",
        result={"current_step": "processing", "percentage": 50},
    )

    wire = to_wire_dict(payload)

    assert wire["taskId"] == "task_789"
    assert wire["contextId"] == "ctx_789"
    assert wire["status"]["state"] == "working"
    assert wire["status"]["message"]["role"] == "agent"
    assert wire["status"]["message"]["parts"][0]["data"] == {
        "current_step": "processing",
        "percentage": 50,
    }


def test_mcp_payload_dumps_to_snake_case_wire_keys() -> None:
    """MCP wire shape is snake_case per mcp-webhook-payload.json."""
    payload = create_mcp_webhook_payload(
        task_id="task_123",
        task_type="create_media_buy",
        operation_id="op_test_123",
        status="completed",
        result={"media_buy_id": "mb_1"},
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
    )

    wire = to_wire_dict(payload)

    assert wire["task_id"] == "task_123"
    assert wire["task_type"] == "create_media_buy"
    assert wire["status"] == "completed"
    assert wire["result"] == {"media_buy_id": "mb_1"}
    assert wire["idempotency_key"] == "whk_01HW9D2T3VXQ5M7K9N1P3R5S7U"
    assert wire["notification_id"] == "task_123.terminal"


def test_mcp_payload_requires_registered_operation_id() -> None:
    with pytest.raises(TypeError, match="operation_id"):
        create_mcp_webhook_payload(
            task_id="task_123",
            task_type="create_media_buy",
            status="completed",
        )

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="operation_id"):
        McpWebhookPayload.model_validate(
            {
                "idempotency_key": "whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
                "task_id": "task_123",
                "task_type": "create_media_buy",
                "status": "completed",
                "timestamp": datetime.now(timezone.utc),
            }
        )


def test_mcp_pydantic_model_dumps_to_snake_case_wire_keys() -> None:
    """Adopters that construct ``McpWebhookPayload`` directly get the
    same wire shape as the dict path — single seam, no per-shape branch.
    """
    model = McpWebhookPayload(
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
        task_id="task_456",
        task_type="create_media_buy",
        operation_id="op_test_123",
        status="completed",
        timestamp=datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc),
    )

    wire = to_wire_dict(model)

    assert wire["task_id"] == "task_456"
    assert wire["task_type"] == "create_media_buy"
    assert wire["status"] == "completed"
    # ``mode="json", exclude_none=True`` is load-bearing — None fields
    # would otherwise pollute the wire body.
    assert wire["operation_id"] == "op_test_123"
    assert "context_id" not in wire


def test_plain_dict_passes_through_unchanged() -> None:
    """Hand-built dicts (legacy adopter passthrough) round-trip verbatim."""
    raw = {"task_id": "t1", "status": "working", "extra": {"nested": True}}

    wire = to_wire_dict(raw)

    assert wire == raw
    # Defensive copy — caller mutating the returned dict must not
    # mutate the input.
    assert wire is not raw


def test_unsupported_type_raises_type_error() -> None:
    """Silent fallthrough would mask integration bugs — fail loud."""
    with pytest.raises(TypeError, match="Unsupported webhook payload type"):
        to_wire_dict("not a payload")  # type: ignore[arg-type]


def test_create_mcp_webhook_payload_returns_typed_instance() -> None:
    """Builder returns ``McpWebhookPayload`` so adopters get attribute
    access and IDE autocomplete without ``model_construct(**dict)`` ceremony."""
    payload = create_mcp_webhook_payload(
        task_id="task_123",
        status="completed",
        task_type="create_media_buy",
        operation_id="op_test_123",
        result={"media_buy_id": "mb_1"},
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
    )

    assert isinstance(payload, McpWebhookPayload)
    assert payload.task_id == "task_123"
    assert payload.idempotency_key == "whk_01HW9D2T3VXQ5M7K9N1P3R5S7U"
    # Wire bytes match the receiver's expectations.
    assert to_wire_dict(payload)["task_type"] == "create_media_buy"


def test_create_mcp_webhook_payload_rejects_invalid_task_type() -> None:
    """``task_type`` is restricted to the closed :class:`TaskType` enum.
    Unknown operations fail at construction so the publisher catches the bug
    before the receiver does."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="task_type"):
        create_mcp_webhook_payload(
            task_id="task_123",
            status="completed",
            task_type="not_a_task",
            operation_id="op_test_123",
            idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
        )


def test_create_mcp_webhook_payload_auto_derives_protocol_from_task_type() -> None:
    """When caller doesn't pass ``protocol``, the builder fills it from
    the ``task_type`` → ``AdcpProtocol`` mapping that mirrors the JS
    SDK's ``protocolForTool``. Cross-SDK webhook bodies classify
    operations identically without callers having to remember the map."""
    cases = {
        "create_media_buy": "media-buy",
        "get_products": "media-buy",
        "get_brand_identity": "brand",
        "create_property_list": "governance",
        "activate_signal": "signals",
        "sync_creatives": "creative",
    }
    for task_type, expected_protocol in cases.items():
        payload = create_mcp_webhook_payload(
            task_id="t",
            status="completed",
            task_type=task_type,
            operation_id="op_test_123",
            idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
        )
        assert to_wire_dict(payload)["protocol"] == expected_protocol, task_type


def test_create_mcp_webhook_payload_explicit_protocol_overrides_auto_derive() -> None:
    """An explicit ``protocol=`` always wins — the auto-derive is a
    convenience, not a constraint. Adopters with a tracked task that
    spans protocols (rare but spec-allowed) keep full control."""
    from adcp.types import AdcpProtocol

    payload = create_mcp_webhook_payload(
        task_id="t",
        status="completed",
        task_type="create_media_buy",  # would auto-derive to "media-buy"
        operation_id="op_test_123",
        protocol=AdcpProtocol.governance,
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
    )
    assert to_wire_dict(payload)["protocol"] == "governance"


def test_create_mcp_webhook_payload_protocol_kwarg() -> None:
    """``protocol`` is the typed schema field (``AdcpProtocol`` enum).
    Accepts the enum or a kebab-case string; rejects unknown values."""
    from pydantic import ValidationError

    from adcp.types import AdcpProtocol

    payload_enum = create_mcp_webhook_payload(
        task_id="task_1",
        status="completed",
        task_type="create_media_buy",
        operation_id="op_test_123",
        protocol=AdcpProtocol.media_buy,
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
    )
    payload_str = create_mcp_webhook_payload(
        task_id="task_2",
        status="completed",
        task_type="create_media_buy",
        operation_id="op_test_123",
        protocol="media-buy",
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
    )

    assert to_wire_dict(payload_enum)["protocol"] == "media-buy"
    assert to_wire_dict(payload_str)["protocol"] == "media-buy"

    # snake_case is wrong — the spec uses kebab-case for AdcpProtocol values.
    with pytest.raises(ValidationError, match="protocol"):
        create_mcp_webhook_payload(
            task_id="task_3",
            status="completed",
            task_type="create_media_buy",
            operation_id="op_test_123",
            protocol="media_buy",
            idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
        )


def test_token_is_typed_field_not_model_extra() -> None:
    """``McpWebhookPayload.token`` is now a typed schema field.

    Regression for adcp#4339 promotion: token must appear in
    ``model_fields``, not in ``model_extra``, and the typed kwarg path
    must produce a wire dict byte-identical to what the old
    ``additionalProperties`` shim produced.
    """
    token_value = "buyer-supplied-token-abc123456789"
    ik = "whk_01HW9D2T3VXQ5M7K9N1P3R5S7U"

    payload = create_mcp_webhook_payload(
        task_id="task_123",
        status="completed",
        task_type="create_media_buy",
        operation_id="op_test_123",
        token=token_value,
        idempotency_key=ik,
    )

    # token is a typed field, not a stray extra
    assert "token" in McpWebhookPayload.model_fields
    assert payload.token == token_value
    assert "token" not in (payload.model_extra or {})

    wire = to_wire_dict(payload)
    assert wire["token"] == token_value

    # Wire parity: dict built by hand must match the typed kwarg path
    hand_built = McpWebhookPayload.model_validate(
        {
            "idempotency_key": ik,
            "operation_id": "op_test_123",
            "task_id": "task_123",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": payload.timestamp,
            "token": token_value,
        }
    )
    hand_wire = to_wire_dict(hand_built)
    assert hand_wire["token"] == wire["token"]


def test_token_none_omitted_from_wire() -> None:
    """When no token is supplied the key is absent from the wire dict."""
    payload = create_mcp_webhook_payload(
        task_id="task_123",
        status="completed",
        task_type="create_media_buy",
        operation_id="op_test_123",
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
    )
    wire = to_wire_dict(payload)
    assert "token" not in wire
