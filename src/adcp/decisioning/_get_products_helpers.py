"""Fields-projection helper for ``MediaBuyHandler.get_products``.

When a buyer supplies ``GetProductsRequest.fields`` (a 30-value enum,
``min_length=1``), the framework drops unrequested product fields before
returning the response.  This module owns that post-processing.

Four categories of ``Product`` fields are **always** passed through
regardless of the buyer's selection:

1. **Required-by-schema fields** — ``Product.model_fields`` entries where
   ``field_info.is_required()`` is True.  These seven fields cannot be
   ``None`` and the model cannot be constructed without them:
   ``product_id``, ``name``, ``description``, ``publisher_properties``,
   ``delivery_type``, ``pricing_options``, ``reporting_capabilities``.

1a. **Fields the schema root's combinator names** — ``format_ids`` and
   ``format_options``, which the root requires ONE OF. Neither is required on
   its own, so category 1 does not reach them, and dropping both produced a
   document the schema refuses. See ``_COMBINATOR_PRODUCT_FIELDS``.

2. **Non-enum declared fields** — optional ``Product.model_fields`` entries
   that have no corresponding ``GetProductsField`` enum value
   (``allowed_actions``, ``cancellation_policy``, ``ext``,
   ``material_submission``, ``measurement_readiness``, ``measurement_terms``,
   ``performance_standards``, ``property_targeting_allowed``,
   ``vendor_metric_optimization``).  The enum does not name them so a buyer
   has no mechanism to opt in or out; the framework leaves them unchanged.

3. **Extension fields** — keys present in ``Product.__pydantic_extra__``
   (the model carries ``extra='allow'``).  These are seller-defined
   extension fields not named by the spec; the framework passes them through
   unconditionally.

When ``params.fields`` is ``None`` or empty the handler guard
(``if params.fields:``) prevents this function from being called at all;
no copy is made.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from adcp.types import GetProductsField, GetProductsResponse, Product
from adcp.types.domains.media_buy.get_products_request import Fields as _RequestOnlyField
from adcp.types.domains.media_buy.product_fields import ProductResponseField

logger = logging.getLogger(__name__)

#: What ``GetProductsRequest.fields`` actually declares: a union of two
#: generated enums (38 + 17 members). The public ``GetProductsField`` alias
#: resolves to a third, 53-member enum that the request never holds — one of
#: the names #1360 measured as typed a variant off its referent. Accept every
#: spelling: the projection reads only ``.value``, which all three carry.
_FieldSelection = GetProductsField | ProductResponseField | _RequestOnlyField

# ---------------------------------------------------------------------------
# Module-level constants derived from the live model so they stay in sync
# with schema regenerations.
# ---------------------------------------------------------------------------

#: All schema-required Product fields.  The projection must keep these
#: regardless of what the buyer requested — they cannot be None and the
#: model cannot be constructed without them.
_REQUIRED_PRODUCT_FIELDS: frozenset[str] = frozenset(
    k for k, v in Product.model_fields.items() if v.is_required()
)

#: Fields the schema root names in its own combinator, which the projection must
#: also keep. SEPARATE from the set above, and the distinction is the whole
#: point: those fields cannot be absent, these are INDIVIDUALLY OPTIONAL and
#: COLLECTIVELY REQUIRED. ``core/product.json``'s root states
#: ``sent(format_ids) or sent(format_options)``, so neither is required on its
#: own and dropping both produces a product the schema refuses —
#: ``is_required()`` cannot express that, because it asks about one field at a
#: time.
#:
#: Which is exactly how ten tests came to fail: the projection kept the seven
#: individually-required fields, dropped ``format_options`` as
#: optional-and-unrequested, and the re-validation then failed the root's own
#: constraint. A buyer narrowing ``fields`` got back a document the seller
#: cannot legally serve (salesagent-8qela.86).
#:
#: Folding these INTO ``_REQUIRED_PRODUCT_FIELDS`` was tried and is wrong: that
#: set means "cannot be absent", and
#: ``test_projection_keeps_all_required_fields_even_when_not_requested``
#: iterates it asserting each is non-None — true of a required field and false
#: of one arm of a combinator. Two meanings, two sets.
#:
#: The generator declares the names on the class, so this asks the model instead
#: of listing them; a hand-written list is what went stale. Direct attribute
#: access on purpose, so a regeneration that stopped emitting it fails at import
#: rather than quietly reinstating the defect.
_COMBINATOR_PRODUCT_FIELDS: frozenset[str] = Product._COMBINATOR_FIELDS

#: Every string value in the GetProductsField enum (the buyer selection
#: surface, 30 values).
_GET_PRODUCTS_FIELD_VALUES: frozenset[str] = frozenset(f.value for f in GetProductsField)

#: Optional Product.model_fields keys that have no GetProductsField enum
#: entry.  A buyer cannot request or exclude these; the framework always
#: passes them through.
_NON_ENUM_PRODUCT_FIELDS: frozenset[str] = (
    frozenset(Product.model_fields.keys()) - _GET_PRODUCTS_FIELD_VALUES
)


def _project_product_fields(
    response: GetProductsResponse,
    fields: Sequence[_FieldSelection],
) -> GetProductsResponse:
    """Drop unrequested product fields from *response*.

    Keeps: all required-by-schema fields, all non-enum declared fields,
    all extension (``extra='allow'``) fields, and any explicitly requested
    fields.  Drops only: optional enum-declared fields the buyer did not
    request.

    The caller must ensure *fields* is non-empty (the handler guard
    ``if params.fields:`` enforces this).
    """
    requested = frozenset(f.value for f in fields)
    keep_declared = (
        _REQUIRED_PRODUCT_FIELDS | _COMBINATOR_PRODUCT_FIELDS | requested | _NON_ENUM_PRODUCT_FIELDS
    )

    if response.products is None:
        return response

    projected: list[Product] = []
    for p in response.products:
        raw = p.model_dump(mode="json")
        # Extra keys: present in the serialised dict but not declared in
        # model_fields.  model_dump() with extra='allow' emits them at the
        # top level alongside declared fields.
        extra_keys = frozenset(raw.keys()) - frozenset(Product.model_fields.keys())
        all_keep = keep_declared | extra_keys
        filtered = {k: v for k, v in raw.items() if k in all_keep}
        projected.append(Product.model_validate(filtered))

    logger.debug("[adcp.decisioning] fields projection applied to %d product(s)", len(projected))
    return response.model_copy(update={"products": projected})
