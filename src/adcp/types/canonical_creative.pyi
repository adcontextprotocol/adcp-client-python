from collections.abc import Sequence
from typing import Any, ClassVar, Literal, TypeAlias, TypeVar

from adcp.types.base import AdCPBaseModel
from adcp.types.generated_poc.core.canonical_format_kind import CanonicalFormatKind
from adcp.types.legacy import LegacyFormatId

_T = TypeVar("_T", bound=AdCPBaseModel)

class CanonicalBoundaryModel(AdCPBaseModel):
    __adcp_canonical_creative_model__: ClassVar[bool]

class Format(CanonicalBoundaryModel):
    format_option_id: str | None
    publisher_domain: str | None
    format_kind: CanonicalFormatKind
    params: dict[str, Any]
    canonical_formats_only: bool | None
    def __init__(
        self,
        *,
        format_kind: CanonicalFormatKind | str,
        params: dict[str, Any],
        format_option_id: str | None = ...,
        publisher_domain: str | None = ...,
        canonical_formats_only: bool | None = ...,
        v1_format_ref: Sequence[LegacyFormatId | dict[str, Any]] | None = ...,
        **data: Any,
    ) -> None: ...
    @property
    def legacy_format_refs(self) -> tuple[LegacyFormatId, ...]: ...
    def params_as(self, canonical_type: type[_T]) -> _T: ...

ProductFormatDeclaration: TypeAlias = Format

class Placement(CanonicalBoundaryModel):
    format_options: list[Format] | None

class Product(CanonicalBoundaryModel):
    product_id: str
    name: str
    description: str
    format_options: list[Format]
    placements: list[Placement] | None
    pricing_options: list[Any]

class CreativeAsset(CanonicalBoundaryModel):
    creative_id: str
    format_kind: CanonicalFormatKind
    format_option_ref: Any

class Creative(CanonicalBoundaryModel):
    creative_id: str
    format_kind: CanonicalFormatKind
    format_option_ref: Any

class CreativeManifest(CanonicalBoundaryModel): ...

class CreativeVariant(CanonicalBoundaryModel):
    manifest: CreativeManifest | None

class DeliveryCreative(CanonicalBoundaryModel):
    creative_id: str
    format_kind: CanonicalFormatKind | None
    variants: list[CreativeVariant]

class CreativeFilters(CanonicalBoundaryModel): ...
class ProductFilters(CanonicalBoundaryModel): ...

class PackageRequest(CanonicalBoundaryModel):
    product_id: str
    format_option_refs: list[Any] | None
    creatives: list[CreativeAsset] | None

class PackageUpdate(CanonicalBoundaryModel):
    package_id: str
    format_option_refs: list[Any] | None
    creatives: list[CreativeAsset] | None

class Package(CanonicalBoundaryModel):
    package_id: str
    product_id: str | None = ...
    format_option_refs: list[Any] | None = ...
    def __init__(
        self,
        *,
        package_id: str,
        product_id: str | None = ...,
        format_option_refs: list[Any] | None = ...,
        **data: Any,
    ) -> None: ...

class GetProductsRequest(CanonicalBoundaryModel):
    account: Any
    filters: ProductFilters | None
    fields: Any
    refine: Any
    time_budget: Any
    pagination: Any

class GetProductsResponse(CanonicalBoundaryModel):
    products: list[Product] | None
    proposals: Any
    refinement_applied: Any
    def __init__(
        self,
        *,
        products: list[Product] | None = ...,
        proposals: Any = ...,
        refinement_applied: Any = ...,
        **data: Any,
    ) -> None: ...

class CreateMediaBuyRequest(CanonicalBoundaryModel):
    account: Any
    packages: list[PackageRequest] | None

class UpdateMediaBuyRequest(CanonicalBoundaryModel):
    account: Any
    media_buy_id: str
    packages: list[PackageUpdate] | None
    new_packages: list[PackageRequest] | None

class CreateMediaBuyResponse1(CanonicalBoundaryModel):
    media_buy_id: str
    packages: list[Package]
    def __init__(
        self,
        *,
        media_buy_id: str,
        status: Any,
        confirmed_at: Any,
        revision: int,
        packages: list[Package],
        media_buy_status: Any = ...,
        **data: Any,
    ) -> None: ...

class CreateMediaBuyResponse2(CanonicalBoundaryModel): ...
class CreateMediaBuyResponse3(CanonicalBoundaryModel): ...

CreateMediaBuyResponse: TypeAlias = (
    CreateMediaBuyResponse1 | CreateMediaBuyResponse2 | CreateMediaBuyResponse3
)

class UpdateMediaBuyResponse1(CanonicalBoundaryModel):
    media_buy_id: str
    status: Literal["completed"]
    revision: int
    media_buy_status: Any = ...
    affected_packages: Sequence[Package] | None = ...

class UpdateMediaBuyResponse2(CanonicalBoundaryModel): ...
class UpdateMediaBuyResponse3(CanonicalBoundaryModel): ...

UpdateMediaBuyResponse: TypeAlias = (
    UpdateMediaBuyResponse1 | UpdateMediaBuyResponse2 | UpdateMediaBuyResponse3
)

class SyncCreativesRequest(CanonicalBoundaryModel):
    account: Any
    creatives: list[CreativeAsset]

class ListCreativesRequest(CanonicalBoundaryModel):
    account: Any
    filters: CreativeFilters | None
    fields: Any

class ListCreativesResponse(CanonicalBoundaryModel):
    creatives: list[Creative]

class MediaBuyPackage(CanonicalBoundaryModel): ...

class MediaBuy(CanonicalBoundaryModel):
    packages: Sequence[MediaBuyPackage]

class GetMediaBuysResponse(CanonicalBoundaryModel):
    media_buys: Sequence[MediaBuy]

class GetMediaBuyDeliveryResponse(CanonicalBoundaryModel): ...

class GetCreativeDeliveryResponse(CanonicalBoundaryModel):
    creatives: Sequence[DeliveryCreative]

PRIMARY_CANONICAL_MODELS: tuple[type[CanonicalBoundaryModel], ...]

def is_legacy_creative_identity_key(key: object) -> bool: ...
def sanitize_canonical_schema(schema: dict[str, Any]) -> dict[str, Any]: ...
def strip_legacy_creative_identity(value: Any) -> Any: ...
