"""Tests for the v2.5 → v3 ``list_creative_formats`` adapter."""

from __future__ import annotations

from typing import Any

from adcp.compat.legacy import get_legacy_adapter
from adcp.compat.legacy.v2_5 import list_creative_formats as v2_5_lcf


def _normalize(response: Any) -> Any:
    assert v2_5_lcf.ADAPTER.normalize_response is not None
    return v2_5_lcf.ADAPTER.normalize_response(response)


def test_adapter_registered_for_v2_5() -> None:
    adapter = get_legacy_adapter("2.5", "list_creative_formats")
    assert adapter is not None
    assert adapter.tool_name == "list_creative_formats"


def test_adapt_request_is_pass_through() -> None:
    payload = {"adcp_version": "2.5", "filter": {"x": 1}}
    out = v2_5_lcf.ADAPTER.adapt_request(payload)
    assert out == payload
    # Contract: must not mutate input.
    assert out is not payload


def test_normalize_response_wraps_v2_dimensions_into_renders() -> None:
    response = {
        "formats": [
            {"format_id": "display_300x250", "width": 300, "height": 250},
        ]
    }
    out = _normalize(response)
    assert out["formats"][0]["renders"] == [
        {
            "render_id": "primary",
            "role": "primary",
            "dimensions": {"width": 300, "height": 250},
        }
    ]
    # v2 fields stripped.
    assert "width" not in out["formats"][0]
    assert "height" not in out["formats"][0]


def test_normalize_response_uses_existing_dimensions_dict() -> None:
    response = {
        "formats": [
            {"format_id": "x", "dimensions": {"width": 728, "height": 90}},
        ]
    }
    out = _normalize(response)
    assert out["formats"][0]["renders"][0]["dimensions"] == {"width": 728, "height": 90}


def test_normalize_response_passes_v3_renders_through() -> None:
    response = {
        "formats": [
            {
                "format_id": "x",
                "renders": [
                    {
                        "render_id": "primary",
                        "role": "primary",
                        "dimensions": {"width": 300, "height": 250},
                    }
                ],
            }
        ]
    }
    out = _normalize(response)
    # Idempotent — already v3 shape, no change.
    assert out == response


def test_normalize_response_handles_template_formats_no_dims() -> None:
    """Template formats with ``accepts_parameters`` have no dimensions
    at format build time. Leave them untouched."""
    response = {
        "formats": [
            {"format_id": "html_template", "accepts_parameters": True},
        ]
    }
    out = _normalize(response)
    assert "renders" not in out["formats"][0]
    assert out["formats"][0]["accepts_parameters"] is True


def test_normalize_response_handles_raw_array() -> None:
    """Agent omits the ``{formats: [...]}`` wrapper — adapter wraps it."""
    response = [{"format_id": "x", "width": 300, "height": 250}]
    out = _normalize(response)
    assert out == {
        "formats": [
            {
                "format_id": "x",
                "renders": [
                    {
                        "render_id": "primary",
                        "role": "primary",
                        "dimensions": {"width": 300, "height": 250},
                    }
                ],
            }
        ]
    }


def test_normalize_response_passes_through_when_no_formats_key() -> None:
    response = {"error": "no formats"}
    out = _normalize(response)
    assert out == response


def test_normalize_response_preserves_other_format_fields() -> None:
    response = {
        "formats": [
            {
                "format_id": "x",
                "width": 300,
                "height": 250,
                "delivery": "guaranteed",
                "metadata": {"foo": "bar"},
            }
        ]
    }
    out = _normalize(response)
    fmt = out["formats"][0]
    assert fmt["delivery"] == "guaranteed"
    assert fmt["metadata"] == {"foo": "bar"}
    assert fmt["format_id"] == "x"


def test_normalize_response_partial_dims_only_width() -> None:
    """v2 width with no height — emit dimensions with height=0."""
    response = {"formats": [{"format_id": "x", "width": 300}]}
    out = _normalize(response)
    assert out["formats"][0]["renders"][0]["dimensions"] == {"width": 300, "height": 0}
