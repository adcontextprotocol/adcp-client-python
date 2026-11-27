"""Parsing a response does not require ``status`` in SDK 10.x (#1678).

An absent ``status`` reads as ``completed``: ``ProtocolEnvelope.status`` defaults to
``TaskStatus.completed`` in the generated models, and ``validate_response`` accepts a
sync body with no ``status`` key. The server supplies ``status`` wherever the served
release's response requires it; this pins only the lenient read side recorded in
MIGRATION_v9_to_v10.md ("Parsing a response does not require ``status``").
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.types import CreateMediaBuySuccessResponse, GetProductsResponse
from adcp.types.domains.enums.task_status import TaskStatus
from adcp.validation import validate_response
from adcp.validation.client_hooks import validate_incoming_response

# Minimal valid sync bodies at the SDK default pin, deliberately without ``status``.
GET_PRODUCTS: dict[str, Any] = {"products": [], "cache_scope": "public"}
CREATE_MEDIA_BUY: dict[str, Any] = {
    "media_buy_id": "mb-1",
    "packages": [],
    "confirmed_at": "2026-01-01T00:00:00Z",
    "revision": 1,
}


def test_get_products_without_status_reads_as_completed() -> None:
    body = {"products": [], "cache_scope": "public"}
    assert GetProductsResponse(**body).status == TaskStatus.completed
    assert GetProductsResponse.model_validate(body).status == TaskStatus.completed
    parsed = GetProductsResponse.model_validate_json('{"products": [], "cache_scope": "public"}')
    assert parsed.status == TaskStatus.completed


def test_empty_get_products_without_status_reads_as_completed() -> None:
    assert GetProductsResponse(products=[]).status == TaskStatus.completed
    assert GetProductsResponse.model_validate({"products": []}).status == TaskStatus.completed
    parsed = GetProductsResponse.model_validate_json('{"products": []}')
    assert parsed.status == TaskStatus.completed


def test_create_media_buy_success_without_status_reads_as_completed() -> None:
    assert CreateMediaBuySuccessResponse(**CREATE_MEDIA_BUY).status == TaskStatus.completed
    parsed = CreateMediaBuySuccessResponse.model_validate(CREATE_MEDIA_BUY)
    assert parsed.status == TaskStatus.completed


@pytest.mark.parametrize(
    ("tool", "body"), [("get_products", GET_PRODUCTS), ("create_media_buy", CREATE_MEDIA_BUY)]
)
def test_validate_response_accepts_a_sync_body_without_status(
    tool: str, body: dict[str, Any]
) -> None:
    assert "status" not in body
    outcome = validate_response(tool, body)
    assert outcome.valid is True, outcome.issues
    assert outcome.variant == "sync"


@pytest.mark.parametrize(
    ("tool", "body"), [("get_products", GET_PRODUCTS), ("create_media_buy", CREATE_MEDIA_BUY)]
)
def test_strict_buyer_validation_does_not_fail_a_body_without_status(
    tool: str, body: dict[str, Any]
) -> None:
    outcome = validate_incoming_response(tool, body, "strict")
    assert outcome.valid is True, outcome.issues
    assert outcome.issues == []


def test_explicit_submitted_status_still_selects_the_submitted_variant() -> None:
    body = {"status": "submitted", "task_id": "task-1"}
    outcome = validate_response("get_products", body)
    assert outcome.valid is True, outcome.issues
    assert outcome.variant == "submitted"
    assert validate_incoming_response("get_products", body, "strict").variant == "submitted"
    # The default is a read-side fallback: it never overwrites a status that is present.
    assert body["status"] == "submitted"
