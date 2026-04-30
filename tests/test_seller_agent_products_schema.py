"""Regression test: every static product in seller_agent.py is 3.0.1 schema-valid.

Anchors the Python-side verdict on issue #324 (CI vs local divergence with
@adcp/client@5.21.1's Ajv validator).  The Python jsonschema validator confirms
all products are compliant; the CI failures are in the npm-side oneOf error
reporter, not in the response shape we emit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from adcp.validation.schema_validator import validate_response


def _load_seller_agent() -> ModuleType:
    path = Path(__file__).parent.parent / "examples" / "seller_agent.py"
    spec = importlib.util.spec_from_file_location("_seller_agent_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Snapshot at collection time — seed_product() mutates PRODUCTS in-place, so
# a bare reference would change parametrize IDs across test runs in the same
# process if an integration test instantiates DemoStore and calls seed_product.
_PRODUCTS: list[dict[str, Any]] = list(_load_seller_agent().PRODUCTS)


class TestStaticProductsSchemaCompliance:
    """All static PRODUCTS in seller_agent.py must pass get_products response
    validation against the local AdCP 3.0.1 schema cache."""

    def test_full_products_list_passes_response_validation(self) -> None:
        outcome = validate_response("get_products", {"products": _PRODUCTS})
        assert outcome.variant != "skipped", (
            "get_products schema not loaded — regression anchor is a no-op; "
            "check that schemas/cache/ is present"
        )
        assert outcome.valid, (
            f"get_products response with all {len(_PRODUCTS)} products failed "
            f"Python-side schema validation: {outcome.issues}"
        )

    @pytest.mark.parametrize(
        "product",
        _PRODUCTS,
        ids=[p["product_id"] for p in _PRODUCTS],
    )
    def test_individual_product_passes_response_validation(
        self, product: dict[str, Any]
    ) -> None:
        outcome = validate_response("get_products", {"products": [product]})
        assert outcome.variant != "skipped", (
            "get_products schema not loaded — regression anchor is a no-op; "
            "check that schemas/cache/ is present"
        )
        assert outcome.valid, (
            f"Product {product.get('product_id')!r} failed Python-side schema "
            f"validation: {outcome.issues}"
        )
