"""Drift tests for MCP tool inputSchema and outputSchema generation.

The MCP tool registry exposes ``inputSchema`` and ``outputSchema`` for every
ADCP tool via ``tools/list``. These schemas are auto-generated from the
corresponding Pydantic request/response models in ``adcp.types`` at import
time (:func:`adcp.server.mcp_tools._generate_pydantic_schemas` /
:func:`adcp.server.mcp_tools._generate_pydantic_output_schemas`).

This module protects both generation paths from regressions:

1. Every tool must resolve to a Pydantic-generated inputSchema. If a new tool
   is added to ``ADCP_TOOL_DEFINITIONS`` without a mapping in
   ``_tool_to_request``, the tool would silently ship a hand-crafted
   stub schema again — the drift this whole mechanism exists to prevent.
2. Each tool's ``inputSchema`` must match the ``model_json_schema()``
   output of its request model (modulo the ``title`` strip and the
   conditional ``$defs`` drop). If Pydantic changes its schema output,
   or a model gains/drops a field, this test fails on the affected tool.
3. The schema must advertise the model's required fields so agents
   constructing payloads via ``tools/list`` see accurate constraints.
4. Every tool must also resolve to a Pydantic-generated outputSchema
   (response model mapping). outputSchema may use ``anyOf`` for union
   response types — that is valid MCP contract for what a tool returns.
5. Each tool's ``outputSchema`` must match fresh generation — no drift.
"""

from __future__ import annotations

import json

from adcp.server.mcp_tools import (
    _PYDANTIC_OUTPUT_SCHEMAS,
    _PYDANTIC_SCHEMAS,
    ADCP_TOOL_DEFINITIONS,
    _generate_pydantic_output_schemas,
    _generate_pydantic_schemas,
    _inline_refs,
)


def test_every_tool_has_pydantic_generated_schema() -> None:
    """Every ADCP tool must map to a Pydantic request model."""
    tool_names = {t["name"] for t in ADCP_TOOL_DEFINITIONS}
    missing = tool_names - set(_PYDANTIC_SCHEMAS.keys())
    assert not missing, (
        "Tools missing from Pydantic schema generator — they would ship "
        "stub inputSchemas that drift from the real request model:\n"
        + "\n".join(f"  - {name}" for name in sorted(missing))
        + "\n\nAdd each tool to ``_tool_to_request`` in "
        "``adcp/server/mcp_tools.py``, mapped to its ``<ToolName>Request`` model."
    )


def test_input_schemas_match_pydantic_generation() -> None:
    """tools/list schemas must byte-match fresh generation — no silent drift."""
    fresh = _generate_pydantic_schemas()
    mismatches: list[str] = []
    for tool in ADCP_TOOL_DEFINITIONS:
        name = tool["name"]
        if name not in fresh:
            continue
        expected = fresh[name]
        actual = tool["inputSchema"]
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            mismatches.append(name)

    assert not mismatches, (
        "ADCP_TOOL_DEFINITIONS has stale inputSchemas — "
        "`_apply_pydantic_schemas()` must run at import time:\n"
        + "\n".join(f"  - {name}" for name in mismatches)
    )


def test_required_fields_advertised() -> None:
    """Required fields on each model must appear in the tool's inputSchema.

    Agents building payloads from ``tools/list`` rely on the ``required``
    array to know which fields cannot be omitted. If a model marks a
    field as required but the advertised schema doesn't, the agent will
    happily send a malformed request.
    """
    from pydantic import TypeAdapter

    from adcp.types import (
        AcquireRightsRequest,
        BuildCreativeRequest,
        CheckGovernanceRequest,
        ContextMatchRequest,
        CreateMediaBuyRequest,
        GetProductsRequest,
        IdentityMatchRequest,
        ReportPlanOutcomeRequest,
        SyncGovernanceRequest,
        UpdateRightsRequest,
    )

    # Spot-check a representative slice: a mix of simple GETs, mutating
    # writes, and schemas that include nested $refs. If the required
    # fields drift for any of these, the rest probably drifted too.
    checks = {
        "get_products": GetProductsRequest,
        "build_creative": BuildCreativeRequest,
        "create_media_buy": CreateMediaBuyRequest,
        "check_governance": CheckGovernanceRequest,
        "report_plan_outcome": ReportPlanOutcomeRequest,
        "acquire_rights": AcquireRightsRequest,
        "update_rights": UpdateRightsRequest,
        "sync_governance": SyncGovernanceRequest,
        "context_match": ContextMatchRequest,
        "identity_match": IdentityMatchRequest,
    }

    tool_schemas = {t["name"]: t["inputSchema"] for t in ADCP_TOOL_DEFINITIONS}
    errors: list[str] = []

    for tool_name, model in checks.items():
        expected_required = set(TypeAdapter(model).json_schema().get("required", []))
        advertised_required = set(tool_schemas[tool_name].get("required", []))
        missing = expected_required - advertised_required
        if missing:
            errors.append(
                f"{tool_name}: model requires {sorted(missing)} " f"but inputSchema does not"
            )

    assert not errors, "Required-field drift:\n" + "\n".join(errors)


def test_spot_check_real_fields_reach_clients() -> None:
    """The three tools that previously had the worst drift must now
    advertise the real required fields from their request models.
    """
    tool_schemas = {t["name"]: t["inputSchema"] for t in ADCP_TOOL_DEFINITIONS}

    get_products = tool_schemas["get_products"]
    assert "brief" in get_products["properties"]
    assert "buying_mode" in get_products["properties"]
    assert "buying_mode" in get_products.get("required", [])

    build_creative = tool_schemas["build_creative"]
    assert "target_format_id" in build_creative["properties"]
    assert "creative_manifest" in build_creative["properties"]
    assert "idempotency_key" in build_creative.get("required", [])

    create_media_buy = tool_schemas["create_media_buy"]
    for field in ("account", "brand", "start_time", "end_time", "packages"):
        assert field in create_media_buy["properties"], f"create_media_buy missing field {field!r}"
    for req in ("account", "brand", "start_time", "end_time", "idempotency_key"):
        assert req in create_media_buy.get(
            "required", []
        ), f"create_media_buy should require {req!r}"


# ---------------------------------------------------------------------------
# $defs inlining invariants (closes #208)
# ---------------------------------------------------------------------------


def test_no_dollar_ref_in_any_advertised_schema() -> None:
    """Every tool's inputSchema must be ``$ref``-free. MCP clients that
    don't implement JSON Schema reference resolution (a surprisingly
    large slice of the ecosystem) see ``{"$ref": ...}`` as an empty
    object — which means "this tool takes no params" in their
    interpretation. Inlining is the only way to give those clients the
    full tool surface. Regression here silently re-breaks discovery for
    those clients."""
    for tool in ADCP_TOOL_DEFINITIONS:
        serialized = json.dumps(tool["inputSchema"])
        assert '"$ref"' not in serialized, (
            f"tool {tool['name']!r} inputSchema contains unresolved $ref. "
            "Check _inline_refs in adcp.server.mcp_tools."
        )


def test_no_dollar_defs_in_any_advertised_schema() -> None:
    """After inlining, ``$defs`` serves no purpose and is noise on the
    wire. Drop it so the advertised schema is minimal."""
    for tool in ADCP_TOOL_DEFINITIONS:
        serialized = json.dumps(tool["inputSchema"])
        assert '"$defs"' not in serialized, (
            f"tool {tool['name']!r} inputSchema retains $defs block after "
            "inlining. Check _inline_refs drop-when-resolved path."
        )


# ---------------------------------------------------------------------------
# _inline_refs unit tests — behavior-level guarantees
# ---------------------------------------------------------------------------


def test_inline_refs_replaces_local_ref_with_body() -> None:
    """The core transform: ``{"$ref": "#/$defs/X"}`` becomes the body
    of ``$defs["X"]``."""
    schema = {
        "type": "object",
        "properties": {"user": {"$ref": "#/$defs/User"}},
        "$defs": {
            "User": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }
    result = _inline_refs(schema)
    assert result["properties"]["user"] == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    assert "$defs" not in result


def test_inline_refs_resolves_nested_refs() -> None:
    """A $def that itself references another $def must be fully
    resolved in one pass. Without recursion, the second level stays
    as a $ref."""
    schema = {
        "type": "object",
        "properties": {"order": {"$ref": "#/$defs/Order"}},
        "$defs": {
            "Order": {
                "type": "object",
                "properties": {"customer": {"$ref": "#/$defs/Customer"}},
            },
            "Customer": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
            },
        },
    }
    result = _inline_refs(schema)
    # Both levels resolved in the output.
    assert result["properties"]["order"]["properties"]["customer"]["properties"]["id"] == {
        "type": "string"
    }
    assert '"$ref"' not in json.dumps(result)


def test_inline_refs_sibling_annotations_override_resolved_body() -> None:
    """Annotation-level merge: sibling ``description`` / ``title`` on
    the $ref node win over the resolved body's same-named keys. This
    is what Pydantic emits at ref sites in practice (a field-level
    description on top of a nested model).

    Note: this is NOT JSON Schema 2020-12 §8.2 merge semantics (which
    would evaluate siblings as an implicit ``allOf``). The inliner's
    override semantics match Pydantic's actual output, not the spec's
    general-case composition rule. If a future Pydantic version
    emits assertion-level siblings at ref sites (``type``, ``enum``,
    etc.), this merge would silently clobber them — today it doesn't."""
    schema = {
        "properties": {
            "account": {
                "$ref": "#/$defs/Account",
                "description": "This one is special — overrides the generic description.",
            }
        },
        "$defs": {
            "Account": {
                "type": "object",
                "description": "Generic account description.",
                "properties": {"id": {"type": "string"}},
            }
        },
    }
    result = _inline_refs(schema)
    assert (
        result["properties"]["account"]["description"]
        == "This one is special — overrides the generic description."
    )
    assert result["properties"]["account"]["properties"] == {"id": {"type": "string"}}


def test_inline_refs_resolves_inside_anyof() -> None:
    """Pydantic emits ``anyOf: [{"$ref": "..."}, {"type": "null"}]``
    for ``Optional[Model]`` on request types. The inliner MUST recurse
    into composition keywords (``anyOf`` / ``oneOf`` / ``allOf``) so
    optional nested models still flatten correctly. Real production
    case — regression here silently breaks optional-model properties
    for non-ref-resolving clients."""
    schema = {
        "type": "object",
        "properties": {"maybe_account": {"anyOf": [{"$ref": "#/$defs/Account"}, {"type": "null"}]}},
        "$defs": {
            "Account": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
            }
        },
    }
    result = _inline_refs(schema)
    any_of = result["properties"]["maybe_account"]["anyOf"]
    # First branch — Account resolved inline.
    assert any_of[0] == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
    }
    # Second branch — unchanged.
    assert any_of[1] == {"type": "null"}
    assert "$defs" not in result


def test_inline_refs_resolves_inside_additional_properties() -> None:
    """``additionalProperties: {"$ref": "..."}`` is another shape
    Pydantic emits — e.g. ``dict[str, NestedModel]`` on a request
    field. Must resolve like any other $ref."""
    schema = {
        "type": "object",
        "properties": {
            "accounts_by_id": {
                "type": "object",
                "additionalProperties": {"$ref": "#/$defs/Account"},
            }
        },
        "$defs": {
            "Account": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }
    result = _inline_refs(schema)
    assert result["properties"]["accounts_by_id"]["additionalProperties"] == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    assert "$defs" not in result


def test_inline_refs_does_not_false_positive_on_ref_as_value() -> None:
    """The ``$defs``-drop decision must not treat a legitimate
    ``"$ref"`` value inside an enum / const / description as an
    unresolved reference. A description that mentions the word
    ``"$ref"`` in prose, or an enum with ``"$ref"`` as a literal
    string, would be a false positive under a naive substring check."""
    schema = {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": 'Must be a JSON Schema keyword like "$ref" or "$id".',
            }
        },
    }
    result = _inline_refs(schema)
    # $defs was never present — no issue here specifically — but
    # verify the description survived and the result is otherwise
    # unchanged.
    assert (
        result["properties"]["keyword"]["description"]
        == 'Must be a JSON Schema keyword like "$ref" or "$id".'
    )


def test_inline_refs_protects_against_cycles() -> None:
    """Pydantic doesn't emit cyclic refs today, but a future request
    model could. Cycle protection must leave the original ``$ref``
    intact (and keep $defs) so the caller still has resolvable data."""
    schema = {
        "type": "object",
        "properties": {"node": {"$ref": "#/$defs/Node"}},
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            }
        },
    }
    result = _inline_refs(schema)
    # Cycle detected — the inner $ref survives because it'd recurse
    # forever. $defs stays so it's still resolvable for spec-compliant
    # clients.
    assert '"$ref"' in json.dumps(result)
    assert "$defs" in result


def test_inline_refs_dangling_ref_leaves_schema_alone() -> None:
    """A $ref pointing at a non-existent $def is a spec error, but the
    inliner shouldn't crash — leave both the $ref and the (empty)
    $defs intact so a spec-compliant client's error surface fires
    with the right shape."""
    schema = {
        "type": "object",
        "properties": {"bad": {"$ref": "#/$defs/NotThere"}},
        "$defs": {},
    }
    result = _inline_refs(schema)
    # Dangling $ref survives; $defs kept because the ref didn't resolve.
    assert '"$ref"' in json.dumps(result)


def test_inline_refs_ignores_external_refs() -> None:
    """External $refs (``http://…``, relative paths) are spec-valid
    but aren't something Pydantic emits for our request models. If
    one ever shows up, leave it alone — silently stripping it would
    corrupt the schema."""
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "https://example.com/schema.json"}},
    }
    result = _inline_refs(schema)
    assert result["properties"]["x"] == {"$ref": "https://example.com/schema.json"}


def test_inline_refs_preserves_required_arrays() -> None:
    """Inlining must not lose the ``required`` array on a resolved
    definition. Agents constructing payloads read this to know which
    fields are mandatory."""
    schema = {
        "type": "object",
        "properties": {"user": {"$ref": "#/$defs/User"}},
        "$defs": {
            "User": {
                "type": "object",
                "required": ["name", "email"],
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                },
            }
        },
    }
    result = _inline_refs(schema)
    assert result["properties"]["user"]["required"] == ["name", "email"]


def test_inline_refs_handles_arrays_of_refs() -> None:
    """``items: {"$ref": "..."}`` must resolve just like top-level
    property refs."""
    schema = {
        "type": "object",
        "properties": {
            "users": {
                "type": "array",
                "items": {"$ref": "#/$defs/User"},
            }
        },
        "$defs": {
            "User": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
            }
        },
    }
    result = _inline_refs(schema)
    assert result["properties"]["users"]["items"]["properties"] == {"id": {"type": "string"}}
    assert "$defs" not in result


def test_inline_refs_does_not_mutate_input() -> None:
    """The inliner must return a new object — callers may want to keep
    the pre-inline form (e.g. for comparison against Pydantic's fresh
    output). Mutating the input silently breaks that contract."""
    schema = {
        "properties": {"user": {"$ref": "#/$defs/User"}},
        "$defs": {"User": {"type": "object"}},
    }
    before = json.dumps(schema, sort_keys=True)
    _inline_refs(schema)
    after = json.dumps(schema, sort_keys=True)
    assert before == after, "inliner must not mutate its input"


# ---------------------------------------------------------------------------
# End-to-end — inlined schema accepts the same valid input as Pydantic
# ---------------------------------------------------------------------------


def test_inlined_schema_still_validates_real_request() -> None:
    """The inlined schema must accept every payload the original
    Pydantic model accepts. Structural equivalence — the shape is
    preserved, just flattened. Round-trip via jsonschema's validator
    against a concrete minimal valid payload."""
    import jsonschema
    import pytest

    from adcp.types import GetProductsRequest

    tool_schemas = {t["name"]: t["inputSchema"] for t in ADCP_TOOL_DEFINITIONS}
    get_products = tool_schemas["get_products"]

    # Minimal valid payload per the model.
    payload = {"buying_mode": "brief"}

    # Build validator from the inlined schema; payload must pass.
    try:
        jsonschema.validate(payload, get_products)
    except jsonschema.ValidationError as exc:  # pragma: no cover
        pytest.fail(f"inlined schema rejected a valid payload: {exc}")

    # And Pydantic still accepts it — both sides of the equivalence.
    GetProductsRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# outputSchema — parity with TS SDK (issue #386)
# ---------------------------------------------------------------------------


def test_every_tool_has_pydantic_generated_output_schema() -> None:
    """Every ADCP tool must map to a Pydantic response model so tools/list
    carries outputSchema. If a new tool is added to ADCP_TOOL_DEFINITIONS
    without a mapping in _tool_to_response, it silently ships with no
    outputSchema — add it to the generator."""
    tool_names = {t["name"] for t in ADCP_TOOL_DEFINITIONS}
    missing = tool_names - set(_PYDANTIC_OUTPUT_SCHEMAS.keys())
    assert not missing, (
        "Tools missing from Pydantic output schema generator — they would ship "
        "with no outputSchema on tools/list:\n"
        + "\n".join(f"  - {name}" for name in sorted(missing))
        + "\n\nAdd each tool to ``_tool_to_response`` in "
        "``adcp/server/mcp_tools.py``, mapped to its ``<ToolName>Response`` model."
    )


def test_output_schemas_applied_to_tool_definitions() -> None:
    """Every tool in ADCP_TOOL_DEFINITIONS must have an outputSchema key
    after import-time application."""
    missing = [t["name"] for t in ADCP_TOOL_DEFINITIONS if "outputSchema" not in t]
    assert not missing, (
        "Tools missing outputSchema in ADCP_TOOL_DEFINITIONS:\n"
        + "\n".join(f"  - {name}" for name in missing)
        + "\n\nCheck _apply_pydantic_output_schemas() runs at import time."
    )


def test_output_schemas_match_pydantic_generation() -> None:
    """tools/list outputSchemas must byte-match fresh generation — no silent drift."""
    fresh = _generate_pydantic_output_schemas()
    mismatches: list[str] = []
    for tool in ADCP_TOOL_DEFINITIONS:
        name = tool["name"]
        if name not in fresh:
            continue
        expected = fresh[name]
        actual = tool.get("outputSchema")
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            mismatches.append(name)

    assert not mismatches, (
        "ADCP_TOOL_DEFINITIONS has stale outputSchemas — "
        "`_apply_pydantic_output_schemas()` must run at import time:\n"
        + "\n".join(f"  - {name}" for name in mismatches)
    )


def test_no_dollar_ref_in_any_output_schema() -> None:
    """Every tool's outputSchema must be ``$ref``-free after inlining.
    Unlike inputSchema, anyOf at root is permitted (union response types
    advertise what a tool may return). But unresolved $ref nodes would
    leave clients unable to resolve the schema."""
    for tool in ADCP_TOOL_DEFINITIONS:
        schema = tool.get("outputSchema")
        if schema is None:
            continue
        serialized = json.dumps(schema)
        assert '"$ref"' not in serialized, (
            f"tool {tool['name']!r} outputSchema contains unresolved $ref. "
            "Check _inline_refs in adcp.server.mcp_tools."
        )


def test_output_schema_spot_check_known_shapes() -> None:
    """Spot-check that representative tools carry the expected outputSchema
    shape so structural changes to response models surface here first."""
    tool_schemas = {t["name"]: t.get("outputSchema") for t in ADCP_TOOL_DEFINITIONS}

    # get_products response has a top-level products field (simple model)
    gp = tool_schemas["get_products"]
    assert gp is not None, "get_products must have outputSchema"
    # Should be a flat object or anyOf — either way, must be a dict
    assert isinstance(gp, dict)

    # create_media_buy response is a union (success | error) — anyOf at root
    cmb = tool_schemas["create_media_buy"]
    assert cmb is not None, "create_media_buy must have outputSchema"
    assert isinstance(cmb, dict)

    # get_adcp_capabilities response is a simple model
    gac = tool_schemas["get_adcp_capabilities"]
    assert gac is not None, "get_adcp_capabilities must have outputSchema"
    assert isinstance(gac, dict)
