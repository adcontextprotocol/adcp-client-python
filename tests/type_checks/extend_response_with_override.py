"""Adopter pattern: subclass a wire response type and override model_dump.

Verifies the schema-inheritance pattern: an adopter extends a generated
response type with internal fields and provides a model_dump override
that walks nested children. This pattern comes up whenever an adopter's
product or creative type carries extra fields that must be excluded
from the wire but included in internal processing.
"""
from __future__ import annotations

from typing import Any

from pydantic import Field

from adcp.types import GetProductsResponse, Product


class InternalProduct(Product):
    _ad_server_id: str = Field(default="", exclude=True, alias="_ad_server_id")

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        result = super().model_dump(**kwargs)
        return result


class InternalGetProductsResponse(GetProductsResponse):
    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        result = super().model_dump(**kwargs)
        if "products" in result and self.products:
            result["products"] = [p.model_dump(**kwargs) for p in self.products]
        return result
