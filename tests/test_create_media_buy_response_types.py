"""Tests for CreateMediaBuyResponse 3-branch discriminated union.

The wire union has three legitimate shapes:

1. ``CreateMediaBuySuccessResponse`` (sync success): ``media_buy_id`` + ``packages``.
2. ``CreateMediaBuyErrorResponse`` (sync error): ``errors[]``.
3. ``CreateMediaBuySubmittedResponse`` (async accepted): ``status='submitted'``
   + ``task_id`` — buyer polls via tasks/get; ``media_buy_id`` is issued later
   on the completion artifact.

These tests validate that the public alias surface covers all three branches
and that the submitted-branch payload validates against its alias.
"""

from __future__ import annotations

import typing

import pytest


def test_submitted_alias_resolves_to_third_branch() -> None:
    """The submitted alias must point at CreateMediaBuyResponse3 — the
    branch with status='submitted' + task_id."""
    from adcp.types import CreateMediaBuySubmittedResponse
    from adcp.types._generated import CreateMediaBuyResponse3

    assert CreateMediaBuySubmittedResponse is CreateMediaBuyResponse3


def test_submitted_payload_validates() -> None:
    """A spec-compliant submitted payload validates via model_validate."""
    from adcp.types import CreateMediaBuySubmittedResponse

    payload = {
        "status": "submitted",
        "task_id": "task_abc123",
    }
    resp = CreateMediaBuySubmittedResponse.model_validate(payload)
    assert resp.status == "submitted"
    assert resp.task_id == "task_abc123"
    assert resp.adcp_version is None
    assert resp.context_id is None
    assert resp.replayed is False
    assert resp.push_notification_config is None
    assert resp.governance_context is None


def test_submitted_payload_with_optional_message_and_errors() -> None:
    """Optional advisory fields on the submitted envelope are accepted."""
    from adcp.types import CreateMediaBuySubmittedResponse

    payload = {
        "status": "submitted",
        "task_id": "task_xyz",
        "message": "Awaiting IO signature; typical turnaround 2-4 hours.",
    }
    resp = CreateMediaBuySubmittedResponse.model_validate(payload)
    assert resp.message is not None
    assert "IO signature" in resp.message


def test_submitted_payload_missing_task_id_rejected() -> None:
    """task_id is required — a malformed submitted envelope must fail.

    The triage of issue #570 traced the original FastMCP error to a
    submitted-branch payload missing task_id. The schema is correct;
    this test pins that the missing-field validation works.
    """
    from pydantic import ValidationError

    from adcp.types import CreateMediaBuySubmittedResponse

    with pytest.raises(ValidationError):
        CreateMediaBuySubmittedResponse.model_validate({"status": "submitted"})


def test_handler_create_media_buy_return_type_is_union() -> None:
    """The PlatformHandler.create_media_buy annotation must be the
    3-branch union (CreateMediaBuyResponse), not the success branch
    alone — adopters legitimately return any of the three shapes.
    """
    from adcp.decisioning.handler import PlatformHandler
    from adcp.types import CreateMediaBuyResponse

    # handler.py uses ``from __future__ import annotations`` (PEP 563),
    # so signatures carry strings — resolve to runtime objects.
    hints = typing.get_type_hints(PlatformHandler.create_media_buy)
    assert hints["return"] == CreateMediaBuyResponse
