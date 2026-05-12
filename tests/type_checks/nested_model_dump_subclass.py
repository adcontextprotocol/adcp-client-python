"""Adopter pattern: override model_dump() on an AdCPBaseModel subclass.

Verifies that the model_dump(**kwargs: Any) -> dict[str, Any] override
signature is accepted by mypy --strict without type: ignore.

Adopters use this pattern when child models need custom serialization
(e.g. nested type extension, computed fields) beyond what Pydantic's
default child serialization provides.
"""
from __future__ import annotations

from typing import Any

from adcp.types import Creative, Product


class AnnotatedProduct(Product):
    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        result = super().model_dump(**kwargs)
        result["_annotated"] = True
        return result


class AnnotatedCreative(Creative):
    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        result = super().model_dump(**kwargs)
        result["_source"] = "internal"
        return result


p = AnnotatedProduct.model_construct(
    product_id="p1",
    name="Home Page Takeover",
    publisher_properties=[],
    pricing_options=[],
    inventory_type="publisher_owned",
)
dumped = p.model_dump()
assert dumped.get("_annotated") is True
