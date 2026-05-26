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
* Adds ``params: dict[str, Any]`` — the per-canonical body. Required
  per the upstream schema (``required: ["format_kind", "params"]``).
  Kept as an open dict at this level so the same class works across
  all 13 canonical kinds; callers needing typed access SHOULD use
  :meth:`ProductFormatDeclaration.params_as` to validate against the
  typed canonical format class.
* Sets ``extra='allow'`` so future ``ProductFormatDeclaration`` field
  additions in 3.1.x don't break round-trip through this model. Extra
  fields are scanned for credential-shaped key suffixes at construction
  time; presence of one raises (see ``_CREDENTIAL_SHAPED_KEY_SUFFIXES``).
* Enforces the schema's normative cross-field constraint that
  ``canonical_formats_only=True`` and ``v1_format_ref[]`` are mutually
  exclusive (``product-format-declaration.json`` ``allOf.not`` clause).

The generated class is preserved as ``_GeneratedProductFormatDeclaration``
for callers that need the original codegen output (validation hooks,
schema-loader cross-references). New code SHOULD import the
hand-rolled class via :mod:`adcp.types`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

if TYPE_CHECKING:
    from typing_extensions import Self


# Credential-shaped key suffixes — mirrors the dispatcher's
# ``_CREDENTIAL_SHAPED_KEY_SUFFIXES`` in ``adcp.decisioning.dispatch``.
# ``extra='allow'`` on this model + the open ``params`` dict are both
# adopter-controlled bags that round-trip through ``format_options[]``
# into buyer responses and the idempotency replay cache. Sellers
# accidentally stuffing credentials onto a declaration is the same bug
# class as the ``ctx_metadata`` credential leak — block it at
# construction with the same suffix list.
_CREDENTIAL_SHAPED_KEY_SUFFIXES: tuple[str, ...] = (
    "credential",
    "credentials",
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "bearer",
)


def _key_is_credential_shaped(key: str) -> bool:
    """Case-insensitive suffix match against ``_CREDENTIAL_SHAPED_KEY_SUFFIXES``."""
    lowered = key.lower()
    return any(lowered.endswith(suffix) for suffix in _CREDENTIAL_SHAPED_KEY_SUFFIXES)


def _walk_for_credential_keys(value: Any, *, path: str = "") -> str | None:
    """Return the first credential-shaped key path under ``value``, else ``None``.

    Walks ``dict`` / ``list`` / ``tuple`` recursively. Pydantic models
    are walked via ``model_dump(mode="python")`` so the structured
    extras stored under ``__pydantic_extra__`` are reachable.
    """
    if isinstance(value, dict):
        for key, sub in value.items():
            sub_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and _key_is_credential_shaped(key):
                return sub_path
            found = _walk_for_credential_keys(sub, path=sub_path)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            found = _walk_for_credential_keys(item, path=f"{path}[{i}]")
            if found is not None:
                return found
    elif isinstance(value, BaseModel):
        return _walk_for_credential_keys(value.model_dump(mode="python"), path=path)
    return None


_TypedParams = TypeVar("_TypedParams", bound=BaseModel)


class ProductFormatDeclaration(AdCPBaseModel):
    """v2 catalog-side format declaration carrying the canonical discriminator.

    Wire-faithful Python representation of
    ``core/product-format-declaration.json``. See the module docstring for
    why this class replaces the codegen output.
    """

    model_config = ConfigDict(extra="allow")

    format_kind: Annotated[
        CanonicalFormatKind,
        Field(description="The canonical format kind this declaration declares."),
    ]
    params: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Per-canonical body. Shape varies by format_kind — see the "
                "canonical's own schema (``formats/canonical/<kind>.json``). "
                "Use :meth:`params_as` for typed access."
            ),
        ),
    ]
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
        bool,
        Field(
            description=(
                "When true, this declaration has no clean v1 projection — "
                "SDKs MUST NOT synthesize a v1 format_id. Mutually exclusive "
                "with ``v1_format_ref``."
            ),
        ),
    ] = False
    experimental: Annotated[
        bool,
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
                "({agent_url, id}) values. Mutually exclusive with "
                "``canonical_formats_only=True``."
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

    @model_validator(mode="after")
    def _check_mutual_exclusion(self) -> Self:
        """Enforce the schema's ``allOf.not`` clause.

        ``product-format-declaration.json`` declares
        ``canonical_formats_only=True`` and ``v1_format_ref[]`` mutually
        exclusive. The Pydantic model rejects the combination at
        construction so the SDK never launders a wire-invalid declaration
        into a wire-valid one.
        """
        if self.canonical_formats_only and self.v1_format_ref:
            raise ValueError(
                "ProductFormatDeclaration: canonical_formats_only=True is "
                "mutually exclusive with v1_format_ref[] — a declaration can "
                "EITHER assert no v1 projection OR link to v1 named formats, "
                "never both. See product-format-declaration.json#allOf.not."
            )
        return self

    @model_validator(mode="after")
    def _reject_credential_shaped_extras(self) -> Self:
        """Fail-closed scan for credential-shaped keys in ``params`` + extras.

        ``params`` is an open dict and ``model_config['extra']='allow'``
        means unknown top-level fields are stored on the instance. Both
        are adopter-controlled bags that round-trip through
        ``format_options[]`` responses and the idempotency replay cache.
        Mirrors the dispatcher's ``ctx_metadata`` credential gate.
        """
        for bag_name, bag_value in (
            ("params", self.params),
            ("extras", self.__pydantic_extra__),
        ):
            if bag_value is None:
                continue
            found = _walk_for_credential_keys(bag_value, path=bag_name)
            if found is not None:
                raise ValueError(
                    f"ProductFormatDeclaration: {found!r} matches a "
                    f"credential-shaped key suffix and will round-trip to "
                    f"buyers via format_options[]. Move the value to "
                    f"AuthInfo.credential or a typed credential class. "
                    f"See CLAUDE.md → 'ctx_metadata: write-only credentials "
                    f"prohibited' for the equivalent dispatch-side rule."
                )
        return self

    def params_as(self, canonical_type: type[_TypedParams]) -> _TypedParams:
        """Validate ``params`` against the typed canonical-format class.

        Lets buyers and seller-side validators recover full typing on
        the per-canonical body — e.g., ``decl.params_as(CanonicalFormatImage)``
        returns a ``CanonicalFormatImage`` with ``.sizes`` / ``.format`` /
        etc. narrowed. Raises :class:`pydantic.ValidationError` when
        ``params`` doesn't match the canonical's schema.

        Args:
            canonical_type: A Pydantic model class from the canonical
                vocabulary (e.g., :class:`adcp.types.CanonicalFormatImage`).

        Returns:
            An instance of ``canonical_type`` validated against ``params``.
        """
        return canonical_type.model_validate(self.params)


__all__ = [
    "ProductFormatDeclaration",
    "_GeneratedProductFormatDeclaration",
]
