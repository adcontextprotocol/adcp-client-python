"""Types the AdCP ``formats`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.formats import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.formats.<schema>``.
Nothing here is renamed.

This facade is LAZY (PEP 562). It binds no name at import time, because an
eager one makes every leaf of this domain depend on every other leaf: a
submodule import runs this ``__init__``, so one leaf's dependencies become
the whole domain's. That turned a benign submodule-level cycle between
``core`` and ``formats.canonical`` -- two domains whose schemas reference
each other, which Python handles -- into a package-level one that made all
16 modules under ``formats/canonical/`` unimportable on their own.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-16 22:14:49 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The whole importable surface, so type checkers and IDEs see it. Runtime
    # resolution is ``__getattr__`` below; mypy never sees that, so a typo'd
    # import is still an error rather than ``object``.
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

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "formats.canonical._base": (
        "CanonicalFormatBase",
        "CompositionModel",
        "ReferenceMutability",
        "RequiredPixelRatio",
        "Slot",
    ),
    "formats.canonical.agent_placement": (
        "CanonicalFormatAgentPlacementAiSurfaceSponsoredPlacement",
    ),
    "formats.canonical.audio_daast": ("CanonicalFormatDaastAudio",),
    "formats.canonical.audio_hosted": ("CanonicalFormatHostedAudio",),
    "formats.canonical.audio_vast": ("CanonicalFormatVastAudio",),
    "formats.canonical.coordinated_placements": (
        "AssetSource5",
        "AudioCodec2",
        "CanonicalFormatCoordinatedPlacements",
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
        "ServingPolicy",
        "SharedSlot",
        "TransitionMode3",
        "Transitions1",
        "Transitions2",
        "Transitions3",
        "Transitions4",
        "Transitions5",
    ),
    "formats.canonical.display_tag": ("CanonicalFormatDisplayTag",),
    "formats.canonical.html5": ("CanonicalFormatHtml5Banner",),
    "formats.canonical.image": ("CanonicalFormatImage",),
    "formats.canonical.image_carousel": ("CanonicalFormatImageCarousel",),
    "formats.canonical.native_in_feed": ("CanonicalFormatNativeInFeed",),
    "formats.canonical.responsive_creative": ("CanonicalFormatResponsiveCreative",),
    "formats.canonical.seller_rendered_stateful_display": (
        "CanonicalFormatSellerRenderedStatefulDisplay",
        "TransitionMode9",
        "Transitions10",
        "Transitions11",
        "Transitions7",
        "Transitions8",
        "Transitions9",
    ),
    "formats.canonical.sponsored_placement": (
        "CanonicalFormatSponsoredPlacementRetailMediaCatalogDriven",
    ),
    "formats.canonical.video_hosted": ("CanonicalFormatHostedVideo",),
    "formats.canonical.video_vast": ("CanonicalFormatVastVideo",),
}

if not TYPE_CHECKING:
    import importlib

    _DECLARING_MODULE = {name: module for module, names in _DECLARED_BY.items() for name in names}

    def __getattr__(name: str) -> object:
        """Resolve a re-exported name by importing the ONE module declaring it."""
        module = _DECLARING_MODULE.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(f"adcp.types.domains.{module}"), name)
        globals()[name] = value  # cache: __getattr__ fires once per name
        return value

    def __dir__() -> list[str]:
        return sorted(set(__all__) | set(globals()))


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
