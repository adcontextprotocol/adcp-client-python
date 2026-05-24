"""Wire-faithful ``ProductFormatDeclaration`` for the v2 catalog surface.

The upstream schema ``core/product-format-declaration.json`` is a
discriminated ``oneOf`` over 13 ``format_kind`` values, each binding
``params`` to a canonical-specific schema. ``datamodel-code-generator``
collapses this shape to a single class carrying only the shared
properties — ``format_kind`` and ``params`` disappear entirely because
they live on the per-variant branches.

That generated stub is unusable for canonical-formats: it can't carry
the discriminator the projection layer routes on, and it silently drops
``params`` (``extra='ignore'``) so adopters who construct a declaration
with a typed canonical body lose it on serialization.

This module replaces the public ``ProductFormatDeclaration`` symbol
with a hand-rolled class that:

* Carries all 9 shared properties the generator emits.
* Adds ``format_kind: CanonicalFormatKind`` (the discriminator).
* Adds ``params: dict[str, Any] | None`` — the per-canonical body. Kept
  as an open dict at this level so the same class works across all 13
  canonical kinds; callers can validate ``params`` against the typed
  canonical format class (e.g., :class:`adcp.types.CanonicalFormatImage`)
  when they know the kind.
* Sets ``extra='allow'`` so future ``ProductFormatDeclaration`` field
  additions in 3.1.x don't break round-trip through this model.

The generated class is preserved as ``_GeneratedProductFormatDeclaration``
for callers that need the original codegen output (validation hooks,
schema-loader cross-references). New code SHOULD import the
hand-rolled class via :mod:`adcp.types`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import ConfigDict, Field

from adcp.types.base import AdCPBaseModel
from adcp.types.generated_poc.core.canonical_format_kind import (
    CanonicalFormatKind,
)
from adcp.types.generated_poc.core.format_id import (
    FormatReferenceStructuredObject,
)
from adcp.types.generated_poc.core.platform_extension_ref import (
    PlatformExtensionReference,
)
from adcp.types.generated_poc.core.product_format_declaration import (
    ProductFormatDeclaration as _GeneratedProductFormatDeclaration,
)
from adcp.types.generated_poc.core.product_format_declaration import (
    SellerPreference,
)
from adcp.types.generated_poc.enums.channels import MediaChannel


class ProductFormatDeclaration(AdCPBaseModel):
    """v2 catalog-side format declaration carrying the canonical discriminator.

    Wire-faithful Python representation of
    ``core/product-format-declaration.json``. See the module docstring for
    why this class replaces the codegen output.

    The ``params`` field is intentionally an open ``dict[str, Any]``. The
    upstream schema binds ``params`` to a different ``$ref`` per
    ``format_kind`` value (`image` → ``formats/canonical/image.json``,
    `video_vast` → ``formats/canonical/video_vast.json``, etc.). Carrying
    a discriminated union here would propagate the codegen's variant
    numbering problem back into the public API. Callers needing typed
    access SHOULD construct the canonical format class explicitly:

    .. code-block:: python

        from adcp.types import CanonicalFormatKind, CanonicalFormatImage
        from adcp.types import ProductFormatDeclaration

        decl = ProductFormatDeclaration(
            format_kind=CanonicalFormatKind.image,
            params=CanonicalFormatImage(...).model_dump(exclude_none=True),
        )
    """

    model_config = ConfigDict(extra="allow")

    format_kind: Annotated[
        CanonicalFormatKind,
        Field(description="The canonical format kind this declaration declares."),
    ]
    params: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Per-canonical body. Shape varies by format_kind — see the "
                "canonical's own schema (`formats/canonical/<kind>.json`)."
            ),
        ),
    ] = None
    capability_id: Annotated[
        str | None,
        Field(
            description=(
                "Stable identifier for this declaration. REQUIRED when the "
                "parent product's format_options[] contains multiple "
                "declarations sharing the same format_kind."
            ),
        ),
    ] = None
    display_name: Annotated[
        str | None,
        Field(description="Optional seller-controlled human-readable label."),
    ] = None
    applies_to_channels: Annotated[
        list[MediaChannel] | None,
        Field(
            description=(
                "Optional subset of the parent product's channels to which "
                "this declaration applies."
            ),
        ),
    ] = None
    seller_preference: Annotated[
        SellerPreference | None,
        Field(description="Soft routing hint within the accepted set."),
    ] = None
    canonical_formats_only: Annotated[
        bool | None,
        Field(
            description=(
                "When true, this declaration has no clean v1 projection — "
                "SDKs MUST NOT synthesize a v1 format_id."
            ),
        ),
    ] = False
    experimental: Annotated[
        bool | None,
        Field(
            description=("When true, THIS seller's specific declaration may not work as declared."),
        ),
    ] = False
    format_shape: Annotated[
        str | None,
        Field(
            description=(
                "REQUIRED when format_kind='custom'; otherwise MUST be absent. "
                "Recognized format-shape-vocabulary entry."
            ),
        ),
    ] = None
    v1_format_ref: Annotated[
        list[FormatReferenceStructuredObject] | None,
        Field(
            description=(
                "Authoritative v2 → v1 link as one or more v1 format_id "
                "({agent_url, id}) values."
            ),
            min_length=1,
        ),
    ] = None
    format_schema: Annotated[
        PlatformExtensionReference | None,
        Field(
            description=(
                "REQUIRED when format_kind='custom'; otherwise MUST be absent. "
                "URI+digest reference to the custom shape's schema."
            ),
        ),
    ] = None


__all__ = [
    "ProductFormatDeclaration",
    "_GeneratedProductFormatDeclaration",
]
