# ruff: noqa: F401
"""Explicit raw/legacy creative wire types.

Application code should import canonical models from :mod:`adcp` or
:mod:`adcp.types`.  These aliases exist for migration, conformance tooling,
and AdCP 3.0/3.1 wire adapters.
"""

from adcp.types._legacy_assets import (
    coerce_legacy_asset,
    coerce_legacy_assets,
    infer_asset_type,
)
from adcp.types.domains.core.creative_asset import CreativeAsset as LegacyCreativeAsset
from adcp.types.domains.core.creative_filters import CreativeFilters as LegacyCreativeFilters
from adcp.types.domains.core.creative_manifest import (
    CreativeManifest as LegacyCreativeManifest,
)
from adcp.types.domains.core.format import Format as LegacyFormat
from adcp.types.domains.core.format_id import FormatReferenceStructuredObject
from adcp.types.domains.core.package import Package as LegacyPackage
from adcp.types.domains.core.placement import Placement as LegacyPlacement
from adcp.types.domains.core.product import Product as LegacyProduct
from adcp.types.domains.core.product_filters import ProductFilters as LegacyProductFilters
from adcp.types.domains.core.product_format_declaration import (
    ProductFormatDeclaration as LegacyGeneratedProductFormatDeclaration,
)
from adcp.types.domains.core.proposal import (
    TotalBudgetGuidance as LegacyProposalTotalBudgetGuidance,
)
from adcp.types.domains.creative.get_creative_delivery_response import (
    GetCreativeDeliveryResponse as LegacyGetCreativeDeliveryResponse,
)
from adcp.types.domains.creative.list_creatives_request import (
    ListCreativesRequest as LegacyListCreativesRequest,
)
from adcp.types.domains.creative.list_creatives_response import (
    ListCreativesResponse as LegacyListCreativesResponse,
)
from adcp.types.domains.creative.preview_creative_request import (
    PreviewCreativeRequest as LegacyPreviewCreativeRequest,
)
from adcp.types.domains.creative.preview_creative_response import (
    PreviewCreativeResponse as LegacyPreviewCreativeResponse,
)
from adcp.types.domains.creative.preview_creative_response import (
    PreviewCreativeResponse1 as LegacyPreviewCreativeResponse1,
)
from adcp.types.domains.creative.preview_creative_response import (
    PreviewCreativeResponse2 as LegacyPreviewCreativeResponse2,
)
from adcp.types.domains.creative.preview_creative_response import (
    PreviewCreativeResponse3 as LegacyPreviewCreativeResponse3,
)
from adcp.types.domains.creative.preview_creative_response import (
    PreviewCreativeResponse4 as LegacyPreviewCreativeResponse4,
)
from adcp.types.domains.creative.sync_creatives_request import (
    SyncCreativesRequest as LegacySyncCreativesRequest,
)
from adcp.types.domains.creative.sync_creatives_response import (
    SyncCreativesResponse as LegacySyncCreativesResponse,
)
from adcp.types.domains.media_buy.build_creative_request import (
    BuildCreativeRequest as LegacyBuildCreativeRequest,
)
from adcp.types.domains.media_buy.build_creative_response import (
    BuildCreativeResponse as LegacyBuildCreativeResponse,
)
from adcp.types.domains.media_buy.build_creative_response import (
    BuildCreativeResponse1 as LegacyBuildCreativeResponse1,
)
from adcp.types.domains.media_buy.build_creative_response import (
    BuildCreativeResponse2 as LegacyBuildCreativeResponse2,
)
from adcp.types.domains.media_buy.build_creative_response import (
    BuildCreativeResponse3 as LegacyBuildCreativeResponse3,
)
from adcp.types.domains.media_buy.build_creative_response import (
    BuildCreativeResponse4 as LegacyBuildCreativeResponse4,
)
from adcp.types.domains.media_buy.build_creative_response import (
    BuildCreativeResponse5 as LegacyBuildCreativeResponse5,
)
from adcp.types.domains.media_buy.build_creative_response import (
    BuildCreativeResponse6 as LegacyBuildCreativeResponse6,
)
from adcp.types.domains.media_buy.create_media_buy_request import (
    CreateMediaBuyRequest as LegacyCreateMediaBuyRequest,
)
from adcp.types.domains.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse as LegacyCreateMediaBuyResponse,
)
from adcp.types.domains.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse1 as LegacyCreateMediaBuyResponse1,
)
from adcp.types.domains.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse2 as LegacyCreateMediaBuyResponse2,
)
from adcp.types.domains.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse3 as LegacyCreateMediaBuyResponse3,
)
from adcp.types.domains.media_buy.get_media_buy_delivery_response import (
    GetMediaBuyDeliveryResponse as LegacyGetMediaBuyDeliveryResponse,
)
from adcp.types.domains.media_buy.get_media_buys_response import (
    GetMediaBuysResponse as LegacyGetMediaBuysResponse,
)
from adcp.types.domains.media_buy.get_products_request import (
    GetProductsRequest as LegacyGetProductsRequest,
)
from adcp.types.domains.media_buy.get_products_response import (
    GetProductsResponse as LegacyGetProductsResponse,
)
from adcp.types.domains.media_buy.list_creative_formats_request import (
    ListCreativeFormatsRequest as LegacyListCreativeFormatsRequest,
)
from adcp.types.domains.media_buy.list_creative_formats_response import (
    ListCreativeFormatsResponse as LegacyListCreativeFormatsResponse,
)
from adcp.types.domains.media_buy.package_request import (
    PackageRequest as LegacyPackageRequest,
)
from adcp.types.domains.media_buy.package_update import PackageUpdate as LegacyPackageUpdate
from adcp.types.domains.media_buy.update_media_buy_request import (
    UpdateMediaBuyRequest as LegacyUpdateMediaBuyRequest,
)
from adcp.types.domains.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse as LegacyUpdateMediaBuyResponse,
)
from adcp.types.domains.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse1 as LegacyUpdateMediaBuyResponse1,
)
from adcp.types.domains.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse2 as LegacyUpdateMediaBuyResponse2,
)
from adcp.types.domains.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse3 as LegacyUpdateMediaBuyResponse3,
)

# The generated class itself, under the ``Legacy*`` spelling every other name
# in this module uses. It was a SUBCLASS until #1398, written when the generated
# ``agent_url`` was an ``AnyUrl`` that normalized the wire bytes — it carried a
# plain ``str`` annotation, its own ``TypeAdapter(AnyUrl)`` validator, and
# ``StrictInt`` dimensions. #1384 moved ``agent_url`` to ``WireUrl``
# (``StrictStr`` + ``_require_absolute_url`` + ``WithJsonSchema``), which is
# that subclass's whole purpose, and left every redeclaration a WEAKENING of
# the parent it narrowed: ``agent_url: str`` dropped the strictness, the
# absolute-URL validator and the ``format: uri`` schema; ``StrictInt``
# dimensions dropped ``SchemaInt``'s JSON-schema-integer coercion, so
# ``width: 300.0`` validated against the generated class the SDK's own model
# fields declare and was REFUSED by the public name for it; and
# ``duration_ms: StrictInt | StrictFloat`` widened a ``StrictFloat``. The
# ``model_dump`` override was a bare ``super()`` call. The parent already
# carries ``extra='allow'`` and the same ``id`` pattern, so there is nothing
# left for a subclass to hold.
LegacyFormatId = FormatReferenceStructuredObject
LegacyFormatReferenceStructuredObject = FormatReferenceStructuredObject
LegacyProductFormatDeclaration = LegacyGeneratedProductFormatDeclaration

# One semantic name per BRANCH of every response union whose tool reaches this
# surface. Not a curated subset of them (#1399): where the bare public name for
# an branch is the canonical SUBCLASS, the generated branch itself has no public
# spelling at all without one of these, and a seller reading an off-the-wire
# response can then only name it through ``adcp.types.domains.<domain>.*`` —
# which is the private tree this release exists to retire.
#
# The set used to be curated, and it had five holes at once:
# ``LegacyUpdateMediaBuySubmittedResponse`` and all three
# ``LegacyCreateMediaBuy*Response`` branches were absent while
# ``LegacyUpdateMediaBuy{Success,Error}Response`` shipped, and the generator had
# already opened a fifth by adding a fourth ``preview_creative`` branch that nobody
# named. ``tests/test_legacy_surface_names_every_branch.py`` derives the required
# set from the generated response unions, so a sixth fails the build instead of
# reaching an adopter.
LegacyBuildCreativeSuccessResponse = LegacyBuildCreativeResponse1
LegacyBuildCreativeErrorResponse = LegacyBuildCreativeResponse2
LegacyBuildCreativeSubmittedResponse = LegacyBuildCreativeResponse6
LegacyCreateMediaBuySuccessResponse = LegacyCreateMediaBuyResponse1
LegacyCreateMediaBuyErrorResponse = LegacyCreateMediaBuyResponse2
LegacyCreateMediaBuySubmittedResponse = LegacyCreateMediaBuyResponse3
LegacyPreviewCreativeSingleResponse = LegacyPreviewCreativeResponse1
LegacyPreviewCreativeBatchResponse = LegacyPreviewCreativeResponse2
LegacyPreviewCreativeVariantResponse = LegacyPreviewCreativeResponse3
LegacyPreviewCreativeSubmittedResponse = LegacyPreviewCreativeResponse4
LegacyUpdateMediaBuySuccessResponse = LegacyUpdateMediaBuyResponse1
LegacyUpdateMediaBuyErrorResponse = LegacyUpdateMediaBuyResponse2
LegacyUpdateMediaBuySubmittedResponse = LegacyUpdateMediaBuyResponse3

__all__ = [
    "infer_asset_type",
    "coerce_legacy_asset",
    "coerce_legacy_assets",
    # The class generated model fields are typed with, under its own name as
    # well as under ``LegacyFormatId``. ``adcp.types`` reaches it through the
    # two ``Legacy*`` spellings below and carries the canonical format model
    # under the bare ``Format`` / ``FormatId`` names instead.
    "FormatReferenceStructuredObject",
    "LegacyBuildCreativeRequest",
    "LegacyBuildCreativeResponse",
    "LegacyBuildCreativeResponse1",
    "LegacyBuildCreativeResponse2",
    "LegacyBuildCreativeResponse3",
    "LegacyBuildCreativeResponse4",
    "LegacyBuildCreativeResponse5",
    "LegacyBuildCreativeResponse6",
    "LegacyBuildCreativeErrorResponse",
    "LegacyBuildCreativeSubmittedResponse",
    "LegacyBuildCreativeSuccessResponse",
    "LegacyCreateMediaBuyRequest",
    "LegacyCreateMediaBuyResponse",
    "LegacyCreateMediaBuyResponse1",
    "LegacyCreateMediaBuyResponse2",
    "LegacyCreateMediaBuyResponse3",
    "LegacyCreateMediaBuyErrorResponse",
    "LegacyCreateMediaBuySubmittedResponse",
    "LegacyCreateMediaBuySuccessResponse",
    "LegacyCreativeAsset",
    "LegacyCreativeFilters",
    "LegacyCreativeManifest",
    "LegacyFormat",
    "LegacyFormatId",
    "LegacyFormatReferenceStructuredObject",
    "LegacyGetCreativeDeliveryResponse",
    "LegacyGetMediaBuyDeliveryResponse",
    "LegacyGetMediaBuysResponse",
    "LegacyGetProductsRequest",
    "LegacyGetProductsResponse",
    "LegacyListCreativeFormatsRequest",
    "LegacyListCreativeFormatsResponse",
    "LegacyListCreativesRequest",
    "LegacyListCreativesResponse",
    "LegacyPackage",
    "LegacyPackageRequest",
    "LegacyPackageUpdate",
    "LegacyPlacement",
    "LegacyPreviewCreativeRequest",
    "LegacyPreviewCreativeResponse",
    "LegacyPreviewCreativeResponse1",
    "LegacyPreviewCreativeResponse2",
    "LegacyPreviewCreativeResponse3",
    "LegacyPreviewCreativeResponse4",
    "LegacyPreviewCreativeBatchResponse",
    "LegacyPreviewCreativeSingleResponse",
    "LegacyPreviewCreativeSubmittedResponse",
    "LegacyPreviewCreativeVariantResponse",
    "LegacyProduct",
    "LegacyProductFilters",
    "LegacyProductFormatDeclaration",
    "LegacyProposalTotalBudgetGuidance",
    "LegacySyncCreativesRequest",
    "LegacySyncCreativesResponse",
    "LegacyUpdateMediaBuyErrorResponse",
    "LegacyUpdateMediaBuyRequest",
    "LegacyUpdateMediaBuyResponse",
    "LegacyUpdateMediaBuyResponse1",
    "LegacyUpdateMediaBuyResponse2",
    "LegacyUpdateMediaBuyResponse3",
    "LegacyUpdateMediaBuySubmittedResponse",
    "LegacyUpdateMediaBuySuccessResponse",
]
