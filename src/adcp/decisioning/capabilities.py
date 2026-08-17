# ruff: noqa: E501
"""Capability sub-models for declaring :class:`DecisioningCapabilities`.

The canonical adopter-facing namespace for typed capability declarations.
Mirrors the AdCP ``get_adcp_capabilities`` response wire schema 1:1 — every
sub-model name in this module matches the wire field type it populates.

Typical usage::

    from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform
    from adcp.decisioning.capabilities import (
        Account, MediaBuy, Targeting, GeoMetros,
        IdempotencySupported, Specialism,
    )

    class HelloSeller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=[Specialism.sales_non_guaranteed],
            adcp=Adcp(
                major_versions=[3],
                idempotency=IdempotencySupported(
                    supported=True, replay_ttl_seconds=86400,
                ),
            ),
            account=Account(supported_billing=["operator"]),
            media_buy=MediaBuy(
                supported_pricing_models=["cpm"],
                execution=Execution(
                    targeting=Targeting(
                        geo_countries=True,
                        geo_metros=GeoMetros(nielsen_dma=True),
                    ),
                ),
            ),
        )

The names ``Account``, ``MediaBuy``, ``Creative`` collide with unrelated
wire types in :mod:`adcp.types`. This submodule re-aliases the
disambiguated forms (``CapabilitiesAccount`` etc. in
:mod:`adcp.types.capabilities`) back to the wire-spec names within its
own namespace, so adopter code reads cleanly against the spec.
"""

from __future__ import annotations

from adcp.types.capabilities import (
    A2ui,
    Accreditation,
    Adcp,
    AgeRestriction,
    AttributionWindow,
    AudienceTargeting,
    Avatar,
    Brand,
    Commerce,
    ComplianceTesting,
    Components,
    CompromiseNotification,
    ConversionTracking,
    CreativeSpecs,
    Endpoint,
    Execution,
    ExperimentalFeature,
    Features,
    GeoMetros,
    GeoPostalAreas,
    GeoProximity,
    Governance,
    Idempotency,
    IdempotencySupported,
    IdempotencyUnsupported,
    Identity,
    KeyOrigins,
    KeywordTargets,
    LifecycleTool,
    MatchingLatencyHours,
    Measurement,
    Metric,
    Modalities,
    NegativeKeywords,
    Portfolio,
    RequestSigning,
    SiCapabilities,
    Signals,
    SignalsFeatures,
    Specialism,
    SponsoredIntelligence,
    SupportedProtocol,
    Targeting,
    Transport,
    TrustedMatch,
    Video,
    Voice,
    WebhookSigning,
)
from adcp.types.capabilities import (
    CapabilitiesAccount as Account,
)
from adcp.types.capabilities import (
    CapabilitiesContentStandards as ContentStandards,
)
from adcp.types.capabilities import (
    CapabilitiesCreative as Creative,
)
from adcp.types.capabilities import (
    CapabilitiesMediaBuy as MediaBuy,
)

__all__ = [
    "A2ui",
    "Account",
    "Adcp",
    "AgeRestriction",
    "Accreditation",
    "AttributionWindow",
    "AudienceTargeting",
    "Avatar",
    "Brand",
    "Commerce",
    "ComplianceTesting",
    "Components",
    "CompromiseNotification",
    "ContentStandards",
    "ConversionTracking",
    "Creative",
    "CreativeSpecs",
    "Endpoint",
    "Execution",
    "ExperimentalFeature",
    "Features",
    "GeoMetros",
    "GeoPostalAreas",
    "GeoProximity",
    "Governance",
    "Idempotency",
    "IdempotencySupported",
    "IdempotencyUnsupported",
    "Identity",
    "KeyOrigins",
    "KeywordTargets",
    "LifecycleTool",
    "MatchingLatencyHours",
    "MediaBuy",
    "Measurement",
    "Metric",
    "Modalities",
    "NegativeKeywords",
    "Portfolio",
    "RequestSigning",
    "SiCapabilities",
    "Signals",
    "SignalsFeatures",
    "Specialism",
    "SponsoredIntelligence",
    "SupportedProtocol",
    "Targeting",
    "Transport",
    "TrustedMatch",
    "Video",
    "Voice",
    "WebhookSigning",
]
