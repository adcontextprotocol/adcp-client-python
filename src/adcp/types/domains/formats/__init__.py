"""Types the AdCP ``formats`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.formats import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.formats.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:44:03 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.types.domains.formats.canonical._base import (
        CanonicalFormatBase,
        CompositionModel,
        ReferenceMutability,
        RequiredPixelRatio,
        Slot,
    )
    from adcp.types.domains.formats.canonical.agent_placement import (
        CanonicalFormatAgentPlacementAiSurfaceSponsoredPlacement,
    )
    from adcp.types.domains.formats.canonical.audio_daast import CanonicalFormatDaastAudio
    from adcp.types.domains.formats.canonical.audio_hosted import CanonicalFormatHostedAudio
    from adcp.types.domains.formats.canonical.audio_vast import CanonicalFormatVastAudio
    from adcp.types.domains.formats.canonical.coordinated_placements import (
        AssetSource5,
        AudioCodec2,
        CanonicalFormatCoordinatedPlacements,
        Components,
        Components1,
        Components10,
        Components11,
        Components12,
        Components13,
        Components2,
        Components3,
        Components4,
        Components5,
        Components6,
        Components7,
        Components8,
        Components9,
        Container1,
        ImageFormat1,
        Params,
        Params10,
        Params11,
        Params12,
        Params13,
        Params2,
        Params3,
        Params4,
        Params5,
        Params6,
        Params7,
        Params8,
        Params9,
        ServingPolicy,
        SharedSlot,
        TransitionMode3,
        Transitions1,
        Transitions2,
        Transitions3,
        Transitions4,
        Transitions5,
    )
    from adcp.types.domains.formats.canonical.display_tag import CanonicalFormatDisplayTag
    from adcp.types.domains.formats.canonical.html5 import CanonicalFormatHtml5Banner
    from adcp.types.domains.formats.canonical.image import CanonicalFormatImage
    from adcp.types.domains.formats.canonical.image_carousel import CanonicalFormatImageCarousel
    from adcp.types.domains.formats.canonical.native_in_feed import CanonicalFormatNativeInFeed
    from adcp.types.domains.formats.canonical.responsive_creative import (
        CanonicalFormatResponsiveCreative,
    )
    from adcp.types.domains.formats.canonical.seller_rendered_stateful_display import (
        CanonicalFormatSellerRenderedStatefulDisplay,
        TransitionMode9,
        Transitions10,
        Transitions11,
        Transitions7,
        Transitions8,
        Transitions9,
    )
    from adcp.types.domains.formats.canonical.sponsored_placement import (
        CanonicalFormatSponsoredPlacementRetailMediaCatalogDriven,
    )
    from adcp.types.domains.formats.canonical.video_hosted import CanonicalFormatHostedVideo
    from adcp.types.domains.formats.canonical.video_vast import CanonicalFormatVastVideo

# Explicit exports
__all__ = [
    "AssetSource5",
    "AudioCodec2",
    "CanonicalFormatAgentPlacementAiSurfaceSponsoredPlacement",
    "CanonicalFormatBase",
    "CanonicalFormatCoordinatedPlacements",
    "CanonicalFormatDaastAudio",
    "CanonicalFormatDisplayTag",
    "CanonicalFormatHostedAudio",
    "CanonicalFormatHostedVideo",
    "CanonicalFormatHtml5Banner",
    "CanonicalFormatImage",
    "CanonicalFormatImageCarousel",
    "CanonicalFormatNativeInFeed",
    "CanonicalFormatResponsiveCreative",
    "CanonicalFormatSellerRenderedStatefulDisplay",
    "CanonicalFormatSponsoredPlacementRetailMediaCatalogDriven",
    "CanonicalFormatVastAudio",
    "CanonicalFormatVastVideo",
    "Components",
    "Components1",
    "Components10",
    "Components11",
    "Components12",
    "Components13",
    "Components2",
    "Components3",
    "Components4",
    "Components5",
    "Components6",
    "Components7",
    "Components8",
    "Components9",
    "CompositionModel",
    "Container1",
    "ImageFormat1",
    "Params",
    "Params10",
    "Params11",
    "Params12",
    "Params13",
    "Params2",
    "Params3",
    "Params4",
    "Params5",
    "Params6",
    "Params7",
    "Params8",
    "Params9",
    "ReferenceMutability",
    "RequiredPixelRatio",
    "ServingPolicy",
    "SharedSlot",
    "Slot",
    "TransitionMode3",
    "TransitionMode9",
    "Transitions1",
    "Transitions10",
    "Transitions11",
    "Transitions2",
    "Transitions3",
    "Transitions4",
    "Transitions5",
    "Transitions7",
    "Transitions8",
    "Transitions9",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "AssetSource5": "adcp.types.domains.formats.canonical.coordinated_placements",
    "AudioCodec2": "adcp.types.domains.formats.canonical.coordinated_placements",
    "CanonicalFormatAgentPlacementAiSurfaceSponsoredPlacement": "adcp.types.domains.formats.canonical.agent_placement",
    "CanonicalFormatBase": "adcp.types.domains.formats.canonical._base",
    "CanonicalFormatCoordinatedPlacements": "adcp.types.domains.formats.canonical.coordinated_placements",
    "CanonicalFormatDaastAudio": "adcp.types.domains.formats.canonical.audio_daast",
    "CanonicalFormatDisplayTag": "adcp.types.domains.formats.canonical.display_tag",
    "CanonicalFormatHostedAudio": "adcp.types.domains.formats.canonical.audio_hosted",
    "CanonicalFormatHostedVideo": "adcp.types.domains.formats.canonical.video_hosted",
    "CanonicalFormatHtml5Banner": "adcp.types.domains.formats.canonical.html5",
    "CanonicalFormatImage": "adcp.types.domains.formats.canonical.image",
    "CanonicalFormatImageCarousel": "adcp.types.domains.formats.canonical.image_carousel",
    "CanonicalFormatNativeInFeed": "adcp.types.domains.formats.canonical.native_in_feed",
    "CanonicalFormatResponsiveCreative": "adcp.types.domains.formats.canonical.responsive_creative",
    "CanonicalFormatSellerRenderedStatefulDisplay": "adcp.types.domains.formats.canonical.seller_rendered_stateful_display",
    "CanonicalFormatSponsoredPlacementRetailMediaCatalogDriven": "adcp.types.domains.formats.canonical.sponsored_placement",
    "CanonicalFormatVastAudio": "adcp.types.domains.formats.canonical.audio_vast",
    "CanonicalFormatVastVideo": "adcp.types.domains.formats.canonical.video_vast",
    "Components": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components1": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components10": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components11": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components12": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components13": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components2": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components3": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components4": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components5": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components6": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components7": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components8": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Components9": "adcp.types.domains.formats.canonical.coordinated_placements",
    "CompositionModel": "adcp.types.domains.formats.canonical._base",
    "Container1": "adcp.types.domains.formats.canonical.coordinated_placements",
    "ImageFormat1": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params10": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params11": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params12": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params13": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params2": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params3": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params4": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params5": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params6": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params7": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params8": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Params9": "adcp.types.domains.formats.canonical.coordinated_placements",
    "ReferenceMutability": "adcp.types.domains.formats.canonical._base",
    "RequiredPixelRatio": "adcp.types.domains.formats.canonical._base",
    "ServingPolicy": "adcp.types.domains.formats.canonical.coordinated_placements",
    "SharedSlot": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Slot": "adcp.types.domains.formats.canonical._base",
    "TransitionMode3": "adcp.types.domains.formats.canonical.coordinated_placements",
    "TransitionMode9": "adcp.types.domains.formats.canonical.seller_rendered_stateful_display",
    "Transitions1": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Transitions10": "adcp.types.domains.formats.canonical.seller_rendered_stateful_display",
    "Transitions11": "adcp.types.domains.formats.canonical.seller_rendered_stateful_display",
    "Transitions2": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Transitions3": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Transitions4": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Transitions5": "adcp.types.domains.formats.canonical.coordinated_placements",
    "Transitions7": "adcp.types.domains.formats.canonical.seller_rendered_stateful_display",
    "Transitions8": "adcp.types.domains.formats.canonical.seller_rendered_stateful_display",
    "Transitions9": "adcp.types.domains.formats.canonical.seller_rendered_stateful_display",
}

if not TYPE_CHECKING:
    import importlib as _importlib

    def __getattr__(name: str) -> object:
        module_name = _DECLARED_IN.get(name)
        if module_name is None:
            # Not a declared name: let the import system try a submodule, or fail.
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(_importlib.import_module(module_name), name)
        globals()[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted(set(globals()) | set(__all__))
