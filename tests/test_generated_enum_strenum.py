from __future__ import annotations

from pathlib import Path

from adcp.types import CreativeAction, GetProductsRequest, MediaBuyStatus
from adcp.types.generated_poc.media_buy.get_products_request import BuyingMode
from tests.conftest import validate_union

GENERATED_TYPES_DIR = Path(__file__).parents[1] / "src" / "adcp" / "types" / "generated_poc"


def test_generated_enums_are_string_comparable() -> None:
    assert MediaBuyStatus.active == "active"
    assert "active" == MediaBuyStatus.active
    assert isinstance(MediaBuyStatus.active, str)
    assert str(MediaBuyStatus.active) == "active"


def test_generated_enums_hash_like_strings() -> None:
    values = {"created"}

    assert CreativeAction.created in values
    assert values == {CreativeAction.created}


def test_generated_enum_sources_use_strenum() -> None:
    for path in GENERATED_TYPES_DIR.rglob("*.py"):
        source = path.read_text()
        assert "from enum import Enum" not in source, path
        assert "(Enum):" not in source, path


def test_enum_coercion_still_returns_enum_members() -> None:
    req = validate_union(GetProductsRequest, {"buying_mode": "wholesale"})

    assert req.buying_mode == BuyingMode.wholesale
    assert isinstance(req.buying_mode, BuyingMode)
