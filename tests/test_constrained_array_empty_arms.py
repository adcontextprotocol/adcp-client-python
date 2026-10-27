"""A schema-valid document validates or fails with ``ValidationError``; it never raises (#1616).

datamodel-code-generator renders an array that carries an ``allOf`` of
``contains``/``if``-``then`` constraints as ``list[Item] | <empty model>``:
``losses`` on the legacy purchase continuation, ``refinements`` on
``refine_proposals_request``, ``results`` on ``refine_proposals_response``.
Pydantic applied the array's ``min_length`` to the whole union, so a dict that
reached the model arm raised ``TypeError: object of type 'Losses' has no len()``
instead of a ``ValidationError`` -- and every schema-valid ``McpWebhookPayload``
whose ``result`` union visited that arm crashed the same way.
``post_generate_fixes.drop_empty_model_arms_from_array_fields`` strips the arm;
these tests hold that line on the generated tree and on the public models.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from adcp.types import McpWebhookPayload
from adcp.types.domains.core.async_response_data import AdcpAsyncResponseData
from adcp.types.domains.media_buy.refine_proposals_request import RefineProposalsRequest
from adcp.types.domains.media_buy.refine_proposals_response import RefineProposalsResponse1
from adcp.types.domains.media_buy.request_proposals_response import PurchaseContinuation1
from adcp.validation.schema_loader import get_named_validator
from tests.conformance import _round_trip as rt

WEBHOOK_SCHEMA = "core/mcp-webhook-payload.json"
ASYNC_SCHEMA = "core/async-response-data.json"
_GENERATED_DIR = Path(__file__).resolve().parents[1] / "src/adcp/types/domains"


def _webhook_examples() -> list[Any]:
    document = json.loads((rt.schema_root() / WEBHOOK_SCHEMA).read_text())
    examples = document["examples"]
    assert len(examples) == 4, "the bundle ships four mcp-webhook-payload examples"
    return examples


@pytest.mark.parametrize("example", _webhook_examples(), ids=lambda example: example["description"])
def test_every_bundle_webhook_example_validates(example: dict[str, Any]) -> None:
    """Each example's ``data`` is schema-valid and parses into ``McpWebhookPayload``."""
    validator = get_named_validator(WEBHOOK_SCHEMA)
    assert validator is not None
    payload = example["data"]
    validator.validate(payload)
    parsed = McpWebhookPayload.model_validate_json(json.dumps(payload))
    assert parsed.task_id == payload["task_id"]
    assert McpWebhookPayload.model_validate(payload).status == parsed.status


@pytest.mark.parametrize("example", _webhook_examples(), ids=lambda example: example["description"])
def test_every_completed_webhook_example_validates(example: dict[str, Any]) -> None:
    """The sweep's completed form of each example -- the document that raised -- parses."""
    completed = rt.complete_example(WEBHOOK_SCHEMA, example)
    assert completed is not None, "the completed example must still be schema-valid"
    parsed = McpWebhookPayload.model_validate_json(json.dumps(completed))
    assert parsed.task_id == completed["task_id"]


def test_synthesized_async_response_data_validates() -> None:
    """A schema-valid ``async-response-data`` document resolves through the union."""
    payload = rt.synthesize_named(ASYNC_SCHEMA)
    assert payload is not None and rt.is_valid(ASYNC_SCHEMA, payload)
    TypeAdapter(AdcpAsyncResponseData).validate_json(json.dumps(payload))


_LEGACY_CONTINUATION = {
    "kind": "legacy_create",
    "continuation_token": "cont_0123456789abcdef",
    "continuation_expires_at": "2026-01-01T00:00:00Z",
    "source_adcp_version": "3.1",
    "product_ids": ["prod_1"],
    "losses": ["feed_version_not_atomic", "pricing_version_not_atomic"],
    "requires_explicit_acceptance": True,
}


@pytest.mark.parametrize(
    ("model", "payload", "field"),
    [
        pytest.param(PurchaseContinuation1, _LEGACY_CONTINUATION, "losses", id="losses"),
        pytest.param(
            RefineProposalsRequest,
            {"idempotency_key": "idem_0123456789abcdef", "refinements": []},
            "refinements",
            id="refinements",
        ),
        pytest.param(RefineProposalsResponse1, {"results": []}, "results", id="results"),
    ],
)
def test_a_dict_on_a_constrained_array_is_a_validation_error(
    model: type[Any], payload: dict[str, Any], field: str
) -> None:
    """A non-list reaches the field's constraints as a ``ValidationError``, not a ``TypeError``."""
    with pytest.raises(ValidationError) as refused:
        model.model_validate_json(json.dumps({**payload, field: {}}))
    assert {error["type"] for error in refused.value.errors()} >= {"list_type"}
    with pytest.raises(ValidationError):
        model.model_validate({**payload, field: {"not": "a list"}})


def test_legacy_continuation_still_accepts_its_losses() -> None:
    parsed = PurchaseContinuation1.model_validate(_LEGACY_CONTINUATION)
    assert [loss.value for loss in parsed.losses] == _LEGACY_CONTINUATION["losses"]
    with pytest.raises(ValidationError):  # min_length=2 still applies to the list
        PurchaseContinuation1.model_validate(
            {**_LEGACY_CONTINUATION, "losses": ["feed_version_not_atomic"]}
        )


def _union_arms(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union_arms(node.left) + _union_arms(node.right)
    return [node]


def test_generated_tree_has_no_empty_model_arm_beside_a_list() -> None:
    """The generator rule holds across the whole tree, not only the three known fields."""
    violations: list[str] = []
    for path in sorted(_GENERATED_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        empty = {
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and [ast.unparse(base) for base in node.bases] == ["AdCPBaseModel"]
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Pass)
        }
        if not empty:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign):
                continue
            for union in ast.walk(node.annotation):
                if not (isinstance(union, ast.BinOp) and isinstance(union.op, ast.BitOr)):
                    continue
                arms = _union_arms(union)
                has_list = any(ast.unparse(arm).startswith("list[") for arm in arms)
                empty_arms = [ast.unparse(arm) for arm in arms if ast.unparse(arm) in empty]
                if has_list and empty_arms:
                    violations.append(
                        f"{path.relative_to(_GENERATED_DIR)}:{node.lineno} {empty_arms}"
                    )
                    break
    assert not violations, "empty-model arms beside a list (#1616):\n" + "\n".join(violations)
