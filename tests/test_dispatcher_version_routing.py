"""Stage 3 tests: dispatcher reads ``adcp_version`` off the wire.

Exercises ``create_tool_caller``'s version detection. Three scenarios:

1. Buyer omits version fields → validator runs against 3.0 compatibility
   after legacy shape probes.
2. Buyer claims a supported version → validator runs against that
   version's schema (Stage 2 loader receives the matching ``version=``).
3. Buyer claims an unsupported version → dispatcher raises
   ``VERSION_UNSUPPORTED`` *before* dispatching to the handler.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from adcp import get_adcp_spec_version
from adcp._version import normalize_to_release_precision
from adcp.exceptions import ADCPTaskError
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.validation.client_hooks import ValidationHookConfig


class _RecorderHandler(ADCPHandler[Any]):
    """Records the params it receives so tests can assert on dispatch."""

    adcp_capabilities = {"media_buy": {"features": {"canonical_creatives": True}}}

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.contexts: list[ToolContext] = []

    async def get_products(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.received.append(params)
        self.contexts.append(ctx)
        return {"products": []}


@pytest.mark.asyncio
async def test_no_version_field_validator_uses_legacy_30_compat() -> None:
    """Buyer omits ``adcp_version`` and ``adcp_major_version`` — the
    legacy input should be normalized before SDK-pin validation."""
    handler = _RecorderHandler()

    with patch("adcp.validation.schema_validator.validate_request") as mock_validate:
        mock_validate.return_value = type("Outcome", (), {"valid": True, "issues": []})()
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="warn"),
        )
        await caller({"buying_mode": "brief", "brief": "Q4"})

    assert mock_validate.call_count == 1
    _, kwargs = mock_validate.call_args
    assert kwargs.get("version") is None
    assert handler.contexts[0].resolved_adcp_version == "3.0"


@pytest.mark.asyncio
async def test_validation_skipped_entirely_when_config_omitted() -> None:
    """Regression guard: without ``ValidationHookConfig``, no validator
    runs at all — separate from version detection."""
    handler = _RecorderHandler()
    caller = create_tool_caller(handler, "get_products")  # no validation=

    with patch("adcp.validation.schema_validator.validate_request") as mock_validate:
        await caller({"buying_mode": "brief", "brief": "Q4"})

    assert mock_validate.call_count == 0


@pytest.mark.asyncio
async def test_explicit_adcp_version_threads_through_to_validator() -> None:
    """Buyer sets ``adcp_version='3.0'``; validation follows normalization."""
    handler = _RecorderHandler()

    # Patch must be active when ``create_tool_caller`` runs — its import
    # of ``validate_request`` is local-scope, so the closure captures
    # whichever binding existed at construction time.
    with patch("adcp.validation.schema_validator.validate_request") as mock_validate:
        mock_validate.return_value = type("Outcome", (), {"valid": True, "issues": []})()
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="warn"),
        )
        await caller(
            {
                "adcp_version": "3.0",
                "buying_mode": "brief",
                "brief": "Q4",
            },
        )

    assert mock_validate.call_count == 1
    _, kwargs = mock_validate.call_args
    assert kwargs.get("version") is None


@pytest.mark.asyncio
async def test_exact_packaged_adcp_version_threads_through_to_validator() -> None:
    """Capabilities may advertise the packaged beta line; selecting it is supported."""
    handler = _RecorderHandler()
    exact_version = normalize_to_release_precision(get_adcp_spec_version())

    with patch("adcp.validation.schema_validator.validate_request") as mock_validate:
        mock_validate.return_value = type("Outcome", (), {"valid": True, "issues": []})()
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="warn"),
        )
        await caller(
            {
                "adcp_version": exact_version,
                "buying_mode": "brief",
                "brief": "Q4",
            },
        )

    _, kwargs = mock_validate.call_args
    assert kwargs.get("version") == exact_version
    assert handler.contexts[0].resolved_adcp_version == exact_version


@pytest.mark.asyncio
async def test_custom_unnegotiated_default_can_leave_version_unset() -> None:
    handler = _RecorderHandler()
    caller = create_tool_caller(
        handler,
        "get_products",
        default_unnegotiated_adcp_version=None,
    )

    await caller({"buying_mode": "brief", "brief": "Q4"})

    assert handler.contexts[0].resolved_adcp_version is None


@pytest.mark.asyncio
async def test_adcp_major_version_int_threads_through_to_validator() -> None:
    """Pre-3.1 buyer sets only ``adcp_major_version=3`` → 3.0 compatibility."""
    handler = _RecorderHandler()

    with patch("adcp.validation.schema_validator.validate_request") as mock_validate:
        mock_validate.return_value = type("Outcome", (), {"valid": True, "issues": []})()
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="warn"),
        )
        await caller(
            {
                "adcp_major_version": 3,
                "buying_mode": "brief",
                "brief": "Q4",
            },
        )

    assert mock_validate.call_count == 1
    _, kwargs = mock_validate.call_args
    assert kwargs.get("version") is None


@pytest.mark.asyncio
async def test_unsupported_major_version_raises_version_unsupported() -> None:
    """Future-major buyer (e.g. ``adcp_major_version=4``) gets a clean
    ``VERSION_UNSUPPORTED`` error — *before* the handler runs.

    """
    handler = _RecorderHandler()
    caller = create_tool_caller(handler, "get_products")

    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({"adcp_major_version": 4})

    err = exc_info.value.errors[0]
    assert err.code == "VERSION_UNSUPPORTED"
    assert "4" in err.message
    assert err.details is not None
    # Wire value preserves int type (was sent as int, echoed as int).
    assert err.details.get("claimed_version") == 4
    assert "supported_versions" in err.details

    # Handler must NOT have been invoked.
    assert handler.received == []


@pytest.mark.asyncio
async def test_unsupported_adcp_version_string_raises_version_unsupported() -> None:
    """A version outside both ``COMPATIBLE_ADCP_VERSIONS`` and
    ``LEGACY_ADAPTER_VERSIONS`` raises VERSION_UNSUPPORTED. v2.5 is
    handled via the legacy adapter path (Stage 4) — pick an unsupported
    version that's neither native nor legacy."""
    handler = _RecorderHandler()
    caller = create_tool_caller(handler, "get_products")

    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({"adcp_version": "1.0"})

    err = exc_info.value.errors[0]
    assert err.code == "VERSION_UNSUPPORTED"
    assert err.details is not None
    assert err.details.get("claimed_version") == "1.0"
    assert handler.received == []


@pytest.mark.asyncio
async def test_unsupported_32_release_never_dispatches_or_validates() -> None:
    """A release with no bundled validator must fail before dispatch."""
    handler = _RecorderHandler()

    with patch("adcp.validation.schema_validator.validate_request") as mock_validate:
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="warn"),
        )
        with pytest.raises(ADCPTaskError) as exc_info:
            await caller({"adcp_version": "3.2", "brief": "Q4"})

    err = exc_info.value.errors[0]
    assert err.code == "VERSION_UNSUPPORTED"
    assert err.details is not None
    assert err.details["claimed_version"] == "3.2"
    assert handler.received == []
    mock_validate.assert_not_called()


@pytest.mark.asyncio
async def test_version_detection_runs_after_pre_validation_hook() -> None:
    """A pre-validation hook can populate the version envelope; detection
    must see the post-hook params, not the wire input."""

    def hook(_tool: str, args: dict[str, Any]) -> dict[str, Any]:
        # Legacy buyer omitted the field; hook supplies a supported version.
        return {**args, "adcp_version": "3.0"}

    handler = _RecorderHandler()

    with patch("adcp.validation.schema_validator.validate_request") as mock_validate:
        mock_validate.return_value = type("Outcome", (), {"valid": True, "issues": []})()
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="warn"),
            pre_validation_hook=hook,
        )
        await caller({"buying_mode": "brief", "brief": "Q4"})

    _, kwargs = mock_validate.call_args
    assert kwargs.get("version") is None


@pytest.mark.asyncio
async def test_response_validation_uses_same_wire_version() -> None:
    """Response validation should resolve against the same version the
    request claimed — so a v2.5 buyer's response gets v2.5-schema-checked.
    """
    handler = _RecorderHandler()
    exact_version = normalize_to_release_precision(get_adcp_spec_version())

    with patch("adcp.validation.schema_validator.validate_response") as mock_validate:
        mock_validate.return_value = type("Outcome", (), {"valid": True, "issues": []})()
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="off", responses="warn"),
        )
        await caller(
            {
                "adcp_version": exact_version,
                "buying_mode": "brief",
                "brief": "Q4",
            },
        )

    _, kwargs = mock_validate.call_args
    assert kwargs.get("version") == exact_version


@pytest.mark.asyncio
async def test_stable_31_wire_version_is_not_accepted_while_packaged_line_is_beta() -> None:
    """Only exact advertised versions are accepted for release-precision routing."""
    exact_version = normalize_to_release_precision(get_adcp_spec_version())
    if exact_version == "3.1":
        pytest.skip("Package now advertises stable 3.1")

    handler = _RecorderHandler()
    caller = create_tool_caller(handler, "get_products")

    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({"adcp_version": "3.1"})

    err = exc_info.value.errors[0]
    assert err.code == "VERSION_UNSUPPORTED"
    assert err.details is not None
    assert err.details.get("claimed_version") == "3.1"
    assert exact_version in err.details.get("supported_versions", [])
    assert handler.received == []
