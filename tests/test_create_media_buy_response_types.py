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
    from adcp.types.canonical_creative import CreateMediaBuyResponse3

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


def test_confirmed_at_accepts_null_and_stays_required() -> None:
    """``confirmed_at`` is required *and* nullable — both axes hold.

    Regression for #1137. ``media-buy/create-media-buy-response.json`` types
    ``confirmed_at`` as ``["string", "null"]`` and lists it in the success
    branch's ``required``: a buy still awaiting seller commitment (e.g.
    ``pending_creatives``) has no instant to report, so the key must be
    present and may be null.
    """
    from pydantic import ValidationError

    from adcp.types import CreateMediaBuySuccessResponse

    field = CreateMediaBuySuccessResponse.model_fields["confirmed_at"]
    assert field.is_required(), "confirmed_at must not gain a default"
    assert type(None) in typing.get_args(field.annotation)

    provisional = CreateMediaBuySuccessResponse(
        media_buy_id="mb_1",
        packages=[],
        confirmed_at=None,
        revision=1,
    )
    assert provisional.confirmed_at is None

    parsed = CreateMediaBuySuccessResponse.model_validate(
        {"media_buy_id": "mb_1", "packages": [], "confirmed_at": None, "revision": 1}
    )
    assert parsed.confirmed_at is None

    with pytest.raises(ValidationError):
        CreateMediaBuySuccessResponse.model_validate(
            {"media_buy_id": "mb_1", "packages": [], "revision": 1}
        )


def test_confirmed_at_null_survives_serialization_when_none_is_kept() -> None:
    """A null ``confirmed_at`` round-trips whenever ``None`` is not excluded.

    ``AdCPBaseModel.model_dump`` still defaults to ``exclude_none=True``, which
    drops the key; reconciling that blanket default with required-and-nullable
    fields is tracked separately (#1137's second-order note) and deliberately
    out of scope here. What this pins is that the *model* carries the null, so
    an explicit ``exclude_none=False`` dump emits the schema-required key.
    """
    from adcp.types import CreateMediaBuySuccessResponse

    resp = CreateMediaBuySuccessResponse(
        media_buy_id="mb_1",
        packages=[],
        confirmed_at=None,
        revision=1,
    )
    dumped = resp.model_dump(exclude_none=False)
    assert "confirmed_at" in dumped
    assert dumped["confirmed_at"] is None

    assert CreateMediaBuySuccessResponse.model_validate(dumped).confirmed_at is None


def test_confirmed_at_still_accepts_a_commitment_timestamp() -> None:
    """Widening to ``| None`` must not loosen datetime validation."""
    from datetime import datetime, timezone

    from pydantic import ValidationError

    from adcp.types import CreateMediaBuySuccessResponse

    committed = CreateMediaBuySuccessResponse.model_validate(
        {
            "media_buy_id": "mb_1",
            "packages": [],
            "confirmed_at": "2026-05-27T12:00:00Z",
            "revision": 1,
        }
    )
    assert committed.confirmed_at == datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(ValidationError):
        CreateMediaBuySuccessResponse.model_validate(
            {
                "media_buy_id": "mb_1",
                "packages": [],
                "confirmed_at": "not-a-timestamp",
                "revision": 1,
            }
        )
