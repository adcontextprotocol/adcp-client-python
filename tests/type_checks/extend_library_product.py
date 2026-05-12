"""Adopter pattern: subclass Product and add internal-only fields.

Verifies that extending a generated SDK type with extra ``exclude=True``
fields type-checks cleanly under ``mypy --strict``.  The internal field
must not appear in the serialised response — tested here as a type
contract (mypy), not a serialisation contract (see
test_response_builder_subclass.py for the runtime side).
"""
from __future__ import annotations

from typing import Any

from pydantic import Field

from adcp.types import Product


class InternalProduct(Product):
    implementation_config: dict[str, Any] = Field(default_factory=dict, exclude=True)
    seller_notes: str | None = Field(default=None, exclude=True)


def make_product(ad_server: str, template_id: str) -> InternalProduct:
    return InternalProduct.model_construct(
        product_id="p1",
        name="Display Home",
        publisher_properties=[],
        pricing_options=[],
        inventory_type="publisher_owned",
        implementation_config={"ad_server": ad_server, "line_item_template_id": template_id},
        seller_notes="budget-locked to Q3",
    )


product = make_product("gam", "internal-42")
assert product.implementation_config["ad_server"] == "gam"
