"""The canonical models' overriding list fields read as precise lists (#1416).

Each canonical model below redeclares a generated ``list[<wire class>]`` field
as ``list[<canonical class>]``. Those overrides used to be annotated
``SchemaVariant[...]``, a marker only ``adcp.types.mypy_plugin`` understands:
with the plugin the fields were ``Any``, without it (and under pyright) they
were not valid types at all, so ``for p in response.products`` was an error.

This fixture iterates every one of those fields and reads an attribute of the
element. It is graded three ways by ``tests/test_canonical_override_field_types.py``:
``mypy --strict`` with the repository configuration (plugin on), ``mypy --strict``
with a bare configuration (plugin off), and pyright.
"""

from __future__ import annotations

from typing_extensions import assert_type

from adcp.types import (
    CreateMediaBuyResponse1,
    Creative,
    CreativeAsset,
    Format,
    GetProductsResponse,
    ListCreativesResponse,
    Package,
    PackageRequest,
    PackageUpdate,
    Placement,
    Product,
    SyncCreativesRequest,
    UpdateMediaBuyRequest,
)

# ``adcp.types.DeliveryCreative`` is the generated wire row; the canonical served
# creative that retypes ``variants`` is reached through ``GetCreativeDeliveryResponse``
# and spelled from its module here, as ``creative_asset_binding.py`` does.
from adcp.types.canonical_creative import DeliveryCreative, _DeliveryCreativeVariant


def placement_format_options(placement: Placement) -> list[str]:
    assert_type(placement.format_options, list[Format] | None)
    return [str(option.format_kind) for option in placement.format_options or []]


def product_format_options(product: Product) -> list[str]:
    assert_type(product.format_options, list[Format])
    return [str(option.format_kind) for option in product.format_options]


def product_placements(product: Product) -> list[str]:
    assert_type(product.placements, list[Placement] | None)
    return [placement.placement_id for placement in product.placements or []]


def delivery_creative_variants(creative: DeliveryCreative) -> list[str]:
    assert_type(creative.variants, list[_DeliveryCreativeVariant])
    return [variant.variant_id for variant in creative.variants]


def package_update_creatives(update: PackageUpdate) -> list[str]:
    assert_type(update.creatives, list[CreativeAsset] | None)
    return [asset.creative_id for asset in update.creatives or []]


def get_products_response_products(response: GetProductsResponse) -> list[str]:
    assert_type(response.products, list[Product] | None)
    return [product.product_id for product in response.products or []]


def update_media_buy_request_new_packages(request: UpdateMediaBuyRequest) -> list[str]:
    assert_type(request.new_packages, list[PackageRequest] | None)
    return [package.product_id for package in request.new_packages or []]


def create_media_buy_response1_packages(response: CreateMediaBuyResponse1) -> list[str]:
    assert_type(response.packages, list[Package])
    return [package.package_id for package in response.packages]


def sync_creatives_request_creatives(request: SyncCreativesRequest) -> list[str]:
    assert_type(request.creatives, list[CreativeAsset])
    return [asset.creative_id for asset in request.creatives]


def list_creatives_response_creatives(response: ListCreativesResponse) -> list[str]:
    assert_type(response.creatives, list[Creative])
    return [creative.creative_id for creative in response.creatives]
