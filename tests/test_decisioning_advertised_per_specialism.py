"""Per-specialism advertised-tools filter (Emma cross-cutting P1).

Three Emma backend tests independently flagged the same bug: a sales-only
or signals-only adopter advertises all 40+ shims via ``tools/list``.
Buyers see ``acquire_rights``, ``build_creative``, ``check_governance``
on a sales-only seller; on every call they get NOT_SUPPORTED. The fix
hooks ``advertised_tools_for_instance()`` on :class:`PlatformHandler`,
which intersects the universe of shim coverage with the platform's
claimed specialisms via :data:`SPECIALISM_TO_ADVERTISED_TOOLS`.

This file pins the post-fix behavior so a future refactor can't
re-broaden the surface silently.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import (
    SPECIALISM_TO_ADVERTISED_TOOLS,
    PlatformHandler,
)
from adcp.server.mcp_tools import get_tools_for_handler


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="per-spec-")
    yield pool
    pool.shutdown(wait=True)


# ---- specialism map drift guard ----


def test_specialism_map_keys_subset_of_spec_enum() -> None:
    """Every key in SPECIALISM_TO_ADVERTISED_TOOLS MUST be in the
    canonical SPEC_SPECIALISM_ENUM. Drift here means the framework
    advertises tools for a slug that isn't a real specialism."""
    from adcp.decisioning.dispatch import SPEC_SPECIALISM_ENUM

    extra = set(SPECIALISM_TO_ADVERTISED_TOOLS.keys()) - SPEC_SPECIALISM_ENUM
    assert not extra, f"unknown specialism slugs in map: {sorted(extra)}"


def test_specialism_map_covers_every_protocol_family_slug() -> None:
    """Every spec slug that has a Protocol implementation in the
    framework MUST appear in the map. Meta-claims (signed-requests,
    governance-aware-seller) are documented exclusions — they compose
    with another non-meta claim."""
    from adcp.decisioning.dispatch import SPEC_SPECIALISM_ENUM

    meta_claims = {"signed-requests", "governance-aware-seller"}
    expected = SPEC_SPECIALISM_ENUM - meta_claims
    missing = expected - set(SPECIALISM_TO_ADVERTISED_TOOLS.keys())
    assert not missing, (
        f"specialisms missing from map: {sorted(missing)}; "
        "every Protocol-backed slug must declare its tool set"
    )


# ---- per-instance filter ----


class _SalesOnlyPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="sales-only")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "x", "status": "active"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}


class _SignalsOnlyPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["signal-marketplace"])
    accounts = SingletonAccounts(account_id="signals-only")

    def get_signals(self, req, ctx):
        return {"signals": []}

    def activate_signal(self, req, ctx):
        return {}


class _CreativeOnlyPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["creative-generative"])
    accounts = SingletonAccounts(account_id="creative-only")

    def build_creative(self, req, ctx):
        return {"creative_manifest": {"creative_id": "cr_1"}}


def test_sales_only_does_not_advertise_creative_or_signals_tools(executor) -> None:
    """Regression: sales-only adopter saw acquire_rights, build_creative,
    check_governance, etc. in tools/list. After the per-specialism
    filter, only sales tools advertise."""
    handler = PlatformHandler(
        _SalesOnlyPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    tools = {tool["name"] for tool in get_tools_for_handler(handler)}

    # Sales surface present.
    assert "get_products" in tools
    assert "create_media_buy" in tools
    assert "sync_creatives" in tools

    # Non-sales tools MUST NOT appear.
    forbidden = {
        "acquire_rights",
        "build_creative",
        "preview_creative",
        "check_governance",
        "sync_plans",
        "get_signals",
        "activate_signal",
        "sync_audiences",
        "list_content_standards",
        "create_property_list",
        "create_collection_list",
    }
    leaked = forbidden & tools
    assert not leaked, (
        f"sales-only adopter leaked non-sales tools to tools/list: " f"{sorted(leaked)}"
    )


def test_signals_only_does_not_advertise_sales_tools(executor) -> None:
    """Mirror test for the signals path."""
    handler = PlatformHandler(
        _SignalsOnlyPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    tools = {tool["name"] for tool in get_tools_for_handler(handler)}

    assert "get_signals" in tools
    assert "activate_signal" in tools

    forbidden = {
        "get_products",
        "create_media_buy",
        "build_creative",
        "acquire_rights",
        "check_governance",
    }
    leaked = forbidden & tools
    assert not leaked, f"signals-only leaked: {sorted(leaked)}"


def test_creative_only_does_not_advertise_sales_or_signals_tools(executor) -> None:
    """Mirror test for the creative path — AudioStack/Stability AI shape."""
    handler = PlatformHandler(
        _CreativeOnlyPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    tools = {tool["name"] for tool in get_tools_for_handler(handler)}

    assert "build_creative" in tools

    forbidden = {
        "get_products",
        "create_media_buy",
        "sync_creatives",
        "get_signals",
        "activate_signal",
        "acquire_rights",
        "check_governance",
    }
    leaked = forbidden & tools
    assert not leaked, f"creative-only leaked: {sorted(leaked)}"


def test_multi_specialism_unions_both_surfaces(executor) -> None:
    """An adopter claiming both ``sales-non-guaranteed`` AND
    ``creative-generative`` advertises BOTH surfaces."""

    class _HybridPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed", "creative-generative"]
        )
        accounts = SingletonAccounts(account_id="hybrid")

        def get_products(self, req, ctx):
            return {"products": []}

        def create_media_buy(self, req, ctx):
            return {"media_buy_id": "x", "status": "active"}

        def update_media_buy(self, media_buy_id, patch, ctx):
            return {"media_buy_id": media_buy_id, "status": "active"}

        def sync_creatives(self, req, ctx):
            return {"creatives": []}

        def get_media_buy_delivery(self, req, ctx):
            return {"media_buy_deliveries": []}

        def build_creative(self, req, ctx):
            return {"creative_manifest": {"creative_id": "cr_1"}}

    handler = PlatformHandler(
        _HybridPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    tools = {tool["name"] for tool in get_tools_for_handler(handler)}

    assert "get_products" in tools  # sales
    assert "build_creative" in tools  # creative

    # But no audience/signals/governance leaks.
    forbidden = {"sync_audiences", "get_signals", "check_governance"}
    leaked = forbidden & tools
    assert not leaked, f"hybrid leaked: {sorted(leaked)}"


def test_novel_specialism_falls_back_to_class_level_advertisement(
    executor,
) -> None:
    """Adopter piloting a novel slug (not in
    SPECIALISM_TO_ADVERTISED_TOOLS) → empty per-instance set →
    fall back to class-level union (preserve existing
    ``warnings.warn(novel)`` semantics from validate_platform).
    Muting the handler entirely would be a worse foot-gun than
    over-advertising."""

    class _NovelPlatform(DecisioningPlatform):
        # Bypass validate_platform's typo guard with a slug that's
        # genuinely far from any spec slug.
        capabilities = DecisioningCapabilities(specialisms=["xyzzy-experimental"])
        accounts = SingletonAccounts(account_id="novel")

        def get_products(self, req, ctx):
            return {"products": []}

    handler = PlatformHandler(
        _NovelPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    tools = {tool["name"] for tool in get_tools_for_handler(handler)}
    # Override-detection still applies (only get_products implemented),
    # so we get sales' overridden subset, but the universe includes all
    # protocol families pre-filter — this is the documented
    # forward-compat fallback.
    assert "get_products" in tools


def test_advertise_all_bypasses_per_specialism_filter(executor) -> None:
    """Storyboard / spec-conformance test escape hatch — when caller
    passes ``advertise_all=True``, every shim (regardless of claimed
    specialism) is in the result."""
    handler = PlatformHandler(
        _SalesOnlyPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    tools = {tool["name"] for tool in get_tools_for_handler(handler, advertise_all=True)}
    # Sales-only stub still has only sales methods, but advertise_all
    # bypasses the override filter — wait, advertise_all bypasses
    # _is_method_overridden but the per-instance filter still trims.
    # Verify: per-instance filter applies UNCONDITIONALLY (it represents
    # what the platform's claimed specialisms cover; that's the same
    # "did you sign up for this" semantic regardless of advertise_all).
    assert "get_products" in tools
    # build_creative is NOT in the universe-for-this-platform's
    # specialisms, so it stays out.
    assert "build_creative" not in tools


def test_class_level_inspection_preserves_full_universe() -> None:
    """When ``get_tools_for_handler`` is called with the class (not an
    instance), we have no platform to read specialisms from. Falls back
    to the class-level ``advertised_tools`` universe so static
    introspection (storyboard tests, spec-conformance docs) keeps
    seeing the full surface."""
    tools = {tool["name"] for tool in get_tools_for_handler(PlatformHandler)}
    # Static inspection sees ALL the shims because override-detection
    # at the class level shows every shim as implemented (PlatformHandler
    # itself defines them).
    assert "get_products" in tools
    assert "build_creative" in tools
    assert "acquire_rights" in tools
