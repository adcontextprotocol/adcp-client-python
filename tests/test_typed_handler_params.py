"""Typed handler params — closes #214.

Before this PR, handlers took ``params: dict[str, Any]`` and wrote
``params.get("buying_mode")`` everywhere. Rounds 4–7 of DX validation
flagged this as the biggest structural boilerplate complaint: no IDE
autocomplete, no Pydantic validation at the handler boundary, typos
land silently as ``None`` at runtime.

The dispatcher now inspects the handler override's ``params``
annotation. When it's a Pydantic model, the raw dict is
``model_validate``'d before the handler runs — the handler receives a
typed instance with autocomplete and validation. Invalid payloads
surface as a structured ``INVALID_REQUEST`` AdCP error (spec-typed
recovery classification) instead of a raw Pydantic traceback.

Legacy ``params: dict[str, Any]`` handlers keep working — the
dispatcher falls back to the dict path when no Pydantic model is in
the annotation. This is a pure DX upgrade, not a breaking change.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from adcp.exceptions import ADCPTaskError
from adcp.server import ADCPHandler, ToolContext
from adcp.server.mcp_tools import (
    _resolve_params_pydantic_model,
    create_tool_caller,
)
from adcp.types import GetProductsRequest
from adcp.types.legacy import LegacyListCreativeFormatsRequest as ListCreativeFormatsRequest

# ---------------------------------------------------------------------------
# _resolve_params_pydantic_model — the signature inspection helper
# ---------------------------------------------------------------------------


def test_resolves_direct_pydantic_annotation():
    """``params: GetProductsRequest`` — the primary target shape."""

    async def fn(self, params: GetProductsRequest, context: ToolContext | None = None) -> Any:
        return {}

    assert _resolve_params_pydantic_model(fn) is GetProductsRequest


def test_resolves_union_with_pydantic_and_dict():
    """``params: GetProductsRequest | dict[str, Any]`` is the shape the
    specialized SDK bases already declare. The helper picks the Pydantic
    branch so existing specialized-base subclasses get typed dispatch
    without code changes."""

    async def fn(
        self,
        params: GetProductsRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> Any:
        return {}

    assert _resolve_params_pydantic_model(fn) is GetProductsRequest


def test_returns_none_for_dict_annotation():
    """``params: dict[str, Any]`` — legacy signature. No deserialization
    happens; dispatcher passes the dict through."""

    async def fn(self, params: dict[str, Any], context: ToolContext | None = None) -> Any:
        return {}

    assert _resolve_params_pydantic_model(fn) is None


def test_returns_none_for_missing_annotation():
    """``params`` with no annotation — legacy pattern. Pass through."""

    async def fn(self, params, context=None) -> Any:
        return {}

    assert _resolve_params_pydantic_model(fn) is None


def test_returns_none_for_non_pydantic_class():
    """A class that isn't a Pydantic model doesn't trigger typed
    dispatch — we're not going to synthesize validation logic for
    arbitrary user types."""

    class _NotPydantic:
        pass

    async def fn(self, params: _NotPydantic, context: ToolContext | None = None) -> Any:
        return {}

    assert _resolve_params_pydantic_model(fn) is None


# ---------------------------------------------------------------------------
# Dispatcher hands typed instance to handler
# ---------------------------------------------------------------------------


async def test_typed_handler_receives_pydantic_instance():
    """The primary #214 promise. Author writes
    ``async def get_products(self, params: GetProductsRequest, ...)``
    and the handler gets a typed instance — with attribute access,
    autocomplete, and validation already done."""
    received: list[Any] = []

    class _Agent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(
            self,
            params: GetProductsRequest,
            context: ToolContext | None = None,
        ) -> Any:
            received.append(params)
            return {"products": []}

    caller = create_tool_caller(_Agent(), "get_products")
    await caller({"buying_mode": "brief", "promoted_offering": "test"})

    assert len(received) == 1
    # Typed instance — attribute access works (would fail on a dict).
    assert isinstance(received[0], GetProductsRequest)
    assert received[0].buying_mode.value == "brief"
    assert received[0].promoted_offering == "test"


async def test_legacy_dict_handler_still_works():
    """Backward-compat. Pre-#214 handlers with
    ``params: dict[str, Any]`` keep getting dicts — the dispatcher
    sees no Pydantic annotation and passes through."""
    received: list[Any] = []

    class _Agent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(
            self, params: dict[str, Any], context: ToolContext | None = None
        ) -> Any:
            received.append(params)
            return {"products": []}

    caller = create_tool_caller(_Agent(), "get_products")
    await caller({"buying_mode": "brief"})

    assert len(received) == 1
    assert isinstance(received[0], dict)
    assert received[0]["buying_mode"] == "brief"


async def test_validation_error_surfaces_as_invalid_request():
    """A Pydantic ValidationError at the dispatcher boundary must NOT
    propagate as a raw traceback. It surfaces as a structured
    ADCPTaskError with code ``INVALID_REQUEST`` so ``translate_error``
    maps it to the right MCP/A2A error shape and clients can programmatic-
    handle recovery."""

    class _Agent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(
            self,
            params: GetProductsRequest,
            context: ToolContext | None = None,
        ) -> Any:
            return {"products": []}

    caller = create_tool_caller(_Agent(), "get_products")

    with pytest.raises(ADCPTaskError) as exc_info:
        # Missing required field `buying_mode`.
        await caller({"promoted_offering": "test"})

    err = exc_info.value
    assert "INVALID_REQUEST" in err.error_codes
    # The error carries the Pydantic validation details so downstream
    # can inspect programmatically.
    assert err.errors[0].details is not None
    assert "validation_errors" in err.errors[0].details
    # The field path is lifted onto Error.field — the spec's dedicated
    # field for programmatic client handling (vs. parsing the message).
    assert err.errors[0].field == "buying_mode"


async def test_validation_error_strips_input_value():
    """**PII/secret-leak regression guard**. Pydantic's ``errors()``
    echoes the raw offending input under ``input`` (and ``ctx``/``url``).
    In multi-hop agent chains the error flows through broker
    intermediaries — echoing a mistyped bearer token or secret-shaped
    value exposes it. The dispatcher strips ``input``/``ctx``/``url``
    before wrapping in ADCPTaskError. Regression here would silently
    reintroduce the leak (security review of PR #238)."""

    class _Agent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(
            self,
            params: GetProductsRequest,
            context: ToolContext | None = None,
        ) -> Any:
            return {"products": []}

    caller = create_tool_caller(_Agent(), "get_products")
    # Submit a value the caller might regret broadcasting — a
    # secret-shaped string for a field with the wrong type
    # constraint. The error must NOT echo it back.
    sensitive = "sk_live_SUPER_SECRET_VALUE_xyz"
    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({"buying_mode": sensitive})

    err = exc_info.value
    # The raw sensitive string must not appear anywhere in the error.
    details_serialised = str(err.errors[0].details)
    assert sensitive not in details_serialised
    assert sensitive not in err.errors[0].message
    # Structural details still carry loc/msg/type — client debuggability
    # is preserved via the field path.
    validation_errors = err.errors[0].details["validation_errors"]
    assert validation_errors
    assert "loc" in validation_errors[0]
    assert "msg" in validation_errors[0]
    # And explicitly the stripped keys are gone.
    assert "input" not in validation_errors[0]
    assert "url" not in validation_errors[0]


def test_mcp_error_translation_embeds_field_path():
    """``translate_error`` for MCP previously dropped ``Error.field``
    because MCP's ToolError has no structured ``data`` channel. The
    fix embeds the field path in the code prefix: ``INVALID_REQUEST[field]:
    message``. A2A already carries ``field`` structurally via the data
    passthrough. Regression guard — dropping ``field`` on the MCP side
    leaves clients stuck parsing free-form English to find what went
    wrong."""
    from adcp.server.translate import translate_error
    from adcp.types import Error

    err = Error(
        code="INVALID_REQUEST",
        field="packages[0].budget",
        message="Value should be positive",
    )
    mcp_error = translate_error(err, protocol="mcp")
    # ToolError's text — the only channel MCP has.
    text = str(mcp_error)
    assert "INVALID_REQUEST[packages[0].budget]" in text
    assert "Value should be positive" in text


async def test_mixed_typed_and_legacy_handlers_coexist():
    """Sellers migrate incrementally — some handlers typed, others
    still dict. Both must route correctly on the same handler
    instance."""
    typed_received: list[Any] = []
    dict_received: list[Any] = []

    class _Agent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(
            self, params: GetProductsRequest, context: ToolContext | None = None
        ) -> Any:
            typed_received.append(params)
            return {"products": []}

        # Still legacy-style.
        async def sync_creatives(
            self, params: dict[str, Any], context: ToolContext | None = None
        ) -> Any:
            dict_received.append(params)
            return {"results": []}

    agent = _Agent()
    typed_caller = create_tool_caller(agent, "get_products")
    dict_caller = create_tool_caller(agent, "sync_creatives")

    await typed_caller({"buying_mode": "brief"})
    await dict_caller({"creatives": []})

    assert isinstance(typed_received[0], GetProductsRequest)
    assert isinstance(dict_received[0], dict)


async def test_context_echo_uses_raw_dict_not_validated_model():
    """ADCP requires the server to echo the ``context`` field from the
    request into the response. The wire ``context`` field isn't part
    of typed request models — the dispatcher reads it from the raw
    dict, not from the validated instance, so context echo still
    works under typed dispatch."""

    class _Agent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(
            self, params: GetProductsRequest, context: ToolContext | None = None
        ) -> Any:
            return {"products": []}

    caller = create_tool_caller(_Agent(), "get_products")
    result = await caller(
        {
            "buying_mode": "brief",
            "context": {"conversation_id": "c-1"},
        }
    )
    # inject_context copied the request.context into the response.
    assert result.get("context") == {"conversation_id": "c-1"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_handler_returning_already_typed_params_no_double_validation():
    """When the handler calls ``Model.model_validate(params)`` itself
    (the specialized SDK bases still do this today), the typed
    dispatch passing a typed instance must NOT break it. Pydantic
    ``model_validate`` on an already-typed instance is a no-op —
    returns the same object, validators are skipped. Verify the
    existing specialized-base pattern is unaffected."""
    from adcp.types import GetProductsResponse

    received_types: list[type] = []

    class _Agent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(
            self,
            params: GetProductsRequest | dict[str, Any],
            context: ToolContext | None = None,
        ) -> Any:
            # Specialized-base pattern: defensively re-validate.
            req = GetProductsRequest.model_validate(params)
            received_types.append(type(req))
            return GetProductsResponse(products=[])

    caller = create_tool_caller(_Agent(), "get_products")
    await caller({"buying_mode": "brief"})

    # Dispatch handed the method a typed instance; the method's
    # defensive model_validate was a no-op pass-through. No crash,
    # no error — the existing pattern keeps working.
    assert received_types == [GetProductsRequest]


# ---------------------------------------------------------------------------
# Custom Pydantic model — not limited to the generated request types
# ---------------------------------------------------------------------------


class _StrictGetProductsRequest(BaseModel):
    """Module-level custom model.

    Defined at module scope because ``typing.get_type_hints`` needs to
    resolve the forward reference string (``from __future__ import
    annotations`` stringifies all annotations) against a reachable
    namespace — the handler module globals. Models defined inside a
    function body live in a local namespace that the dispatcher can't
    see. Production handlers define their params models at module
    top-level, so this limitation matches real usage.
    """

    buying_mode: str
    promoted_offering: str


async def test_custom_pydantic_model_also_works():
    """Authors aren't restricted to the SDK's generated request classes.
    Any Pydantic model declared on the ``params`` annotation triggers
    typed dispatch. Useful for sellers who want to layer additional
    validation (stricter field constraints, invariants) on top of the
    spec shape."""
    received: list[Any] = []

    class _Agent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(
            self,
            params: _StrictGetProductsRequest,
            context: ToolContext | None = None,
        ) -> Any:
            received.append(params)
            return {"products": []}

    caller = create_tool_caller(_Agent(), "get_products")
    await caller({"buying_mode": "brief", "promoted_offering": "test"})

    assert isinstance(received[0], _StrictGetProductsRequest)
    assert received[0].buying_mode == "brief"


# ---------------------------------------------------------------------------
# A second tool — prove the plumbing is tool-agnostic
# ---------------------------------------------------------------------------


async def test_typed_dispatch_on_second_tool():
    """Coverage for a second tool to prove the typed-dispatch plumbing
    isn't ``get_products``-specific — the signature inspection walks
    every handler method the same way."""
    received: list[Any] = []

    class _Agent(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def list_creative_formats_legacy(
            self,
            params: ListCreativeFormatsRequest,
            context: ToolContext | None = None,
        ) -> Any:
            received.append(params)
            return {"formats": []}

    caller = create_tool_caller(_Agent(), "list_creative_formats")
    await caller({})

    assert len(received) == 1
    assert isinstance(received[0], ListCreativeFormatsRequest)
