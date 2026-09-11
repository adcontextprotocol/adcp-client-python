from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, Literal, TypeAlias, TypeVar

from adcp.types.base import AdCPBaseModel
from adcp.types.generated_poc.core.canonical_format_kind import CanonicalFormatKind
from adcp.types.generated_poc.core.protocol_envelope import ProtocolEnvelope
from adcp.types.generated_poc.core.version_envelope import AdcpVersionEnvelope
from adcp.types.generated_poc.enums.task_status import TaskStatus
from adcp.types.legacy import LegacyFormatId

_T = TypeVar("_T", bound=AdCPBaseModel)

class CanonicalBoundaryModel(AdCPBaseModel):
    __adcp_canonical_creative_model__: ClassVar[bool]

class _CanonicalResponseEnvelope(AdcpVersionEnvelope, ProtocolEnvelope, CanonicalBoundaryModel):
    """Stub-only Liskov bridge for canonical responses; not a runtime class.

    The canonical response clones inherit :class:`AdcpVersionEnvelope`,
    :class:`ProtocolEnvelope` and :class:`CanonicalBoundaryModel` directly at
    runtime, so ``isinstance``/``issubclass`` agree with this stub. Collapsing
    them into one private ancestor exists purely for the type checker: several
    schema arms pin an envelope field to a ``Literal``, and narrowing an
    inherited mutable attribute is a Liskov violation. Relaxing the pinned
    fields to ``Any`` once here lets each arm declare its precise literal with
    no per-field suppression, and keeps plain-string construction
    (``status="completed"``) working for adopters.

    This is a bridge, not a public type. Every concrete response below
    re-declares ``status`` with the exact annotation its runtime model carries,
    so no adopter ever reads ``Any`` off one of them — enforced by
    ``test_canonical_response_stub_status_matches_runtime``. The ``= ...``
    matters as much as the type: ``status`` is defaulted on every runtime
    response, so a bare ``status: Any`` would make the synthesized ``__init__``
    demand it and reject a plain ``GetMediaBuysResponse(media_buys=[])``.
    """

    status: Any = ...

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
    format_kind: CanonicalFormatKind | str
    format_option_ref: Any

class Creative(CanonicalBoundaryModel):
    creative_id: str
    format_kind: CanonicalFormatKind | str
    format_option_ref: Any

class CreativeManifest(CanonicalBoundaryModel):
    format_kind: CanonicalFormatKind | str | None = ...
    assets: dict[str, Any]

class CreativeVariant(CanonicalBoundaryModel):
    manifest: CreativeManifest | None

class DeliveryCreative(CanonicalBoundaryModel):
    creative_id: str
    format_kind: CanonicalFormatKind | str | None
    variants: list[CreativeVariant]

class CreativeFilters(CanonicalBoundaryModel): ...
class ProductFilters(CanonicalBoundaryModel): ...

class PackageRequest(AdcpVersionEnvelope, CanonicalBoundaryModel):
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

class GetProductsRequest(AdcpVersionEnvelope, CanonicalBoundaryModel):
    account: Any
    filters: ProductFilters | None
    fields: Any
    refine: Any
    time_budget: Any
    pagination: Any

class GetProductsResponse(_CanonicalResponseEnvelope):
    status: TaskStatus = ...
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

class CreateMediaBuyRequest(AdcpVersionEnvelope, CanonicalBoundaryModel):
    account: Any
    packages: list[PackageRequest] | None

class UpdateMediaBuyRequest(AdcpVersionEnvelope, CanonicalBoundaryModel):
    account: Any
    media_buy_id: str
    packages: list[PackageUpdate] | None
    new_packages: list[PackageRequest] | None

class CreateMediaBuyResponse1(_CanonicalResponseEnvelope):
    media_buy_id: str
    packages: list[Package]
    # AdCP 3.2 drops the synchronous task-envelope status from this arm, so the
    # runtime model pins it to the single outcome and defaults it. Mirror both
    # halves: the literal and the default.
    status: Literal["completed"] = ...
    # Required *and* nullable: the schema lists confirmed_at in the success
    # branch's ``required`` while typing it ``["string", "null"]``. A buy
    # awaiting seller commitment carries the key with a null value.
    confirmed_at: datetime | None
    def __init__(
        self,
        *,
        media_buy_id: str,
        confirmed_at: datetime | None,
        revision: int,
        packages: list[Package],
        status: Literal["completed"] = ...,
        media_buy_status: Any = ...,
        **data: Any,
    ) -> None: ...

class CreateMediaBuyResponse2(_CanonicalResponseEnvelope):
    status: TaskStatus = ...

class CreateMediaBuyResponse3(_CanonicalResponseEnvelope):
    status: Literal[TaskStatus.submitted] = ...

CreateMediaBuyResponse: TypeAlias = (
    CreateMediaBuyResponse1 | CreateMediaBuyResponse2 | CreateMediaBuyResponse3
)

class UpdateMediaBuyResponse1(_CanonicalResponseEnvelope):
    media_buy_id: str
    # The 3.x schema arm pins ``status`` to the single synchronous outcome.
    # ``_CanonicalResponseEnvelope`` is what makes this precise literal legal
    # without a per-field suppression, and it keeps ``status="completed"``
    # constructible — see tests/type_checks/extend_response_with_sequence.py.
    status: Literal["completed"] = ...
    revision: int
    media_buy_status: Any = ...
    affected_packages: Sequence[Package] | None = ...

class UpdateMediaBuyResponse2(_CanonicalResponseEnvelope):
    status: TaskStatus = ...

class UpdateMediaBuyResponse3(_CanonicalResponseEnvelope):
    status: Literal[TaskStatus.submitted] = ...

UpdateMediaBuyResponse: TypeAlias = (
    UpdateMediaBuyResponse1 | UpdateMediaBuyResponse2 | UpdateMediaBuyResponse3
)

class SyncCreativesRequest(AdcpVersionEnvelope, CanonicalBoundaryModel):
    account: Any
    creatives: list[CreativeAsset]

class ListCreativesRequest(AdcpVersionEnvelope, CanonicalBoundaryModel):
    account: Any
    filters: CreativeFilters | None
    fields: Any

class ListCreativesResponse(_CanonicalResponseEnvelope):
    status: TaskStatus = ...
    creatives: list[Creative]

class MediaBuyPackage(CanonicalBoundaryModel): ...

class MediaBuy(CanonicalBoundaryModel):
    packages: Sequence[MediaBuyPackage]

class GetMediaBuysResponse(_CanonicalResponseEnvelope):
    status: TaskStatus = ...
    media_buys: Sequence[MediaBuy]

class GetMediaBuyDeliveryResponse(_CanonicalResponseEnvelope):
    status: TaskStatus = ...

class GetCreativeDeliveryResponse(_CanonicalResponseEnvelope):
    status: TaskStatus = ...
    creatives: Sequence[DeliveryCreative]

PRIMARY_CANONICAL_MODELS: tuple[type[CanonicalBoundaryModel], ...]

def is_legacy_creative_identity_key(key: object) -> bool: ...
def sanitize_canonical_schema(schema: dict[str, Any]) -> dict[str, Any]: ...
def strip_legacy_creative_identity(value: Any) -> Any: ...
