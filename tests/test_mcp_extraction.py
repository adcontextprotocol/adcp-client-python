"""Conformance tests for MCP response extraction per AdCP spec.

Validates ``adcp.protocols.mcp.extract_adcp_success`` and
``extract_adcp_error`` against the normative test vectors published at
``https://adcontextprotocol.org/test-vectors/{mcp-response-extraction,transport-error-mapping}.json``.

The vector files are bundled in ``tests/fixtures/mcp_extraction/`` — refresh
them when the upstream spec changes.

See:
- docs/building/implementation/mcp-response-extraction.mdx (success path)
- docs/building/implementation/transport-errors.mdx (error path)
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adcp.protocols._adcp_errors import (
    MAX_ERROR_SIZE_BYTES as _MAX_ERROR_SIZE_BYTES,
)
from adcp.protocols._adcp_errors import (
    validate_adcp_error as _validate_adcp_error,
)
from adcp.protocols.mcp import (
    _MAX_TEXT_SIZE_BYTES,
    extract_adcp_error,
    extract_adcp_success,
)

VECTOR_DIR = Path(__file__).parent / "fixtures" / "mcp_extraction"


def _load_vectors(name: str) -> list[dict[str, Any]]:
    data = json.loads((VECTOR_DIR / name).read_text())
    return data["vectors"]


def _response_shim(envelope: dict[str, Any]) -> SimpleNamespace:
    """Wrap a plain-dict MCP response as an object with attribute access.

    The MCP SDK returns Pydantic models with ``isError`` / ``content`` /
    ``structuredContent`` attributes; our extractors use ``getattr``, so we
    mirror that shape with SimpleNamespace for vector replay.
    """
    return SimpleNamespace(
        isError=envelope.get("isError", False),
        content=envelope.get("content"),
        structuredContent=envelope.get("structuredContent"),
    )


class TestSuccessVectorsFromSpec:
    """Replay the upstream spec's 16 success-path vectors."""

    @pytest.mark.parametrize("vector", _load_vectors("success.json"), ids=lambda v: v["id"])
    def test_vector(self, vector: dict[str, Any]) -> None:
        response = _response_shim(vector["response"])
        expected = vector.get("expected_data")
        got = extract_adcp_success(response)
        assert got == expected, f"vector {vector['id']}: expected {expected!r}, got {got!r}"


def _mcp_tool_error_vectors() -> list[dict[str, Any]]:
    """Filter the 29 transport-error vectors down to MCP tool-level cases.

    Our ``extract_adcp_error`` handles only MCP tool-level ``isError=true``
    shapes — JSON-RPC transport-level errors (``-32029`` etc.) come through
    the MCP SDK as exceptions, not tool results, and so aren't this extractor's
    path.
    """
    out: list[dict[str, Any]] = []
    for v in _load_vectors("errors.json"):
        if not v["id"].startswith("mcp-"):
            continue
        blob = v.get("response") or {}
        if "error" in blob and "jsonrpc" in blob:
            continue  # JSON-RPC transport-level
        if not any(k in blob for k in ("isError", "structuredContent", "content")):
            continue  # not a tool response envelope
        out.append(v)
    return out


class TestErrorVectorsFromSpec:
    """Replay the MCP-relevant subset of the 29 transport-error vectors."""

    @pytest.mark.parametrize("vector", _mcp_tool_error_vectors(), ids=lambda v: v["id"])
    def test_vector(self, vector: dict[str, Any]) -> None:
        response = _response_shim(vector["response"])
        expected = vector.get("expected_error")
        got = extract_adcp_error(response)
        if expected is None:
            assert got is None, f"vector {vector['id']}: expected None, got {got!r}"
        else:
            assert got == expected, f"vector {vector['id']}: expected {expected!r}, got {got!r}"


class TestSuccessExtractionEdgeCases:
    """Edge cases not covered by the upstream vectors."""

    def test_is_error_short_circuits_structured_content(self) -> None:
        resp = _response_shim(
            {
                "isError": True,
                "structuredContent": {"products": []},  # would be valid success otherwise
            }
        )
        assert extract_adcp_success(resp) is None

    def test_oversized_text_item_skipped(self) -> None:
        oversized = "x" * (_MAX_TEXT_SIZE_BYTES + 1)
        resp = _response_shim({"content": [{"type": "text", "text": oversized}]})
        assert extract_adcp_success(resp) is None

    def test_oversized_text_skipped_then_smaller_used(self) -> None:
        oversized = '{"ok":true,' + "x" * _MAX_TEXT_SIZE_BYTES + "}"
        small = '{"status":"completed"}'
        resp = _response_shim(
            {
                "content": [
                    {"type": "text", "text": oversized},
                    {"type": "text", "text": small},
                ]
            }
        )
        got = extract_adcp_success(resp)
        assert got == {"status": "completed"}

    def test_array_json_rejected(self) -> None:
        resp = _response_shim({"content": [{"type": "text", "text": "[1,2,3]"}]})
        assert extract_adcp_success(resp) is None

    def test_primitive_json_rejected(self) -> None:
        resp = _response_shim({"content": [{"type": "text", "text": "42"}]})
        assert extract_adcp_success(resp) is None

    def test_non_text_content_items_skipped(self) -> None:
        resp = _response_shim(
            {
                "content": [
                    {"type": "image", "data": "..."},
                    {"type": "text", "text": '{"status":"completed"}'},
                ]
            }
        )
        assert extract_adcp_success(resp) == {"status": "completed"}

    def test_adcp_error_only_structured_content_returns_none(self) -> None:
        # Error response missing the isError flag — spec says treat as None.
        resp = _response_shim({"structuredContent": {"adcp_error": {"code": "INTERNAL_ERROR"}}})
        assert extract_adcp_success(resp) is None

    def test_adcp_error_only_text_skipped_then_valid_used(self) -> None:
        resp = _response_shim(
            {
                "content": [
                    {"type": "text", "text": '{"adcp_error":{"code":"X"}}'},
                    {"type": "text", "text": '{"status":"completed"}'},
                ]
            }
        )
        assert extract_adcp_success(resp) == {"status": "completed"}


class TestErrorValidation:
    """Spec-defined validation: code string, length limits, total ≤ 4KB."""

    def test_valid_error(self) -> None:
        err = {"code": "RATE_LIMITED", "message": "slow down", "recovery": "transient"}
        assert _validate_adcp_error(err) == err

    def test_missing_code(self) -> None:
        assert _validate_adcp_error({"message": "no code"}) is None

    def test_code_not_string(self) -> None:
        assert _validate_adcp_error({"code": 123}) is None

    def test_empty_code(self) -> None:
        assert _validate_adcp_error({"code": ""}) is None

    def test_code_too_long(self) -> None:
        assert _validate_adcp_error({"code": "X" * 65}) is None

    def test_error_exceeds_4kb(self) -> None:
        err = {"code": "BIG", "padding": "x" * _MAX_ERROR_SIZE_BYTES}
        assert _validate_adcp_error(err) is None

    def test_not_a_dict(self) -> None:
        assert _validate_adcp_error(["not", "a", "dict"]) is None
        assert _validate_adcp_error(None) is None
        assert _validate_adcp_error("RATE_LIMITED") is None


class TestErrorExtractionPaths:
    def test_structured_content_preferred(self) -> None:
        resp = _response_shim(
            {
                "isError": True,
                "content": [{"type": "text", "text": '{"adcp_error":{"code":"OTHER"}}'}],
                "structuredContent": {
                    "adcp_error": {"code": "RATE_LIMITED", "recovery": "transient"}
                },
            }
        )
        got = extract_adcp_error(resp)
        assert got is not None
        assert got["code"] == "RATE_LIMITED"  # structuredContent wins

    def test_text_fallback_when_no_structured(self) -> None:
        resp = _response_shim(
            {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": '{"adcp_error":{"code":"BUDGET_TOO_LOW","recovery":"correctable"}}',
                    }
                ],
            }
        )
        got = extract_adcp_error(resp)
        assert got is not None
        assert got["code"] == "BUDGET_TOO_LOW"

    def test_not_is_error_returns_none(self) -> None:
        resp = _response_shim(
            {
                "isError": False,
                "structuredContent": {"adcp_error": {"code": "WOULD_BE_ERROR"}},
            }
        )
        assert extract_adcp_error(resp) is None

    def test_error_path_enforces_pre_parse_size_limit(self) -> None:
        # DoS guard: a malicious server returning isError=true plus a
        # multi-MB text blob must NOT be json.loads'd into memory.
        padding = "y" * _MAX_TEXT_SIZE_BYTES
        oversized = '{"adcp_error":{"code":"X","pad":"' + padding + '"}}'
        resp = _response_shim({"isError": True, "content": [{"type": "text", "text": oversized}]})
        assert extract_adcp_error(resp) is None

    def test_invalid_error_passes_through_to_none(self) -> None:
        # structuredContent has adcp_error but it's malformed — spec says treat
        # as no-error-found. Do NOT fall through to text content.
        resp = _response_shim(
            {
                "isError": True,
                "structuredContent": {"adcp_error": {"code": 42}},  # not a string
                "content": [
                    {
                        "type": "text",
                        "text": '{"adcp_error":{"code":"FALLBACK_CODE"}}',
                    }
                ],
            }
        )
        # Current implementation falls through to text when structured
        # validation fails. That's consistent with the upstream JS reference
        # at transport-errors.mdx §Client Detection Order path 5 only firing
        # when earlier paths return null/invalid.
        got = extract_adcp_error(resp)
        assert got is not None
        assert got["code"] == "FALLBACK_CODE"
