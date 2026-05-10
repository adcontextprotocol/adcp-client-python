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

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import (
    PlatformHandler,
    _project_build_creative,
    _project_sync_audiences,
)
from adcp.decisioning.webhook_emit import _BACKGROUND_WEBHOOK_TASKS
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
        # Account roster (unioned into every sales-* claim)
        "sync_accounts",
        "list_accounts",
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
        "update_rights",
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
        "update_rights",
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


# ---- _project_build_creative arms ----


def test_project_build_creative_passthrough_dict_envelope() -> None:
    """Already-shaped envelope dict is unchanged."""
    envelope = {"creative_manifest": {"creative_id": "cr_1"}}
    assert _project_build_creative(envelope) is envelope

    multi_envelope = {"creative_manifests": [{"creative_id": "cr_1"}]}
    assert _project_build_creative(multi_envelope) is multi_envelope


def test_project_build_creative_passthrough_pydantic_envelope() -> None:
    """A fully-shaped :class:`BuildCreativeSuccessResponse` (carries
    ``creative_manifest``/``creative_manifests`` as attrs) is unchanged
    — the shim preserves the typed return for response_validator
    middleware."""

    class _SuccessEnvelope:
        creative_manifest = {"creative_id": "cr_1"}

    envelope = _SuccessEnvelope()
    assert _project_build_creative(envelope) is envelope


def test_project_build_creative_wraps_bare_manifest() -> None:
    """A bare :class:`CreativeManifest` (Pydantic model with
    ``model_dump``) is wrapped into ``{creative_manifest: ...}``."""

    class _Manifest:
        def model_dump(self, mode: str = "json") -> dict:
            return {"creative_id": "cr_1", "format_id": "audio_30s"}

    projected = _project_build_creative(_Manifest())
    assert projected == {"creative_manifest": {"creative_id": "cr_1", "format_id": "audio_30s"}}


def test_project_build_creative_wraps_list_into_multi_envelope() -> None:
    """A ``Sequence[CreativeManifest]`` is wrapped into
    ``{creative_manifests: [...]}``."""

    class _Manifest:
        def __init__(self, cid: str) -> None:
            self.cid = cid

        def model_dump(self, mode: str = "json") -> dict:
            return {"creative_id": self.cid}

    projected = _project_build_creative([_Manifest("a"), _Manifest("b")])
    assert projected == {"creative_manifests": [{"creative_id": "a"}, {"creative_id": "b"}]}


def test_project_build_creative_passes_through_unknown_shape() -> None:
    """Adopters returning an unrecognized non-list, non-Pydantic shape
    (rare — e.g., a string error sentinel) get a passthrough so the wire
    validator can surface a precise mis-shape error."""
    sentinel = "weird_string_return"
    assert _project_build_creative(sentinel) == sentinel


# ---- _project_sync_audiences arms ----


def test_project_sync_audiences_wraps_list() -> None:
    """A list of audience-result rows wraps into ``{audiences: [...]}``."""

    class _Row:
        def __init__(self, aid: str) -> None:
            self.aid = aid

        def model_dump(self, mode: str = "json") -> dict:
            return {"audience_id": self.aid}

    projected = _project_sync_audiences([_Row("a1"), _Row("a2")])
    assert projected == {"audiences": [{"audience_id": "a1"}, {"audience_id": "a2"}]}


def test_project_sync_audiences_passthrough_envelope_dict() -> None:
    """Already-shaped envelope is unchanged."""
    envelope = {"audiences": [{"audience_id": "a1"}]}
    assert _project_sync_audiences(envelope) is envelope


def test_project_sync_audiences_passthrough_dict_rows() -> None:
    """List of plain dicts (no model_dump) — the row passthrough
    inside the comprehension is exercised."""
    projected = _project_sync_audiences([{"audience_id": "a1"}])
    assert projected == {"audiences": [{"audience_id": "a1"}]}


# ---- build_creative gate when platform doesn't implement ----


@pytest.mark.asyncio
async def test_build_creative_unsupported_when_platform_lacks_method(executor) -> None:
    """A platform that doesn't implement ``build_creative`` (sales-only
    adopter who ended up routing through here, e.g. via
    ``advertise_all=True`` mis-configuration) surfaces
    ``UNSUPPORTED_FEATURE`` rather than ``INTERNAL_ERROR`` from the
    AttributeError wrapper."""

    class _NoCreative(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-direct"])
        accounts = SingletonAccounts(account_id="hello")

        # Deliberately no build_creative.

    handler = PlatformHandler(
        _NoCreative(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import BuildCreativeRequest

    with pytest.raises(AdcpError) as exc_info:
        await handler.build_creative(BuildCreativeRequest.model_construct(), ToolContext())
    assert exc_info.value.code == "UNSUPPORTED_FEATURE"
    assert "build_creative" in str(exc_info.value)


# ---- update_rights shim routes through ----


@pytest.mark.asyncio
async def test_update_rights_shim_routes_to_platform(executor) -> None:
    """Brand rights includes ``update_rights`` (extend term, change
    scope, revoke). Routes through with no account on the wire."""

    class _BrandRightsAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["brand-rights"])
        accounts = SingletonAccounts(account_id="hello")

        def get_brand_identity(self, req, ctx):
            return {}

        def get_rights(self, req, ctx):
            return {}

        def acquire_rights(self, req, ctx):
            return {}

        def update_rights(self, req, ctx):
            return {"rights_id": "r_1", "status": "updated"}

    handler = PlatformHandler(
        _BrandRightsAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    from adcp.types import UpdateRightsRequest

    result = await handler.update_rights(UpdateRightsRequest.model_construct(), ToolContext())
    assert result == {"rights_id": "r_1", "status": "updated"}


# ---- F12 auto-emit on new webhook-eligible shims ----


def _push_config_params(req_cls, *, url: str = "https://buyer.example.com/wh", **extra):
    """Build a request via ``model_construct`` carrying
    ``push_notification_config`` so the auto-emit gate fires."""

    class _Config:
        pass

    cfg = _Config()
    cfg.url = url
    cfg.token = None
    return req_cls.model_construct(push_notification_config=cfg, **extra)


@pytest.mark.asyncio
async def test_get_signals_auto_emits_completion_webhook(executor) -> None:
    """``get_signals`` is in :data:`SPEC_WEBHOOK_TASK_TYPES`. With a
    buyer-supplied ``push_notification_config.url``, the shim must
    auto-emit a sync-completion webhook after the platform returns."""
    sender = AsyncMock()

    class _SignalsAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["signal-marketplace"])
        accounts = SingletonAccounts(account_id="hello")

        def get_signals(self, req, ctx):
            return {"signals": [{"signal_id": "s1"}]}

        def activate_signal(self, req, ctx):
            return {}

    handler = PlatformHandler(
        _SignalsAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
        auto_emit_completion_webhooks=True,
    )
    from adcp.types import GetSignalsRequest

    req = _push_config_params(GetSignalsRequest)
    await handler.get_signals(req, ToolContext())
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)

    sender.send_mcp.assert_awaited_once()
    call_kwargs = sender.send_mcp.await_args.kwargs
    assert call_kwargs["task_type"] == "get_signals"
    assert call_kwargs["status"] == "completed"
    assert call_kwargs["result"] == {"signals": [{"signal_id": "s1"}]}


@pytest.mark.asyncio
async def test_acquire_rights_auto_emits_completion_webhook(executor) -> None:
    """``acquire_rights`` is in the spec enum; auto-emit fires."""
    sender = AsyncMock()

    class _BrandRights(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["brand-rights"])
        accounts = SingletonAccounts(account_id="hello")

        def get_brand_identity(self, req, ctx):
            return {}

        def get_rights(self, req, ctx):
            return {}

        def acquire_rights(self, req, ctx):
            return {"rights_id": "r1", "status": "acquired"}

    handler = PlatformHandler(
        _BrandRights(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
    )
    from adcp.types import AcquireRightsRequest

    req = _push_config_params(AcquireRightsRequest)
    await handler.acquire_rights(req, ToolContext())
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)

    sender.send_mcp.assert_awaited_once()
    assert sender.send_mcp.await_args.kwargs["task_type"] == "acquire_rights"


@pytest.mark.asyncio
async def test_sync_audiences_auto_emits_with_projected_envelope(executor) -> None:
    """``sync_audiences`` returns a list arm from the platform; the
    shim projects to ``{audiences: [...]}`` AND auto-emits the
    projected (envelope) shape on the webhook ``result`` field —
    receivers see the wire envelope, not the bare list."""
    sender = AsyncMock()

    class _AudienceAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["audience-sync"])
        accounts = SingletonAccounts(account_id="hello")

        def sync_audiences(self, audiences, ctx):
            # Return the bare-list ergonomic arm (not the envelope).
            return [{"audience_id": "a1", "status": "deployed"}]

    handler = PlatformHandler(
        _AudienceAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
    )
    from adcp.types import SyncAudiencesRequest

    req = _push_config_params(SyncAudiencesRequest, audiences=[{"audience_id": "a1"}])
    result = await handler.sync_audiences(req, ToolContext())
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)

    # Shim's return is the envelope.
    assert result == {"audiences": [{"audience_id": "a1", "status": "deployed"}]}
    sender.send_mcp.assert_awaited_once()
    # Webhook receives the envelope, not the bare list.
    assert sender.send_mcp.await_args.kwargs["task_type"] == "sync_audiences"
    assert sender.send_mcp.await_args.kwargs["result"] == result


@pytest.mark.asyncio
async def test_property_list_ops_dont_auto_emit_because_schema_forbids_push_notif(
    executor,
) -> None:
    """Property-list request schemas declare ``additionalProperties:
    false`` and don't include ``push_notification_config`` — the wire
    forbids buyers from registering a webhook URL on these ops, so
    the F12 auto-emit gate naturally skips. The shim still calls
    :meth:`_maybe_auto_emit_sync_completion` defensively (mirrors the
    sales-* pattern), so a future schema change that adds push-notif
    would activate auto-emit without further shim wiring.

    This test pins the current state: zero webhook deliveries on the
    property-list dispatch path. If
    ``schemas/cache/property/create-property-list-request.json`` ever
    grows ``push_notification_config``, this test will surface that as
    expected behavior change and the assertion needs to flip.
    """
    sender = AsyncMock()

    class _PropAgent(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["property-lists"])
        accounts = SingletonAccounts(account_id="hello")

        def create_property_list(self, req, ctx):
            return {"list_id": "pl1", "fetch_token": "tok"}

        def update_property_list(self, req, ctx):
            return {}

        def get_property_list(self, req, ctx):
            return {}

        def list_property_lists(self, req, ctx):
            return {}

        def delete_property_list(self, req, ctx):
            return {}

    handler = PlatformHandler(
        _PropAgent(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
    )
    from adcp.types import CreatePropertyListRequest

    # ``model_construct`` strips the kwarg because the schema is
    # ``extra: forbid`` — we end up with a request that has no
    # ``push_notification_config`` attr at all, exactly matching
    # production wire behavior.
    req = _push_config_params(CreatePropertyListRequest)
    assert not hasattr(req, "push_notification_config")
    await handler.create_property_list(req, ToolContext())
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)

    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_get_creative_delivery_auto_emits_completion_webhook(executor) -> None:
    """``get_creative_delivery`` is in :data:`SPEC_WEBHOOK_TASK_TYPES`
    and its wire schema allows ``push_notification_config`` (additional
    properties: true). With a buyer-supplied URL the shim fires a
    sync-completion webhook."""
    sender = AsyncMock()

    class _AdServer(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["creative-ad-server"])
        accounts = SingletonAccounts(account_id="hello")

        def build_creative(self, req, ctx):
            return {}

        def preview_creative(self, req, ctx):
            return {}

        def get_creative_delivery(self, req, ctx):
            return {"creatives": [{"creative_id": "c1", "impressions": 100}]}

    handler = PlatformHandler(
        _AdServer(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
    )
    from adcp.types import GetCreativeDeliveryRequest

    req = _push_config_params(GetCreativeDeliveryRequest)
    await handler.get_creative_delivery(req, ToolContext())
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)

    sender.send_mcp.assert_awaited_once()
    assert sender.send_mcp.await_args.kwargs["task_type"] == "get_creative_delivery"


# ---- sync_accounts / list_accounts route through AccountStore ----


class _AccountsWithUpsertAndList:
    """Minimal AccountStore exposing the optional ``upsert`` /
    ``list`` Protocol methods (:class:`AccountStoreUpsert` /
    :class:`AccountStoreList`)."""

    resolution = "derived"

    def __init__(self) -> None:
        self.upsert_calls: list[dict] = []
        self.list_calls: list[dict] = []

    def resolve(self, ref, auth_info=None):
        from adcp.decisioning.types import Account

        return Account(id="acct_1", name="acct_1", status="active", metadata={})

    def upsert(self, refs, ctx=None):
        from adcp.decisioning.types import SyncAccountsResultRow

        self.upsert_calls.append({"refs": refs, "ctx": ctx})
        return [
            SyncAccountsResultRow(
                brand={"domain": "acme.com"},
                operator="acme.com",
                action="created",
                status="active",
                account_id="acct_acme",
            )
        ]

    def list(self, filter=None, ctx=None):
        from adcp.decisioning.types import Account

        self.list_calls.append({"filter": filter, "ctx": ctx})
        return [Account(id="acct_acme", name="Acme", status="active", metadata={})]


@pytest.mark.asyncio
async def test_sync_accounts_routes_to_account_store_upsert(executor) -> None:
    """``sync_accounts`` shim wires through to
    ``platform.accounts.upsert`` with a :class:`ResolveContext` carrying
    ``tool_name='sync_accounts'``. Without this wire-through, every
    AdCP sales-* adopter implementing :class:`AccountStoreUpsert`
    would surface ``OPERATION_NOT_SUPPORTED`` on the wire (the
    ``ADCPHandler._not_supported`` baseline) regardless of what the
    store declares."""
    accounts = _AccountsWithUpsertAndList()

    class _Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])

    seller = _Seller()
    seller.accounts = accounts

    handler = PlatformHandler(seller, executor=executor, registry=InMemoryTaskRegistry())
    from adcp.types import SyncAccountsRequest

    req = SyncAccountsRequest.model_construct(
        idempotency_key="abcdef0123456789",
        accounts=[
            {"brand": {"domain": "acme.com"}, "operator": "acme.com", "billing": "advertiser"}
        ],
    )
    result = await handler.sync_accounts(req, ToolContext())

    assert len(accounts.upsert_calls) == 1
    call = accounts.upsert_calls[0]
    # Refs are the per-account entries from the wire request.
    assert len(call["refs"]) == 1
    # ResolveContext carries the tool name for adopter audit / gating.
    assert call["ctx"].tool_name == "sync_accounts"
    # Wire envelope shape.
    assert "accounts" in result
    assert result["accounts"][0]["account_id"] == "acct_acme"


@pytest.mark.asyncio
async def test_list_accounts_routes_to_account_store_list(executor) -> None:
    """``list_accounts`` shim wires through to
    ``platform.accounts.list`` with a :class:`ResolveContext` carrying
    ``tool_name='list_accounts'``."""
    accounts = _AccountsWithUpsertAndList()

    class _Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])

    seller = _Seller()
    seller.accounts = accounts

    handler = PlatformHandler(seller, executor=executor, registry=InMemoryTaskRegistry())
    from adcp.types import ListAccountsRequest

    req = ListAccountsRequest.model_construct()
    result = await handler.list_accounts(req, ToolContext())

    assert len(accounts.list_calls) == 1
    assert accounts.list_calls[0]["ctx"].tool_name == "list_accounts"
    assert "accounts" in result
    assert result["accounts"][0]["account_id"] == "acct_acme"


@pytest.mark.asyncio
async def test_sync_accounts_unsupported_when_store_lacks_upsert(executor) -> None:
    """A platform whose :class:`AccountStore` doesn't implement the
    optional :class:`AccountStoreUpsert` Protocol surfaces
    ``OPERATION_NOT_SUPPORTED`` (via :class:`NotImplementedResponse`)
    — distinct from the ``AttributeError`` that an unguarded
    ``getattr().()`` chain would produce."""
    from adcp.server.base import NotImplementedResponse

    class _Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="hello")  # no upsert / list

    handler = PlatformHandler(_Seller(), executor=executor, registry=InMemoryTaskRegistry())
    from adcp.types import SyncAccountsRequest

    req = SyncAccountsRequest.model_construct(idempotency_key="abcdef0123456789", accounts=[])
    result = await handler.sync_accounts(req, ToolContext())
    assert isinstance(result, NotImplementedResponse)
    assert result.supported is False
    assert "sync_accounts" in result.reason


@pytest.mark.asyncio
async def test_list_accounts_unsupported_when_store_lacks_list(executor) -> None:
    """Same OPERATION_NOT_SUPPORTED gate for ``list_accounts``."""
    from adcp.server.base import NotImplementedResponse

    class _Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="hello")  # no upsert / list

    handler = PlatformHandler(_Seller(), executor=executor, registry=InMemoryTaskRegistry())
    from adcp.types import ListAccountsRequest

    req = ListAccountsRequest.model_construct()
    result = await handler.list_accounts(req, ToolContext())
    assert isinstance(result, NotImplementedResponse)
    assert result.supported is False
    assert "list_accounts" in result.reason


@pytest.mark.asyncio
async def test_sync_accounts_strips_credentials_from_extra_allow_pydantic_row(
    executor,
) -> None:
    """Adopter returns a row that smuggles
    ``governance_agents[i].authentication`` past the codegen schema
    via ``extra='allow'`` (or a loose-dict spread). The framework's
    defense-in-depth scrubber on the projected envelope removes it
    before the response leaves the shim — the leak vector the
    framework guards against on every other account-bearing path
    must close on this dispatch path too."""

    class _SmugglerStore:
        resolution = "derived"

        def resolve(self, ref, auth_info=None):
            from adcp.decisioning.types import Account

            return Account(id="acct_1", name="acct_1", status="active", metadata={})

        def upsert(self, refs, ctx=None):
            # Loose dict carrying the write-only credential. Adopter
            # spread an internal record onto the row.
            return [
                {
                    "brand": {"domain": "acme.com"},
                    "operator": "acme.com",
                    "action": "created",
                    "status": "active",
                    "account_id": "acct_acme",
                    "governance_agents": [
                        {
                            "agent_url": "https://gov.example.com",
                            "authentication": {"credentials": "Bearer leaked_token_xyz"},
                        }
                    ],
                    "billing_entity": {
                        "name": "Acme",
                        "bank": {"iban": "GB29NWBK60161331926819"},
                    },
                }
            ]

    class _Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])

    seller = _Seller()
    seller.accounts = _SmugglerStore()

    handler = PlatformHandler(seller, executor=executor, registry=InMemoryTaskRegistry())
    from adcp.types import SyncAccountsRequest

    req = SyncAccountsRequest.model_construct(
        idempotency_key="abcdef0123456789",
        accounts=[
            {"brand": {"domain": "acme.com"}, "operator": "acme.com", "billing": "advertiser"}
        ],
    )
    result = await handler.sync_accounts(req, ToolContext())

    row = result["accounts"][0]
    # governance_agents[i].authentication is the write-only credential
    # field — must not survive on the wire.
    assert "authentication" not in row["governance_agents"][0]
    # billing_entity.bank is write-only too.
    assert "bank" not in row["billing_entity"]


@pytest.mark.asyncio
async def test_sync_accounts_handles_pydantic_envelope_return(executor) -> None:
    """An adopter who returns a fully-shaped Pydantic
    ``SyncAccountsResponse`` (a natural mistake when reading the
    response type alias) gets projected through ``model_dump`` so the
    credential scrubber's dict-walker reaches every row. Without the
    Pydantic-envelope handling the shim returns a Pydantic instance
    that bypasses the dict-walker scrub."""
    from adcp.types import SyncAccountsRequest

    class _EnvelopeStore:
        resolution = "derived"

        def resolve(self, ref, auth_info=None):
            from adcp.decisioning.types import Account

            return Account(id="acct_1", name="acct_1", status="active", metadata={})

        def upsert(self, refs, ctx=None):
            # Adopter pre-shaped the wire envelope (bypass the typed
            # row). Return a dict shape (Pydantic envelope path is
            # exercised symbolically — what matters is the
            # non-list result path doesn't drop the wire keys).
            return {
                "accounts": [
                    {
                        "brand": {"domain": "acme.com"},
                        "operator": "acme.com",
                        "action": "created",
                        "status": "active",
                        "account_id": "acct_acme",
                    }
                ],
                "dry_run": False,
            }

    class _Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])

    seller = _Seller()
    seller.accounts = _EnvelopeStore()

    handler = PlatformHandler(seller, executor=executor, registry=InMemoryTaskRegistry())
    req = SyncAccountsRequest.model_construct(
        idempotency_key="abcdef0123456789",
        accounts=[
            {"brand": {"domain": "acme.com"}, "operator": "acme.com", "billing": "advertiser"}
        ],
    )
    result = await handler.sync_accounts(req, ToolContext())
    # The pre-shaped envelope passes through (dict path) and the
    # credential scrubber runs on it.
    assert isinstance(result, dict)
    assert result["accounts"][0]["account_id"] == "acct_acme"
    assert result["dry_run"] is False


def test_advertised_tools_for_instance_drops_account_tools_without_store_methods() -> None:
    """Per-instance filter drops ``sync_accounts`` / ``list_accounts``
    when the platform's :class:`AccountStore` doesn't expose the
    optional Protocol methods. Sales-* claims union the tools in by
    default, but the filter prevents over-advertisement when adopters
    haven't wired :class:`AccountStoreUpsert` / :class:`AccountStoreList`."""

    class _Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="hello")

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        handler = PlatformHandler(_Seller(), executor=pool, registry=InMemoryTaskRegistry())
        advertised = handler.advertised_tools_for_instance()
        assert "sync_accounts" not in advertised
        assert "list_accounts" not in advertised
    finally:
        pool.shutdown(wait=True)


def test_advertised_tools_for_instance_logs_dropped_account_tools(caplog) -> None:
    """When a sales-* claim drops ``sync_accounts`` / ``list_accounts``
    because the store doesn't expose ``upsert`` / ``list``, the
    framework emits a one-line ``logger.info`` so the adopter has a
    breadcrumb pointing at the missing optional Protocol. Without
    this log, downstream storyboard scenarios stuck on
    ``skipped (missing_tool)`` have no actionable signal."""
    import logging

    class _Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="hello")  # no upsert / list

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        handler = PlatformHandler(_Seller(), executor=pool, registry=InMemoryTaskRegistry())
        with caplog.at_level(logging.INFO, logger="adcp.decisioning.handler"):
            handler.advertised_tools_for_instance()
            # Second call must NOT re-emit (per-handler dedupe).
            handler.advertised_tools_for_instance()

        relevant = [r for r in caplog.records if "advertised_tools" in r.getMessage()]
        # Two drops × first-call only = two distinct log lines.
        assert len(relevant) == 2
        messages = sorted(r.getMessage() for r in relevant)
        assert "'list_accounts'" in messages[0]
        assert "'sync_accounts'" in messages[1]
        assert "AccountStoreList" in messages[0]
        assert "AccountStoreUpsert" in messages[1]
    finally:
        pool.shutdown(wait=True)


def test_advertised_tools_for_instance_includes_account_tools_when_store_implements() -> None:
    """When the platform's :class:`AccountStore` exposes ``upsert`` and
    ``list``, the per-instance set advertises both account tools."""

    class _Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])

    seller = _Seller()
    seller.accounts = _AccountsWithUpsertAndList()

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        handler = PlatformHandler(seller, executor=pool, registry=InMemoryTaskRegistry())
        advertised = handler.advertised_tools_for_instance()
        assert "sync_accounts" in advertised
        assert "list_accounts" in advertised
    finally:
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_update_rights_does_not_auto_emit(executor) -> None:
    """``update_rights`` is NOT in :data:`SPEC_WEBHOOK_TASK_TYPES` — the
    spec enum freezes at the closed 20-value set per
    ``schemas/cache/enums/task-type.json``. Adding it requires a
    cross-language pin bump; until then, the shim's no-auto-emit
    behavior is the correct posture (skip + warn). Without this guard
    a buyer registering a webhook URL on ``update_rights`` would see
    a webhook the spec enum doesn't allow, and conformant verifiers
    would reject it.
    """
    sender = AsyncMock()

    class _BrandRights(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["brand-rights"])
        accounts = SingletonAccounts(account_id="hello")

        def get_brand_identity(self, req, ctx):
            return {}

        def get_rights(self, req, ctx):
            return {}

        def acquire_rights(self, req, ctx):
            return {}

        def update_rights(self, req, ctx):
            return {}

    handler = PlatformHandler(
        _BrandRights(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
    )
    from adcp.types import UpdateRightsRequest

    req = _push_config_params(UpdateRightsRequest)
    await handler.update_rights(req, ToolContext())
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)

    sender.send_mcp.assert_not_called()
