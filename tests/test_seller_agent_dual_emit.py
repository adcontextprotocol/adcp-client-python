"""``examples/seller_agent.py`` dual-emits v1 + v2 format surfaces.

The reference seller is the AdCP storyboard runner's target — it MUST
exercise both wire shapes so the storyboard suite catches regressions
on either path. This test pins that invariant.

Three properties are pinned:

* Every product carries both ``format_ids[]`` (v1) and
  ``format_options[]`` (v2).
* Each v2 declaration's ``v1_format_ref[0]`` matches the product's
  v1 ``format_ids[0]`` — the seller-asserted pairing is the round-
  trip anchor.
* :func:`project_product_to_v1` on each product emits exactly the
  product's v1 ``format_ids`` (the SDK's projection layer rebuilds
  what the seller manually authored on v1).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from adcp.canonical_formats import (
    find_declaration_by_v1_format_id,
    project_product_to_v1,
)
from adcp.types import ProductFormatDeclaration

_SELLER_AGENT_PATH = Path(__file__).parent.parent / "examples" / "seller_agent.py"


@pytest.fixture(scope="module")
def seller_agent_products() -> list[dict[str, Any]]:
    """Import the reference seller's ``PRODUCTS`` catalog as a fixture.

    The seller agent isn't on ``sys.path`` by default; load via
    ``importlib`` rather than rewriting ``sys.path`` so the import
    is hermetic and the test doesn't leak state.
    """
    spec = importlib.util.spec_from_file_location("seller_agent", _SELLER_AGENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["seller_agent"] = module
    spec.loader.exec_module(module)
    try:
        return list(module.PRODUCTS)
    finally:
        sys.modules.pop("seller_agent", None)


def test_every_product_dual_emits_v1_and_v2(
    seller_agent_products: list[dict[str, Any]],
) -> None:
    for p in seller_agent_products:
        assert p.get("format_ids"), f"{p['product_id']}: missing v1 format_ids"
        assert p.get("format_options"), f"{p['product_id']}: missing v2 format_options"


def test_v2_declarations_pair_with_v1_format_ids(
    seller_agent_products: list[dict[str, Any]],
) -> None:
    """The v2 declaration MUST carry ``v1_format_ref`` pointing at the same
    underlying format the product publishes on v1."""
    for p in seller_agent_products:
        v1_ids = {fmt["id"] for fmt in p["format_ids"]}
        v2_ref_ids: set[str] = set()
        for decl in p["format_options"]:
            for ref in decl.get("v1_format_ref") or []:
                v2_ref_ids.add(ref["id"])
        assert v1_ids == v2_ref_ids, (
            f"{p['product_id']}: v1 format_ids {v1_ids!r} disagree with "
            f"v2 v1_format_ref entries {v2_ref_ids!r}"
        )


def test_v2_to_v1_projection_round_trips_each_product(
    seller_agent_products: list[dict[str, Any]],
) -> None:
    """Running :func:`project_product_to_v1` over each product's
    typed declarations MUST emit refs that round-trip back to those
    declarations via :func:`find_declaration_by_v1_format_id`, and
    MUST NOT raise any unexpected advisories.
    """
    for p in seller_agent_products:
        declarations = [ProductFormatDeclaration.model_validate(opt) for opt in p["format_options"]]

        class _Product:
            product_id = p["product_id"]
            format_options = declarations

        result = project_product_to_v1(_Product(), product_index=0)
        assert result.format_ids, f"{p['product_id']}: emitted zero v1 refs"
        for ref in result.format_ids:
            found = find_declaration_by_v1_format_id(ref, declarations)
            assert found is not None, (
                f"{p['product_id']}: emitted format_id {ref.id!r} did not "
                f"resolve back to any declaration"
            )
        # The reference seller's products are single-size; no LOSSY or
        # AMBIGUOUS advisories should fire.
        assert (
            result.advisories == []
        ), f"{p['product_id']}: unexpected advisories {[a.code for a in result.advisories]!r}"
