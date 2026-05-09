"""Tests for create_a2a_webhook_payload status mapping correctness.

Guards against issue #603: previously, statuses not in the AdCP→A2A map
(canceled, rejected, auth_required) silently fell back to TASK_STATE_UNSPECIFIED
(proto3 zero value), which MessageToDict omits — producing an invalid
``{"status": {}}`` wire shape with no ``state`` field. Unknown statuses now
raise ValueError instead.
"""

from __future__ import annotations

import types

import pytest
from a2a.types import Task, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict

from adcp.types import GeneratedTaskStatus
from adcp.webhooks import create_a2a_webhook_payload


def _wire_state(obj: Task | TaskStatusUpdateEvent) -> str:
    """Serialize to wire dict and return the normalized status.state string."""
    d = MessageToDict(obj, preserving_proto_field_name=False)
    state = d.get("status", {}).get("state", "")
    if isinstance(state, str) and state.startswith("TASK_STATE_"):
        state = state[len("TASK_STATE_") :].lower().replace("_", "-")
    return state


# --- terminal statuses return Task ---


def test_canceled_returns_task_with_canceled_state() -> None:
    payload = create_a2a_webhook_payload(
        task_id="t1",
        status=GeneratedTaskStatus.canceled,
        context_id="ctx1",
        result={},
    )
    assert isinstance(payload, Task)
    assert _wire_state(payload) == "canceled"


def test_rejected_returns_task_with_rejected_state() -> None:
    payload = create_a2a_webhook_payload(
        task_id="t2",
        status=GeneratedTaskStatus.rejected,
        context_id="ctx2",
        result={},
    )
    assert isinstance(payload, Task)
    assert _wire_state(payload) == "rejected"


# --- intermediate statuses return TaskStatusUpdateEvent ---


def test_auth_required_returns_event_with_auth_required_state() -> None:
    payload = create_a2a_webhook_payload(
        task_id="t3",
        status=GeneratedTaskStatus.auth_required,
        context_id="ctx3",
        result={},
    )
    assert isinstance(payload, TaskStatusUpdateEvent)
    assert _wire_state(payload) == "auth-required"


# --- unknown status raises ValueError ---


def test_unknown_status_value_raises_value_error() -> None:
    # Simulate a caller passing an enum-like object whose .value is not in
    # the AdCP→A2A map (e.g. a future enum member not yet supported).
    fake_status = types.SimpleNamespace(value="bogus_status")
    with pytest.raises(ValueError, match="unknown status"):
        create_a2a_webhook_payload(
            task_id="t4",
            status=fake_status,  # type: ignore[arg-type]
            context_id="ctx4",
            result={},
        )


def test_unknown_status_error_message_names_known_states() -> None:
    fake_status = types.SimpleNamespace(value="bogus_status")
    with pytest.raises(ValueError, match="canceled"):
        create_a2a_webhook_payload(
            task_id="t5",
            status=fake_status,  # type: ignore[arg-type]
            context_id="ctx5",
            result={},
        )
