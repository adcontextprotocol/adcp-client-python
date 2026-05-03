"""Tests for the v3-shape detection heuristic and its wire-up in
``ADCPClient.refresh_capabilities``.

The heuristic exists so a single failed schema validation on
``get_adcp_capabilities`` doesn't silently re-classify a v3 agent as v2 —
which downstream tooling turns into the cascade of confusing
"AdCP schema data for version v2.5 not found" errors. Port of JS commit
27bd79d (#1201). See issue #461.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from adcp import ADCPClient
from adcp.capabilities import looks_like_v3_capabilities
from adcp.exceptions import ADCPError
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus


def _make_config() -> AgentConfig:
    return AgentConfig(
        id="test-seller",
        agent_uri="https://seller.example.com",
        protocol=Protocol.A2A,
    )


# ============================================================================
# looks_like_v3_capabilities — pure heuristic
# ============================================================================


class TestLooksLikeV3CapabilitiesPositive:
    """The heuristic recognizes any one v3-shape signal."""

    def test_detects_adcp_envelope_block(self):
        assert looks_like_v3_capabilities({"adcp": {"major_versions": [3]}}) is True

    def test_detects_supported_protocols_array(self):
        assert looks_like_v3_capabilities({"supported_protocols": ["signals"]}) is True

    def test_detects_empty_supported_protocols_array(self):
        # Field presence alone is a v3 signal — v2 doesn't have this top-level field.
        assert looks_like_v3_capabilities({"supported_protocols": []}) is True

    def test_detects_account_block(self):
        assert looks_like_v3_capabilities({"account": {"require_operator_auth": True}}) is True

    def test_detects_media_buy_block(self):
        assert looks_like_v3_capabilities({"media_buy": {"features": {}}}) is True

    def test_detects_signals_block(self):
        assert looks_like_v3_capabilities({"signals": {"catalog_signals": True}}) is True

    def test_detects_creative_block(self):
        assert looks_like_v3_capabilities({"creative": {"supports_compliance": True}}) is True

    def test_detects_brand_block(self):
        assert looks_like_v3_capabilities({"brand": {"rights": True}}) is True

    def test_detects_governance_block(self):
        assert looks_like_v3_capabilities({"governance": {"spend_authority": True}}) is True

    def test_detects_sponsored_intelligence_block(self):
        assert looks_like_v3_capabilities({"sponsored_intelligence": {"offerings": []}}) is True

    def test_detects_compliance_testing_block(self):
        assert looks_like_v3_capabilities({"compliance_testing": {"scenarios": []}}) is True

    def test_detects_partial_v3_response_with_one_missing_field(self):
        """The exact case that surfaced this issue: a v3 agent missing one
        required field — adcp envelope present, supported_protocols present,
        account present but missing supported_billing.
        """
        assert (
            looks_like_v3_capabilities(
                {
                    "adcp": {"major_versions": [3]},
                    "supported_protocols": ["signals"],
                    "account": {"require_operator_auth": True},
                }
            )
            is True
        )


class TestLooksLikeV3CapabilitiesNegative:
    """The heuristic rejects empty / non-dict / shape-mismatched inputs."""

    def test_rejects_none(self):
        assert looks_like_v3_capabilities(None) is False

    def test_rejects_empty_dict(self):
        assert looks_like_v3_capabilities({}) is False

    def test_rejects_list(self):
        assert looks_like_v3_capabilities([]) is False

    def test_rejects_string(self):
        assert looks_like_v3_capabilities("v3") is False

    def test_rejects_number(self):
        assert looks_like_v3_capabilities(42) is False

    def test_rejects_object_with_only_unknown_fields(self):
        assert looks_like_v3_capabilities({"foo": "bar", "baz": 1}) is False

    def test_rejects_supported_protocols_when_not_an_array(self):
        # Malformed — string instead of array. Don't promote to v3.
        assert looks_like_v3_capabilities({"supported_protocols": "signals"}) is False

    def test_rejects_v3_block_when_null(self):
        assert looks_like_v3_capabilities({"media_buy": None}) is False

    def test_rejects_adcp_when_null(self):
        assert looks_like_v3_capabilities({"adcp": None}) is False

    def test_rejects_adcp_when_array(self):
        # Defensive: arrays are not plain objects.
        assert looks_like_v3_capabilities({"adcp": []}) is False

    def test_rejects_v3_block_when_array(self):
        assert looks_like_v3_capabilities({"media_buy": []}) is False


# ============================================================================
# refresh_capabilities — wire-up
# ============================================================================


def _success_capabilities_dict() -> dict:
    """A clean v3 capabilities dict that passes strict schema validation."""
    return {
        "adcp": {
            "major_versions": [3],
            "idempotency": {"supported": True, "replay_ttl_seconds": 86400},
        },
        "supported_protocols": ["media_buy"],
    }


def _broken_v3_capabilities_dict() -> dict:
    """A response that is structurally v3-shaped but fails strict validation
    — has the v3 envelope but a non-validating supported_protocols entry.
    """
    return {
        "adcp": {
            "major_versions": [3],
            "idempotency": {"supported": True, "replay_ttl_seconds": 86400},
        },
        "supported_protocols": ["not-a-real-protocol"],
        "account": {"require_operator_auth": True},
    }


def _v2_shaped_response_dict() -> dict:
    """A response that has none of the v3 signals — heuristic returns False."""
    return {"some_legacy_field": "value", "tools": []}


class TestRefreshCapabilitiesV3Detection:
    """Wire-up: refresh_capabilities surfaces v3 validation errors loudly."""

    @pytest.mark.asyncio
    async def test_clean_v3_response_parses_unchanged(self):
        """Clean v3 capabilities → fetched and cached, no warnings."""
        client = ADCPClient(_make_config())

        raw = TaskResult(
            status=TaskStatus.COMPLETED,
            data=_success_capabilities_dict(),
            success=True,
        )
        with patch.object(
            client.adapter, "get_adcp_capabilities", new_callable=AsyncMock
        ) as mock_adapter:
            mock_adapter.return_value = raw

            caps = await client.refresh_capabilities()

        assert caps is not None
        # Cached.
        assert client.capabilities is caps
        assert client.feature_resolver is not None

    @pytest.mark.asyncio
    async def test_broken_v3_response_raises_loud_v3_error(self):
        """V3-shaped response with a validation bug → loud v3 error.

        The error message must reference v3 explicitly so downstream users
        don't waste time looking for a v2.5-schema-not-found cascade — the
        underlying problem is a wire-shape bug in the agent.
        """
        client = ADCPClient(_make_config())
        broken_dict = _broken_v3_capabilities_dict()

        # Adapter returns the broken dict twice: once for the typed call (which
        # fails validation downstream), once for the re-fetch that lets the
        # heuristic inspect the raw shape.
        raw = TaskResult(
            status=TaskStatus.COMPLETED,
            data=broken_dict,
            success=True,
        )
        with patch.object(
            client.adapter, "get_adcp_capabilities", new_callable=AsyncMock
        ) as mock_adapter:
            mock_adapter.return_value = raw

            with pytest.raises(ADCPError) as exc_info:
                await client.refresh_capabilities()

        msg = str(exc_info.value)
        # Must reference v3 explicitly — that's the whole point.
        assert "v3" in msg
        # Must NOT trigger the v2.5-schema-not-found cascade or claim v2.5.
        assert "v2.5" not in msg
        # Must reference schema validation so the fix-it path is obvious.
        assert "validation" in msg.lower() or "schema" in msg.lower()

    @pytest.mark.asyncio
    async def test_genuine_v2_response_falls_through_to_transport_error(self):
        """Genuine non-v3 response on validation failure → existing error path.

        This is the "still downgrades correctly" lane: the heuristic returns
        False, so the v3-loud branch is skipped and the existing
        "Failed to fetch capabilities" error is raised. (The Python client
        doesn't ship a v2-synthetic auto-fallback inside refresh_capabilities;
        callers wanting one use ``build_synthetic_capabilities`` directly.)
        """
        client = ADCPClient(_make_config())
        v2_dict = _v2_shaped_response_dict()

        raw = TaskResult(
            status=TaskStatus.COMPLETED,
            data=v2_dict,
            success=True,
        )
        with patch.object(
            client.adapter, "get_adcp_capabilities", new_callable=AsyncMock
        ) as mock_adapter:
            mock_adapter.return_value = raw

            with pytest.raises(ADCPError) as exc_info:
                await client.refresh_capabilities()

        msg = str(exc_info.value)
        # Must NOT claim v3 — heuristic correctly identified this as not-v3.
        assert "v3" not in msg
        # Hits the original transport-style error.
        assert "Failed to fetch capabilities" in msg

    @pytest.mark.asyncio
    async def test_transport_failure_with_no_data_still_raises(self):
        """Original transport-failure path is preserved (no re-fetch attempt)."""
        client = ADCPClient(_make_config())

        failed = TaskResult(
            status=TaskStatus.FAILED,
            data=None,
            success=False,
            error="Connection refused",
        )
        with patch.object(
            client.adapter, "get_adcp_capabilities", new_callable=AsyncMock
        ) as mock_adapter:
            mock_adapter.return_value = failed

            with pytest.raises(ADCPError, match="Failed to fetch capabilities"):
                await client.refresh_capabilities()

        # Adapter is called exactly once — no second probe for raw shape on
        # transport-style failures (no data ever arrived).
        assert mock_adapter.call_count == 1
