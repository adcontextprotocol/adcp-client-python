"""``status`` on compact responses follows the served release's own schema.

AdCP 3.3 composes ``core/protocol-envelope.json`` into every response root, so
``status`` is REQUIRED on ``list_products`` and ``decline_proposals`` there. The
closed 3.2.x roots have none (``list_products`` forbids it; ``decline_proposals``
permits it only on its submitted arm). The SDK reads the answer from the release's
schema instead of a version string, and it never strips a field for any other
tool or release (matches adcontextprotocol/adcp-client#3493).
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.compat.versions import convert_payload
from adcp.server.mcp_tools import _normalize_response_envelope
from adcp.types.domains.core.protocol_envelope import ProtocolEnvelope
from adcp.types.domains.media_buy.decline_proposals_response import DeclineProposalsResponse1
from adcp.types.domains.media_buy.list_products_response import ListProductsResponse1
from adcp.validation import validate_response
from adcp.validation._response_envelope import uses_task_status_envelope
from adcp.validation.schema_loader import _is_protocol_envelope_arm, get_validator

COMPOSED = "3.3-beta.3"
CLOSED = "3.2"

LISTED: dict[str, Any] = {
    "outcome": "listed",
    "products": [],
    "feed_version": "v1",
    "cache_scope": "public",
}
DECLINED: dict[str, Any] = {"results": [{"proposal_id": "proposal-1", "outcome": "declined"}]}
COMPACT = [("list_products", LISTED), ("decline_proposals", DECLINED)]


@pytest.mark.parametrize("tool", ["list_products", "decline_proposals"])
def test_release_schema_decides_whether_status_belongs(tool: str) -> None:
    assert uses_task_status_envelope(tool, COMPOSED) is True
    assert uses_task_status_envelope(tool, "3.3.0-beta.3") is True
    assert uses_task_status_envelope(tool, None) is True  # the SDK pin is 3.3.0-beta.3
    assert uses_task_status_envelope(tool, CLOSED) is False
    assert uses_task_status_envelope(tool, "3.2.7") is False
    # A release that predates the composition keeps its own contract even when
    # it is a 3.3 prerelease.
    assert uses_task_status_envelope(tool, "3.3-beta.2") is False


@pytest.mark.parametrize("tool", ["get_products", "create_media_buy", "sync_creatives"])
@pytest.mark.parametrize("version", [COMPOSED, CLOSED, None])
def test_every_other_tool_keeps_the_status_envelope(tool: str, version: str | None) -> None:
    assert uses_task_status_envelope(tool, version) is True


def test_unparseable_or_unserved_release_keeps_the_pre_composition_shape() -> None:
    assert uses_task_status_envelope("list_products", "not-a-version") is False
    assert uses_task_status_envelope("list_products", "9.9") is False


@pytest.mark.parametrize(("tool", "body"), COMPACT)
def test_composed_release_requires_status_and_closed_release_refuses_it(
    tool: str, body: dict[str, Any]
) -> None:
    composed = get_validator(tool, "sync", version=COMPOSED)
    closed = get_validator(tool, "sync", version=CLOSED)
    assert composed is not None and closed is not None
    with_status = {**body, "status": "completed"}
    assert composed.is_valid(with_status)
    assert not composed.is_valid(body)
    assert closed.is_valid(body)
    assert not closed.is_valid(with_status)


@pytest.mark.parametrize(("tool", "body"), COMPACT)
def test_validate_response_supplies_the_status_a_composed_release_requires(
    tool: str, body: dict[str, Any]
) -> None:
    assert validate_response(tool, body, version=COMPOSED).valid
    assert validate_response(tool, body, version=CLOSED).valid
    assert not validate_response(tool, {**body, "status": "completed"}, version=CLOSED).valid


@pytest.mark.parametrize(("tool", "body"), COMPACT)
def test_server_emits_status_only_where_the_served_release_has_it(
    tool: str, body: dict[str, Any]
) -> None:
    served_3_3 = dict(body)
    _normalize_response_envelope(tool, served_3_3, {}, adcp_version=COMPOSED)
    assert served_3_3["status"] == "completed"

    served_3_2 = dict(body)
    _normalize_response_envelope(tool, served_3_2, {}, adcp_version=CLOSED)
    assert "status" not in served_3_2


def test_typed_default_status_does_not_reach_a_closed_3_2_root() -> None:
    dumped = ListProductsResponse1(**LISTED).model_dump(mode="json", exclude_none=True)
    assert dumped["status"] == "completed"  # the typed model carries the 3.3 default
    _normalize_response_envelope("list_products", dumped, {}, adcp_version=CLOSED)
    assert "status" not in dumped
    assert validate_response("list_products", dumped, version=CLOSED).valid


def test_a_submitted_decline_keeps_its_status_under_both_releases() -> None:
    for version in (COMPOSED, CLOSED):
        submitted = {"status": "submitted", "task_id": "task-1"}
        _normalize_response_envelope("decline_proposals", submitted, {}, adcp_version=version)
        assert submitted == {"status": "submitted", "task_id": "task-1"}


def test_conversion_down_drops_the_status_a_3_2_root_refuses() -> None:
    for tool, body in COMPACT:
        converted = convert_payload(
            tool,
            {**body, "status": "completed"},
            direction="sync",
            source_version=COMPOSED,
            target_version=CLOSED,
        )
        assert "status" not in converted.payload
        assert [loss.pointer for loss in converted.losses] == ["/status"]


def test_conversion_up_supplies_the_status_a_3_3_root_requires() -> None:
    for tool, body in COMPACT:
        converted = convert_payload(
            tool, body, direction="sync", source_version=CLOSED, target_version=COMPOSED
        )
        assert converted.payload["status"] == "completed"
        assert validate_response(tool, converted.payload, version=COMPOSED).valid


def test_conversion_down_keeps_a_submitted_status() -> None:
    converted = convert_payload(
        "decline_proposals",
        {"status": "submitted", "task_id": "task-1"},
        direction="submitted",
        source_version=COMPOSED,
        target_version=CLOSED,
    )
    assert converted.payload["status"] == "submitted"
    assert converted.losses == ()


def test_envelope_arm_is_recognised_inlined_or_by_reference() -> None:
    assert _is_protocol_envelope_arm(
        {"$ref": "https://adcontextprotocol.org/schemas/3.3.0-beta.3/core/protocol-envelope.json"}
    )
    assert _is_protocol_envelope_arm(
        {"required": ["status"], "properties": {"status": {"type": "string"}}}
    )
    assert not _is_protocol_envelope_arm({"$ref": "https://example.test/core/context.json"})
    assert not _is_protocol_envelope_arm({"properties": {"status": {"type": "string"}}})
    assert not _is_protocol_envelope_arm("not a schema")


@pytest.mark.parametrize("model", [ListProductsResponse1, DeclineProposalsResponse1])
def test_generated_responses_inherit_the_protocol_envelope_fields(model: type) -> None:
    assert issubclass(model, ProtocolEnvelope)
    for field in (
        "status",
        "task_id",
        "timestamp",
        "adcp_version",
        "adcp_major_version",
        "replayed",
    ):
        assert field in model.model_fields, field
