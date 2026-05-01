"""Per-specialism handler shim coverage.

The breadth-sprint (PRs #332-#335) shipped 10 Protocol classes +
REQUIRED_METHODS coverage at the static layer, but the Emma DX smoke
test surfaced that the runtime ``PlatformHandler`` only had 9 sales-*
shims — every non-sales tool 404'd at the wire even though
capabilities + validate_platform reported green. This file pins the
fix: every wire tool the framework dispatches has a handler shim that
routes from ``params: <Request>`` through ``_invoke_platform_method``
to ``platform.<method>(params, ctx)``.

Test surfaces:

* ``advertised_tools`` covers every spec wire tool we ship.
* Each shim routes through to the platform method (one per Protocol
  family, smoke-tested via stub platform).
* Optional methods surface ``UNSUPPORTED_FEATURE`` when the platform
  doesn't implement them — distinct from ``INTERNAL_ERROR`` (which is
  what would happen without the runtime gate).
* The ``account`` field is gracefully optional — tools without
  ``account`` on the wire still resolve via auth-only path.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.server.base import ToolContext


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-shim-")
    yield pool
    pool.shutdown(wait=True)


# ---- advertised_tools coverage ----


def test_advertised_tools_covers_every_specialism_wire_tool() -> None:
    """``PlatformHandler.advertised_tools`` includes every wire tool
    across all 10 Protocol families. Without this, the breadth-sprint
    Protocols were dead code at runtime — buyers would 404 on
    ``build_creative``, ``get_signals``, etc."""
    expected = {
        # Sales
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
        "get_media_buys",
        "provide_performance_feedback",
        "list_creative_formats",
        "list_creatives",
        # Creative (Builder + AdServer)
        "build_creative",
        "preview_creative",
        "get_creative_delivery",
        # Signals
        "get_signals",
        "activate_signal",
        # Audience
        "sync_audiences",
        # Governance
        "check_governance",
        "sync_plans",
        "report_plan_outcome",
        "get_plan_audit_logs",
        # Brand Rights
        "get_brand_identity",
        "get_rights",
        "acquire_rights",
        # Content Standards
        "list_content_standards",
        "get_content_standards",
        "create_content_standards",
        "update_content_standards",
        "calibrate_content",
        "validate_content_delivery",
        "get_media_buy_artifacts",
        "get_creative_features",
        # Property Lists
        "create_property_list",
        "update_property_list",
        "get_property_list",
        "list_property_lists",
        "delete_property_list",
        # Collection Lists
        "create_collection_list",
        "update_collection_list",
        "get_collection_list",
        "list_collection_lists",
        "delete_collection_list",
    }
    assert PlatformHandler.advertised_tools == expected


@pytest.mark.parametrize(
    "tool_name",
    [
        "build_creative",
        "preview_creative",
        "get_creative_delivery",
        "get_signals",
        "activate_signal",
        "sync_audiences",
        "check_governance",
        "sync_plans",
        "report_plan_outcome",
        "get_plan_audit_logs",
        "get_brand_identity",
        "get_rights",
        "acquire_rights",
        "list_content_standards",
        "get_content_standards",
        "create_content_standards",
        "update_content_standards",
        "calibrate_content",
        "validate_content_delivery",
        "get_media_buy_artifacts",
        "get_creative_features",
        "create_property_list",
        "update_property_list",
        "get_property_list",
        "list_property_lists",
        "delete_property_list",
        "create_collection_list",
        "update_collection_list",
        "get_collection_list",
        "list_collection_lists",
        "delete_collection_list",
    ],
)
def test_handler_shim_method_exists(tool_name: str) -> None:
    """Every advertised non-sales tool has a corresponding shim method
    on PlatformHandler. Without this, ``tools/list`` advertises tools
    the handler can't actually dispatch — buyer-facing 404."""
    assert hasattr(PlatformHandler, tool_name), (
        f"PlatformHandler is missing the {tool_name!r} shim — " "advertised but undispatchable."
    )


# ---- Shim dispatch via stub platforms ----


@pytest.mark.asyncio
async def test_build_creative_shim_routes_to_platform(executor) -> None:
    """End-to-end: shim → ``_invoke_platform_method`` → platform method."""
    captured: list[str] = []

    class _CreativeBuilder(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["creative-generative"])
        accounts = SingletonAccounts(account_id="hello")

        def build_creative(self, req, ctx):
            captured.append("build_creative_called")
            return {"creative_manifest": {"creative_id": "cr_1"}}

    handler = PlatformHandler(
        _CreativeBuilder(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    # Use model_construct to bypass the wire-spec validation; the test
    # is about shim routing, not request shape.
    from adcp.types import BuildCreativeRequest

    req = BuildCreativeRequest.model_construct()
    result = await handler.build_creative(req, ToolContext())
    assert captured == ["build_creative_called"]
    assert result == {"creative_manifest": {"creative_id": "cr_1"}}


@pytest.mark.asyncio
async def test_get_signals_shim_routes_to_platform(executor) -> None:
    captured: list[str] = []

    class _SignalsAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["signal-marketplace"])
        accounts = SingletonAccounts(account_id="hello")

        def get_signals(self, req, ctx):
            captured.append("get_signals_called")
            return {"signals": []}

        def activate_signal(self, req, ctx):
            return {}

    handler = PlatformHandler(
        _SignalsAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import GetSignalsRequest

    result = await handler.get_signals(GetSignalsRequest.model_construct(), ToolContext())
    assert captured == ["get_signals_called"]
    assert result == {"signals": []}


@pytest.mark.asyncio
async def test_sync_audiences_shim_arg_projects_audiences_list(executor) -> None:
    """The ``sync_audiences`` wire request carries ``audiences[]`` but
    the AudiencePlatform method signature is ``sync_audiences(audiences,
    ctx)`` — adopter ergonomic. The shim arg-projects."""
    received_audiences: list = []

    class _AudienceAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["audience-sync"])
        accounts = SingletonAccounts(account_id="hello")

        def sync_audiences(self, audiences, ctx):
            received_audiences.extend(audiences)
            return {"audiences": []}

    handler = PlatformHandler(
        _AudienceAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import SyncAudiencesRequest

    fake_audiences = [{"audience_id": "a1"}, {"audience_id": "a2"}]
    req = SyncAudiencesRequest.model_construct(audiences=fake_audiences)
    await handler.sync_audiences(req, ToolContext())
    assert received_audiences == fake_audiences


@pytest.mark.asyncio
async def test_check_governance_shim_routes_to_platform(executor) -> None:
    class _GovernanceAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-spend-authority"],
            governance_aware=True,
        )
        accounts = SingletonAccounts(account_id="hello")

        def check_governance(self, req, ctx):
            return {"status": "approved"}

        def sync_plans(self, req, ctx):
            return {}

        def report_plan_outcome(self, req, ctx):
            return {}

        def get_plan_audit_logs(self, req, ctx):
            return {}

    handler = PlatformHandler(
        _GovernanceAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import CheckGovernanceRequest

    result = await handler.check_governance(CheckGovernanceRequest.model_construct(), ToolContext())
    assert result == {"status": "approved"}


@pytest.mark.asyncio
async def test_acquire_rights_shim_routes_to_platform(executor) -> None:
    class _BrandRightsAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["brand-rights"])
        accounts = SingletonAccounts(account_id="hello")

        def get_brand_identity(self, req, ctx):
            return {}

        def get_rights(self, req, ctx):
            return {"rights": []}

        def acquire_rights(self, req, ctx):
            return {"rights_id": "r_1", "status": "acquired"}

    handler = PlatformHandler(
        _BrandRightsAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import AcquireRightsRequest

    result = await handler.acquire_rights(AcquireRightsRequest.model_construct(), ToolContext())
    assert result == {"rights_id": "r_1", "status": "acquired"}


@pytest.mark.asyncio
async def test_list_content_standards_shim_routes_to_platform(executor) -> None:
    class _ContentStandardsAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["content-standards"])
        accounts = SingletonAccounts(account_id="hello")

        def list_content_standards(self, req, ctx):
            return {"standards": []}

        def get_content_standards(self, req, ctx):
            return {}

        def create_content_standards(self, req, ctx):
            return {}

        def update_content_standards(self, req, ctx):
            return {}

        def calibrate_content(self, req, ctx):
            return {}

        def validate_content_delivery(self, req, ctx):
            return {}

    handler = PlatformHandler(
        _ContentStandardsAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import ListContentStandardsRequest

    result = await handler.list_content_standards(
        ListContentStandardsRequest.model_construct(), ToolContext()
    )
    assert result == {"standards": []}


@pytest.mark.asyncio
async def test_create_property_list_shim_routes_to_platform(executor) -> None:
    class _PropertyListsAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["property-lists"])
        accounts = SingletonAccounts(account_id="hello")

        def create_property_list(self, req, ctx):
            return {"list_id": "pl_1", "fetch_token": "tok_x"}

        def update_property_list(self, req, ctx):
            return {}

        def get_property_list(self, req, ctx):
            return {}

        def list_property_lists(self, req, ctx):
            return {}

        def delete_property_list(self, req, ctx):
            return {}

    handler = PlatformHandler(
        _PropertyListsAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import CreatePropertyListRequest

    result = await handler.create_property_list(
        CreatePropertyListRequest.model_construct(), ToolContext()
    )
    assert result == {"list_id": "pl_1", "fetch_token": "tok_x"}


# ---- Optional-method UNSUPPORTED_FEATURE gate ----


@pytest.mark.asyncio
async def test_preview_creative_unsupported_when_platform_lacks_method(
    executor,
) -> None:
    """``preview_creative`` is OPTIONAL on CreativeBuilderPlatform.
    A platform claiming ``creative-generative`` without
    ``preview_creative`` should surface ``UNSUPPORTED_FEATURE`` to the
    buyer — NOT ``INTERNAL_ERROR`` (which is what AttributeError →
    dispatch wrapper would produce without the runtime gate)."""

    class _BuilderWithoutPreview(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["creative-generative"])
        accounts = SingletonAccounts(account_id="hello")

        def build_creative(self, req, ctx):
            return {}

        # Deliberately no preview_creative — the Protocol marks it optional.

    handler = PlatformHandler(
        _BuilderWithoutPreview(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import PreviewCreativeRequest

    with pytest.raises(AdcpError) as exc_info:
        await handler.preview_creative(PreviewCreativeRequest.model_construct(), ToolContext())
    assert exc_info.value.code == "UNSUPPORTED_FEATURE"
    # Buyer-facing message points at the missing method.
    assert "preview_creative" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_creative_features_unsupported_when_platform_lacks_method(
    executor,
) -> None:
    """``get_creative_features`` is OPTIONAL on
    ContentStandardsPlatform — analyzer-pipeline-only adopters omit
    it. Same UNSUPPORTED_FEATURE surface."""

    class _ContentStandardsNoAnalyzer(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["content-standards"])
        accounts = SingletonAccounts(account_id="hello")

        def list_content_standards(self, req, ctx):
            return {}

        def get_content_standards(self, req, ctx):
            return {}

        def create_content_standards(self, req, ctx):
            return {}

        def update_content_standards(self, req, ctx):
            return {}

        def calibrate_content(self, req, ctx):
            return {}

        def validate_content_delivery(self, req, ctx):
            return {}

        # Optional analyzer reads omitted.

    handler = PlatformHandler(
        _ContentStandardsNoAnalyzer(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import GetCreativeFeaturesRequest

    with pytest.raises(AdcpError) as exc_info:
        await handler.get_creative_features(
            GetCreativeFeaturesRequest.model_construct(), ToolContext()
        )
    assert exc_info.value.code == "UNSUPPORTED_FEATURE"


# ---- AudioStack DX regression ----


@pytest.mark.asyncio
async def test_audiostack_style_creative_generative_agent_dispatches(executor) -> None:
    """Direct regression test for the Emma AudioStack DX failure: a
    ``creative-generative`` agent claiming the slug + implementing
    ``build_creative`` MUST be reachable via the shim. Pre-fix:
    advertised but unrouted (404). Post-fix: end-to-end dispatch."""
    audiostack_calls: list[dict] = []

    class _AudioStackSeller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["creative-generative"],
            channels=["audio"],
        )
        accounts = SingletonAccounts(account_id="audiostack")

        def build_creative(self, req, ctx):
            # Stub AudioStack call — the test is about the SDK
            # dispatch, not the third-party API.
            audiostack_calls.append({"req": req, "ctx_account_id": ctx.account.id})
            return {
                "creative_manifest": {
                    "creative_id": "as_synthesized_001",
                    "format_id": "audio_30s",
                    "asset_url": "https://cdn.audiostack.ai/synth/001.mp3",
                }
            }

    handler = PlatformHandler(
        _AudioStackSeller(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    # build_creative is in the advertised set
    assert "build_creative" in handler.advertised_tools

    from adcp.types import BuildCreativeRequest

    req = BuildCreativeRequest.model_construct()
    result = await handler.build_creative(req, ToolContext())

    # The shim called through; AudioStack stub recorded the invocation.
    assert len(audiostack_calls) == 1
    assert audiostack_calls[0]["ctx_account_id"].startswith("audiostack:")
    # The wire envelope made it back.
    assert result["creative_manifest"]["creative_id"] == "as_synthesized_001"
