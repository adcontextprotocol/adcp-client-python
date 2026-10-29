"""A seller on SDK defaults must be callable by a buyer on SDK defaults.

The SDK buyer applies two bounds to every response, ``tools/list`` included:
the 4 MiB byte budget and :data:`adcp._json_bounds.MAX_JSON_MEMBERS`
(100,000 object members plus array elements). The MCP client lists tools
before ``call_tool``, so a seller whose listing crosses either bound fails
*every* call from an SDK buyer, not just discovery.

Issue #1692: a version-pinned seller (every decisioning ``serve()`` seller is
one) advertised its release's bundled response schemas unpruned. A 12-tool
3.2 sales seller listed ~106,000 members in 2.5 MB — under the byte budget,
over the member bound — and every ``call_tool`` raised
``ADCPResponseTooComplexError``.

These tests pin the invariant with headroom (half the bound, the same
threshold at which the server warns at startup), check the compaction that
restored it stays a relaxation of the bundled schema, and drive a real SDK
client against a real decisioning seller.
"""

from __future__ import annotations

import json
import logging
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

from adcp._json_bounds import MAX_JSON_MEMBERS
from adcp._version import resolve_adcp_version
from adcp.decisioning import DecisioningCapabilities
from adcp.decisioning.capabilities import Account, Features, MediaBuy, SupportedProtocol
from adcp.server import ADCPHandler, create_mcp_server
from adcp.server.mcp_tools import (
    _MISSING,
    ADCP_TOOL_DEFINITIONS,
    _compact_wire_schema,
    _local_json_refs,
    _resolve_json_pointer,
    get_tools_for_handler,
)
from adcp.server.serve import (
    BUYER_RESPONSE_MEMBER_BOUND,
    _discovery_payload_members,
    warn_if_discovery_payload_is_oversized,
)
from adcp.validation.schema_loader import get_mcp_schema

_EXAMPLES = str(Path(__file__).parent.parent / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

from hello_seller import HelloSeller  # noqa: E402

#: Headroom: a default sales seller stays under half the buyer's bound, which
#: is also where the startup warning starts. Room for the next few tools.
_SALES_SELLER_CEILING = MAX_JSON_MEMBERS // 2

#: Every tool a sales-* specialism advertises (sales-catalog-driven is the
#: widest: the shared sales surface plus ``sync_catalogs``).
_SALES_TOOLS = frozenset(
    {
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "get_media_buys",
        "get_media_buy_delivery",
        "sync_creatives",
        "list_creatives",
        "provide_performance_feedback",
        "list_creative_formats",
        "list_accounts",
        "sync_accounts",
        "sync_catalogs",
    }
)

#: The issue's 3.1/3.2 pair, the SDK's packaged release (what a decisioning
#: seller is pinned to when it names none), and no pin at all (the
#: Pydantic-derived schemas a plain ``ADCPHandler`` advertises).
_PINS = ("3.1", "3.2", resolve_adcp_version(None), None)


def _members(value: Any) -> int:
    """Independent count, the one the issue used: members plus elements."""
    if isinstance(value, dict):
        return len(value) + sum(_members(item) for item in value.values())
    if isinstance(value, list):
        return len(value) + sum(_members(item) for item in value)
    return 0


async def _noop(self: Any, params: Any, context: Any = None) -> None:
    return None


def _sales_handler(pin: str | None) -> ADCPHandler[Any]:
    body: dict[str, Any] = {name: _noop for name in _SALES_TOOLS}
    body["advertised_tools"] = set(_SALES_TOOLS)
    if pin is not None:
        body["_adcp_version"] = pin
    return type("_SalesSeller", (ADCPHandler,), body)()


def _quiet_tools(handler: Any, **kwargs: Any) -> list[dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return get_tools_for_handler(handler, **kwargs)


class _FullSalesSeller(HelloSeller):
    """The issue's seller shape on the decisioning path, plus catalogs."""

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed", "sales-catalog-driven"],
        supported_protocols=[SupportedProtocol.media_buy],
        account=Account(supported_billing=["operator"]),
        media_buy=MediaBuy(
            supported_pricing_models=["cpm"],
            # 3.1 buyers need a declared creative dialect.
            features=Features(canonical_creatives=True),
        ),
    )

    def sync_catalogs(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"catalogs": []}


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pin", _PINS)
def test_a_full_sales_seller_lists_well_inside_the_buyer_member_bound(pin: str | None) -> None:
    tools = _quiet_tools(_sales_handler(pin))
    assert {tool["name"] for tool in tools} >= _SALES_TOOLS - {"list_creative_formats"}

    members = _discovery_payload_members(tools)
    assert members < _SALES_SELLER_CEILING, (
        f"a sales seller pinned to {pin or 'nothing'} lists {members} "
        f"JSON members; an SDK buyer refuses more than {MAX_JSON_MEMBERS}, and this "
        f"test keeps a default seller under {_SALES_SELLER_CEILING} so it can grow."
    )


def test_the_issue_repro_fits() -> None:
    """Issue #1692's script, verbatim in shape: a 3.2 pin plus 3.1/3.2."""

    # Tool discovery is keyed by class name; keep this one unique.
    class _Issue1692Seller(ADCPHandler[Any]):
        _adcp_version = "3.2"

    for name in _SALES_TOOLS - {"sync_catalogs"}:
        setattr(_Issue1692Seller, name, _noop)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mcp = create_mcp_server(_Issue1692Seller(), supported_versions=["3.1", "3.2"])

    import asyncio

    tools = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in asyncio.run(mcp.list_tools())
    ]
    listing = {"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}}
    assert len(tools) == 11
    # Was 106,250 on 10.0.0b1.
    assert _members(listing) < _SALES_SELLER_CEILING


@pytest.mark.parametrize("supported_versions", [["3.1", "3.2"], None])
def test_the_decisioning_serve_path_fits(
    monkeypatch: pytest.MonkeyPatch, supported_versions: list[str] | None
) -> None:
    """``adcp.decisioning.serve()`` — what the issue's real seller runs."""
    import asyncio

    from adcp.decisioning import serve

    # ``adcp.server.serve`` the attribute is the function; patch the module.
    server_serve = sys.modules["adcp.server.serve"]

    captured: dict[str, Any] = {}

    def fake_serve(handler: Any, **kwargs: Any) -> None:
        captured["handler"] = handler
        captured["kwargs"] = kwargs

    monkeypatch.setattr(server_serve, "serve", fake_serve)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        serve(
            _FullSalesSeller(),
            supported_versions=supported_versions,
            auto_emit_completion_webhooks=False,
        )
        mcp = create_mcp_server(
            captured["handler"], advertise_all=captured["kwargs"]["advertise_all"]
        )
    tools = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in asyncio.run(mcp.list_tools())
    ]
    listing = {"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}}

    assert {tool["name"] for tool in tools} >= {"get_products", "sync_catalogs"}
    assert _members(listing) < _SALES_SELLER_CEILING


async def test_an_sdk_buyer_calls_an_sdk_seller_on_defaults() -> None:
    """End to end, nothing tuned: the SDK client's own bounds, a real
    ``tools/list`` before ``call_tool``, and the MCP client's validation of
    ``structuredContent`` against the compacted ``outputSchema``."""
    httpx2 = pytest.importorskip("httpx2")
    from asgi_lifespan import LifespanManager

    from adcp import ADCPClient, AgentConfig
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.types import GetProductsRequest

    handler, executor, _ = create_adcp_server_from_platform(
        _FullSalesSeller(),
        supported_versions=["3.1", "3.2"],
        auto_emit_completion_webhooks=False,
        validate_at_init=False,
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mcp = create_mcp_server(handler, stateless_http=True, allowed_hosts=["localhost"])
        # ASGITransport buffers; JSON responses keep the exchange in one body.
        mcp.settings.json_response = True
        app = mcp.streamable_http_app(streamable_http_path="/mcp")

        def factory(**kwargs: Any) -> Any:
            return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), **kwargs)

        async with LifespanManager(app):
            async with ADCPClient(
                AgentConfig(id="seller", agent_uri="http://localhost/mcp", protocol="mcp"),
                httpx_client_factory=factory,
            ) as client:
                result = await client.get_products(
                    GetProductsRequest(buying_mode="brief", brief="display")
                )
    finally:
        executor.shutdown(wait=True)

    assert result.success, result.error
    assert result.data is not None
    assert [product.product_id for product in result.data.products] == ["display-rotation"]


# ---------------------------------------------------------------------------
# The compaction behind it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["3.1", "3.2", "3.3.0-beta.1"])
def test_every_compacted_bundled_response_schema_is_sound(version: str) -> None:
    """Valid JSON Schema, no dangling references, and smaller.

    Bundled schemas reference arbitrary pointers (into ``oneOf`` arms,
    nested ``definitions``), so this sweeps every tool of every pinnable
    release rather than trusting a sample.
    """
    jsonschema = pytest.importorskip("jsonschema")

    shrunk = 0
    for tool in ADCP_TOOL_DEFINITIONS:
        schema = get_mcp_schema(tool["name"], "sync", version=version)
        if schema is None:
            continue
        compact = _compact_wire_schema(schema)
        jsonschema.validators.validator_for(compact).check_schema(compact)
        for ref in _local_json_refs(compact, []):
            assert _resolve_json_pointer(compact, ref) is not _MISSING, (tool["name"], ref)
        assert _members(compact) <= _members(schema)
        shrunk += _members(compact) < _members(schema)
    assert shrunk


def test_pinned_output_schemas_are_compacted_but_inputs_are_exact() -> None:
    tools = {tool["name"]: tool for tool in _quiet_tools(_sales_handler("3.2"))}

    advertised = tools["get_products"]
    assert advertised["inputSchema"] == get_mcp_schema("get_products", "request", version="3.2")
    bundled = get_mcp_schema("get_products", "sync", version="3.2")
    assert advertised["outputSchema"] != bundled
    assert _members(advertised["outputSchema"]) < _members(bundled) // 4
    # Root response fields survive by name.
    assert set(advertised["outputSchema"]["properties"]) == set(bundled["properties"])


@pytest.mark.parametrize("mode", ["defs", "inline"])
def test_non_compact_modes_keep_the_exact_bundled_response_schema(mode: str) -> None:
    tools = {tool["name"]: tool for tool in _quiet_tools(_sales_handler("3.2"), schema_mode=mode)}
    assert tools["get_products"]["outputSchema"] == get_mcp_schema(
        "get_products", "sync", version="3.2"
    )


def test_compaction_drops_optional_nested_detail_but_keeps_the_name() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = {
        "type": "object",
        "properties": {
            "item": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "detail": {"type": "object", "properties": {"x": {"type": "integer"}}},
                },
                "required": ["id"],
                "additionalProperties": False,
            }
        },
    }
    compact = _compact_wire_schema(schema)
    item = compact["properties"]["item"]
    assert item["properties"] == {"id": {"type": "string"}, "detail": True}
    assert item["additionalProperties"] is False

    jsonschema.validate({"item": {"id": "a", "detail": {"x": "loosened"}}}, compact)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"item": {"id": "a", "smuggled": 1}}, compact)


def test_compaction_never_loosens_a_negation() -> None:
    """``not`` of a looser schema is a stricter schema. A response the
    bundle accepts must not start failing the advertised one — the MCP
    client would reject the tool result."""
    jsonschema = pytest.importorskip("jsonschema")
    nested = {"type": "object", "properties": {"deep": {"type": "object"}}}
    schema = {
        "type": "object",
        "properties": {
            "item": {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "not": {"$ref": "#/$defs/Forbidden"},
                "if": {"properties": {"mode": {"$ref": "#/$defs/Mode"}}},
                "then": {"required": ["a"]},
            }
        },
        "$defs": {
            "Forbidden": {
                "type": "object",
                "properties": {"opt": {"type": "object", "properties": {"n": nested}}},
                "required": ["opt"],
            },
            "Mode": {"type": "object", "properties": {"n": nested}},
        },
    }
    compact = _compact_wire_schema(schema)
    assert compact["$defs"] == schema["$defs"]
    assert compact["properties"]["item"]["not"] == schema["properties"]["item"]["not"]

    accepted = {"item": {"a": "x", "opt": {"n": {"deep": 1}}}}
    jsonschema.validate(accepted, schema)
    jsonschema.validate(accepted, compact)


def test_a_loosened_one_of_becomes_any_of() -> None:
    """Two loosened arms can both match; ``oneOf`` would then fail."""
    jsonschema = pytest.importorskip("jsonschema")
    arm = {
        "type": "object",
        "properties": {"payload": {"type": "object", "properties": {"kind": {"const": "x"}}}},
    }
    other = {
        "type": "object",
        "properties": {"payload": {"type": "object", "properties": {"kind": {"const": "y"}}}},
    }
    schema = {
        "type": "object",
        "properties": {
            "wrapper": {
                "type": "object",
                "properties": {"choice": {"oneOf": [arm, other]}},
                "required": ["choice"],
            }
        },
    }
    instance = {"wrapper": {"choice": {"payload": {"kind": "x"}}}}
    jsonschema.validate(instance, schema)

    compact = _compact_wire_schema(schema)
    choice = compact["properties"]["wrapper"]["properties"]["choice"]
    assert "oneOf" not in choice and len(choice["anyOf"]) == 2
    jsonschema.validate(instance, compact)


def test_compaction_keeps_every_reference_target() -> None:
    """A reference into an optional subtree pins that subtree."""
    role = "#/properties/item/properties/fonts/definitions/role"
    schema = {
        "type": "object",
        "properties": {
            "item": {
                "type": "object",
                "properties": {
                    "fonts": {
                        "type": "object",
                        "definitions": {"role": {"type": "string"}},
                        "properties": {"r": {"$ref": role}},
                    },
                    "uses": {"$ref": role},
                },
            }
        },
    }
    compact = _compact_wire_schema(schema)
    for ref in _local_json_refs(compact, []):
        assert _resolve_json_pointer(compact, ref) == {"type": "string"}


# ---------------------------------------------------------------------------
# The startup guard
# ---------------------------------------------------------------------------


def _wide_tool(members: int) -> dict[str, Any]:
    return {
        "name": "wide",
        "description": "x",
        "inputSchema": {"type": "object", "enum": [None] * members},
    }


def test_member_count_matches_the_buyer_scanner() -> None:
    """The guard counts what the client's ``JsonBoundsScanner`` counts."""
    from adcp._json_bounds import JsonBoundsScanner

    tools = _quiet_tools(_sales_handler("3.2"))
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}}).encode()
    scanner = JsonBoundsScanner(max_members=10_000_000)
    scanner.feed(body)
    # ``_discovery_entry`` keeps ``annotations`` (which FastMCP does not
    # send), so the guard may read high, never low.
    assert _discovery_payload_members(tools) >= scanner.members
    assert _discovery_payload_members(tools) - scanner.members < 1_000


def test_a_listing_over_the_member_bound_warns_and_logs_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="adcp.server"):
        with pytest.warns(UserWarning, match="100000-member JSON bound.*will REJECT"):
            warn_if_discovery_payload_is_oversized(
                tool_defs=[_wide_tool(BUYER_RESPONSE_MEMBER_BOUND + 1)],
                advertise_all=False,
            )
    assert any("JSON members" in record.getMessage() for record in caplog.records)


def test_a_listing_past_half_the_member_bound_warns() -> None:
    with pytest.warns(UserWarning, match="over half an AdCP default buyer response bound"):
        warn_if_discovery_payload_is_oversized(
            tool_defs=[_wide_tool(BUYER_RESPONSE_MEMBER_BOUND // 2 + 1)],
            advertise_all=False,
        )


def test_a_default_sales_seller_boots_without_a_size_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        create_mcp_server(_sales_handler("3.2"), name="sales")
    oversized = [w for w in caught if "tools/list is" in str(w.message)]
    assert not oversized, [str(w.message) for w in oversized]
