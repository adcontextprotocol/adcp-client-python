"""Regression tests for the public CreativeAsset binding (issue #1141)."""

from __future__ import annotations

from pydantic import BaseModel

import adcp.types
from adcp.types.canonical_creative import CanonicalBoundaryModel


def test_creative_asset_is_concrete_canonical_class() -> None:
    cls = adcp.types.CreativeAsset

    assert isinstance(cls, type), "CreativeAsset must be a class, not a union"
    assert issubclass(cls, CanonicalBoundaryModel)
    assert issubclass(cls, BaseModel)
    assert cls.__name__ == "CreativeAsset"
    assert cls is not adcp.types.LegacyCreativeAsset
    assert "format_id" not in cls.model_fields
    assert "format_kind" in cls.model_fields
    assert cls.model_fields["format_kind"].is_required()
