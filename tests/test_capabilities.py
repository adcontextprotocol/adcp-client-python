"""Tests for feature capability validation: supports/require API."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from adcp import AccountReferenceById, ADCPClient, SyncEventSourcesRequest
from adcp.capabilities import FeatureResolver, validate_capabilities
from adcp.exceptions import ADCPError, ADCPFeatureUnsupportedError
from adcp.server.base import ADCPHandler
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus
from adcp.types.generated_poc.core.media_buy_features import MediaBuyFeatures
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    Adcp as AdcpInfo,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    Execution,
    ExtensionsSupportedItem,
    GetAdcpCapabilitiesResponse,
    Idempotency,
    MajorVersion,
    MediaBuy,
    Signals,
    SupportedProtocol,
    Targeting,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    Features as SignalsFeatures,
)


def _make_config() -> AgentConfig:
    return AgentConfig(
        id="test-seller",
        agent_uri="https://seller.example.com",
        protocol=Protocol.A2A,
    )


def _make_capabilities(
    *,
    protocols: list[str] | None = None,
    media_buy_features: dict | None = None,
    signals_features: dict | None = None,
    extensions: list[str] | None = None,
    targeting: dict | None = None,
) -> GetAdcpCapabilitiesResponse:
    """Build a capabilities response for testing."""
    protocol_list = protocols or ["media_buy"]
    supported = [SupportedProtocol(p) for p in protocol_list]

    media_buy = None
    if "media_buy" in protocol_list:
        mb_kwargs: dict = {}
        if media_buy_features is not None:
            mb_kwargs["features"] = MediaBuyFeatures(**media_buy_features)
        if targeting is not None:
            mb_kwargs["execution"] = Execution(targeting=Targeting(**targeting))
        media_buy = MediaBuy(**mb_kwargs)

    signals = None
    if "signals" in protocol_list:
        sig_kwargs: dict = {}
        if signals_features is not None:
            sig_kwargs["features"] = SignalsFeatures(**signals_features)
        signals = Signals(**sig_kwargs)

    extensions_supported = None
    if extensions is not None:
        extensions_supported = [ExtensionsSupportedItem(root=e) for e in extensions]

    return GetAdcpCapabilitiesResponse(
        adcp=AdcpInfo(
            major_versions=[MajorVersion(root=3)],
            idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
        ),
        supported_protocols=supported,
        media_buy=media_buy,
        signals=signals,
        extensions_supported=extensions_supported,
    )


def _make_task_result(caps: GetAdcpCapabilitiesResponse) -> TaskResult:
    return TaskResult(
        status=TaskStatus.COMPLETED,
        data=caps,
        success=True,
    )


def _client_with_caps(
    caps: GetAdcpCapabilitiesResponse,
    **kwargs,
) -> ADCPClient:
    """Create a client and inject cached capabilities."""
    client = ADCPClient(_make_config(), **kwargs)
    client._capabilities = caps
    client._feature_resolver = FeatureResolver(caps)
    client._capabilities_fetched_at = time.monotonic()
    return client


# ========================================================================
# supports() tests
# ========================================================================


class TestSupports:
    """Tests for seller.supports(feature)."""

    def test_media_buy_feature_true(self):
        caps = _make_capabilities(
            media_buy_features={"inline_creative_management": True, "property_list_filtering": True}
        )
        client = _client_with_caps(caps)

        assert client.supports("inline_creative_management") is True
        assert client.supports("property_list_filtering") is True

    def test_media_buy_feature_false(self):
        caps = _make_capabilities(media_buy_features={"inline_creative_management": False})
        client = _client_with_caps(caps)

        assert client.supports("inline_creative_management") is False

    def test_media_buy_feature_absent(self):
        caps = _make_capabilities(media_buy_features={})
        client = _client_with_caps(caps)

        assert client.supports("inline_creative_management") is False

    def test_media_buy_features_none(self):
        """When media_buy exists but features is None."""
        caps = _make_capabilities(protocols=["media_buy"])
        client = _client_with_caps(caps)

        assert client.supports("inline_creative_management") is False

    def test_signals_feature_true(self):
        caps = _make_capabilities(
            protocols=["media_buy", "signals"],
            signals_features={"catalog_signals": True},
        )
        client = _client_with_caps(caps)

        assert client.supports("catalog_signals") is True

    def test_signals_feature_false(self):
        caps = _make_capabilities(
            protocols=["media_buy", "signals"],
            signals_features={"catalog_signals": False},
        )
        client = _client_with_caps(caps)

        assert client.supports("catalog_signals") is False

    def test_protocol_support(self):
        caps = _make_capabilities(protocols=["media_buy", "signals"])
        client = _client_with_caps(caps)

        assert client.supports("media_buy") is True
        assert client.supports("signals") is True
        assert client.supports("governance") is False
        assert client.supports("creative") is False

    def test_extension_support(self):
        caps = _make_capabilities(extensions=["scope3", "garm"])
        client = _client_with_caps(caps)

        assert client.supports("ext:scope3") is True
        assert client.supports("ext:garm") is True
        assert client.supports("ext:iab_tcf") is False

    def test_extension_no_extensions_declared(self):
        caps = _make_capabilities()
        client = _client_with_caps(caps)

        assert client.supports("ext:scope3") is False

    def test_targeting_support(self):
        caps = _make_capabilities(targeting={"geo_countries": True, "language": True})
        client = _client_with_caps(caps)

        assert client.supports("targeting.geo_countries") is True
        assert client.supports("targeting.language") is True
        assert client.supports("targeting.geo_regions") is False

    def test_targeting_no_execution(self):
        caps = _make_capabilities()
        client = _client_with_caps(caps)

        assert client.supports("targeting.geo_countries") is False

    def test_targeting_nonexistent_field(self):
        """Targeting field not in model_fields returns False."""
        caps = _make_capabilities(targeting={"geo_countries": True})
        client = _client_with_caps(caps)

        assert client.supports("targeting.nonexistent_field") is False
        assert client.supports("targeting.__class__") is False

    def test_model_fields_guard_on_features(self):
        """Pydantic internals are not treated as features."""
        caps = _make_capabilities(media_buy_features={"inline_creative_management": True})
        client = _client_with_caps(caps)

        assert client.supports("model_dump") is False
        assert client.supports("model_fields") is False
        assert client.supports("__class__") is False

    def test_unknown_feature_returns_false(self):
        caps = _make_capabilities(media_buy_features={"inline_creative_management": True})
        client = _client_with_caps(caps)

        assert client.supports("completely_bogus_feature") is False
        assert client.supports("") is False

    def test_no_capabilities_raises(self):
        client = ADCPClient(_make_config())

        with pytest.raises(ADCPError, match="capabilities have not been fetched"):
            client.supports("inline_creative_management")

    @pytest.mark.asyncio
    async def test_fetch_then_supports(self):
        """fetch_capabilities() then supports() works."""
        caps = _make_capabilities(media_buy_features={"inline_creative_management": True})
        client = ADCPClient(_make_config())

        with patch.object(client, "get_adcp_capabilities", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _make_task_result(caps)
            await client.fetch_capabilities()

        assert client.supports("inline_creative_management") is True
        mock_get.assert_called_once()


# ========================================================================
# require() tests
# ========================================================================


class TestRequire:
    """Tests for seller.require(*features)."""

    def test_require_no_features_is_noop(self):
        """require() with zero arguments does not raise."""
        caps = _make_capabilities()
        client = _client_with_caps(caps)
        client.require()  # Should not raise

    def test_require_all_present(self):
        caps = _make_capabilities(
            media_buy_features={"inline_creative_management": True, "property_list_filtering": True}
        )
        client = _client_with_caps(caps)

        # Should not raise
        client.require("inline_creative_management", "property_list_filtering")

    def test_require_missing_single(self):
        caps = _make_capabilities(media_buy_features={"property_list_filtering": True})
        client = _client_with_caps(caps)

        with pytest.raises(ADCPFeatureUnsupportedError) as exc_info:
            client.require("inline_creative_management")

        error = exc_info.value
        assert "inline_creative_management" in error.message
        assert error.unsupported_features == ["inline_creative_management"]

    def test_require_missing_multiple(self):
        caps = _make_capabilities(media_buy_features={"inline_creative_management": True})
        client = _client_with_caps(caps)

        with pytest.raises(ADCPFeatureUnsupportedError) as exc_info:
            client.require("property_list_filtering", "catalog_management")

        error = exc_info.value
        assert "property_list_filtering" in error.message
        assert "catalog_management" in error.message
        assert set(error.unsupported_features) == {
            "property_list_filtering",
            "catalog_management",
        }

    def test_require_mixed_namespaces(self):
        """require() works across protocols, extensions, and features."""
        caps = _make_capabilities(
            protocols=["media_buy", "signals"],
            media_buy_features={"inline_creative_management": True},
            extensions=["scope3"],
        )
        client = _client_with_caps(caps)

        # Should not raise
        client.require("media_buy", "inline_creative_management", "ext:scope3")

    def test_require_error_lists_declared_features(self):
        """Error message includes what the seller does support."""
        caps = _make_capabilities(
            media_buy_features={
                "inline_creative_management": True,
                "property_list_filtering": True,
            }
        )
        client = _client_with_caps(caps)

        with pytest.raises(ADCPFeatureUnsupportedError) as exc_info:
            client.require("catalog_management")

        error_str = str(exc_info.value)
        assert "inline_creative_management" in error_str and "property_list_filtering" in error_str

    def test_require_includes_agent_context(self):
        caps = _make_capabilities()
        client = _client_with_caps(caps)

        with pytest.raises(ADCPFeatureUnsupportedError) as exc_info:
            client.require("inline_creative_management")

        error = exc_info.value
        assert error.agent_id == "test-seller"
        assert error.agent_uri == "https://seller.example.com"


# ========================================================================
# Capabilities caching tests
# ========================================================================


class TestCapabilitiesCaching:
    """Tests for capabilities caching and refresh."""

    @pytest.mark.asyncio
    async def test_fetch_capabilities_caches(self):
        caps = _make_capabilities(media_buy_features={"inline_creative_management": True})
        client = ADCPClient(_make_config())

        with patch.object(client, "get_adcp_capabilities", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _make_task_result(caps)

            await client.fetch_capabilities()
            await client.fetch_capabilities()

        # Should only call once due to caching
        mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_capabilities_bypasses_cache(self):
        caps = _make_capabilities(media_buy_features={"inline_creative_management": True})
        client = ADCPClient(_make_config())

        with patch.object(client, "get_adcp_capabilities", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _make_task_result(caps)

            await client.fetch_capabilities()
            await client.refresh_capabilities()

        assert mock_get.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self):
        caps = _make_capabilities(media_buy_features={"inline_creative_management": True})
        client = ADCPClient(_make_config(), capabilities_ttl=0.0)

        with patch.object(client, "get_adcp_capabilities", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _make_task_result(caps)

            await client.fetch_capabilities()
            # TTL is 0, so next fetch should re-fetch
            await client.fetch_capabilities()

        assert mock_get.call_count == 2

    def test_capabilities_property(self):
        caps = _make_capabilities()
        client = _client_with_caps(caps)

        assert client.capabilities is caps

    def test_capabilities_property_none_when_not_fetched(self):
        client = ADCPClient(_make_config())
        assert client.capabilities is None

    @pytest.mark.asyncio
    async def test_refresh_capabilities_failure_raises(self):
        client = ADCPClient(_make_config())

        failed_result = TaskResult(
            status=TaskStatus.FAILED,
            data=None,
            success=False,
            error="Connection refused",
        )
        with patch.object(client, "get_adcp_capabilities", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = failed_result

            with pytest.raises(ADCPError, match="Failed to fetch capabilities"):
                await client.refresh_capabilities()

    @pytest.mark.asyncio
    async def test_fetch_capabilities_success_true_data_none(self):
        """success=True but data=None still raises."""
        client = ADCPClient(_make_config())

        result = TaskResult(
            status=TaskStatus.COMPLETED,
            data=None,
            success=True,
        )
        with patch.object(client, "get_adcp_capabilities", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = result

            with pytest.raises(ADCPError, match="Failed to fetch capabilities"):
                await client.fetch_capabilities()


# ========================================================================
# Automatic validation (validate_features) tests
# ========================================================================


class TestValidateFeatures:
    """Tests for automatic feature validation on task calls."""

    def test_validate_skips_unmapped_task(self):
        """Tasks not in TASK_FEATURE_MAP are not validated."""
        caps = _make_capabilities(media_buy_features={})
        client = _client_with_caps(caps, validate_features=True)
        # get_products is not in TASK_FEATURE_MAP, should not raise
        client._validate_task_features("get_products")

    @pytest.mark.asyncio
    async def test_sync_event_sources_requires_property_list_filtering(self):
        caps = _make_capabilities(media_buy_features={"inline_creative_management": True})
        client = _client_with_caps(caps, validate_features=True)

        request = SyncEventSourcesRequest(
            idempotency_key="test-idempotency-key",
            account=AccountReferenceById(account_id="acc1"),
        )

        with pytest.raises(ADCPFeatureUnsupportedError, match="property_list_filtering"):
            await client.sync_event_sources(request)

    @pytest.mark.asyncio
    async def test_log_event_requires_property_list_filtering(self):
        """validate_features raises before request validation."""
        caps = _make_capabilities(media_buy_features={"inline_creative_management": True})
        client = _client_with_caps(caps, validate_features=True)

        # Use _validate_task_features directly since LogEventRequest
        # requires events with min_length=1
        with pytest.raises(ADCPFeatureUnsupportedError, match="property_list_filtering"):
            client._validate_task_features("log_event")

    @pytest.mark.asyncio
    async def test_validation_passes_when_feature_supported(self):
        """When the feature is supported, the call proceeds normally."""
        caps = _make_capabilities(media_buy_features={"property_list_filtering": True})
        client = _client_with_caps(caps, validate_features=True)

        mock_result = TaskResult(
            status=TaskStatus.COMPLETED,
            data={"event_sources": []},
            success=True,
        )
        with patch.object(client.adapter, "sync_event_sources", new_callable=AsyncMock) as mock:
            mock.return_value = mock_result
            result = await client.sync_event_sources(
                SyncEventSourcesRequest(
                    idempotency_key="test-idempotency-key",
                    account=AccountReferenceById(account_id="acc1"),
                )
            )
        assert result is not None

    @pytest.mark.asyncio
    async def test_validation_skipped_when_not_opted_in(self):
        """When validate_features is False (default), no validation."""
        caps = _make_capabilities(media_buy_features={})  # No features declared
        client = _client_with_caps(caps, validate_features=False)

        mock_result = TaskResult(
            status=TaskStatus.COMPLETED,
            data={"event_sources": []},
            success=True,
        )
        with patch.object(client.adapter, "sync_event_sources", new_callable=AsyncMock) as mock:
            mock.return_value = mock_result
            # Should NOT raise even though property_list_filtering is not declared
            result = await client.sync_event_sources(
                SyncEventSourcesRequest(
                    idempotency_key="test-idempotency-key",
                    account=AccountReferenceById(account_id="acc1"),
                )
            )
        assert result is not None

    @pytest.mark.asyncio
    async def test_validation_skipped_when_no_capabilities(self):
        """When capabilities haven't been fetched, skip validation."""
        client = ADCPClient(_make_config(), validate_features=True)

        mock_result = TaskResult(
            status=TaskStatus.COMPLETED,
            data={"event_sources": []},
            success=True,
        )
        with patch.object(client.adapter, "sync_event_sources", new_callable=AsyncMock) as mock:
            mock.return_value = mock_result
            # Should NOT raise even though no capabilities cached
            result = await client.sync_event_sources(
                SyncEventSourcesRequest(
                    idempotency_key="test-idempotency-key",
                    account=AccountReferenceById(account_id="acc1"),
                )
            )
        assert result is not None


# ========================================================================
# ADCPFeatureUnsupportedError exception tests
# ========================================================================


class TestADCPFeatureUnsupportedError:
    """Tests for the ADCPFeatureUnsupportedError exception."""

    def test_exception_fields(self):
        error = ADCPFeatureUnsupportedError(
            unsupported_features=["inline_creative_management"],
            declared_features=["property_list_filtering", "inline_creative_management"],
            agent_id="test-seller",
            agent_uri="https://seller.example.com",
        )

        assert error.unsupported_features == ["inline_creative_management"]
        assert error.declared_features == [
            "property_list_filtering",
            "inline_creative_management",
        ]
        assert error.agent_id == "test-seller"
        assert error.agent_uri == "https://seller.example.com"

    def test_exception_message_format(self):
        error = ADCPFeatureUnsupportedError(
            unsupported_features=["inline_creative_management"],
            declared_features=["property_list_filtering"],
            agent_id="test-seller",
            agent_uri="https://seller.example.com",
        )

        msg = str(error)
        assert "inline_creative_management" in msg
        assert "test-seller" in msg
        assert "seller.example.com" in msg
        assert "property_list_filtering" in msg

    def test_exception_inherits_from_adcp_error(self):
        error = ADCPFeatureUnsupportedError(
            unsupported_features=["x"],
        )
        assert isinstance(error, ADCPError)


# ========================================================================
# FeatureResolver standalone tests
# ========================================================================


class TestFeatureResolver:
    """Tests for FeatureResolver used independently of ADCPClient."""

    def test_resolver_supports(self):
        caps = _make_capabilities(
            protocols=["media_buy", "signals"],
            media_buy_features={"inline_creative_management": True},
            extensions=["scope3"],
            targeting={"geo_countries": True},
        )
        resolver = FeatureResolver(caps)

        assert resolver.supports("media_buy") is True
        assert resolver.supports("inline_creative_management") is True
        assert resolver.supports("ext:scope3") is True
        assert resolver.supports("targeting.geo_countries") is True
        assert resolver.supports("property_list_filtering") is False

    def test_resolver_require_raises(self):
        caps = _make_capabilities(media_buy_features={})
        resolver = FeatureResolver(caps)

        with pytest.raises(ADCPFeatureUnsupportedError) as exc_info:
            resolver.require("inline_creative_management", agent_id="test")

        assert exc_info.value.agent_id == "test"

    def test_resolver_get_declared_features(self):
        caps = _make_capabilities(
            protocols=["media_buy"],
            media_buy_features={
                "inline_creative_management": True,
                "property_list_filtering": True,
            },
            extensions=["scope3"],
            targeting={"geo_countries": True},
        )
        resolver = FeatureResolver(caps)

        declared = resolver.get_declared_features()
        assert "media_buy" in declared
        assert "inline_creative_management" in declared
        assert "property_list_filtering" in declared
        assert "ext:scope3" in declared
        assert "targeting.geo_countries" in declared

    def test_resolver_capabilities_property(self):
        caps = _make_capabilities()
        resolver = FeatureResolver(caps)
        assert resolver.capabilities is caps


# ========================================================================
# Server-side validate_capabilities tests
# ========================================================================


class TestValidateCapabilities:
    """Tests for server-side validate_capabilities()."""

    def test_warns_on_declared_but_unimplemented(self):

        class MyHandler(ADCPHandler):
            pass  # Doesn't override anything

        caps = _make_capabilities(media_buy_features={"property_list_filtering": True})

        warnings = validate_capabilities(MyHandler(), caps)
        assert len(warnings) > 0
        assert any("property_list_filtering" in w for w in warnings)
        assert any("log_event" in w or "sync_event_sources" in w for w in warnings)

    def test_no_warnings_when_handler_overrides(self):

        class MyHandler(ADCPHandler):
            async def log_event(self, params, context=None):
                return {"status": "ok"}

            async def sync_event_sources(self, params, context=None):
                return {"status": "ok"}

        caps = _make_capabilities(media_buy_features={"property_list_filtering": True})

        warnings = validate_capabilities(MyHandler(), caps)
        assert len(warnings) == 0

    def test_no_warnings_when_feature_not_declared(self):

        class MyHandler(ADCPHandler):
            pass

        caps = _make_capabilities(media_buy_features={})  # No features declared

        warnings = validate_capabilities(MyHandler(), caps)
        assert len(warnings) == 0

    def test_warns_on_partial_implementation(self):
        """If only some handler methods for a feature are overridden."""

        class MyHandler(ADCPHandler):
            async def log_event(self, params, context=None):
                return {"status": "ok"}

            # sync_event_sources NOT overridden

        caps = _make_capabilities(media_buy_features={"property_list_filtering": True})

        warnings = validate_capabilities(MyHandler(), caps)
        assert len(warnings) == 1
        assert "sync_event_sources" in warnings[0]

    def test_no_warnings_when_mixin_overrides(self):
        """Overrides inherited from an intermediate class are detected."""

        class ConversionMixin(ADCPHandler):
            async def log_event(self, params, context=None):
                return {"status": "ok"}

            async def sync_event_sources(self, params, context=None):
                return {"status": "ok"}

        class MyHandler(ConversionMixin):
            pass  # Inherits overrides from mixin

        caps = _make_capabilities(media_buy_features={"property_list_filtering": True})

        warnings = validate_capabilities(MyHandler(), caps)
        assert len(warnings) == 0

    def test_multiple_handler_methods_warned(self):
        """All handler methods for a declared feature produce warnings when unoverridden."""

        class MyHandler(ADCPHandler):
            pass

        caps = _make_capabilities(media_buy_features={"property_list_filtering": True})

        warnings = validate_capabilities(MyHandler(), caps)
        method_names = {w.split("'")[3] for w in warnings}
        assert "log_event" in method_names
        assert "sync_event_sources" in method_names


class TestTaskFeatureMapCompleteness:
    """Verify TASK_FEATURE_MAP covers all expected domains."""

    def test_all_expected_domains_present(self) -> None:
        from adcp.capabilities import TASK_FEATURE_MAP

        domains = set(TASK_FEATURE_MAP.values())
        expected = {
            "property_list_filtering",
            "inline_creative_management",
            "catalog_management",
            "content_standards",
            "signals",
            "creative_agent",
            "campaign_governance",
            "property_lists",
            "collection_lists",
            "trusted_match",
            "sponsored_intelligence",
            "brand",
        }
        assert expected <= domains, f"Missing domains: {expected - domains}"

    def test_all_values_are_strings(self) -> None:
        from adcp.capabilities import TASK_FEATURE_MAP

        for task, feature in TASK_FEATURE_MAP.items():
            assert isinstance(task, str), f"Task key {task} is not a string"
            assert isinstance(feature, str), f"Feature {feature} is not a string"


class TestBuildSyntheticCapabilities:
    """Tests for build_synthetic_capabilities()."""

    def test_default_major_versions(self) -> None:
        from adcp.capabilities import build_synthetic_capabilities

        result = build_synthetic_capabilities(["media_buy"])
        assert result["adcp"]["major_versions"] == [2]
        assert result["supported_protocols"] == ["media_buy"]

    def test_custom_major_versions(self) -> None:
        from adcp.capabilities import build_synthetic_capabilities

        result = build_synthetic_capabilities(
            ["media_buy", "signals"],
            major_versions=[2, 3],
        )
        assert result["adcp"]["major_versions"] == [2, 3]
        assert result["supported_protocols"] == ["media_buy", "signals"]

    def test_importable_from_top_level(self) -> None:
        from adcp import build_synthetic_capabilities

        assert callable(build_synthetic_capabilities)


class TestSupportsV3:
    """Tests for FeatureResolver.supports_v3()."""

    def test_v3_supported(self) -> None:
        caps = GetAdcpCapabilitiesResponse(
            adcp=AdcpInfo(
                major_versions=[MajorVersion(3)],
                idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
            ),
            supported_protocols=[SupportedProtocol("media_buy")],
        )
        resolver = FeatureResolver(caps)
        assert resolver.supports_v3() is True

    def test_v2_only(self) -> None:
        caps = GetAdcpCapabilitiesResponse(
            adcp=AdcpInfo(
                major_versions=[MajorVersion(2)],
                idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
            ),
            supported_protocols=[SupportedProtocol("media_buy")],
        )
        resolver = FeatureResolver(caps)
        assert resolver.supports_v3() is False

    def test_both_v2_and_v3(self) -> None:
        caps = GetAdcpCapabilitiesResponse(
            adcp=AdcpInfo(
                major_versions=[MajorVersion(2), MajorVersion(3)],
                idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
            ),
            supported_protocols=[SupportedProtocol("media_buy")],
        )
        resolver = FeatureResolver(caps)
        assert resolver.supports_v3() is True
