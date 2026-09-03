# ruff: noqa: E501
"""Capability sub-models surfaced from the bundled ``get_adcp_capabilities_response`` schema.

Adopters declaring ``DecisioningCapabilities`` for a platform (see
:mod:`adcp.decisioning.platform`) need access to the full set of
typed capability sub-models — ``Account``, ``MediaBuy``, ``Targeting``,
``GeoMetros``, ``Idempotency`` etc. The generated Pydantic classes
already exist in
``adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response``,
but three of them (``Account``, ``MediaBuy``, ``Creative``) collide on
name with unrelated wire types already exported from :mod:`adcp.types`.

This module sits in the import-architecture whitelist (alongside
``aliases.py``, ``_ergonomic.py``, ``_forward_compat.py``,
``_generated.py``) for direct ``generated_poc`` imports. It pulls the capabilities sub-models out
under disambiguated names, so the colliding three don't shadow the
wire types when re-exported from :mod:`adcp.types`. Adopters never
import from this module directly — :mod:`adcp.decisioning.capabilities`
is the canonical adopter-facing namespace and re-aliases the three
disambiguated names back to their wire-spec form.

Layering::

    generated_poc/bundled/protocol/get_adcp_capabilities_response.py
        ↓ (this module — disambiguates colliding names)
    adcp.types.capabilities
        ↓ (re-exported from)
    adcp.types.__init__
        ↓ (re-aliased to wire-spec names within submodule namespace)
    adcp.decisioning.capabilities  ← adopter-facing import path
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import get_args as _get_args

if TYPE_CHECKING:
    from adcp.types.base import AdCPBaseModel

from adcp.types._generated import CountryFusedPostalCodeSystem as LegacyPostalCodeSystem
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    A2ui,
    Accreditation,
    Adcp,
    AgeRestriction,
    AttributionWindow,
    AudienceTargeting,
    Avatar,
    Commerce,
    ComplianceTesting,
    Components,
    CompromiseNotification,
    ConversionTracking,
    CreativeSpecs,
    Endpoint,
    Execution,
    ExperimentalFeature,
    GeoMetros,
    GeoPostalAreas,
    GeoProximity,
    Governance,
    Idempotency,
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
    Signals,
    Specialism,
    SponsoredIntelligence,
    SupportedProtocol,
    Targeting,
    TrustedMatch,
    Video,
    Voice,
    WebhookSigning,
)

# Top-level capability protocol blocks.
#
# Three names (``Account``, ``MediaBuy``, ``Creative``) collide with
# wire types in :mod:`adcp.types`. Imported here under
# ``CapabilitiesAccount`` / ``CapabilitiesMediaBuy`` /
# ``CapabilitiesCreative`` so the re-export from :mod:`adcp.types`
# doesn't shadow the wire types. The submodule
# :mod:`adcp.decisioning.capabilities` re-aliases them back to ``Account``
# / ``MediaBuy`` / ``Creative`` within its own namespace, so adopters
# writing the ``DecisioningCapabilities`` declaration get the wire-spec
# names.
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    Account as CapabilitiesAccount,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    Adcp as _Adcp,
)

# ``Capabilities`` (line 580 of the generated module) is the SI-block's
# inner ``capabilities`` field type — modalities / components / commerce
# / a2ui / mcp_apps. Re-aliased here as ``SiCapabilities`` to disambiguate
# from :class:`adcp.decisioning.DecisioningCapabilities` and to make the
# import site self-documenting.
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    Capabilities as SiCapabilities,
)

# ``ContentStandards`` collides with ``adcp.types.ContentStandards`` (the
# unrelated wire model on the content-standards protocol). Same
# disambiguation pattern as ``Account`` / ``MediaBuy`` / ``Creative`` —
# imported here under a ``Capabilities*`` alias and re-aliased back to
# the wire-spec name within :mod:`adcp.decisioning.capabilities`.
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    ContentStandards as CapabilitiesContentStandards,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    Creative as CapabilitiesCreative,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    GetAdcpCapabilitiesResponse as _CapabilitiesResponse,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    Idempotency as IdempotencySupported,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    MediaBuy as _MediaBuy,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    Preview as CapabilitiesPreview,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    RenderingOrigin as CapabilitiesPreviewRenderingOrigin,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    Route as CapabilitiesPreviewRoute,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    Signals as _Signals,
)
from adcp.types.generated_poc.core.reporting_delivery_capabilities import (
    ReportingDeliveryCapabilities,
)


class MediaBuy(_MediaBuy):
    """Media-buy capabilities using the canonical reporting model identity."""

    reporting_delivery: ReportingDeliveryCapabilities | None = None  # type: ignore[assignment]


CapabilitiesMediaBuy = MediaBuy

# ``Signals.features`` and the unsupported arm of the ``Adcp.idempotency``
# discriminated union are inline schemas the codegen materializes under
# numbered class names (``Features<N>`` / ``Idempotency<N>``). Those
# numbers are not stable across regens: ``datamodel-code-generator``
# 0.56.1 assigns them from a global counter whose traversal order shifts
# with both upstream schema layout AND filesystem-iteration order
# (APFS-on-macOS vs ext4-on-Linux), so the same pinned generator produces
# ``Features1`` in one environment and ``Features2`` in another. Reach
# the classes via their parents' field annotation — both ``Signals`` and
# ``Adcp`` are stable wire-spec class names, and the field annotations
# carry the union arm types directly.
_media_buy_features_arms = [
    arm for arm in _get_args(_MediaBuy.model_fields["features"].annotation) if arm is not type(None)
]
if len(_media_buy_features_arms) != 1:
    raise RuntimeError(
        "capabilities: MediaBuy.features annotation lost its concrete type "
        f"(got {_media_buy_features_arms!r})"
    )
Features: type = _media_buy_features_arms[0]

if TYPE_CHECKING:
    Transport = AdCPBaseModel
else:
    _transport_arms = _get_args(Endpoint.model_fields["transports"].annotation)
    if len(_transport_arms) != 1:
        raise RuntimeError(
            "capabilities: Endpoint.transports annotation lost its concrete item type "
            f"(got {_transport_arms!r})"
        )
    Transport = _transport_arms[0]

if TYPE_CHECKING:
    # The generated capability class is numbered and the suffix is not stable
    # across generator layouts. Give static consumers the stable Pydantic base;
    # runtime still receives the exact field-derived class below.
    Brand = AdCPBaseModel
else:
    _brand_capability_arms = [
        arm
        for arm in _get_args(_CapabilitiesResponse.model_fields["brand"].annotation)
        if arm is not type(None)
    ]
    if len(_brand_capability_arms) != 1:
        raise RuntimeError(
            "capabilities: response brand annotation lost its concrete type "
            f"(got {_brand_capability_arms!r})"
        )
    Brand = _brand_capability_arms[0]

_signals_features_arms = [
    arm for arm in _get_args(_Signals.model_fields["features"].annotation) if arm is not type(None)
]
if len(_signals_features_arms) != 1:
    raise RuntimeError(
        "capabilities: Signals.features annotation lost its concrete type "
        f"(got {_signals_features_arms!r})"
    )
SignalsFeatures: type = _signals_features_arms[0]

_idempotency_arms = [
    arm
    for arm in _get_args(_Adcp.model_fields["idempotency"].annotation)
    if arm is not IdempotencySupported and arm is not type(None)
]
if len(_idempotency_arms) != 1:
    raise RuntimeError(
        "capabilities: expected exactly one non-supported Idempotency arm, "
        f"got {_idempotency_arms!r}"
    )
IdempotencyUnsupported: type = _idempotency_arms[0]

__all__ = [
    "A2ui",
    "Adcp",
    "AgeRestriction",
    "Accreditation",
    "AttributionWindow",
    "AudienceTargeting",
    "Avatar",
    "Brand",
    "CapabilitiesAccount",
    "CapabilitiesContentStandards",
    "CapabilitiesCreative",
    "CapabilitiesMediaBuy",
    "CapabilitiesPreview",
    "CapabilitiesPreviewRenderingOrigin",
    "CapabilitiesPreviewRoute",
    "Commerce",
    "ComplianceTesting",
    "Components",
    "CompromiseNotification",
    "ConversionTracking",
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
    "LegacyPostalCodeSystem",
    "MatchingLatencyHours",
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
