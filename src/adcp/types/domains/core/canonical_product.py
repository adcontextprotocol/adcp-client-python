"""Types declared by the AdCP ``core/canonical_product`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.core.canonical_product import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.canonical_product import (
    CanonicalProduct,
    CatalogMatch,
    MatchedGtin,
    PublisherDomain,
    PublisherProperty,
    PublisherProperty1,
    PublisherProperty2,
    PublisherProperty3,
    PublisherProperty4,
    PublisherProperty5,
    PublisherProperty6,
    PublisherProperty7,
)

# Explicit exports
__all__ = [
    "CanonicalProduct",
    "CatalogMatch",
    "MatchedGtin",
    "PublisherDomain",
    "PublisherProperty",
    "PublisherProperty1",
    "PublisherProperty2",
    "PublisherProperty3",
    "PublisherProperty4",
    "PublisherProperty5",
    "PublisherProperty6",
    "PublisherProperty7",
]
