"""``tools/list`` stays under AdCP's 128 KiB discovery target by default (#1909).

Two defaults changed together:

* ``outputSchema`` is no longer listed (``advertise_output_schemas=False``);
  results still travel as ``structuredContent``.
* ``schema_mode="lean"`` bounds the input schemas: every root field keeps its
  name, fields the root rules require keep their whole schema, scalars and
  scalar lists keep theirs, and every other optional field is reduced to its
  type and first-sentence description.

``schema_mode="compact"`` plus ``advertise_output_schemas=True`` restores the
previous listing.
"""

from __future__ import annotations

import importlib
import json
import warnings
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from asgi_lifespan import LifespanManager
from jsonschema.validators import validator_for

from adcp.server import ADCPHandler, ServeConfig, create_mcp_server
from adcp.server.base import ToolContext
from adcp.server.mcp_tools import (
    ADCP_TOOL_DEFINITIONS,
    MCPToolSet,
    _first_sentence,
    _lean_annotations,
    _lean_input_schema,
    get_tools_for_handler,
)
from adcp.server.test_controller import TestControllerStore, register_test_controller
from adcp.validation.schema_loader import get_schema_url

_serve_mod = importlib.import_module("adcp.server.serve")

#: AdCP's per-page ``tools/list`` target.
PAGE_BUDGET = 128 * 1024

#: The 14-tool sales page the budget is measured on.
SALES_PAGE: tuple[str, ...] = (
    "get_adcp_capabilities",
    "get_products",
    "create_media_buy",
    "update_media_buy",
    "get_media_buys",
    "get_media_buy_delivery",
    "sync_creatives",
    "list_creatives",
    "list_creative_formats",
    "list_accounts",
    "sync_accounts",
    "get_signals",
    "activate_signal",
    "provide_performance_feedback",
)

PINNED = "3.3-rc.3"
VERSIONS: tuple[str | None, ...] = ("3.2", PINNED, None)
IDEMPOTENCY_KEY = "idem-key-0123456789"


def _page(
    version: str | None, *, mode: str = "lean", outputs: bool = False
) -> dict[str, dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        definitions = get_tools_for_handler(
            ADCPHandler,
            advertise_all=True,
            adcp_version=version,
            schema_mode=mode,  # type: ignore[arg-type]
            advertise_output_schemas=outputs,
        )
    return {d["name"]: d for d in definitions if d["name"] in SALES_PAGE}


def _bytes(definitions: dict[str, dict[str, Any]]) -> int:
    return len(json.dumps({"tools": list(definitions.values())}, separators=(",", ":")))


def _walk_descriptions(node: Any, *, names: bool = False) -> Iterator[str]:
    """Every ``description`` keyword (not property name) in a schema."""
    if isinstance(node, dict):
        for key, value in node.items():
            if names:
                yield from _walk_descriptions(value)
            elif key == "description" and isinstance(value, str):
                yield value
            elif key in ("enum", "const", "default", "examples"):
                continue
            else:
                yield from _walk_descriptions(
                    value, names=key in ("properties", "$defs", "patternProperties")
                )
    elif isinstance(node, list):
        for item in node:
            yield from _walk_descriptions(item)


# ----------------------------------------------------------------------
# (a) Budget
# ----------------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_sales_page_fits_the_discovery_budget_without_output_schemas(
    version: str | None,
) -> None:
    page = _page(version)

    assert set(page) == set(SALES_PAGE)
    size = _bytes(page)
    assert size < PAGE_BUDGET, f"{version}: lean sales page is {size} B, over {PAGE_BUDGET}"
    for name, tool in page.items():
        assert "outputSchema" not in tool, name


@pytest.mark.parametrize("version", VERSIONS)
def test_lean_is_far_smaller_than_the_previous_default(version: str | None) -> None:
    lean = _bytes(_page(version))
    previous = _bytes(_page(version, mode="compact", outputs=True))

    assert previous > PAGE_BUDGET
    assert lean * 3 < previous


@pytest.mark.parametrize("version", VERSIONS)
def test_every_lean_root_property_is_an_object_schema(version: str | None) -> None:
    """MCP clients such as the TypeScript SDK refuse a boolean root property.

    The storyboard runner's ``@adcp/sdk`` client rejected ``tools/list`` as
    invalid when a reduced root field (``bidding``) was the schema ``true``.
    """
    tools = get_tools_for_handler(ADCPHandler, advertise_all=True, adcp_version=version)
    for tool in tools:
        for name, schema in (tool["inputSchema"].get("properties") or {}).items():
            assert isinstance(schema, dict), (tool["name"], name, schema)


# ----------------------------------------------------------------------
# (b) Keep rules
# ----------------------------------------------------------------------


@pytest.mark.parametrize("version", [PINNED, None])
def test_create_media_buy_keeps_packages_whole_because_an_arm_requires_it(
    version: str | None,
) -> None:
    lean = _page(version)["create_media_buy"]["inputSchema"]
    compact = _page(version, mode="compact")["create_media_buy"]["inputSchema"]

    # ``packages`` is optional at the root; one anyOf arm requires it.
    assert "packages" not in compact.get("required", [])
    if version is not None:
        # The Pydantic path has no root rules; the packaged wire schema tells
        # the lean reducer to keep the field there instead.
        assert "anyOf" in compact
    packages = lean["properties"]["packages"]
    assert packages["type"] == "array"
    assert "items" in packages
    assert packages == _lean_annotations(compact["properties"]["packages"])
    # What it references is still defined.
    for ref in _refs(packages):
        assert ref in lean.get("$defs", {}), ref


def _refs(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref[len("#/$defs/") :]
            found.append(name.replace("~1", "/").replace("~0", "~"))
        for value in node.values():
            found.extend(_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_refs(item))
    return found


@pytest.mark.parametrize("version", [PINNED, None])
def test_get_products_reduces_an_optional_object_to_type_and_description(
    version: str | None,
) -> None:
    lean = _page(version)["get_products"]["inputSchema"]
    compact = _page(version, mode="compact")["get_products"]["inputSchema"]

    filters = lean["properties"]["filters"]
    assert set(filters) <= {"type", "description"}, filters
    assert "type" in filters and "description" in filters
    # The compact shape carried more than that, so something was dropped.
    assert not set(compact["properties"]["filters"]) <= {"type", "description"} or (
        len(json.dumps(compact["properties"]["filters"])) > len(json.dumps(filters))
    )
    # Every root field keeps its name.
    assert set(lean["properties"]) == set(compact["properties"])
    assert lean["required"] == compact["required"]


@pytest.mark.parametrize("version", [PINNED, None])
def test_scalar_and_scalar_list_fields_keep_their_schemas(version: str | None) -> None:
    lean = _page(version)["get_products"]["inputSchema"]["properties"]
    compact = _page(version, mode="compact")["get_products"]["inputSchema"]["properties"]

    # A scalar...
    assert lean["brief"]["type"] == "string"
    assert lean["brief"] == _lean_annotations(compact["brief"])
    # ...and a list of scalars (an array of strings).
    policies = lean["required_policies"]
    assert policies["type"] == "array"
    assert policies["items"] == {"type": "string"}
    assert policies == _lean_annotations(compact["required_policies"])


def test_lean_drops_definitions_only_reduced_fields_reached() -> None:
    lean = _page(PINNED)["get_products"]["inputSchema"]
    compact = _page(PINNED, mode="compact")["get_products"]["inputSchema"]

    assert len(lean.get("$defs", {})) < len(compact.get("$defs", {}))
    for ref in _refs(lean):
        assert ref in lean.get("$defs", {}), ref


# ----------------------------------------------------------------------
# (c) Descriptions
# ----------------------------------------------------------------------


@pytest.mark.parametrize("version", [PINNED, None])
def test_descriptions_keep_only_their_first_sentence(version: str | None) -> None:
    lean = _page(version)["get_products"]["inputSchema"]["properties"]["brief"]
    compact = _page(version, mode="compact")["get_products"]["inputSchema"]["properties"]["brief"]

    # Known long description: the compact one has a second sentence.
    assert compact["description"].startswith("Natural language description of campaign")
    assert len(compact["description"]) > len(lean["description"])
    assert lean["description"] == "Natural language description of campaign requirements."
    assert compact["description"].startswith(lean["description"])


@pytest.mark.parametrize("version", VERSIONS)
def test_no_lean_description_has_a_second_sentence(version: str | None) -> None:
    for name, tool in _page(version).items():
        for text in _walk_descriptions(tool["inputSchema"]):
            assert _first_sentence(text) == text, (name, text)


def test_lean_annotations_cut_descriptions_but_keep_names_and_values() -> None:
    schema = {
        "type": "object",
        "title": "Dropped",
        "description": "Root. Second sentence.",
        "examples": [{"x": 1}],
        "$comment": "dropped",
        "properties": {
            # Property *names* that collide with annotation keywords.
            "description": {
                "type": "string",
                "title": "T",
                "description": "First. Second.",
                "default": "Literal. Value.",
            },
            "title": {"type": "string", "enum": ["Keep. This.", "And. That."]},
            "kind": {"const": "Exact. Literal."},
        },
    }

    lean = _lean_annotations(schema)

    assert "title" not in lean and "examples" not in lean and "$comment" not in lean
    assert lean["description"] == "Root."
    assert set(lean["properties"]) == {"description", "title", "kind"}
    assert lean["properties"]["description"]["description"] == "First."
    assert "title" not in lean["properties"]["description"]
    assert lean["properties"]["description"]["default"] == "Literal. Value."
    assert lean["properties"]["title"]["enum"] == ["Keep. This.", "And. That."]
    assert lean["properties"]["kind"]["const"] == "Exact. Literal."


def test_first_sentence_boundaries() -> None:
    assert _first_sentence("One. Two.") == "One."
    assert _first_sentence("No terminator") == "No terminator"
    assert _first_sentence("Ends here.") == "Ends here."
    assert _first_sentence("Version 3.2 is fine. Next.") == "Version 3.2 is fine."


def _hand_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "must": {"type": "string"},
            "via_arm": {"type": "object", "properties": {"a": {"type": "string"}}},
            "via_dep": {"type": "object", "properties": {"b": {"type": "string"}}},
            "via_ref_arm": {"type": "object", "properties": {"c": {"type": "string"}}},
            "optional_obj": {
                "type": "object",
                "title": "Optional",
                "description": "Optional object. More detail.",
                "properties": {"deep": {"$ref": "#/$defs/Deep"}},
            },
            "optional_ref": {"$ref": "#/$defs/Deep", "description": "By ref. More."},
            "scalar": {"type": "integer", "description": "Scalar. More."},
            "scalars": {"type": "array", "items": {"type": "string"}},
            "objects": {"type": "array", "items": {"$ref": "#/$defs/Deep"}},
            "anything": {"description": "Untyped."},
            "bare": {},
        },
        "required": ["must"],
        "dependentRequired": {"scalar": ["via_dep"]},
        "anyOf": [
            {"required": ["via_arm"]},
            {"$ref": "#/$defs/Arm"},
        ],
        "$defs": {
            "Deep": {"type": "object", "properties": {"x": {"type": "string"}}},
            "Arm": {"required": ["via_ref_arm"]},
            "Unused": {"type": "object"},
        },
    }


def test_lean_input_schema_keep_rules_on_a_hand_written_schema() -> None:
    schema = _hand_schema()
    original = json.loads(json.dumps(schema))

    lean = _lean_input_schema(schema, "https://example.test/schema.json")

    assert schema == original, "input must not be mutated"
    props = lean["properties"]
    assert set(props) == set(schema["properties"])
    # Required somewhere: root required, anyOf arm, dependentRequired, $ref arm.
    for kept in ("must", "via_arm", "via_dep", "via_ref_arm"):
        assert props[kept] == schema["properties"][kept], kept
    # Scalars and scalar lists keep their schema (annotations trimmed).
    assert props["scalar"] == {"type": "integer", "description": "Scalar."}
    assert props["scalars"] == schema["properties"]["scalars"]
    # Other optional fields reduce to type/description.
    assert props["optional_obj"] == {"type": "object", "description": "Optional object."}
    assert props["optional_ref"] == {"type": "object", "description": "By ref."}
    assert props["objects"] == {"type": "array"}
    assert props["anything"] == {"description": "Untyped."}
    # Definitions only reduced fields reached are dropped.
    assert "Deep" not in lean.get("$defs", {})
    assert "Unused" not in lean.get("$defs", {})
    assert "Arm" in lean["$defs"]
    assert lean["required"] == ["must"]
    assert lean["x-adcp-schema"] == "https://example.test/schema.json"


def test_lean_input_schema_also_keep_and_missing_url() -> None:
    lean = _lean_input_schema(_hand_schema(), None, also_keep=frozenset({"optional_obj"}))

    assert "x-adcp-schema" not in lean
    assert "properties" in lean["properties"]["optional_obj"]
    assert "Deep" in lean["$defs"]


def test_lean_input_schema_without_properties_is_left_alone() -> None:
    lean = _lean_input_schema({"type": "object"}, None)

    assert lean == {"type": "object"}


# ----------------------------------------------------------------------
# (d) x-adcp-schema
# ----------------------------------------------------------------------


def test_x_adcp_schema_names_the_published_schema_at_each_pin() -> None:
    rc = _page(PINNED)["create_media_buy"]["inputSchema"]
    old = _page("3.2")["create_media_buy"]["inputSchema"]

    assert rc["x-adcp-schema"] == (
        "https://adcontextprotocol.org/schemas/3.3.0-rc.3/media-buy/create-media-buy-request.json"
    )
    assert old["x-adcp-schema"] == (
        "https://adcontextprotocol.org/schemas/3.2.7/media-buy/create-media-buy-request.json"
    )


def test_x_adcp_schema_is_lean_only() -> None:
    compact = _page(PINNED, mode="compact")["create_media_buy"]["inputSchema"]

    assert "x-adcp-schema" not in compact


def test_get_schema_url_for_an_unknown_tool_is_none() -> None:
    assert get_schema_url("no_such_tool", "request", version=PINNED) is None
    assert get_schema_url("no_such_tool", "request") is None
    assert get_schema_url("create_media_buy", "request", version=PINNED) is not None


# ----------------------------------------------------------------------
# (e) Looseness
# ----------------------------------------------------------------------

_ACCOUNT = {"account_id": "acct-1"}
_REQUESTS: dict[str, dict[str, Any]] = {
    "get_products": {
        "buying_mode": "brief",
        "brief": "video ads for runners",
        "filters": {"delivery_type": "guaranteed"},
        "pagination": {"max_results": 5},
        "required_policies": ["policy-1"],
    },
    "create_media_buy": {
        "idempotency_key": IDEMPOTENCY_KEY,
        "account": _ACCOUNT,
        "brand": {"domain": "acme.example"},
        "start_time": "asap",
        "end_time": "2026-12-31T00:00:00Z",
        "packages": [{"product_id": "p1", "budget": 1000, "pricing_option_id": "po1"}],
    },
    "update_media_buy": {
        "idempotency_key": IDEMPOTENCY_KEY,
        "account": _ACCOUNT,
        "media_buy_id": "mb-1",
        "paused": True,
    },
    "sync_creatives": {
        "idempotency_key": IDEMPOTENCY_KEY,
        "account": _ACCOUNT,
        "creatives": [
            {
                "creative_id": "c1",
                "name": "Creative",
                "format_id": {"agent_url": "https://formats.example", "id": "display"},
                "assets": {},
            }
        ],
    },
}


def _errors(schema: dict[str, Any], request: dict[str, Any]) -> list[str]:
    # The draft comes from each schema's own ``$schema`` (draft-07 or 2020-12).
    return [e.message for e in validator_for(schema)(schema).iter_errors(request)]


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("tool_name", sorted(_REQUESTS))
def test_lean_accepts_every_request_the_compact_schema_accepts(
    version: str | None, tool_name: str
) -> None:
    compact = _page(version, mode="compact")[tool_name]["inputSchema"]
    lean = _page(version)[tool_name]["inputSchema"]
    request = _REQUESTS[tool_name]

    if _errors(compact, request):
        # Only the unpinned (Pydantic) sync_creatives needs more than the
        # request builds; the pinned pages must all be non-vacuous.
        assert version is None, (version, tool_name, _errors(compact, request))
        pytest.skip("request not valid for the unpinned compact schema")
    assert _errors(lean, request) == []


@pytest.mark.parametrize("version", VERSIONS)
def test_lean_still_rejects_a_request_missing_a_required_field(version: str | None) -> None:
    lean = _page(version)["get_products"]["inputSchema"]
    without = {k: v for k, v in _REQUESTS["get_products"].items() if k != "buying_mode"}

    assert _errors(lean, without)

    create = _page(version)["create_media_buy"]["inputSchema"]
    no_idempotency = {
        k: v for k, v in _REQUESTS["create_media_buy"].items() if k != "idempotency_key"
    }
    assert _errors(create, no_idempotency)


# ----------------------------------------------------------------------
# (f) End to end over MCP
# ----------------------------------------------------------------------


class _LeanToolsListSeller(ADCPHandler):
    advertised_tools = {"get_products"}

    async def get_products(self, params: dict[str, Any], context: ToolContext | None = None):
        return {"products": []}


async def _post(client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    resp = await client.post("/mcp/", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    # Stateful streamable-http binds later requests to the initialize session.
    session_id = resp.headers.get("mcp-session-id")
    if session_id is not None:
        client.headers["mcp-session-id"] = session_id
    for line in resp.text.splitlines():
        if line.strip().startswith("data: "):
            return json.loads(line.strip().removeprefix("data: "))
    return json.loads(resp.text)


async def _session(client: httpx.AsyncClient) -> None:
    await _post(
        client,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        },
    )


async def _list_and_call(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    mcp = create_mcp_server(_LeanToolsListSeller(), name="lean-e2e", **kwargs)
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.transport_security.allowed_hosts = ["localhost", "127.0.0.1"]
    app = mcp.streamable_http_app()
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            follow_redirects=True,
        ) as client:
            await _session(client)
            listed = await _post(
                client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
            called = await _post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "get_products",
                        "arguments": {"buying_mode": "brief", "brief": "video"},
                    },
                },
            )
    return listed, called


@pytest.mark.asyncio
async def test_default_list_omits_output_schema_but_calls_return_structured_content() -> None:
    listed, called = await _list_and_call()

    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert "get_products" in tools
    assert all("outputSchema" not in t for t in tools.values())
    assert tools["get_products"]["inputSchema"]["x-adcp-schema"].endswith(
        "/media-buy/get-products-request.json"
    )
    result = called["result"]
    assert not result.get("isError"), result
    assert result["structuredContent"]["products"] == []


@pytest.mark.asyncio
async def test_opting_in_lists_output_schema_and_leaves_structured_content_unchanged() -> None:
    _, default_call = await _list_and_call()
    listed, opted_call = await _list_and_call(advertise_output_schemas=True)

    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert tools["get_products"]["outputSchema"]
    assert "products" in tools["get_products"]["outputSchema"]["properties"]
    assert opted_call["result"]["structuredContent"] == default_call["result"]["structuredContent"]
    assert opted_call["result"]["structuredContent"]["products"] == []


def test_mcp_tool_set_threads_both_options() -> None:
    default = MCPToolSet(_LeanToolsListSeller())
    opted = MCPToolSet(_LeanToolsListSeller(), schema_mode="compact", advertise_output_schemas=True)

    assert all("outputSchema" not in d for d in default.tool_definitions)
    assert any("outputSchema" in d for d in opted.tool_definitions)
    lean = next(d for d in default.tool_definitions if d["name"] == "get_products")
    compact = next(d for d in opted.tool_definitions if d["name"] == "get_products")
    assert "x-adcp-schema" in lean["inputSchema"]
    assert "x-adcp-schema" not in compact["inputSchema"]


# ----------------------------------------------------------------------
# (g) Threading through serve()
# ----------------------------------------------------------------------


class _CapturedError(Exception):
    """Raised by the stub so nothing binds a socket."""


def _capture_create_mcp_server(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def stub(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        raise _CapturedError

    monkeypatch.setattr(_serve_mod, "create_mcp_server", stub)
    return calls


@pytest.mark.parametrize("transport", ["streamable-http", "both"])
def test_serve_defaults_are_lean_and_output_free(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    calls = _capture_create_mcp_server(monkeypatch)

    with pytest.raises(_CapturedError):
        _serve_mod.serve(_LeanToolsListSeller(), transport=transport)  # type: ignore[arg-type]

    assert calls[0]["schema_mode"] == "lean"
    assert calls[0]["advertise_output_schemas"] is False


@pytest.mark.parametrize("transport", ["streamable-http", "both"])
def test_serve_kwargs_reach_create_mcp_server(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    calls = _capture_create_mcp_server(monkeypatch)

    with pytest.raises(_CapturedError):
        _serve_mod.serve(
            _LeanToolsListSeller(),
            transport=transport,  # type: ignore[arg-type]
            schema_mode="compact",
            advertise_output_schemas=True,
        )

    assert calls[0]["schema_mode"] == "compact"
    assert calls[0]["advertise_output_schemas"] is True


@pytest.mark.parametrize("transport", ["streamable-http", "both"])
def test_serve_config_reaches_create_mcp_server(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    calls = _capture_create_mcp_server(monkeypatch)
    cfg = ServeConfig(transport=transport, schema_mode="defs", advertise_output_schemas=True)  # type: ignore[arg-type]

    with pytest.raises(_CapturedError):
        _serve_mod.serve(_LeanToolsListSeller(), config=cfg)

    assert calls[0]["schema_mode"] == "defs"
    assert calls[0]["advertise_output_schemas"] is True


def test_serve_config_defaults() -> None:
    cfg = ServeConfig()

    assert cfg.schema_mode == "lean"
    assert cfg.advertise_output_schemas is False


@pytest.mark.parametrize("transport", ["streamable-http", "both"])
def test_serve_passes_options_to_the_transport_helpers(transport: str) -> None:
    helper = "_serve_mcp" if transport == "streamable-http" else "_serve_mcp_and_a2a"

    with patch.object(_serve_mod, helper) as mock:
        _serve_mod.serve(
            _LeanToolsListSeller(),
            transport=transport,  # type: ignore[arg-type]
            schema_mode="compact",
            advertise_output_schemas=True,
        )

    _, kwargs = mock.call_args
    assert kwargs["schema_mode"] == "compact"
    assert kwargs["advertise_output_schemas"] is True


def test_compact_mode_is_exactly_the_previous_compact_input_schema() -> None:
    by_name = {t["name"]: t for t in ADCP_TOOL_DEFINITIONS}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        unpinned = {
            d["name"]: d
            for d in get_tools_for_handler(ADCPHandler, advertise_all=True, schema_mode="compact")
        }

    for name in SALES_PAGE:
        assert unpinned[name]["inputSchema"] == by_name[name]["inputSchema"], name
        assert "x-adcp-schema" not in unpinned[name]["inputSchema"]
        assert "outputSchema" not in unpinned[name]


def test_compact_mode_with_outputs_matches_the_registry_definitions() -> None:
    by_name = {t["name"]: t for t in ADCP_TOOL_DEFINITIONS}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        listed = get_tools_for_handler(
            ADCPHandler,
            advertise_all=True,
            schema_mode="compact",
            advertise_output_schemas=True,
        )

    for definition in listed:
        if definition["name"] in SALES_PAGE:
            assert definition == by_name[definition["name"]], definition["name"]


# ----------------------------------------------------------------------
# (h) Unknown mode
# ----------------------------------------------------------------------


def test_unknown_schema_mode_still_raises() -> None:
    with pytest.raises(ValueError):
        get_tools_for_handler(ADCPHandler, advertise_all=True, schema_mode="bogus")  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# comply_test_controller under the lean discovery defaults
# ----------------------------------------------------------------------

_CONTROLLER_URL_SUFFIX = "compliance/comply-test-controller-request.json"


class _Store(TestControllerStore):
    async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
        return {"previous_state": "active", "current_state": status}


@pytest.fixture
def _sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADCP_SANDBOX", "1")


async def _controller_tool(mcp: Any) -> Any:
    tools = {t.name: t for t in await mcp.list_tools()}
    return tools["comply_test_controller"]


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")))


@pytest.mark.asyncio
async def test_controller_inherits_the_lean_default_from_the_server() -> None:
    mcp = create_mcp_server(_LeanToolsListSeller())
    register_test_controller(mcp, _Store())

    tool = await _controller_tool(mcp)

    assert tool.output_schema is None
    assert tool.input_schema["x-adcp-schema"].endswith(_CONTROLLER_URL_SUFFIX)
    assert _size(tool.input_schema) < 5 * 1024


@pytest.mark.asyncio
async def test_controller_inherits_the_servers_opt_out() -> None:
    mcp = create_mcp_server(
        _LeanToolsListSeller(), schema_mode="compact", advertise_output_schemas=True
    )
    register_test_controller(mcp, _Store())

    tool = await _controller_tool(mcp)

    assert tool.output_schema
    assert "x-adcp-schema" not in tool.input_schema
    assert _size(tool.input_schema) > 5 * 1024


@pytest.mark.asyncio
async def test_explicit_controller_kwargs_override_the_servers_settings() -> None:
    mcp = create_mcp_server(_LeanToolsListSeller())
    register_test_controller(mcp, _Store(), schema_mode="inline", advertise_output_schemas=True)

    tool = await _controller_tool(mcp)

    assert tool.output_schema
    assert "x-adcp-schema" not in tool.input_schema
    # The exact schema is the large one.
    assert _size(tool.input_schema) > 100 * 1024

    compact_server = create_mcp_server(
        _LeanToolsListSeller(), schema_mode="compact", advertise_output_schemas=True
    )
    register_test_controller(
        compact_server, _Store(), schema_mode="lean", advertise_output_schemas=False
    )
    lean = await _controller_tool(compact_server)
    assert lean.output_schema is None
    assert lean.input_schema["x-adcp-schema"].endswith(_CONTROLLER_URL_SUFFIX)


def test_controller_rejects_an_unknown_schema_mode() -> None:
    mcp = create_mcp_server(_LeanToolsListSeller())

    with pytest.raises(ValueError):
        register_test_controller(mcp, _Store(), schema_mode="bogus")  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_sandbox")
async def test_a_lean_listed_controller_still_validates_calls_exactly() -> None:
    mcp = create_mcp_server(_LeanToolsListSeller())
    register_test_controller(mcp, _Store())
    assert (await _controller_tool(mcp)).output_schema is None
    call = mcp._tool_manager._tools["comply_test_controller"].fn
    account = {"sandbox": True}

    ok = await call(
        account=account,
        scenario="force_account_status",
        params={"account_id": "acc-1", "status": "suspended"},
    )
    unknown = await call(account=account, scenario="no_such_scenario")
    missing = await call(account=account, scenario="force_account_status", params={})

    assert ok["success"] is True and ok["current_state"] == "suspended"
    assert unknown["success"] is False and unknown["error"] == "UNKNOWN_SCENARIO"
    assert missing["success"] is False and missing["error"] == "INVALID_PARAMS"
    assert "account_id" in missing["error_detail"] and "status" in missing["error_detail"]


@pytest.mark.parametrize("transport", ["streamable-http", "both"])
def test_serve_threads_the_settings_to_the_test_controller(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    from adcp.server import test_controller as controller_mod

    seen: list[Any] = []

    def stub(mcp: Any, store: Any, **kwargs: Any) -> None:
        seen.append((mcp, store, kwargs))
        raise _CapturedError

    monkeypatch.setattr(controller_mod, "register_test_controller", stub)
    store = _Store()

    with pytest.raises(_CapturedError):
        _serve_mod.serve(
            _LeanToolsListSeller(),
            transport=transport,  # type: ignore[arg-type]
            test_controller=store,
            schema_mode="compact",
            advertise_output_schemas=True,
        )

    mcp, received_store, kwargs = seen[0]
    assert received_store is store
    # register_test_controller reads the server's own settings when it is not
    # given explicit ones, so the server must carry what serve() was given.
    assert kwargs.get("schema_mode", mcp._adcp_schema_mode) == "compact"
    assert kwargs.get("advertise_output_schemas", mcp._adcp_advertise_output_schemas) is True
    assert mcp._adcp_schema_mode == "compact"
    assert mcp._adcp_advertise_output_schemas is True


@pytest.mark.parametrize("transport", ["streamable-http", "both"])
def test_serve_defaults_reach_the_test_controller_as_lean(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    from adcp.server import test_controller as controller_mod

    seen: list[Any] = []

    def stub(mcp: Any, store: Any, **kwargs: Any) -> None:
        seen.append((mcp, kwargs))
        raise _CapturedError

    monkeypatch.setattr(controller_mod, "register_test_controller", stub)

    with pytest.raises(_CapturedError):
        _serve_mod.serve(
            _LeanToolsListSeller(),
            transport=transport,  # type: ignore[arg-type]
            test_controller=_Store(),
        )

    mcp, kwargs = seen[0]
    assert kwargs.get("schema_mode", mcp._adcp_schema_mode) == "lean"
    assert kwargs.get("advertise_output_schemas", mcp._adcp_advertise_output_schemas) is False


@pytest.mark.asyncio
async def test_a_mounted_sales_catalog_with_the_controller_stays_small() -> None:
    bare = create_mcp_server(ADCPHandler(), advertise_all=True)
    mounted = create_mcp_server(ADCPHandler(), advertise_all=True)
    register_test_controller(mounted, _Store())

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keep = {*SALES_PAGE, "comply_test_controller"}
        without = [t for t in await bare.list_tools() if t.name in keep]
        tools = [t for t in await mounted.list_tools() if t.name in keep]

    assert {t.name for t in tools} == keep
    assert all(t.output_schema is None for t in tools)

    def wire(listed: list[Any]) -> int:
        return _size([t.model_dump(by_alias=True, exclude_none=True) for t in listed])

    assert wire(tools) < PAGE_BUDGET
    # The controller costs a couple of KB, not another schema's worth.
    assert wire(tools) - wire(without) < 5 * 1024
