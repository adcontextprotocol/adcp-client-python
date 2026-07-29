"""Per-specialism Protocol tests.

Covers ``SignalsPlatform`` (signal-marketplace),
``OwnedSignalsPlatform`` (signal-owned), and ``AudiencePlatform``
(audience-sync). The ``SalesPlatform`` Protocol is exercised end-to-end
by the foundation tests
(``test_decisioning_handler.py``, ``test_hello_seller_integration.py``);
this file fills the breadth-sprint Batch 1 coverage for the two
specialisms shipped alongside it.

Three test surfaces per Protocol:

1. ``runtime_checkable`` conformance — a class implementing the
   methods passes ``isinstance`` against the Protocol.
2. ``validate_platform`` required-method enforcement — claiming the
   slug without the methods fails server boot.
3. Public exports — the Protocol is on ``adcp.decisioning.__all__``
   so adopters import from the canonical surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adcp.decisioning import (
    AudiencePlatform,
    BrandRightsPlatform,
    CampaignGovernancePlatform,
    CollectionListsPlatform,
    ContentStandardsPlatform,
    CreativeAdServerPlatform,
    CreativeBuilderPlatform,
    DecisioningCapabilities,
    DecisioningPlatform,
    OwnedSignalsPlatform,
    PropertyListsPlatform,
    SalesPlatform,
    SignalsPlatform,
    SingletonAccounts,
)
from adcp.decisioning.dispatch import (
    REQUIRED_METHODS_PER_SPECIALISM,
    validate_platform,
)
from adcp.decisioning.types import AdcpError
from adcp.validation.version import resolve_bundle_key

# ---- Public exports ----


def test_specialism_protocols_are_publicly_exported() -> None:
    """All public Protocol classes (Batches 0–4) are on
    ``adcp.decisioning.__all__`` so adopters import from the canonical
    public surface, not the internal ``adcp.decisioning.specialisms.*``
    modules.

    Breadth sprint complete: every spec specialism slug except
    ``governance-aware-seller`` now has a Protocol class +
    REQUIRED_METHODS coverage."""
    import adcp.decisioning as dx

    assert "SalesPlatform" in dx.__all__
    assert "OwnedSignalsPlatform" in dx.__all__
    assert "SignalsPlatform" in dx.__all__
    assert "AudiencePlatform" in dx.__all__
    assert "CreativeBuilderPlatform" in dx.__all__
    assert "CreativeAdServerPlatform" in dx.__all__
    assert "CampaignGovernancePlatform" in dx.__all__
    assert "BrandRightsPlatform" in dx.__all__
    assert "ContentStandardsPlatform" in dx.__all__
    assert "PropertyListsPlatform" in dx.__all__
    assert "CollectionListsPlatform" in dx.__all__
    assert dx.OwnedSignalsPlatform is OwnedSignalsPlatform
    assert dx.SignalsPlatform is SignalsPlatform
    assert dx.AudiencePlatform is AudiencePlatform
    assert dx.CreativeBuilderPlatform is CreativeBuilderPlatform
    assert dx.CreativeAdServerPlatform is CreativeAdServerPlatform
    assert dx.CampaignGovernancePlatform is CampaignGovernancePlatform
    assert dx.BrandRightsPlatform is BrandRightsPlatform
    assert dx.ContentStandardsPlatform is ContentStandardsPlatform
    assert dx.PropertyListsPlatform is PropertyListsPlatform
    assert dx.CollectionListsPlatform is CollectionListsPlatform


# ---- SignalsPlatform ----


def test_signals_platform_runtime_checkable() -> None:
    """A class with ``get_signals`` + ``activate_signal`` passes
    ``isinstance`` against :class:`SignalsPlatform` thanks to the
    Protocol's ``@runtime_checkable`` decoration."""

    class _SignalsImpl:
        def get_signals(self, req, ctx):
            return {"signals": []}

        def activate_signal(self, req, ctx):
            return {"deployments": []}

    assert isinstance(_SignalsImpl(), SignalsPlatform)


def test_signals_platform_runtime_check_fails_when_methods_missing() -> None:
    """A class missing ``activate_signal`` does NOT pass the
    isinstance check. ``runtime_checkable`` matches by attribute name
    presence."""

    class _Partial:
        def get_signals(self, req, ctx):
            return {"signals": []}

        # Missing: activate_signal

    assert not isinstance(_Partial(), SignalsPlatform)


def test_owned_signals_platform_runtime_checkable_with_get_signals_only() -> None:
    """``signal-owned`` supports discovery-only owned signal catalogs."""

    class _OwnedSignalsImpl:
        def get_signals(self, req, ctx):
            return {"signals": []}

    impl = _OwnedSignalsImpl()
    assert isinstance(impl, OwnedSignalsPlatform)
    assert not isinstance(impl, SignalsPlatform)


def test_validate_platform_enforces_signal_marketplace_methods() -> None:
    """A platform claiming ``signal-marketplace`` without implementing
    ``get_signals`` + ``activate_signal`` fails fast at server boot."""

    class _PartialSignalsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["signal-marketplace"])
        accounts = SingletonAccounts(account_id="hello")

        # Implements only get_signals; missing activate_signal.
        def get_signals(self, req, ctx):
            return {"signals": []}

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialSignalsPlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "activate_signal" in missing_methods


def test_validate_platform_enforces_signal_owned_get_signals_only() -> None:
    """``signal-owned`` requires catalog discovery but not activation."""

    class _PartialSignalOwnedPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["signal-owned"])
        accounts = SingletonAccounts(account_id="hello")
        # Implements neither method.

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialSignalOwnedPlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert missing_methods == {"get_signals"}


def test_validate_platform_passes_for_signal_owned_without_activate_signal() -> None:
    """Seller-owned signals are already usable in media-buy targeting."""

    class _OwnedSignalPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["signal-owned"])
        accounts = SingletonAccounts(account_id="hello")

        def get_signals(self, req, ctx):
            return {"signals": []}

    validate_platform(_OwnedSignalPlatform())


def test_validate_platform_passes_for_complete_signals_platform() -> None:
    """Happy path — fully-implemented signals platform passes."""

    class _CompleteSignalsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["signal-marketplace"])
        accounts = SingletonAccounts(account_id="hello")

        def get_signals(self, req, ctx):
            return {"signals": []}

        def activate_signal(self, req, ctx):
            return {"deployments": []}

    validate_platform(_CompleteSignalsPlatform())


def test_signal_marketplace_and_signal_owned_method_sets_are_distinct() -> None:
    """Marketplace signals provision to destinations; owned signals do not."""

    assert REQUIRED_METHODS_PER_SPECIALISM["signal-marketplace"] == {
        "get_signals",
        "activate_signal",
    }
    assert REQUIRED_METHODS_PER_SPECIALISM["signal-owned"] == {"get_signals"}


def test_signal_owned_manifest_exercises_discovery_only() -> None:
    """Bundled conformance manifest must match SDK validation.

    A ``signal-owned`` platform is discovery-only, so the manifest should
    not make conformance runners exercise marketplace activation for that
    specialism.
    """

    repo_root = Path(__file__).resolve().parents[1]
    bundle = resolve_bundle_key((repo_root / "src/adcp/ADCP_VERSION").read_text().strip())
    manifest_path = repo_root / f"schemas/cache/{bundle}/manifest.json"
    manifest = json.loads(manifest_path.read_text())

    signal_owned = manifest["specialisms"]["signal_owned"]
    assert signal_owned["entry_point_tools"] == ["get_signals"]
    assert signal_owned["exercised_tools"] == [
        "get_adcp_capabilities",
        "get_signals",
    ]
    assert "signal_owned" not in manifest["tools"]["activate_signal"]["specialisms"]
    assert "signal_marketplace" in manifest["tools"]["activate_signal"]["specialisms"]


# ---- AudiencePlatform ----


def test_audience_platform_runtime_checkable() -> None:
    """A class with ``sync_audiences`` + ``poll_audience_statuses``
    passes ``isinstance`` against :class:`AudiencePlatform`."""

    class _AudienceImpl:
        def sync_audiences(self, audiences, ctx):
            return {"audiences": []}

        def poll_audience_statuses(self, audience_ids, ctx):
            return {}

    assert isinstance(_AudienceImpl(), AudiencePlatform)


def test_validate_platform_enforces_audience_sync_required_method() -> None:
    """A platform claiming ``audience-sync`` without implementing
    ``sync_audiences`` fails fast. ``poll_audience_statuses`` is
    NOT required (adopter-internal helper)."""

    class _PartialAudiencePlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["audience-sync"])
        accounts = SingletonAccounts(account_id="hello")
        # Missing sync_audiences entirely.

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialAudiencePlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "sync_audiences" in missing_methods


def test_validate_platform_passes_for_audience_sync_with_only_required_method() -> None:
    """``poll_audience_statuses`` is adopter-internal — not required
    for spec coverage. A platform implementing only ``sync_audiences``
    passes validation."""

    class _MinimalAudiencePlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["audience-sync"])
        accounts = SingletonAccounts(account_id="hello")

        def sync_audiences(self, audiences, ctx):
            return {"audiences": []}

    validate_platform(_MinimalAudiencePlatform())


def test_audience_sync_required_methods_pinned() -> None:
    """Contract test — the ``audience-sync`` required-method set is
    deliberately narrow (``sync_audiences`` only;
    ``poll_audience_statuses`` is adopter-internal).
    REQUIRED_METHODS_PER_SPECIALISM tracks the wire-required surface,
    not the full Protocol."""
    assert REQUIRED_METHODS_PER_SPECIALISM["audience-sync"] == {"sync_audiences"}


# ---- Cross-specialism: validate_platform doesn't conflate slugs ----


def test_signals_platform_can_compose_with_sales() -> None:
    """A platform claiming both ``sales-non-guaranteed`` and
    ``signal-marketplace`` must satisfy both Protocols' required
    methods. Cross-specialism composition is supported."""

    class _ComposedPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed", "signal-marketplace"]
        )
        accounts = SingletonAccounts(account_id="hello")

        # Sales-* methods
        def get_products(self, req, ctx):
            return {"products": []}

        def create_media_buy(self, req, ctx):
            return {}

        def update_media_buy(self, media_buy_id, patch, ctx):
            return {}

        def sync_creatives(self, req, ctx):
            return {}

        def get_media_buy_delivery(self, req, ctx):
            return {}

        # Signals methods
        def get_signals(self, req, ctx):
            return {"signals": []}

        def activate_signal(self, req, ctx):
            return {"deployments": []}

    validate_platform(_ComposedPlatform())


def test_sales_platform_protocol_still_runtime_checkable() -> None:
    """Round-trip: the existing ``SalesPlatform`` Protocol still works
    (Batch 1 didn't accidentally break the v6.0 baseline)."""

    class _SalesImpl:
        def get_products(self, req, ctx):
            return {"products": []}

        def create_media_buy(self, req, ctx):
            return {}

        def update_media_buy(self, media_buy_id, patch, ctx):
            return {}

        def sync_creatives(self, req, ctx):
            return {}

        def get_media_buy_delivery(self, req, ctx):
            return {}

        # Optional methods left unimplemented — runtime_checkable
        # checks attribute presence; methods on the Protocol that
        # aren't on the impl fail isinstance.

    # Required methods present, optional missing — runtime_checkable
    # matches by full attribute set so this is False (acceptable; the
    # base SalesPlatform declares 9 methods and runtime_checkable
    # requires all of them).
    # The validate_platform path uses a narrower required-set check,
    # which is what production servers actually rely on.
    assert REQUIRED_METHODS_PER_SPECIALISM["sales-non-guaranteed"] == {
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
    }

    # Smoke check that SalesPlatform symbol is still a runtime-checkable
    # Protocol (not redefined or shadowed). We verify by isinstance
    # against a minimal-but-complete impl rather than checking
    # ``_is_protocol`` (a private CPython typing internal — brittle
    # against typing-module changes).
    # ``sync_catalogs`` is required on the Protocol body (gated to
    # ``sales-catalog-driven`` at runtime by validate_platform, but
    # the @runtime_checkable isinstance path requires all declared
    # methods — a known and documented structural consequence of
    # adding it to the Protocol rather than a separate mixin).
    class _SalesShim:
        def get_products(self, req, ctx):
            return {"products": []}

        def create_media_buy(self, req, ctx):
            return {}

        def update_media_buy(self, media_buy_id, patch, ctx):
            return {}

        def sync_creatives(self, req, ctx):
            return {}

        def get_media_buy_delivery(self, req, ctx):
            return {}

        def get_media_buys(self, req, ctx):
            return {}

        def provide_performance_feedback(self, req, ctx):
            return {}

        def list_creative_formats_legacy(self, req, ctx):
            return {}

        def list_creatives(self, req, ctx):
            return {}

        def sync_catalogs(self, req, ctx):
            return {}

    assert isinstance(_SalesShim(), SalesPlatform)


# ---- CreativeBuilderPlatform ----


def test_creative_builder_runtime_checkable_is_strict_structural_match() -> None:
    """``runtime_checkable`` matches by attribute presence across ALL
    declared Protocol methods (strict structural-AND). Documents the
    contract: a class implementing only the wire-required methods
    will NOT pass ``isinstance`` because optional Protocol methods
    aren't present.

    ``validate_platform`` uses the narrower
    REQUIRED_METHODS_PER_SPECIALISM gate — that's what production
    servers actually rely on for spec coverage. This is consistent
    with SalesPlatform's behavior (same pattern across all
    specialism Protocols)."""

    class _MinimalBuilder:
        def build_creative_legacy(self, req, ctx):
            return {}

    # Minimal impl satisfies the wire-required set but lacks the
    # optional Protocol methods → strict isinstance is False.
    assert not isinstance(_MinimalBuilder(), CreativeBuilderPlatform)


def test_creative_builder_runtime_checkable_full() -> None:
    """A class with every Protocol method (required + optional) passes
    the strict runtime_checkable structural match."""

    class _FullBuilder:
        def build_creative_legacy(self, req, ctx):
            return {}

        def preview_creative_legacy(self, req, ctx):
            return {}

        def sync_creatives(self, req, ctx):
            return {}

        def validate_input(self, req, ctx):
            return {}

    assert isinstance(_FullBuilder(), CreativeBuilderPlatform)


def test_validate_platform_enforces_creative_template_method() -> None:
    """``creative-template`` requires ``build_creative_legacy`` only —
    Optional methods don't gate server boot. A platform claiming the
    slug without ``build_creative_legacy`` fails fast."""

    class _MissingBuildPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["creative-template"])
        accounts = SingletonAccounts(account_id="hello")

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_MissingBuildPlatform())
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "build_creative_legacy" in missing_methods


def test_validate_platform_passes_creative_template_minimal() -> None:
    """Minimal ``creative-template`` adopter implementing only
    ``build_creative_legacy`` passes validation; optional methods can be
    absent."""

    class _MinimalTemplatePlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["creative-template"])
        accounts = SingletonAccounts(account_id="hello")

        def build_creative_legacy(self, req, ctx):
            return {}

    validate_platform(_MinimalTemplatePlatform())


def test_creative_builder_specialisms_share_method_set() -> None:
    """Creative builder specialisms gate on the same single
    method (``build_creative_legacy``). Drift in
    REQUIRED_METHODS_PER_SPECIALISM here surfaces as a visible test
    failure since they should track together."""
    expected = {"build_creative_legacy"}
    assert REQUIRED_METHODS_PER_SPECIALISM["creative-template"] == expected
    assert REQUIRED_METHODS_PER_SPECIALISM["creative-generative"] == expected
    assert REQUIRED_METHODS_PER_SPECIALISM["creative-transformers"] == expected


def test_creative_builder_protocol_has_no_refine_creative() -> None:
    """Regression-guard: ``refine_creative`` was a hallucinated wire
    surface in earlier port drafts. The spec invokes refinement via
    ``build_creative`` itself with ``creative_id`` referencing the
    prior build (per
    ``schemas/cache/media-buy/build-creative-request.json``); there
    is no ``refine-creative-*.json`` schema and no wire tool. If
    someone re-adds ``refine_creative`` to the Protocol thinking it's
    a missing method, this test breaks."""
    assert not hasattr(CreativeBuilderPlatform, "refine_creative")


def test_build_creative_response_includes_submitted_arm() -> None:
    """The spec now includes the task-submitted arm in build_creative responses."""
    import typing

    from adcp.types import LegacyBuildCreativeResponse

    arms = typing.get_args(LegacyBuildCreativeResponse)
    assert len(arms) > 0, "LegacyBuildCreativeResponse should be a Union of arms"
    submitted_arms = [
        arm
        for arm in arms
        if hasattr(arm, "model_fields")
        and {"task_id", "status"}.issubset(set(arm.model_fields.keys()))
    ]
    assert [arm.__name__ for arm in submitted_arms] == ["BuildCreativeResponse6"]


# ---- CreativeAdServerPlatform ----


def test_creative_ad_server_runtime_checkable_full() -> None:
    """An ad-server impl with all required + optional methods passes
    the runtime_checkable check."""

    class _AdServerImpl:
        def build_creative_legacy(self, req, ctx):
            return {}

        def preview_creative_legacy(self, req, ctx):
            return {}

        def list_creatives(self, req, ctx):
            return {}

        def get_creative_delivery(self, req, ctx):
            return {}

        def sync_creatives(self, req, ctx):
            return {}

    assert isinstance(_AdServerImpl(), CreativeAdServerPlatform)


def test_validate_platform_enforces_creative_ad_server_required_methods() -> None:
    """``creative-ad-server`` requires four methods. A platform
    claiming the slug without all four fails fast at server boot."""

    class _PartialAdServerPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["creative-ad-server"])
        accounts = SingletonAccounts(account_id="hello")

        # Implements only the explicit legacy build + preview methods;
        # missing list_creatives + get_creative_delivery.
        def build_creative_legacy(self, req, ctx):
            return {}

        def preview_creative_legacy(self, req, ctx):
            return {}

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialAdServerPlatform())
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "list_creatives" in missing_methods
    assert "get_creative_delivery" in missing_methods


def test_validate_platform_passes_creative_ad_server_with_required_methods() -> None:
    """Adopter implementing the four required ``creative-ad-server``
    methods passes validation. ``sync_creatives`` is optional."""

    class _CompleteAdServerPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["creative-ad-server"])
        accounts = SingletonAccounts(account_id="hello")

        def build_creative_legacy(self, req, ctx):
            return {}

        def preview_creative_legacy(self, req, ctx):
            return {}

        def list_creatives(self, req, ctx):
            return {}

        def get_creative_delivery(self, req, ctx):
            return {}

    validate_platform(_CompleteAdServerPlatform())


def test_creative_ad_server_required_methods_pinned() -> None:
    """Contract test — ``creative-ad-server`` requires the four
    methods JS marks non-optional in the Protocol interface
    (``build_creative_legacy``, ``preview_creative_legacy``, ``list_creatives``,
    ``get_creative_delivery``). ``sync_creatives`` is optional in
    JS too."""
    expected = {
        "build_creative_legacy",
        "preview_creative_legacy",
        "list_creatives",
        "get_creative_delivery",
    }
    assert REQUIRED_METHODS_PER_SPECIALISM["creative-ad-server"] == expected


def test_creative_ad_server_distinct_from_builder() -> None:
    """The two creative Protocols enforce different method sets — an
    ad-server adopter must implement four methods; a builder adopter
    only one. Confirms the architectural distinction at the
    REQUIRED_METHODS layer."""
    builder_methods = REQUIRED_METHODS_PER_SPECIALISM["creative-template"]
    ad_server_methods = REQUIRED_METHODS_PER_SPECIALISM["creative-ad-server"]
    # Builder is a strict subset of ad-server (the legacy build method is shared).
    assert builder_methods < ad_server_methods
    # But ad-server has extra requirements (preview, list, delivery).
    assert ad_server_methods - builder_methods == {
        "preview_creative_legacy",
        "list_creatives",
        "get_creative_delivery",
    }


# ---- CampaignGovernancePlatform ----


def test_campaign_governance_runtime_checkable_full() -> None:
    """A class with all four governance methods passes
    ``isinstance`` against :class:`CampaignGovernancePlatform`."""

    class _GovernanceImpl:
        def check_governance(self, req, ctx):
            return {"status": "approved"}

        def sync_plans(self, req, ctx):
            return {"plans": []}

        def report_plan_outcome(self, req, ctx):
            return {}

        def get_plan_audit_logs(self, req, ctx):
            return {"logs": []}

    assert isinstance(_GovernanceImpl(), CampaignGovernancePlatform)


def _make_complete_governance_platform_class(governance_aware: bool):
    """Helper: build a governance platform with all four required
    methods + the given ``governance_aware`` flag."""

    class _CompleteGovernancePlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-spend-authority"],
            governance_aware=governance_aware,
        )
        accounts = SingletonAccounts(account_id="hello")

        def check_governance(self, req, ctx):
            return {}

        def sync_plans(self, req, ctx):
            return {}

        def report_plan_outcome(self, req, ctx):
            return {}

        def get_plan_audit_logs(self, req, ctx):
            return {}

    return _CompleteGovernancePlatform


def test_validate_platform_enforces_governance_spend_authority_methods() -> None:
    """A platform claiming ``governance-spend-authority`` without
    implementing the four required methods fails fast at server boot.
    Use ``governance_aware=True`` to isolate the required-method gate
    from the governance-aware security gate."""

    class _PartialGovernancePlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-spend-authority"],
            governance_aware=True,
        )
        accounts = SingletonAccounts(account_id="hello")

        # Implements only check_governance + sync_plans;
        # missing report_plan_outcome + get_plan_audit_logs.
        def check_governance(self, req, ctx):
            return {}

        def sync_plans(self, req, ctx):
            return {}

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialGovernancePlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "report_plan_outcome" in missing_methods
    assert "get_plan_audit_logs" in missing_methods


def test_validate_platform_passes_for_complete_governance_platform() -> None:
    """Happy path — fully-implemented governance platform with
    ``governance_aware=True`` passes both gates."""
    validate_platform(_make_complete_governance_platform_class(governance_aware=True)())


def test_governance_security_gate_independent_of_required_methods() -> None:
    """SECURITY REGRESSION GUARD: A platform with all four governance
    methods present but ``governance_aware=False`` STILL fails server
    boot. Required-method enforcement and governance-aware enforcement
    are independent gates; both fire.

    Without this invariant, an adopter who happens to satisfy the
    method coverage could silently skip the governance security gate
    — which is the exact regression the foundation's
    ``validate_platform`` was designed to prevent. This test pins
    that the addition of required-method coverage in Batch 3 doesn't
    accidentally short-circuit the security gate."""
    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_make_complete_governance_platform_class(governance_aware=False)())
    msg = str(exc_info.value).lower()
    assert "governance" in msg
    # Verify the failure is the governance-aware gate, NOT a
    # required-methods complaint (the methods ARE all implemented).
    assert "governance_aware" in str(exc_info.value)


def test_governance_specialisms_share_method_set() -> None:
    """Both governance-AGENT specialisms gate on the same four
    methods. Drift in REQUIRED_METHODS_PER_SPECIALISM here surfaces
    as a visible test failure — they share the
    CampaignGovernancePlatform Protocol surface."""
    expected = {
        "check_governance",
        "sync_plans",
        "report_plan_outcome",
        "get_plan_audit_logs",
    }
    assert REQUIRED_METHODS_PER_SPECIALISM["governance-spend-authority"] == expected
    assert REQUIRED_METHODS_PER_SPECIALISM["governance-delivery-monitor"] == expected


def test_governance_aware_seller_is_not_a_governance_agent_protocol() -> None:
    """``governance-aware-seller`` names a SELLER claim — a sales-*
    archetype that composes with a governance agent. It does NOT
    implement CampaignGovernancePlatform; it integrates WITH a
    platform that does. The slug stays unenforced in
    REQUIRED_METHODS_PER_SPECIALISM (no method-coverage rule) until
    sync_governance handler shim wiring lands for sales adopters.

    Pins the architectural distinction: governance-aware-seller is
    NOT in the REQUIRED_METHODS map; the other two governance-* slugs
    ARE. All three remain in GOVERNANCE_SPECIALISMS for the
    foundation's governance-aware security gate."""
    from adcp.decisioning import GOVERNANCE_SPECIALISMS

    assert "governance-aware-seller" in GOVERNANCE_SPECIALISMS
    assert "governance-aware-seller" not in REQUIRED_METHODS_PER_SPECIALISM
    assert "governance-spend-authority" in REQUIRED_METHODS_PER_SPECIALISM
    assert "governance-delivery-monitor" in REQUIRED_METHODS_PER_SPECIALISM


# ---- BrandRightsPlatform ----


def test_brand_rights_runtime_checkable() -> None:
    """A class with the required and optional brand-rights methods passes
    ``isinstance`` against :class:`BrandRightsPlatform`."""

    class _BrandRightsImpl:
        def get_brand_identity(self, req, ctx):
            return {}

        def get_rights(self, req, ctx):
            return {"rights": []}

        def acquire_rights(self, req, ctx):
            return {}

        def verify_brand_claim(self, req, ctx):
            return {}

        def verify_brand_claims(self, req, ctx):
            return {}

    assert isinstance(_BrandRightsImpl(), BrandRightsPlatform)


def test_validate_platform_enforces_brand_rights_methods() -> None:
    """A platform claiming ``brand-rights`` without all three
    required methods fails fast at server boot."""

    class _PartialBrandRightsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["brand-rights"])
        accounts = SingletonAccounts(account_id="hello")

        # Missing get_brand_identity + acquire_rights.
        def get_rights(self, req, ctx):
            return {}

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialBrandRightsPlatform())
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "get_brand_identity" in missing_methods
    assert "acquire_rights" in missing_methods


def test_brand_rights_required_methods_pinned() -> None:
    """Contract test — ``brand-rights`` requires the three sync wire
    tools per ``schemas/cache/brand/*``."""
    assert REQUIRED_METHODS_PER_SPECIALISM["brand-rights"] == {
        "get_brand_identity",
        "get_rights",
        "acquire_rights",
    }


# ---- ContentStandardsPlatform ----


def test_content_standards_runtime_checkable_full() -> None:
    """A class with all 8 content-standards methods (6 required + 2
    optional analyzer reads) passes the strict structural match."""

    class _ContentStandardsImpl:
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

        def get_media_buy_artifacts(self, req, ctx):
            return {}

        def get_creative_features(self, req, ctx):
            return {}

    assert isinstance(_ContentStandardsImpl(), ContentStandardsPlatform)


def test_validate_platform_enforces_content_standards_required_methods() -> None:
    """A platform claiming ``content-standards`` without all six
    required methods fails fast. Analyzer reads
    (``get_media_buy_artifacts``, ``get_creative_features``) are
    optional and don't gate server boot."""

    class _PartialContentStandardsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["content-standards"])
        accounts = SingletonAccounts(account_id="hello")

        # Missing 4 of 6 required methods.
        def list_content_standards(self, req, ctx):
            return {}

        def get_content_standards(self, req, ctx):
            return {}

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialContentStandardsPlatform())
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "create_content_standards" in missing_methods
    assert "update_content_standards" in missing_methods
    assert "calibrate_content" in missing_methods
    assert "validate_content_delivery" in missing_methods


def test_validate_platform_passes_content_standards_minimal() -> None:
    """Minimal compliant ``content-standards`` adopter — implements
    only the 6 required methods, no analyzer reads. Validates."""

    class _MinimalContentStandardsPlatform(DecisioningPlatform):
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

    validate_platform(_MinimalContentStandardsPlatform())


def test_content_standards_required_methods_pinned() -> None:
    """Contract test — ``content-standards`` requires 6 methods.
    Analyzer reads are optional."""
    expected = {
        "list_content_standards",
        "get_content_standards",
        "create_content_standards",
        "update_content_standards",
        "calibrate_content",
        "validate_content_delivery",
    }
    assert REQUIRED_METHODS_PER_SPECIALISM["content-standards"] == expected


# ---- PropertyListsPlatform / CollectionListsPlatform ----


def test_property_lists_runtime_checkable() -> None:
    """A class with the 5 property-list CRUD methods passes the
    structural match."""

    class _PropertyListsImpl:
        def create_property_list(self, req, ctx):
            return {}

        def update_property_list(self, req, ctx):
            return {}

        def get_property_list(self, req, ctx):
            return {}

        def list_property_lists(self, req, ctx):
            return {}

        def delete_property_list(self, req, ctx):
            return {}

    assert isinstance(_PropertyListsImpl(), PropertyListsPlatform)


def test_collection_lists_runtime_checkable() -> None:
    """A class with the 5 collection-list CRUD methods passes."""

    class _CollectionListsImpl:
        def create_collection_list(self, req, ctx):
            return {}

        def update_collection_list(self, req, ctx):
            return {}

        def get_collection_list(self, req, ctx):
            return {}

        def list_collection_lists(self, req, ctx):
            return {}

        def delete_collection_list(self, req, ctx):
            return {}

    assert isinstance(_CollectionListsImpl(), CollectionListsPlatform)


def test_validate_platform_enforces_property_lists_methods() -> None:
    """``property-lists`` requires all 5 CRUD methods (no optional)."""

    class _PartialPropertyListsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["property-lists"])
        accounts = SingletonAccounts(account_id="hello")

        # Missing delete_property_list — security-critical revocation
        # path. The required-methods gate catches this at server boot
        # so an adopter can't ship a list-publishing surface without
        # the revocation primitive.
        def create_property_list(self, req, ctx):
            return {}

        def update_property_list(self, req, ctx):
            return {}

        def get_property_list(self, req, ctx):
            return {}

        def list_property_lists(self, req, ctx):
            return {}

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialPropertyListsPlatform())
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "delete_property_list" in missing_methods


def test_validate_platform_enforces_collection_lists_methods() -> None:
    """``collection-lists`` mirrors ``property-lists`` — same 5-method
    CRUD shape on collection-list types."""

    class _MinimalCollectionListsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["collection-lists"])
        accounts = SingletonAccounts(account_id="hello")

        def create_collection_list(self, req, ctx):
            return {}

        def update_collection_list(self, req, ctx):
            return {}

        def get_collection_list(self, req, ctx):
            return {}

        def list_collection_lists(self, req, ctx):
            return {}

        def delete_collection_list(self, req, ctx):
            return {}

    validate_platform(_MinimalCollectionListsPlatform())


def test_lists_required_methods_pinned() -> None:
    """Contract test — both list specialisms require their respective
    5-method CRUD set. Drift here surfaces as a visible failure
    since the Protocol surfaces should track together."""
    assert REQUIRED_METHODS_PER_SPECIALISM["property-lists"] == {
        "create_property_list",
        "update_property_list",
        "get_property_list",
        "list_property_lists",
        "delete_property_list",
    }
    assert REQUIRED_METHODS_PER_SPECIALISM["collection-lists"] == {
        "create_collection_list",
        "update_collection_list",
        "get_collection_list",
        "list_collection_lists",
        "delete_collection_list",
    }


# ---- Breadth-sprint completeness pin ----


def test_every_spec_slug_except_governance_aware_seller_is_enforced() -> None:
    """Breadth sprint complete: every spec specialism slug except
    ``governance-aware-seller`` has a REQUIRED_METHODS_PER_SPECIALISM
    entry. ``governance-aware-seller`` stays unenforced by design —
    it's a SELLER composition claim (sales-* archetype that
    integrates with a governance agent), NOT a wire-implementor
    claim."""
    from adcp.decisioning.dispatch import SPEC_SPECIALISM_ENUM

    enforced = set(REQUIRED_METHODS_PER_SPECIALISM.keys())
    spec = set(SPEC_SPECIALISM_ENUM)
    # ``signed-requests`` is deprecated per spec (moved to universal
    # storyboards); not a Protocol-implementor claim.
    unenforced = spec - enforced
    assert unenforced == {"governance-aware-seller", "signed-requests"}, (
        f"Unexpected unenforced spec slugs: {unenforced}. After the "
        "breadth sprint, only ``governance-aware-seller`` (SELLER "
        "composition claim) and ``signed-requests`` (deprecated, "
        "moved to universal storyboards) should be unenforced."
    )
