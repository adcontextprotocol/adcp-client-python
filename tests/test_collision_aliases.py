"""Tests for cross-module name collision aliases (#911, Step 2).

Several bare type names are defined in more than one generated_poc module
(snapshotted in scripts/collision_allowlist.json). When adopters write
``from adcp.types import Creative`` they silently get whichever module wins
the consolidate sort order. aliases.py provides ``<Context><BaseName>`` aliases
so each per-module variant can be imported unambiguously.

These tests assert that each alias resolves to the class defined in its named
source module (by ``__module__``), NOT to the first-sorted winner, so that a
future renumber or sort-order shift can never silently repoint an alias.
"""

from __future__ import annotations

import importlib

import pytest

# (alias name, source module under generated_poc, base class name in that module)
COLLISION_ALIASES: list[tuple[str, str, str]] = [
    # Creative — 4 single-class variants. ListCreativesCreative is a union
    # since AdCP 3.1.8 split the listing record into legacy-format and
    # canonical-format branches.
    ("DeliveryCreative", "creative.get_creative_delivery_response", "Creative"),
    ("SyncCreativesCreative", "creative.sync_creatives_response", "Creative"),
    ("BuildCreativeCreative", "media_buy.build_creative_response", "Creative"),
    ("CapabilitiesCreative", "protocol.get_adcp_capabilities_response", "Creative"),
    # Account — 4 meaningful variants
    ("CoreAccount", "core.account", "Account"),
    ("SyncAccountsAccount", "account.sync_accounts_response", "Account"),
    ("SyncGovernanceAccount", "account.sync_governance_request", "Account"),
    ("CapabilitiesAccount", "protocol.get_adcp_capabilities_response", "Account"),
    # Authentication — 5 variants
    ("PushNotificationAuthentication", "core.push_notification_config", "Authentication"),
    ("NotificationAuthentication", "core.notification_config", "Authentication"),
    ("ReportingWebhookAuthentication", "core.reporting_webhook", "Authentication"),
    ("GovernanceAuthentication", "account.sync_governance_request", "Authentication"),
    ("CreateMediaBuyAuthentication", "media_buy.create_media_buy_request", "Authentication"),
    # MediaBuy — 3 variants
    ("CoreMediaBuy", "core.media_buy", "MediaBuy"),
    ("GetMediaBuysMediaBuy", "media_buy.get_media_buys_response", "MediaBuy"),
    ("CapabilitiesMediaBuy", "protocol.get_adcp_capabilities_response", "MediaBuy"),
    # GovernanceAgent — 2 variants
    ("CoreGovernanceAgent", "core.account", "GovernanceAgent"),
    ("SyncGovernanceGovernanceAgent", "account.sync_governance_request", "GovernanceAgent"),
    # CreditLimit — 2 variants
    ("CoreCreditLimit", "core.account", "CreditLimit"),
    ("SyncAccountsCreditLimit", "account.sync_accounts_response", "CreditLimit"),
    # Setup — 3 variants
    ("CoreSetup", "core.account", "Setup"),
    ("SyncAccountsSetup", "account.sync_accounts_response", "Setup"),
    ("SyncEventSourcesSetup", "media_buy.sync_event_sources_response", "Setup"),
    # Sort — 3 variants
    ("ListCreativesSort", "creative.list_creatives_request", "Sort"),
    ("TasksListSort", "core.tasks_list_request", "Sort"),
    ("ListTasksSort", "protocol.list_tasks_request", "Sort"),
    # Signal — 2 variants
    ("GetSignalsSignal", "signals.get_signals_response", "Signal"),
    ("WholesaleFeedSignal", "core.wholesale_feed_event", "Signal"),
    # DeclaredBy — 2 variants
    ("ProvenanceDeclaredBy", "core.provenance", "DeclaredBy"),
    ("SiSponsoredContextDeclaredBy", "sponsored_intelligence.si_sponsored_context", "DeclaredBy"),
    # TmpxMacro — 2 variants
    ("IdentityMatchTmpxMacro", "trusted_match.identity_match_response", "TmpxMacro"),
    ("ProviderRegistrationTmpxMacro", "trusted_match.provider_registration", "TmpxMacro"),
    # Unit — 4 variants
    ("DurationUnit", "core.duration", "Unit"),
    ("OverlayUnit", "core.overlay", "Unit"),
    ("RealEstateUnit", "core.real_estate_item", "Unit"),
    ("VehicleUnit", "core.vehicle_item", "Unit"),
]


def _source_class(module_suffix: str, base_name: str) -> type:
    module = importlib.import_module(f"adcp.types.generated_poc.{module_suffix}")
    return getattr(module, base_name)


@pytest.mark.parametrize(("alias_name", "module_suffix", "base_name"), COLLISION_ALIASES)
def test_alias_resolves_to_named_module_variant(
    alias_name: str, module_suffix: str, base_name: str
) -> None:
    """Each alias is the exact class defined in its named source module."""
    from adcp.types import aliases

    alias_cls = getattr(aliases, alias_name)
    expected = _source_class(module_suffix, base_name)
    assert alias_cls is expected, (
        f"{alias_name} should be {base_name} from {module_suffix}, "
        f"got a class from {alias_cls.__module__}"
    )


@pytest.mark.parametrize(("alias_name", "module_suffix", "base_name"), COLLISION_ALIASES)
def test_alias_importable_from_adcp_types(
    alias_name: str, module_suffix: str, base_name: str
) -> None:
    """Each alias is importable from the public ``adcp.types`` surface."""
    import adcp.types as types_module

    assert hasattr(types_module, alias_name), f"{alias_name} not on adcp.types"
    assert alias_name in types_module.__all__, f"{alias_name} not in adcp.types.__all__"
    # Public surface must agree with the aliases module identity.
    from adcp.types import aliases

    assert getattr(types_module, alias_name) is getattr(aliases, alias_name)


@pytest.mark.parametrize(("alias_name", "module_suffix", "base_name"), COLLISION_ALIASES)
def test_alias_in_aliases_all(alias_name: str, module_suffix: str, base_name: str) -> None:
    """Each alias is listed in ``aliases.__all__``."""
    from adcp.types import aliases

    assert alias_name in aliases.__all__, f"{alias_name} missing from aliases.__all__"


def test_alias_set_is_internally_consistent() -> None:
    """The alias table has no duplicate alias names and covers each base
    name's set of source modules with one alias per (base, module) pair."""
    alias_names = [a for a, _, _ in COLLISION_ALIASES]
    assert len(alias_names) == len(set(alias_names)), "duplicate alias names in table"

    seen: set[tuple[str, str]] = set()
    for _alias, module_suffix, base_name in COLLISION_ALIASES:
        key = (base_name, module_suffix)
        assert key not in seen, f"duplicate (base, module) pair: {key}"
        seen.add(key)


def test_distinct_variants_are_distinct_classes() -> None:
    """Aliases that name structurally different variants are not the same class.

    These are the silent-swap cases from #911 — the whole reason the aliases
    exist. If two of these ever became the same object, the disambiguation
    would be meaningless.
    """
    from adcp.types import aliases as a

    # Creative: delivery (6 fields) vs listing (17+ fields) vs sync result.
    assert a.DeliveryCreative is not a.ListCreativesCreative
    assert a.DeliveryCreative is not a.SyncCreativesCreative
    assert a.ListCreativesCreative is not a.SyncCreativesCreative

    # Account: full core entity vs sync-accounts response item.
    assert a.CoreAccount is not a.SyncAccountsAccount
    assert a.CoreAccount is not a.SyncGovernanceAccount

    # Authentication: notification_config makes credentials optional.
    assert a.NotificationAuthentication is not a.PushNotificationAuthentication

    # MediaBuy: core entity vs capabilities descriptor.
    assert a.CoreMediaBuy is not a.CapabilitiesMediaBuy
    assert a.CoreMediaBuy is not a.GetMediaBuysMediaBuy

    # GovernanceAgent / CreditLimit / Setup: core vs sync variants.
    assert a.CoreGovernanceAgent is not a.SyncGovernanceGovernanceAgent
    assert a.CoreCreditLimit is not a.SyncAccountsCreditLimit
    assert a.CoreSetup is not a.SyncAccountsSetup
    assert a.CoreSetup is not a.SyncEventSourcesSetup

    # Sort: list_creatives uses a different field enum.
    assert a.ListCreativesSort is not a.TasksListSort

    # Signal: get_signals discovery shape vs wholesale feed event.
    assert a.GetSignalsSignal is not a.WholesaleFeedSignal

    # DeclaredBy: provenance and SI sponsored context use different role enums.
    assert a.ProvenanceDeclaredBy is not a.SiSponsoredContextDeclaredBy

    # TmpxMacro: emitted macro/value pairs vs registered macro-name strings.
    assert a.IdentityMatchTmpxMacro is not a.ProviderRegistrationTmpxMacro

    # Unit: four distinct unit enums.
    assert len({a.DurationUnit, a.OverlayUnit, a.RealEstateUnit, a.VehicleUnit}) == 4


def test_notification_config_authentication_makes_credentials_optional() -> None:
    """The notification_config variant differs from the others: credentials
    is optional there but required in push_notification_config. This is the
    concrete shape difference that makes the silent swap unsafe (#911)."""
    from adcp.types import aliases as a

    assert a.NotificationAuthentication.model_fields["credentials"].is_required() is False
    assert a.PushNotificationAuthentication.model_fields["credentials"].is_required() is True


def test_listing_creative_is_the_rich_shape() -> None:
    """ListCreativesCreative is the rich record union adopters usually want,
    distinct from the lean delivery-totals Creative that wins the bare name.
    Asserting a couple of marker fields guards the mapping by shape."""
    from adcp.types import aliases as a
    from adcp.types.generated_poc.creative.list_creatives_response import (
        Creatives,
        Creatives1,
    )

    delivery_fields = set(a.DeliveryCreative.model_fields)

    # Delivery variant is the lean totals view.
    assert "totals" in delivery_fields
    assert "variant_count" in delivery_fields
    assert a.ListCreativesCreative == (Creatives | Creatives1)

    # Both listing branches carry the full creative record.
    for listing_cls in (Creatives, Creatives1):
        listing_fields = set(listing_cls.model_fields)
        assert "status" in listing_fields
        assert "assets" in listing_fields
        assert "assignments" in listing_fields
        assert listing_fields != delivery_fields


def test_tmpx_macro_aliases_cover_distinct_shapes() -> None:
    """TMPX macro aliases distinguish emitted values from registered names."""
    from adcp.types import aliases as a

    assert set(a.IdentityMatchTmpxMacro.model_fields) == {"name", "value"}
    assert set(a.ProviderRegistrationTmpxMacro.model_fields) == {"root"}
