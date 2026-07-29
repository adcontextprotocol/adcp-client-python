# ruff: noqa: F401
"""Explicit raw/legacy creative wire types.

Application code should import canonical models from :mod:`adcp` or
:mod:`adcp.types`.  These aliases exist for migration, conformance tooling,
and AdCP 3.0/3.1 wire adapters.
"""

from typing import Annotated, Any

from pydantic import AnyUrl, ConfigDict, Field, StrictFloat, StrictInt, TypeAdapter, field_validator

from adcp.types.generated_poc.core.creative_asset import CreativeAsset as LegacyCreativeAsset
from adcp.types.generated_poc.core.creative_filters import CreativeFilters as LegacyCreativeFilters
from adcp.types.generated_poc.core.format import Format as LegacyFormat
from adcp.types.generated_poc.core.format_id import FormatReferenceStructuredObject
from adcp.types.generated_poc.core.package import Package as LegacyPackage
from adcp.types.generated_poc.core.placement import Placement as LegacyPlacement
from adcp.types.generated_poc.core.product import Product as LegacyProduct
from adcp.types.generated_poc.core.product_filters import ProductFilters as LegacyProductFilters
from adcp.types.generated_poc.core.product_format_declaration import (
    ProductFormatDeclaration as LegacyGeneratedProductFormatDeclaration,
)
from adcp.types.generated_poc.creative.get_creative_delivery_response import (
    GetCreativeDeliveryResponse as LegacyGetCreativeDeliveryResponse,
)
from adcp.types.generated_poc.creative.list_creatives_request import (
    ListCreativesRequest as LegacyListCreativesRequest,
)
from adcp.types.generated_poc.creative.list_creatives_response import (
    ListCreativesResponse as LegacyListCreativesResponse,
)
from adcp.types.generated_poc.creative.sync_creatives_request import (
    SyncCreativesRequest as LegacySyncCreativesRequest,
)
from adcp.types.generated_poc.creative.sync_creatives_response import (
    SyncCreativesResponse as LegacySyncCreativesResponse,
)
from adcp.types.generated_poc.media_buy.create_media_buy_request import (
    CreateMediaBuyRequest as LegacyCreateMediaBuyRequest,
)
from adcp.types.generated_poc.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse as LegacyCreateMediaBuyResponse,
)
from adcp.types.generated_poc.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse1 as LegacyCreateMediaBuyResponse1,
)
from adcp.types.generated_poc.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse2 as LegacyCreateMediaBuyResponse2,
)
from adcp.types.generated_poc.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse3 as LegacyCreateMediaBuyResponse3,
)
from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (
    GetMediaBuyDeliveryResponse as LegacyGetMediaBuyDeliveryResponse,
)
from adcp.types.generated_poc.media_buy.get_media_buys_response import (
    GetMediaBuysResponse as LegacyGetMediaBuysResponse,
)
from adcp.types.generated_poc.media_buy.get_products_request import (
    GetProductsRequest as LegacyGetProductsRequest,
)
from adcp.types.generated_poc.media_buy.get_products_response import (
    GetProductsResponse as LegacyGetProductsResponse,
)
from adcp.types.generated_poc.media_buy.list_creative_formats_request import (
    ListCreativeFormatsRequest as LegacyListCreativeFormatsRequest,
)
from adcp.types.generated_poc.media_buy.list_creative_formats_response import (
    ListCreativeFormatsResponse as LegacyListCreativeFormatsResponse,
)
from adcp.types.generated_poc.media_buy.package_request import (
    PackageRequest as LegacyPackageRequest,
)
from adcp.types.generated_poc.media_buy.package_update import PackageUpdate as LegacyPackageUpdate
from adcp.types.generated_poc.media_buy.update_media_buy_request import (
    UpdateMediaBuyRequest as LegacyUpdateMediaBuyRequest,
)
from adcp.types.generated_poc.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse as LegacyUpdateMediaBuyResponse,
)
from adcp.types.generated_poc.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse1 as LegacyUpdateMediaBuyResponse1,
)
from adcp.types.generated_poc.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse2 as LegacyUpdateMediaBuyResponse2,
)
from adcp.types.generated_poc.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse3 as LegacyUpdateMediaBuyResponse3,
)

_URL_ADAPTER = TypeAdapter(AnyUrl)


class LegacyFormatId(FormatReferenceStructuredObject):
    """Legacy tuple that validates a URL without rewriting its wire spelling."""

    model_config = ConfigDict(extra="allow")

    # A wire-preserving string intentionally narrows the generated AnyUrl
    # field: AnyUrl appends a slash and changes the normative legacy tuple.
    agent_url: str  # type: ignore[assignment]
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]+$")]
    width: Annotated[StrictInt | None, Field(ge=1)] = None
    height: Annotated[StrictInt | None, Field(ge=1)] = None
    duration_ms: Annotated[StrictInt | StrictFloat | None, Field(ge=1)] = None

    @field_validator("agent_url")
    @classmethod
    def _validate_agent_url(cls, value: str) -> str:
        _URL_ADAPTER.validate_python(value)
        return value

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Preserve the original agent_url bytes in explicit legacy output."""

        return super().model_dump(**kwargs)


LegacyFormatReferenceStructuredObject = LegacyFormatId
LegacyProductFormatDeclaration = LegacyGeneratedProductFormatDeclaration

__all__ = [
    "LegacyCreateMediaBuyRequest",
    "LegacyCreateMediaBuyResponse",
    "LegacyCreateMediaBuyResponse1",
    "LegacyCreateMediaBuyResponse2",
    "LegacyCreateMediaBuyResponse3",
    "LegacyCreativeAsset",
    "LegacyCreativeFilters",
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
    "LegacyProduct",
    "LegacyProductFilters",
    "LegacyProductFormatDeclaration",
    "LegacySyncCreativesRequest",
    "LegacySyncCreativesResponse",
    "LegacyUpdateMediaBuyRequest",
    "LegacyUpdateMediaBuyResponse",
    "LegacyUpdateMediaBuyResponse1",
    "LegacyUpdateMediaBuyResponse2",
    "LegacyUpdateMediaBuyResponse3",
]
