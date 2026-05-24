"""Public-API surface for canonical-formats types (AdCP 3.1).

Guards that the canonical-formats types are reachable from
:mod:`adcp.types` (rather than only from ``generated_poc``). Failures
here mean adopter code that does ``from adcp.types import …`` will
break — the public surface is part of the contract.
"""

from __future__ import annotations

import adcp.types as types

_EXPECTED_EXPORTS = (
    # Kind enum + projection ref
    "CanonicalFormatKind",
    "CanonicalProjectionReference",
    "CanonicalAssetSource",
    "CanonicalSlotOverride",
    # Declaration
    "ProductFormatDeclaration",
    "ProductFormatSellerPreference",
    # 13 canonical format classes
    "CanonicalFormatBase",
    "CanonicalCompositionModel",
    "CanonicalFormatImage",
    "CanonicalFormatHtml5Banner",
    "CanonicalFormatDisplayTag",
    "CanonicalFormatImageCarousel",
    "CanonicalFormatHostedVideo",
    "CanonicalFormatVastVideo",
    "CanonicalFormatHostedAudio",
    "CanonicalFormatDaastAudio",
    "CanonicalFormatNativeInFeed",
    "CanonicalFormatResponsiveCreative",
    "CanonicalFormatAgentPlacement",
    "CanonicalFormatSponsoredPlacement",
    # Pixel tracker asset
    "PixelTrackerAsset",
    "PixelTrackerEvent",
    "PixelTrackerMethod",
    # Registry types
    "V1V2CanonicalFormatMappingRegistry",
    "V1CanonicalMapping",
    "V1CanonicalGlobPattern",
    "V1CanonicalStructuralPattern",
    "V1CanonicalStructural",
    "V1CanonicalV2Projection",
    "V1CanonicalDimensions",
    # Error envelope sub-enums (for SDK-source advisory construction)
    "Recovery",
    "Source",
)


def test_canonical_formats_symbols_present_on_adcp_types() -> None:
    """Every expected name is reachable as ``adcp.types.<name>``."""
    missing = [name for name in _EXPECTED_EXPORTS if not hasattr(types, name)]
    assert not missing, f"missing public-API exports: {missing}"


def test_canonical_formats_symbols_in_dunder_all() -> None:
    """Every expected name appears in ``adcp.types.__all__``."""
    declared = set(getattr(types, "__all__", []))
    missing = [name for name in _EXPECTED_EXPORTS if name not in declared]
    assert not missing, f"public-API exports not declared in __all__: {missing}"


def test_canonical_format_kind_has_13_values() -> None:
    """Adding/removing a canonical kind is a wire-breaking change — pin the count."""
    from adcp.types import CanonicalFormatKind

    assert len(CanonicalFormatKind) == 13


def test_sdk_id_reachable_via_public_package_path() -> None:
    """``SDK_ID`` is documented as importable from :mod:`adcp.canonical_formats`.

    It's resolved lazily via module ``__getattr__`` so this also
    exercises that dispatch path against the documented import.
    """
    from adcp.canonical_formats import SDK_ID

    assert isinstance(SDK_ID, str)
    assert SDK_ID.startswith("adcontextprotocol-adcp-python@")


def test_sdk_id_uses_canonical_distribution_prefix() -> None:
    """Dev installs and wheel installs MUST emit the same ``sdk_id`` prefix
    so the multi-hop ``(code, field, sdk_id)`` dedup contract holds across
    deployment shapes."""
    from adcp.canonical_formats import SDK_ID

    prefix, _, _ = SDK_ID.partition("@")
    assert prefix == "adcontextprotocol-adcp-python"


def test_canonical_kind_values_match_spec() -> None:
    """The 13 wire values are the canonical-formats vocabulary; lock them in."""
    from adcp.types import CanonicalFormatKind

    actual = {k.value for k in CanonicalFormatKind}
    expected = {
        "image",
        "html5",
        "display_tag",
        "image_carousel",
        "video_hosted",
        "video_vast",
        "audio_hosted",
        "audio_daast",
        "sponsored_placement",
        "native_in_feed",
        "responsive_creative",
        "agent_placement",
        "custom",
    }
    assert actual == expected
