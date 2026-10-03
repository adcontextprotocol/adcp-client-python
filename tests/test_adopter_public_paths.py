"""Every generated type a production adopter imports has a public path.

The names below are the complete set of ``adcp.types.generated_poc`` imports in
one production AdCP seller — 110 ``(module, type)`` pairs over 105 distinct
names across 39 files. They are in this suite because they are evidence rather
than a hypothesis: a real application reached into a private namespace 110 times
because the public one could not serve it.

Against ``adcp==8.0.0`` only 14 of the 110 had a public path that bound the same
class. 50 names were absent from ``adcp.types`` altogether and 46 resolved there
to a DIFFERENT class — the flat namespace binds one class per bare name, so for
a name several schemas declare it hands over whichever module won the sort
order. That winner is sometimes the laxer class: ``adcp.types.QuerySummary`` is
the ``tasks_list_response`` variant, which requires nothing, while the
``list_creatives_response`` variant requires ``returned`` and ``total_matching``
— so an adopter using the bare name validates documents the real field rejects,
with no symptom.

All 110 now resolve through ``adcp.types.domains.<domain>.<schema>`` to the
identical class, and most also through a shorter path. No skips, no exclusions.
"""

from __future__ import annotations

import importlib

import pytest

from scripts.consolidate_exports import schema_domain

#: ``(generated module, type name)``, every ``generated_poc`` import in the
#: adopter's ``src/`` tree. Regenerate by grepping
#: ``from adcp.types.generated_poc`` there; do not curate it, the point is that
#: it is unfiltered.
ADOPTER_IMPORTS: tuple[tuple[str, str], ...] = (
    ("account.sync_accounts_request", "Accounts"),
    ("account.sync_accounts_request", "Accounts1"),
    ("account.sync_accounts_response", "Account"),
    ("brand_discovery", "Brand"),
    ("brand_discovery", "BrandDiscovery3"),
    ("core.account", "CreditLimit"),
    ("core.account", "GovernanceAgent"),
    ("core.account", "Setup"),
    ("core.account_ref", "AccountReference1"),
    ("core.account_ref", "AccountReference2"),
    ("core.agent_signing_key", "AgentSigningKey"),
    ("core.attribution_window", "AttributionWindow"),
    ("core.business_entity", "BusinessEntity"),
    ("core.collection_list_ref", "CollectionListReference"),
    ("core.creative_asset", "CreativeAsset"),
    ("core.creative_filters", "CreativeFilters"),
    ("core.creative_item", "CreativeItem"),
    ("core.creative_item", "CreativeItem1"),
    ("core.creative_item", "CreativeItem2"),
    ("core.creative_variable", "CreativeVariable"),
    ("core.duration", "Unit"),
    ("core.error", "Issue"),
    ("core.experimental_feature_id", "ExperimentalFeatureId"),
    ("core.format", "Format"),
    ("core.geo_delivery_metrics", "GeoDeliveryMetrics"),
    ("core.media_buy_features", "MediaBuyFeatures"),
    ("core.notification_config", "Authentication"),
    ("core.notification_config", "EventType"),
    ("core.package", "Package"),
    ("core.pagination_response", "PaginationResponse"),
    ("core.placement", "Placement"),
    ("core.placement_delivery_metrics", "PlacementDeliveryMetrics"),
    ("core.postal_area_support", "PostalAreaSupport"),
    ("core.pricing_option", "PricingOption"),
    ("core.product", "Product"),
    ("core.product_filters", "ProductFilters"),
    ("core.property", "Identifier"),
    ("core.provenance", "Provenance"),
    ("core.publisher_property_selector", "PublisherPropertySelector1"),
    ("core.push_notification_config", "Authentication"),
    ("core.version_envelope", "AdcpVersionEnvelope"),
    ("core.webhook_challenge", "DeliveryAuth"),
    ("core.webhook_challenge", "WebhookChallenge"),
    ("creative.get_creative_delivery_response", "GetCreativeDeliveryResponse"),
    ("creative.list_creatives_request", "ListCreativesRequest"),
    ("creative.list_creatives_response", "Creative"),
    ("creative.list_creatives_response", "ListCreativesResponse"),
    ("creative.sync_creatives_request", "Assignment"),
    ("creative.sync_creatives_request", "SyncCreativesRequest"),
    ("creative.sync_creatives_response", "SyncCreativesResponse1"),
    ("enums.attribution_model", "AttributionModel"),
    ("enums.billing_party", "BillingParty"),
    ("enums.channels", "MediaChannel"),
    ("enums.creative_approval_status", "CreativeApprovalStatus"),
    ("enums.digital_source_type", "DigitalSourceType"),
    ("enums.media_buy_valid_action", "MediaBuyValidAction"),
    ("enums.metro_system", "MetroAreaSystem"),
    ("enums.pricing_model", "PricingModel"),
    ("enums.snapshot_unavailable_reason", "SnapshotUnavailableReason"),
    ("enums.specialism", "AdcpSpecialism"),
    ("enums.task_status", "TaskStatus"),
    ("media_buy.create_media_buy_request", "CreateMediaBuyRequest"),
    ("media_buy.create_media_buy_response", "CreateMediaBuyResponse1"),
    ("media_buy.create_media_buy_response", "CreateMediaBuyResponse2"),
    ("media_buy.create_media_buy_response", "CreateMediaBuyResponse3"),
    ("media_buy.get_media_buy_delivery_request", "AttributionWindow"),
    ("media_buy.get_media_buy_delivery_response", "ByDeviceTypeItem"),
    ("media_buy.get_media_buy_delivery_response", "GetMediaBuyDeliveryResponse"),
    ("media_buy.get_media_buy_delivery_response", "MediaBuyDelivery"),
    ("media_buy.get_media_buy_delivery_response", "NotificationType"),
    ("media_buy.get_media_buys_response", "CreativeApproval"),
    ("media_buy.get_media_buys_response", "GetMediaBuysResponse"),
    ("media_buy.get_media_buys_response", "MediaBuy"),
    ("media_buy.get_media_buys_response", "Package"),
    ("media_buy.get_media_buys_response", "Snapshot"),
    ("media_buy.get_products_request", "GetProductsRequest"),
    ("media_buy.get_products_response", "GetProductsResponse"),
    ("media_buy.list_creative_formats_response", "CreativeAgent"),
    ("media_buy.package_request", "PackageRequest"),
    ("media_buy.package_update", "PackageUpdate"),
    ("media_buy.update_media_buy_request", "UpdateMediaBuyRequest"),
    ("media_buy.update_media_buy_response", "UpdateMediaBuyResponse1"),
    ("media_buy.update_media_buy_response", "UpdateMediaBuyResponse2"),
    ("media_buy.update_media_buy_response", "UpdateMediaBuyResponse3"),
    ("pricing_options.time_option", "Parameters"),
    ("protocol.get_adcp_capabilities_request", "Protocol"),
    ("protocol.get_adcp_capabilities_response", "Account"),
    ("protocol.get_adcp_capabilities_response", "Adcp"),
    ("protocol.get_adcp_capabilities_response", "CapabilityReportingDeliveryMethod"),
    ("protocol.get_adcp_capabilities_response", "CreativeApprovalMode"),
    ("protocol.get_adcp_capabilities_response", "Execution"),
    ("protocol.get_adcp_capabilities_response", "GeoMetros"),
    ("protocol.get_adcp_capabilities_response", "Idempotency"),
    ("protocol.get_adcp_capabilities_response", "Idempotency1"),
    ("protocol.get_adcp_capabilities_response", "Identity"),
    ("protocol.get_adcp_capabilities_response", "KeyOrigins"),
    ("protocol.get_adcp_capabilities_response", "MajorVersion"),
    ("protocol.get_adcp_capabilities_response", "Measurement"),
    ("protocol.get_adcp_capabilities_response", "MediaBuy"),
    ("protocol.get_adcp_capabilities_response", "Portfolio"),
    ("protocol.get_adcp_capabilities_response", "PublisherDomain"),
    ("protocol.get_adcp_capabilities_response", "RequestSigning"),
    ("protocol.get_adcp_capabilities_response", "SupportedProtocol"),
    ("protocol.get_adcp_capabilities_response", "Targeting"),
    ("protocol.get_adcp_capabilities_response", "TrustedMatch"),
    ("protocol.get_adcp_capabilities_response", "WebhookSigning"),
    ("protocol.get_task_status_request", "GetTaskStatusRequest"),
    ("protocol.get_task_status_response", "HistoryItem"),
    ("protocol.list_tasks_response", "QuerySummary"),
    ("protocol.list_tasks_response", "Task"),
)


def _public_paths(module_name: str, type_name: str) -> list[str]:
    """Every public path that binds exactly the class the deep import binds."""
    import adcp.types
    import adcp.types.domains

    target = getattr(importlib.import_module(f"adcp.types.generated_poc.{module_name}"), type_name)
    paths = []
    if getattr(adcp.types, type_name, None) is target:
        paths.append(f"adcp.types.{type_name}")
    for candidate in (
        f"adcp.types.domains.{schema_domain(module_name)}",
        f"adcp.types.domains.{module_name}",
    ):
        try:
            module = importlib.import_module(candidate)
        except ModuleNotFoundError:
            continue
        if getattr(module, type_name, None) is target:
            paths.append(f"{candidate}.{type_name}")
    return paths


def test_the_adopter_import_list_is_not_empty_and_is_unfiltered() -> None:
    """Guard the evidence: a shrunk list would make the rest of this file vacuous."""
    assert len(ADOPTER_IMPORTS) == 110
    assert len({name for _, name in ADOPTER_IMPORTS}) == 105
    assert len(set(ADOPTER_IMPORTS)) == len(ADOPTER_IMPORTS)


@pytest.mark.parametrize(("module_name", "type_name"), ADOPTER_IMPORTS)
def test_every_adopter_import_has_a_public_path_to_the_same_class(
    module_name: str, type_name: str
) -> None:
    """The deep import and the public path must be the same object, not the same shape.

    No skips and no exclusions. Same shape is not enough: ``isinstance`` and
    every annotation compare by identity, so a public name bound to a
    structurally identical twin still fails to type-check against the field it
    was read from.
    """
    paths = _public_paths(module_name, type_name)
    assert paths, (
        f"{module_name}.{type_name} has no public path — an adopter has to reach into "
        "adcp.types.generated_poc for it, which the migration guide forbids"
    )


def test_the_schema_module_path_serves_every_adopter_import() -> None:
    """The mirror alone covers all of them, without help from the flatter layers.

    The flat namespace and the domain roots are conveniences: one binds a name
    per collision-free type, the other a name per type its domain declares once.
    Only the path that names the defining schema is guaranteed, so that is the
    one asserted whole.
    """
    missing = sorted(
        f"{module_name}.{type_name}"
        for module_name, type_name in ADOPTER_IMPORTS
        if f"adcp.types.domains.{module_name}.{type_name}"
        not in _public_paths(module_name, type_name)
    )
    assert missing == []


def test_the_intra_domain_collisions_are_what_needed_the_extra_depth() -> None:
    """Pin why the domain root was not enough, so the depth is not mistaken for noise.

    ``core`` declares nine different ``Unit`` classes. A domain is one
    namespace, so it cannot bind the name at all — and before the mirror those
    nine had no public path between them.
    """
    import adcp.types.domains.core as core_domain

    units = {
        module_name
        for type_name, module_name in (("Unit", m) for m in _declaring_modules("Unit"))
        if schema_domain(module_name) == "core"
    }
    assert len(units) >= 9, units
    assert "Unit" not in core_domain.__all__

    resolved = {
        getattr(importlib.import_module(f"adcp.types.domains.{module_name}"), "Unit")
        for module_name in units
    }
    assert len(resolved) == len(units), "each schema's Unit must be its own class"


def _declaring_modules(type_name: str) -> set[str]:
    """Every generated module that declares ``type_name``."""
    from scripts.consolidate_exports import scan_declared_names

    return scan_declared_names().get(type_name, set())
