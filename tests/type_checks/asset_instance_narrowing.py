"""Adopter contract for the public creative-asset discriminated union."""

from typing_extensions import assert_type

from adcp.types import AssetInstance, AssetInstanceType, ImageContent, VideoContent


def dimensions(asset: AssetInstance) -> tuple[int, int] | None:
    # Mypy narrows Pydantic model unions by runtime class. The companion
    # AssetInstanceType alias supplies the exhaustive discriminator values.
    if isinstance(asset, ImageContent):
        assert_type(asset, ImageContent)
        return asset.width, asset.height
    if isinstance(asset, VideoContent):
        assert_type(asset, VideoContent)
        return asset.width, asset.height
    return None


def accepts_discriminator(asset_type: AssetInstanceType) -> AssetInstanceType:
    return asset_type
