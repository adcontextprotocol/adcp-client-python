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

from copy import copy
from typing import Any, cast, get_args

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from adcp.types.aliases import FormatAssetUnion, GroupFormatAssetUnion, RepeatableAssetGroup
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
from adcp.types.generated_poc.core.canonical_product import PublisherDomain
from adcp.types.generated_poc.core.format import Format
from adcp.types.generated_poc.core.media_buy_features import MediaBuyFeatures
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    AcceptancePolicyDiscovery,
    PrimaryCountry,
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


def _annotation_contains(annotation: Any, expected: type[BaseModel]) -> bool:
    """Return whether a possibly nested annotation contains ``expected``."""
    return annotation is expected or any(
        _annotation_contains(arg, expected) for arg in get_args(annotation)
    )


def _patch_equivalent_model_field(
    model: type[BaseModel],
    field_name: str,
    bundled_model: type[BaseModel],
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


def _apply_forward_compat() -> None:
    """Apply open-union, capability, and public-model compatibility patches."""
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
