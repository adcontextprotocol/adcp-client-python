"""Forward-compatibility and generated-model composition patches.

Patches Format.assets and RepeatableAssetGroup.assets at import
time so responses containing novel asset_type values (e.g., 'pixel_tracker')
parse as UnknownFormatAsset / UnknownGroupAsset instead of raising a cascade
of ValidationErrors that zero out the entire list_creative_formats response.

This module is intentionally NOT auto-generated. It lives outside
generated_poc/ and is preserved across codegen runs (generate_types.py only
wipes src/adcp/types/generated_poc/).

It also replaces identity-distinct bundled clones at public capability
boundaries with their canonical public model classes. This keeps independently
generated views of the same wire schema composable as typed Python objects.

The rc.3 targeting mutation fields retain TargetingOverlayInput for raw data
while accepting beta.14 TargetingOverlay instances as a compatibility bridge.

Import order: comes after _ergonomic in types/__init__.py (alphabetical by
underscore-preserved sort). Importing this module directly triggers import of
adcp.types.aliases as a side effect (via ``from adcp.types.aliases import …``)
so there is no circular-import risk and no strict ordering requirement against
aliases in __init__.py.

Import layering: this module imports directly from generated_poc (like
aliases.py and _ergonomic.py) because it must patch the generated classes
in-place. It is therefore listed in ALLOWED_FILES in test_import_layering.py.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import copy, deepcopy
from functools import partial
from types import GenericAlias
from typing import TYPE_CHECKING, Annotated, Any, cast, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    GetPydanticSchema,
    SerializerFunctionWrapHandler,
    ValidationError,
    ValidatorFunctionWrapHandler,
    create_model,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import CoreSchema, InitErrorDetails, core_schema

from adcp.types.aliases import FormatAssetUnion, GroupFormatAssetUnion, RepeatableAssetGroup
from adcp.types.base import AdCPBaseModel
from adcp.types.canonical_creative import CreateMediaBuyRequest as _PublicCreateMediaBuyRequest
from adcp.types.canonical_creative import PackageRequest as PublicPackageRequest
from adcp.types.canonical_creative import PackageUpdate as PublicPackageUpdate
from adcp.types.canonical_creative import UpdateMediaBuyRequest as _PublicUpdateMediaBuyRequest
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    AcceptancePolicyDiscovery as BundledAcceptancePolicyDiscovery,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    MediaBuy as BundledCapabilitiesMediaBuy,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    Portfolio as BundledCapabilitiesPortfolio,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    PrimaryCountry as BundledPrimaryCountry,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    PublisherDomain as BundledPublisherDomain,
)
from adcp.types.generated_poc.core.async_response_data import AdcpAsyncResponseData
from adcp.types.generated_poc.core.canonical_format_kind import CanonicalFormatKind
from adcp.types.generated_poc.core.canonical_product import PublisherDomain
from adcp.types.generated_poc.core.creative_manifest import CreativeManifest
from adcp.types.generated_poc.core.creative_variant import CreativeVariant
from adcp.types.generated_poc.core.format import Format
from adcp.types.generated_poc.core.mcp_webhook_payload import McpWebhookPayload
from adcp.types.generated_poc.core.media_buy_features import MediaBuyFeatures
from adcp.types.generated_poc.core.reporting_webhook import (
    ReportingWebhook as _GeneratedReportingWebhook,
)
from adcp.types.generated_poc.core.targeting import TargetingOverlay
from adcp.types.generated_poc.core.targeting_input import TargetingOverlayInput
from adcp.types.generated_poc.core.version_envelope import AdcpVersionEnvelope
from adcp.types.generated_poc.creative.get_creative_delivery_response import (
    Creative as DeliveryCreative,
)
from adcp.types.generated_poc.creative.get_creative_delivery_response import (
    GetCreativeDeliveryResponse,
)
from adcp.types.generated_poc.creative.preview_creative_response import PreviewCreativeResponse3
from adcp.types.generated_poc.media_buy.accept_proposal_request import (
    AcceptProposalRequest as _GeneratedAcceptProposalRequest,
)
from adcp.types.generated_poc.media_buy.build_creative_response import (
    BuildCreativeResponse1,
    BuildCreativeResponse3,
    BuildCreativeResponse4,
)
from adcp.types.generated_poc.media_buy.build_creative_response import (
    Creative as BuildCreative,
)
from adcp.types.generated_poc.media_buy.build_creative_response import (
    Variant as BuildCreativeVariant,
)
from adcp.types.generated_poc.media_buy.buy_products_request import (
    BuyProductsRequest as _GeneratedBuyProductsRequest,
)
from adcp.types.generated_poc.media_buy.control_media_buy_request import (
    ControlMediaBuyRequest as _GeneratedControlMediaBuyRequest,
)
from adcp.types.generated_poc.media_buy.create_media_buy_request import (
    CreateMediaBuyRequest as _GeneratedCreateMediaBuyRequest,
)
from adcp.types.generated_poc.media_buy.package_control import PackageControl
from adcp.types.generated_poc.media_buy.package_request import PackageRequest
from adcp.types.generated_poc.media_buy.package_update import PackageUpdate
from adcp.types.generated_poc.media_buy.product_purchase_input import ProductPurchaseInput
from adcp.types.generated_poc.media_buy.update_media_buy_request import (
    UpdateMediaBuyRequest as _GeneratedUpdateMediaBuyRequest,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    AcceptancePolicyDiscovery,
    PrimaryCountry,
)
from adcp.types.generated_poc.trusted_match.context_match_response import (
    ContextMatchResponseRouterPublisher,
)
from adcp.types.generated_poc.trusted_match.offer import Offer
from adcp.types.generated_poc.trusted_match.provider_context_match_response import (
    ContextMatchResponseProviderRouter,
)

_OpenCanonicalFormatKind = Annotated[
    CanonicalFormatKind | str,
    Field(union_mode="left_to_right"),
]


_ReportingOperationId = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=255,
        pattern="^[A-Za-z0-9_.:-]{1,255}$",
        description=(
            "Opt-in AdCP 3.2 reporting-stream correlation extension pending adcp#7885. "
            "Cooperating sellers store and echo the buyer-supplied value verbatim in "
            "media_buy_delivery webhook payloads. Confirm seller support before use; "
            "registrations without a value cannot enable this interim delivery path."
        ),
    ),
]

if TYPE_CHECKING:

    class ReportingWebhook(_GeneratedReportingWebhook):
        """Static view of the optional field patched into the original runtime class."""

        operation_id: _ReportingOperationId = None

    class AcceptProposalRequest(_GeneratedAcceptProposalRequest):
        reporting_webhook: ReportingWebhook | None = None

    class BuyProductsRequest(_GeneratedBuyProductsRequest):
        reporting_webhook: ReportingWebhook | None = None

    class ControlMediaBuyRequest(_GeneratedControlMediaBuyRequest):
        reporting_webhook: ReportingWebhook | None = None

    class CreateMediaBuyRequest(_PublicCreateMediaBuyRequest):
        reporting_webhook: ReportingWebhook | None = None

    class UpdateMediaBuyRequest(_PublicUpdateMediaBuyRequest):
        reporting_webhook: ReportingWebhook | None = None

else:
    # Keep class identity across public imports and nested generated requests.
    ReportingWebhook = _GeneratedReportingWebhook
    AcceptProposalRequest = _GeneratedAcceptProposalRequest
    BuyProductsRequest = _GeneratedBuyProductsRequest
    ControlMediaBuyRequest = _GeneratedControlMediaBuyRequest
    CreateMediaBuyRequest = _PublicCreateMediaBuyRequest
    UpdateMediaBuyRequest = _PublicUpdateMediaBuyRequest


class _ManifestReadbackModel(AdCPBaseModel):
    """Independent from strict inputs: tolerant instances must not validate as them."""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _normalize_readback(cls, data: Any) -> Any:
        if isinstance(data, AdCPBaseModel) and not isinstance(data, cls):
            return data.model_dump(mode="python")
        return data


class _VersionedManifestReadbackModel(AdcpVersionEnvelope, _ManifestReadbackModel):
    """Keep the shared version envelope on build response nodes."""


def _manifest_readback_clone(
    name: str,
    source: type[AdCPBaseModel],
    overrides: dict[str, Any],
    *,
    validators: dict[str, Any] | None = None,
) -> type[AdCPBaseModel]:
    # Copy all field constraints without inheriting the strict source model.
    # A tolerant subclass would pass its parent's default instance validation.
    fields: dict[str, Any] = {
        key: (overrides.get(key, field.annotation), deepcopy(field))
        for key, field in source.model_fields.items()
    }
    return create_model(
        name,
        __base__=(
            _VersionedManifestReadbackModel
            if issubclass(source, AdcpVersionEnvelope)
            else _ManifestReadbackModel
        ),
        __module__=__name__,
        __validators__=validators,
        **fields,
    )


def _normalize_readback_manifest(data: Any) -> Any:
    # Pydantic binds the generated validator proxy to a callable at runtime.
    normalize = cast(Callable[[Any], Any], CreativeManifest._coerce_standalone_assets)
    return normalize(data)


_ReadbackCreativeManifest = _manifest_readback_clone(
    "_ReadbackCreativeManifest",
    CreativeManifest,
    {"format_kind": _OpenCanonicalFormatKind | None},
    validators={
        # Preserve the generated manifest's standalone-asset normalization,
        # without widening that input model or inheriting from it.
        "_coerce_standalone_assets": model_validator(mode="before")(_normalize_readback_manifest),
    },
)
_DeliveryVariant = _manifest_readback_clone(
    "_DeliveryVariant",
    CreativeVariant,
    {"manifest": _ReadbackCreativeManifest | None},
)
_BuildReadbackVariant = _manifest_readback_clone(
    "_BuildReadbackVariant",
    BuildCreativeVariant,
    {"creative_manifest": _ReadbackCreativeManifest},
)
_BuildReadbackCreative = _manifest_readback_clone(
    "_BuildReadbackCreative",
    BuildCreative,
    {
        # This constraint is inside the optional union in the generated type,
        # so it must stay on the non-None arm when replacing that annotation.
        "variants": Annotated[GenericAlias(list, _BuildReadbackVariant), Field(min_length=1)]
        | None,
    },
)
_ReadbackOffer = _manifest_readback_clone(
    "_ReadbackOffer",
    Offer,
    {"creative_manifest": _ReadbackCreativeManifest | None},
)


def _patch_model_field(model: type[BaseModel], field_name: str, new_annotation: Any) -> None:
    """Replace a Pydantic model field's annotation in-place.

    Sets both model_fields (the FieldInfo dict Pydantic uses for schema
    generation) and __annotations__ (for introspection), then forces a
    schema rebuild. Preserves all constraints and metadata from the original field.
    """
    old_fi = model.model_fields.get(field_name)
    if old_fi is None:
        model.model_fields[field_name] = FieldInfo(annotation=new_annotation)
    else:
        # Keep every generated constraint and piece of field metadata. Rebuilding
        # a FieldInfo from only its default and description silently drops items
        # such as min_length, which matters for composability patches on lists.
        new_fi = copy(old_fi)
        new_fi.annotation = new_annotation
        model.model_fields[field_name] = new_fi
    model.__annotations__[field_name] = new_annotation


def _annotation_contains(annotation: Any, expected: type) -> bool:
    """Return whether a possibly nested annotation contains ``expected``."""
    return annotation is expected or any(
        _annotation_contains(arg, expected) for arg in get_args(annotation)
    )


def _patch_equivalent_model_field(
    model: type[BaseModel],
    field_name: str,
    # A bundled clone is matched by class identity, so any class works. Not
    # ``type[BaseModel]``: a bundled scalar root (``PublisherDomain``,
    # ``PrimaryCountry``) is a ``str`` subclass, not a Pydantic model (#1277).
    bundled_model: type,
    canonical_annotation: Any,
) -> None:
    """Replace a bundled clone only after verifying the generated field shape."""
    field = model.model_fields.get(field_name)
    if field is None or not _annotation_contains(field.annotation, bundled_model):
        actual = None if field is None else field.annotation
        raise RuntimeError(
            f"forward compatibility: {model.__name__}.{field_name} lost its "
            f"bundled {bundled_model.__name__} annotation (got {actual!r})"
        )
    _patch_model_field(model, field_name, canonical_annotation)


def _validate_targeting_overlay(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    """Keep the runtime compatibility union out of request-document error paths."""
    try:
        return handler(value)
    except ValidationError as exc:
        # Raw request validation belongs to the Input schema. On failure the
        # legacy arm repeats its errors, with synthetic class-name loc segments.
        # Normalize at this field boundary so direct Pydantic errors, MCP, A2A,
        # and CLI all agree, including callers that never run error narrowing.
        # Only remove the leading Input arm label; nested genuine unions retain
        # all of their locations and errors. Successful validation is unchanged.
        input_errors: list[InitErrorDetails] = []
        for error in exc.errors(include_url=False):
            if error["loc"][:1] != ("TargetingOverlayInput",):
                continue
            detail: InitErrorDetails = {
                "type": error["type"],
                "loc": error["loc"][1:],
                "input": error["input"],
            }
            if "ctx" in error:
                detail["ctx"] = error["ctx"]
            input_errors.append(detail)
        if not input_errors:
            raise
        raise ValidationError.from_exception_data(exc.title, input_errors) from exc


def _serialize_targeting_overlay(value: Any, handler: SerializerFunctionWrapHandler) -> Any:
    """Use the compatibility union for both Python and JSON serialization."""
    return handler(value)


def _targeting_overlay_schema(
    source: Any, handler: GetCoreSchemaHandler, *, materialized_json: bool
) -> CoreSchema:
    """Keep JSON diagnostics native while bridging legacy Python objects."""
    input_schema = handler.generate_schema(TargetingOverlayInput | None)
    compatibility_schema = handler(source)
    json_schema = input_schema
    if materialized_json:
        # Canonical parents already materialize JSON in their before validator.
        # Re-enter core JSON validation for just this subtree to restore native
        # error codes and extra/missing error order. A core Json schema preserves
        # caller strictness/context; a separate TypeAdapter call would not.
        json_schema = core_schema.no_info_before_validator_function(
            json.dumps,
            core_schema.json_schema(input_schema),
            json_schema_input_schema=input_schema,
        )
    return core_schema.json_or_python_schema(
        # Native JSON parents need no Python callback or round trip. In
        # particular, retain diagnostics for repeated keys in the raw JSON.
        json_schema=json_schema,
        python_schema=core_schema.no_info_wrap_validator_function(
            _validate_targeting_overlay,
            compatibility_schema,
        ),
        # json-or-python otherwise selects the Input-only JSON serializer for
        # to_json(), which cannot serialize legacy collection representations.
        # Keep the original union serializer and advertise only mutation input.
        serialization=core_schema.wrap_serializer_function_ser_schema(
            _serialize_targeting_overlay,
            schema=compatibility_schema,
        ),
    )


def _patch_targeting_overlay(model: type[BaseModel]) -> None:
    """Bridge beta.14 objects only while the generated mutation shape is unchanged."""
    field = model.model_fields.get("targeting_overlay")
    if field is None or field.annotation != TargetingOverlayInput | None:
        actual = None if field is None else field.annotation
        raise RuntimeError(
            f"forward compatibility: {model.__name__}.targeting_overlay lost its "
            f"TargetingOverlayInput | None annotation (got {actual!r})"
        )
    # The implementation classes above are the exact public overlay types.
    # Importing them through the lazy facade here would re-enter _eager.
    # Input first is essential: raw dicts keep their rc.3 mutation wrappers,
    # while existing resolved-state instances (including subclasses) retain
    # identity and internal fields without a lossy model_dump() conversion.
    # The legacy arm is runtime-only: advertising it would widen and duplicate
    # the mutation schema in model_json_schema() and MCP tools/list.
    _patch_model_field(
        model,
        "targeting_overlay",
        Annotated[
            TargetingOverlayInput | SkipJsonSchema[TargetingOverlay] | None,
            Field(union_mode="left_to_right"),
            GetPydanticSchema(
                partial(
                    _targeting_overlay_schema,
                    materialized_json=bool(
                        getattr(model, "__adcp_canonical_creative_model__", False)
                    ),
                )
            ),
        ],
    )
    model.model_rebuild(force=True)


def _apply_forward_compat() -> None:
    """Apply open-union, capability, and public-model compatibility patches."""
    for model in (
        PackageRequest,
        PackageUpdate,
        PackageControl,
        ProductPurchaseInput,
        # aliases has already imported canonical_creative, whose public
        # package facades copy FieldInfo rather than sharing generated fields.
        PublicPackageRequest,
        PublicPackageUpdate,
    ):
        _patch_targeting_overlay(model)

    # _ergonomic eagerly builds this legacy parent before the targeting patch.
    # Refresh its cached nested validator as well as the package model itself.
    _GeneratedCreateMediaBuyRequest.model_rebuild(force=True)

    # 3.2.1 permits additional registration fields but does not declare this ID.
    # Keep the extension outside generated code so a schema refresh cannot drop
    # it again. An eventual upstream core definition takes precedence.
    if "operation_id" not in ReportingWebhook.model_fields:
        # Pydantic accepts annotated unions at runtime; its helper's annotation
        # is narrower than that supported dynamic input.
        reporting_operation_id_annotation: Any = _ReportingOperationId
        ReportingWebhook.model_fields["operation_id"] = FieldInfo.from_annotated_attribute(
            reporting_operation_id_annotation, None
        )
        ReportingWebhook.__annotations__["operation_id"] = _ReportingOperationId
        ReportingWebhook.model_rebuild(force=True)
        # Rebuild after targeting patches too: eager parent validators must
        # capture both the reporting extension and patched package inputs.
        for request in (
            AcceptProposalRequest,
            BuyProductsRequest,
            ControlMediaBuyRequest,
            CreateMediaBuyRequest,
            UpdateMediaBuyRequest,
            _GeneratedCreateMediaBuyRequest,
            _GeneratedUpdateMediaBuyRequest,
        ):
            request.model_rebuild(force=True)

    # All response manifests retain unknown future kinds. Patch the generated
    # response classes themselves so public aliases and indirect wrappers agree;
    # public/generated input manifests and direct Creative/CreativeAsset fields
    # stay strict. Private readback nodes cannot bypass strict input validation.
    _patch_model_field(
        DeliveryCreative,
        "format_kind",
        _OpenCanonicalFormatKind | None,
    )
    _patch_model_field(DeliveryCreative, "variants", GenericAlias(list, _DeliveryVariant))
    DeliveryCreative.model_rebuild(force=True)
    GetCreativeDeliveryResponse.model_rebuild(force=True)

    _patch_model_field(PreviewCreativeResponse3, "manifest", _ReadbackCreativeManifest | None)
    PreviewCreativeResponse3.model_rebuild(force=True)
    _patch_model_field(BuildCreativeResponse1, "creative_manifest", _ReadbackCreativeManifest)
    BuildCreativeResponse1.model_rebuild(force=True)
    _patch_model_field(
        BuildCreativeResponse3, "creative_manifests", GenericAlias(list, _ReadbackCreativeManifest)
    )
    BuildCreativeResponse3.model_rebuild(force=True)
    _patch_model_field(
        BuildCreativeResponse4, "creatives", GenericAlias(list, _BuildReadbackCreative)
    )
    BuildCreativeResponse4.model_rebuild(force=True)

    for response in (ContextMatchResponseRouterPublisher, ContextMatchResponseProviderRouter):
        _patch_model_field(response, "offers", GenericAlias(list, _ReadbackOffer))
        response.model_rebuild(force=True)

    # These eager wrappers captured build/preview validators before the patches.
    # Refresh both levels so completed task callbacks retain typed manifests.
    AdcpAsyncResponseData.model_rebuild(force=True)
    McpWebhookPayload.model_rebuild(force=True)

    _patch_model_field(Format, "assets", list[FormatAssetUnion] | None)
    Format.model_rebuild(force=True)

    _patch_model_field(RepeatableAssetGroup, "assets", list[GroupFormatAssetUnion])
    cast(type[BaseModel], RepeatableAssetGroup).model_rebuild(force=True)

    # The 3.1 canonical-creatives capability was published after the bundled
    # generated model. Preserve it across code generation until the schema
    # bundle catches up; negotiation must not silently discard this evidence.
    canonical_creatives_annotation: Any = bool | None
    bundled_media_buy_features_arms = [
        arm
        for arm in get_args(BundledCapabilitiesMediaBuy.model_fields["features"].annotation)
        if arm is not type(None)
    ]
    if len(bundled_media_buy_features_arms) != 1:
        raise RuntimeError(
            "forward compatibility: MediaBuy.features lost its concrete model "
            f"(got {bundled_media_buy_features_arms!r})"
        )
    bundled_media_buy_features = bundled_media_buy_features_arms[0]
    for features_model in (MediaBuyFeatures, bundled_media_buy_features):
        features_model.model_fields["canonical_creatives"] = FieldInfo(
            annotation=canonical_creatives_annotation,
            default=None,
            description=(
                "Advertises canonical creative identity on AdCP 3.1. AdCP 3.2+ "
                "is canonical by contract."
            ),
        )
        features_model.__annotations__["canonical_creatives"] = canonical_creatives_annotation
        features_model.model_rebuild(force=True)

    # Canonical schemas referenced through the bundled capabilities schema are
    # generated a second time as identity-distinct Pydantic classes. Accept the
    # public SDK classes at these capability boundaries so a typed object does
    # not have to be round-tripped through model_dump() before composition.
    _patch_equivalent_model_field(
        BundledCapabilitiesMediaBuy,
        "acceptance_policy_discovery",
        BundledAcceptancePolicyDiscovery,
        AcceptancePolicyDiscovery | None,
    )
    BundledCapabilitiesMediaBuy.model_rebuild(force=True)

    _patch_equivalent_model_field(
        BundledCapabilitiesPortfolio,
        "publisher_domains",
        BundledPublisherDomain,
        list[PublisherDomain],
    )
    _patch_equivalent_model_field(
        BundledCapabilitiesPortfolio,
        "primary_countries",
        BundledPrimaryCountry,
        list[PrimaryCountry] | None,
    )
    BundledCapabilitiesPortfolio.model_rebuild(force=True)


_apply_forward_compat()
