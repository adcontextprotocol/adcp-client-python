"""Tests for the v2.5 → v3 ``preview_creative`` adapter."""

from __future__ import annotations

from typing import Any

from adcp.compat.legacy import get_legacy_adapter
from adcp.compat.legacy.v2_5 import preview_creative as v2_5_pc


def _normalize(response: Any) -> Any:
    assert v2_5_pc.ADAPTER.normalize_response is not None
    return v2_5_pc.ADAPTER.normalize_response(response)


def test_adapter_registered_for_v2_5() -> None:
    adapter = get_legacy_adapter("2.5", "preview_creative")
    assert adapter is not None
    assert adapter.tool_name == "preview_creative"


def test_adapt_request_is_pass_through() -> None:
    payload = {"adcp_version": "2.5", "creative_id": "c1"}
    out = v2_5_pc.ADAPTER.adapt_request(payload)
    assert out == payload
    assert out is not payload  # shallow copy


def test_normalize_response_renames_v2_render_fields() -> None:
    response = {
        "previews": [
            {
                "preview_id": "p1",
                "renders": [
                    {
                        "output_id": "main",
                        "output_role": "primary",
                        "output_format": "url",
                        "preview_url": "https://example.com/p",
                    }
                ],
                "expires_at": "2026-01-01T00:00:00Z",
            }
        ]
    }
    out = _normalize(response)
    render = out["previews"][0]["renders"][0]
    assert render["render_id"] == "main"
    assert render["role"] == "primary"
    # v2 names dropped.
    assert "output_id" not in render
    assert "output_role" not in render
    # Other fields preserved.
    assert render["preview_url"] == "https://example.com/p"
    assert render["output_format"] == "url"


def test_normalize_response_prefers_v3_names_when_both_present() -> None:
    """A half-migrated server may emit both v2 and v3 field names —
    v3 wins to prevent double-translation issues."""
    response = {
        "previews": [
            {
                "preview_id": "p1",
                "renders": [
                    {
                        "render_id": "v3id",
                        "output_id": "v2id",
                        "role": "v3role",
                        "output_role": "v2role",
                        "output_format": "url",
                    }
                ],
                "expires_at": "2026-01-01T00:00:00Z",
            }
        ]
    }
    out = _normalize(response)
    render = out["previews"][0]["renders"][0]
    assert render["render_id"] == "v3id"
    assert render["role"] == "v3role"


def test_normalize_response_defaults_to_primary_when_missing() -> None:
    response = {
        "previews": [
            {
                "preview_id": "p1",
                "renders": [{"output_format": "url"}],
                "expires_at": "2026-01-01T00:00:00Z",
            }
        ]
    }
    out = _normalize(response)
    render = out["previews"][0]["renders"][0]
    assert render["render_id"] == "primary"
    assert render["role"] == "primary"


def test_normalize_response_handles_batch_results() -> None:
    response = {
        "results": [
            {
                "success": True,
                "response": {
                    "previews": [
                        {
                            "preview_id": "p1",
                            "renders": [
                                {
                                    "output_id": "main",
                                    "output_role": "primary",
                                    "output_format": "url",
                                }
                            ],
                            "expires_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                },
            }
        ]
    }
    out = _normalize(response)
    render = out["results"][0]["response"]["previews"][0]["renders"][0]
    assert render["render_id"] == "main"
    assert render["role"] == "primary"


def test_normalize_response_passes_failed_batch_entries_through() -> None:
    response = {
        "results": [
            {"success": False, "error": "boom"},
            {
                "success": True,
                "response": {
                    "previews": [
                        {
                            "preview_id": "p2",
                            "renders": [{"output_id": "x", "output_format": "url"}],
                            "expires_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                },
            },
        ]
    }
    out = _normalize(response)
    assert out["results"][0] == {"success": False, "error": "boom"}
    assert out["results"][1]["response"]["previews"][0]["renders"][0]["render_id"] == "x"


def test_normalize_response_passes_through_unknown_shape() -> None:
    response = {"something_else": True}
    out = _normalize(response)
    assert out == response


def test_normalize_response_preserves_other_render_fields() -> None:
    response = {
        "previews": [
            {
                "preview_id": "p1",
                "renders": [
                    {
                        "output_id": "main",
                        "output_role": "primary",
                        "output_format": "both",
                        "preview_url": "https://example.com/p",
                        "preview_html": "<div/>",
                        "dimensions": {"width": 300, "height": 250},
                        "embedding": {"requires_https": True},
                    }
                ],
                "expires_at": "2026-01-01T00:00:00Z",
            }
        ]
    }
    out = _normalize(response)
    render = out["previews"][0]["renders"][0]
    assert render["preview_html"] == "<div/>"
    assert render["dimensions"] == {"width": 300, "height": 250}
    assert render["embedding"] == {"requires_https": True}
