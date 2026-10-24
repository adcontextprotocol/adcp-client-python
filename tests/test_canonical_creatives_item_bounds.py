"""Canonical ``creatives`` fields keep the generated item bounds and description.

The canonical clones retype ``creatives`` to canonical ``CreativeAsset`` items.
An override written as a bare ``Field(min_length=1)`` replaced the generated
``FieldInfo`` wholesale, dropping ``max_length=100`` and the description, so the
model accepted -- and advertised -- payloads the bundled wire schema rejects.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from adcp.types import (
    CreateMediaBuyRequest,
    PackageRequest,
    PackageUpdate,
    SyncCreativesRequest,
    UpdateMediaBuyRequest,
)
from adcp.types.generated_poc.creative.sync_creatives_request import (
    SyncCreativesRequest as _GeneratedSyncCreativesRequest,
)
from adcp.types.generated_poc.media_buy.package_request import (
    PackageRequest as _GeneratedPackageRequest,
)
from adcp.types.generated_poc.media_buy.package_update import (
    PackageUpdate as _GeneratedPackageUpdate,
)
from adcp.validation import get_schema, validate_request

_CREATIVE = {"creative_id": "cr_1", "name": "Banner", "format_kind": "image", "assets": {}}
_ACCOUNT = {"account_id": "acct_1"}


def _sync(creatives: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "adcp_version": "3.2",
        "idempotency_key": "idem_sync_000000001",
        "account": _ACCOUNT,
        "creatives": creatives,
    }


def _create(creatives: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "adcp_version": "3.2",
        "idempotency_key": "idem_create_00000001",
        "account": _ACCOUNT,
        "brand": {"domain": "brand.example"},
        "packages": [
            {
                "product_id": "prod_1",
                "pricing_option_id": "po_1",
                "budget": 1000,
                "creatives": creatives,
            }
        ],
        "start_time": "2026-10-01T00:00:00Z",
        "end_time": "2026-10-31T00:00:00Z",
    }


def _update(creatives: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "adcp_version": "3.2",
        "idempotency_key": "idem_update_00000001",
        "account": _ACCOUNT,
        "media_buy_id": "mb_1",
        "packages": [{"package_id": "pkg_1", "creatives": creatives}],
    }


#: (tool, request model, payload builder, JSON pointer and pydantic loc of ``creatives``)
_SURFACES = [
    pytest.param(
        "sync_creatives", SyncCreativesRequest, _sync, "/creatives", ("creatives",), id="sync"
    ),
    pytest.param(
        "create_media_buy",
        CreateMediaBuyRequest,
        _create,
        "/packages/0/creatives",
        ("packages", 0, "creatives"),
        id="package_request",
    ),
    pytest.param(
        "update_media_buy",
        UpdateMediaBuyRequest,
        _update,
        "/packages/0/creatives",
        ("packages", 0, "creatives"),
        id="package_update",
    ),
]


@pytest.mark.parametrize(
    ("count", "schema_keyword", "pydantic_type"),
    [
        (0, "minItems", "too_short"),
        (1, None, None),
        (100, None, None),
        (101, "maxItems", "too_long"),
    ],
)
@pytest.mark.parametrize(("tool", "model", "build", "pointer", "loc"), _SURFACES)
def test_model_and_bundled_schema_agree_on_creatives_item_count(
    tool: str,
    model: Any,
    build: Any,
    pointer: str,
    loc: tuple[Any, ...],
    count: int,
    schema_keyword: str | None,
    pydantic_type: str | None,
) -> None:
    payload = build([_CREATIVE] * count)
    outcome = validate_request(tool, payload)

    if schema_keyword is None:
        assert outcome.valid, outcome.issues
        model.model_validate(payload)
        return

    assert not outcome.valid
    assert any(
        issue.pointer == pointer and issue.keyword == schema_keyword for issue in outcome.issues
    ), outcome.issues
    with pytest.raises(ValidationError) as caught:
        model.model_validate(payload)
    assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [
        (loc, pydantic_type)
    ]


@pytest.mark.parametrize(
    ("canonical", "generated"),
    [
        pytest.param(PackageRequest, _GeneratedPackageRequest, id="package_request"),
        pytest.param(PackageUpdate, _GeneratedPackageUpdate, id="package_update"),
        pytest.param(SyncCreativesRequest, _GeneratedSyncCreativesRequest, id="sync"),
    ],
)
def test_canonical_creatives_inherit_generated_constraints(canonical: Any, generated: Any) -> None:
    canonical_field = canonical.model_fields["creatives"]
    generated_field = generated.model_fields["creatives"]
    assert canonical_field.metadata == generated_field.metadata
    assert canonical_field.description == generated_field.description


def _published_creatives_node(model: Any) -> dict[str, Any]:
    """The ``creatives`` node the model advertises, with a ``T | None`` union folded."""
    node = model.model_json_schema()["properties"]["creatives"]
    arms = [arm for arm in node.get("anyOf", [node]) if arm != {"type": "null"}]
    assert len(arms) == 1, arms
    return {**{key: value for key, value in node.items() if key != "anyOf"}, **arms[0]}


def _bundled_node(tool: str, *path: str) -> dict[str, Any]:
    node = get_schema(tool, "request")
    assert node is not None
    for key in path:
        node = node[key]
    return node


@pytest.mark.parametrize(
    ("model", "bundled"),
    [
        pytest.param(
            SyncCreativesRequest,
            lambda: _bundled_node("sync_creatives", "properties", "creatives"),
            id="sync",
        ),
        pytest.param(
            PackageUpdate,
            lambda: _bundled_node(
                "update_media_buy", "properties", "packages", "items", "properties", "creatives"
            ),
            id="package_update",
        ),
    ],
)
def test_published_schema_carries_the_bundled_bounds(model: Any, bundled: Any) -> None:
    published = _published_creatives_node(model)
    expected = bundled()
    graded = ("type", "description", "minItems", "maxItems")
    assert {key: published.get(key) for key in graded} == {key: expected[key] for key in graded}


def test_package_request_published_schema_carries_the_bounds() -> None:
    # create_media_buy's bundled packages node is an allOf composition, so grade
    # the model's advertised bounds against the generated field directly.
    published = _published_creatives_node(PackageRequest)
    generated = _GeneratedPackageRequest.model_fields["creatives"]
    assert published["minItems"] == 1
    assert published["maxItems"] == 100
    assert published["description"] == generated.description


def test_sync_creatives_still_requires_creatives_on_8x() -> None:
    """SDK 8 keeps ``creatives`` required; assignment-only syncs are a 9.x change."""
    assert SyncCreativesRequest.model_fields["creatives"].is_required()
    assert "creatives" in SyncCreativesRequest.model_json_schema()["required"]
    payload = _sync([])
    del payload["creatives"]
    with pytest.raises(ValidationError) as caught:
        SyncCreativesRequest.model_validate(payload)
    assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [
        (("creatives",), "missing")
    ]
