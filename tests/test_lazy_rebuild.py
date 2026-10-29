"""The import-time patchers defer their rebuilds to first use (#1694).

``_ergonomic``, ``_forward_compat``, ``canonical_creative`` and ``aliases``
edit generated models' ``model_fields`` at import and then need the core
schema re-derived. They used to call ``model_rebuild(force=True)`` on the
spot, which walked each request or response root's whole subtree into
pydantic-core at import — the bulk of the 520 MiB ``adcp.client`` cost.
:func:`adcp.types.base.defer_model_rebuild` puts the model back into the
``defer_build`` state instead, so the patched fields take effect on first use.

This module grades that contract on pydantic's private placeholders, which
the helper depends on: that the patched models are NOT built by importing
the surface, that they DO build on first use with the patches applied, and
that a generated ``RootModel`` parametrizes :class:`AdCPRootModel` without
building at class creation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from adcp.types.base import AdCPBaseModel, AdCPRootModel, defer_model_rebuild

# Models each patcher force-rebuilt at import before #1694. Read from a
# subprocess so the assertion is about the import itself, not about whatever
# an earlier test in this process already validated.
_PATCHED_MODELS = {
    "adcp.types._forward_compat": [
        "CreateMediaBuyRequest",
        "UpdateMediaBuyRequest",
        "ReportingWebhook",
        "McpWebhookPayload",
        "GetCreativeDeliveryResponse",
    ],
    "adcp.types._ergonomic": [
        "GetProductsRequest",
        "ListCreativesResponse",
        "GetMediaBuyDeliveryResponse",
    ],
    "adcp.types.canonical_creative": ["CreativeManifest", "Format", "Product"],
    "adcp.types.domains.adagents": ["AdcpAgentsAuthorization"],
    "adcp.types.domains.core.format": ["CanonicalParameters"],
}

_PROBE = """
import adcp.types._eager
import importlib, json
out = {}
for module, names in SPEC.items():
    mod = importlib.import_module(module)
    for name in names:
        out[f"{module}.{name}"] = getattr(mod, name).__pydantic_complete__
print(json.dumps(out))
"""


def test_importing_the_surface_builds_no_patched_model() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE.replace("SPEC", repr(_PATCHED_MODELS))],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    built = {name for name, complete in json.loads(result.stdout).items() if complete}
    assert not built, f"importing adcp.types._eager built {sorted(built)} at import time"


def test_forward_compat_patches_apply_on_first_use() -> None:
    from adcp.types import CreateMediaBuyRequest, ReportingWebhook

    # The reporting_webhook extension (#1547) is a patched-in field.
    registration = {
        "url": "https://buyer.example/hook",
        "authentication": {
            "schemes": ["Bearer"],
            "credentials": "buyer-reporting-token-1234567890",
        },
        "reporting_frequency": "daily",
    }
    webhook = ReportingWebhook.model_validate({**registration, "operation_id": "op-1"})
    assert webhook.operation_id == "op-1"
    with pytest.raises(ValidationError):
        ReportingWebhook.model_validate({**registration, "operation_id": ""})
    # And the request parent sees it through its own lazily built schema.
    schema = CreateMediaBuyRequest.model_json_schema()
    assert "operation_id" in schema["$defs"]["ReportingWebhook"]["properties"]


def test_ergonomic_patches_apply_on_first_use() -> None:
    from adcp.types.domains.media_buy.list_creative_formats_request import (
        ListCreativeFormatsRequest,
    )

    # The order-preserving unique-items validator is appended to field metadata
    # at import and only takes effect once the schema is built.
    request = ListCreativeFormatsRequest.model_validate(
        {"disclosure_positions": ["overlay", "overlay"]}
    )
    assert [p.value for p in request.disclosure_positions or []] == ["overlay"]


def test_defer_model_rebuild_rebuilds_from_patched_fields() -> None:
    class Node(AdCPBaseModel):
        model_config = ConfigDict(defer_build=True)
        value: int = 0

    assert Node.model_validate({"value": 1}).value == 1
    assert Node.__pydantic_complete__

    # Widen the field the way the patchers do, then defer instead of forcing.
    field = Node.model_fields["value"]
    field.annotation = int | str
    Node.__annotations__["value"] = int | str
    defer_model_rebuild(Node)
    assert not Node.__pydantic_complete__

    assert Node.model_validate({"value": "x"}).value == "x"
    assert Node.__pydantic_complete__
    assert Node(value=2).model_dump() == {"value": 2}
    assert Node.model_json_schema()["properties"]["value"]["anyOf"] == [
        {"type": "integer"},
        {"type": "string"},
    ]


def test_defer_model_rebuild_on_a_never_built_model_is_harmless() -> None:
    class Fresh(AdCPBaseModel):
        model_config = ConfigDict(defer_build=True)
        value: int = 0

    assert not Fresh.__pydantic_complete__
    defer_model_rebuild(Fresh)
    assert not Fresh.__pydantic_complete__
    assert Fresh.model_validate({"value": 3}).value == 3


def test_adcp_root_model_parametrizes_deferred() -> None:
    class Left(BaseModel):
        kind: Literal["left"] = "left"
        x: int

    class Right(BaseModel):
        kind: Literal["right"] = "right"
        y: str

    parametrized = AdCPRootModel[Left | Right]
    assert not parametrized.__pydantic_complete__
    # pydantic's own RootModel builds at the subscript, which is the cost
    # this base exists to avoid.
    assert RootModel[Left | Right].__pydantic_complete__

    class Either(AdCPRootModel[Left | Right]):
        root: Left | Right = Field(discriminator="kind")

    assert not Either.__pydantic_complete__
    assert issubclass(Either, RootModel)
    assert Either.model_validate({"kind": "right", "y": "q"}).root == Right(y="q")
    assert Either.__pydantic_complete__
    assert Either(Left(x=1)).model_dump() == {"kind": "left", "x": 1}
    assert set(Either.model_json_schema()["oneOf"][0]) == {"$ref"}


def test_generated_root_models_parametrize_adcp_root_model() -> None:
    from adcp.types.domains.adagents import AdcpAgentsAuthorization
    from adcp.types.domains.core.format import CanonicalParameters

    for root in (AdcpAgentsAuthorization, CanonicalParameters):
        assert issubclass(root, AdCPRootModel), root
        assert root.model_config.get("defer_build") is True


def test_a_canonical_subclass_may_forward_reference_a_later_model() -> None:
    """The removal hook's deferred rebuild tolerates an unresolved forward reference.

    A subclass of a canonical model re-inherits the removed legacy identity, so
    ``__pydantic_init_subclass__`` removes it again and used to force a rebuild
    there -- which raised when a field named a model defined further down the
    module, an ordinary pydantic pattern that plain ``model_rebuild()`` defers
    (#1690). Deferring the rebuild leaves the class incomplete until the
    caller's own ``model_rebuild()``, with the removal intact.
    """
    from adcp.types import PackageRequest
    from adcp.types.canonical_creative import is_legacy_creative_identity_key

    class WithForwardRef(PackageRequest):
        later: list[Later] | None = None

    class Later(BaseModel):
        name: str

    assert not WithForwardRef.__pydantic_complete__
    WithForwardRef.model_rebuild()

    assert WithForwardRef.__pydantic_complete__
    assert "later" in WithForwardRef.model_fields
    assert not any(is_legacy_creative_identity_key(name) for name in WithForwardRef.model_fields)
    payload = {
        "buyer_ref": "p",
        "product_id": "prod",
        "pricing_option_id": "po",
        "later": [{"name": "n"}],
    }
    assert WithForwardRef.model_validate(payload).later


_DUMP_PROBE = """
from typing import Any
from pydantic import BaseModel
from adcp.types import CreateMediaBuyRequest, Placement
from adcp.types.domains.adagents import AdcpAgentsAuthorization
from adcp.types.domains.core.targeting import TargetingOverlay


class Parent(BaseModel):
    item: Any


# A canonical model, unvalidated: the #1694 CI failure.
assert Parent(item=Placement.model_construct(name="n")).model_dump(mode="json") == {
    "item": {"name": "n"}
}
# A config-deferred generated model, unvalidated: a TypeError on 9.3.0.
assert Parent(item=TargetingOverlay.model_construct()).model_dump(mode="json") == {"item": {}}
# A nested instance built through its parent's schema; its own class never built.
request = CreateMediaBuyRequest.model_validate(
    {
        "buyer_ref": "b",
        "brand": {"domain": "brand.example"},
        "packages": [{"buyer_ref": "p", "product_id": "prod", "pricing_option_id": "po"}],
        "start_time": "asap",
        "end_time": "2030-01-01T00:00:00Z",
        "idempotency_key": "k" * 20,
        "account": {"account_id": "a"},
    }
)
package = request.packages[0]
assert not type(package).__pydantic_complete__
assert Parent(item=package).model_dump(mode="json")["item"]["buyer_ref"] == "p"
assert type(package).__pydantic_complete__
# A deferred generated root, unvalidated.
root = AdcpAgentsAuthorization.model_construct(root=None)
assert Parent(item=root).model_dump(mode="json") == {"item": None}
print("ok")
"""


def test_a_deferred_instance_dumps_through_a_plain_pydantic_parent() -> None:
    """pydantic-core reads ``instance.__pydantic_serializer__`` when it serializes by
    inference (an ``Any`` field, ``serialize_as_any``), and a placeholder there fails
    with ``TypeError: 'MockValSer' object is not an instance of 'SchemaSerializer'``.
    The SDK's placeholders build the model on that read instead. In a subprocess so
    nothing in this process has built the models first."""
    result = subprocess.run(
        [sys.executable, "-I", "-c", _DUMP_PROBE],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "ok"
