"""v2 → v1 canonical-format projection.

Projects ``Product.format_options[]`` (v2) into ``Product.format_ids[]``
(v1) so v1-only buyers see the product without losing visibility, while
emitting non-fatal advisories on ``errors[]`` for the seller to act on.

Resolution order per ``registries/v1-canonical-mapping.json`` (the
"direction of truth" section is normative):

1. ``canonical_formats_only=True`` — no v1 emit and no advisory. The
   seller has explicitly opted out of v1 projection. Note that
   ``ProductFormatDeclaration`` enforces this is mutually exclusive
   with ``v1_format_ref[]`` at construction.
2. ``v1_format_ref[]`` set — emit those refs into ``format_ids[]``.
   Applies to every ``format_kind`` including ``custom`` (a custom
   format MAY carry seller-asserted v1 refs). If ``params.sizes[]``
   count > ``v1_format_ref[]`` count, emit
   ``FORMAT_DECLARATION_V1_LOSSY_MULTI_SIZE`` (advisory only; the partial
   coverage still ships).
3. ``v1_format_ref[]`` absent AND the canonical's ``v1_translatable``
   default is ``False`` — no v1 emit, no advisory. Canonicals
   ``agent_placement``, ``sponsored_placement``, ``responsive_creative``,
   ``image_carousel``, and ``custom`` (without seller-asserted refs)
   are v1-unreachable by design; warning here would spam the wire.
4. ``v1_format_ref[]`` absent AND the canonical is normally
   ``v1_translatable=True`` — emit ``FORMAT_DECLARATION_V1_AMBIGUOUS``.
   The SDK explicitly does NOT synthesize a v1 ``format_id`` from a
   structural registry match; that would produce inter-SDK divergence
   on structurally-equal v2 declarations.

The registry is intentionally NOT consulted on the v2 → v1 path
(see "Direction of truth (normative)" in
``registries/v1-canonical-mapping.json``). The v1 → v2 reverse path
will consume it in a follow-up PR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from adcp.canonical_formats.advisory import _echo_identifier, make_sdk_advisory
from adcp.types import (
    CanonicalFormatKind,
    Error,
    ProductFormatDeclaration,
)
from adcp.types.legacy import LegacyFormatId

FormatId = LegacyFormatId

# Per-canonical ``v1_translatable`` default, mirrored from the schemas
# under ``schemas/cache/<version>/formats/canonical/*.json``. Canonicals
# inherit ``v1_translatable=True`` from ``_base.json``; the six explicit
# False entries are overrides on each canonical's own schema file.
# ``custom`` is treated as not-v1-translatable by default — a custom
# format with no seller-asserted ``v1_format_ref[]`` has no projection
# target — but a custom declaration MAY carry ``v1_format_ref`` and
# project via step 2.
V1_TRANSLATABLE: dict[CanonicalFormatKind, bool] = {
    CanonicalFormatKind.image: True,
    CanonicalFormatKind.html5: True,
    CanonicalFormatKind.display_tag: True,
    CanonicalFormatKind.image_carousel: False,
    CanonicalFormatKind.video_hosted: True,
    CanonicalFormatKind.video_vast: True,
    CanonicalFormatKind.audio_hosted: True,
    CanonicalFormatKind.audio_vast: True,
    CanonicalFormatKind.audio_daast: True,
    CanonicalFormatKind.sponsored_placement: False,
    CanonicalFormatKind.native_in_feed: True,
    CanonicalFormatKind.responsive_creative: False,
    CanonicalFormatKind.agent_placement: False,
    CanonicalFormatKind.seller_rendered_stateful_display: False,
    CanonicalFormatKind.coordinated_placements: False,
    CanonicalFormatKind.custom: False,
}


@dataclass
class V2ToV1Projection:
    """Result of projecting one or more ``ProductFormatDeclaration``s to v1.

    Attributes:
        format_ids: v1 ``format_ids[]`` entries to dual-emit alongside the
            v2 ``format_options[]``. Empty when the declarations are all
            v1-unreachable (custom / canonical_formats_only / non-translatable
            canonicals).
        advisories: SDK-source ``errors[]`` entries to augment the response
            with. Each carries ``source="sdk"`` and ``sdk_id=<this SDK>``.
    """

    format_ids: list[FormatId] = field(default_factory=list)
    advisories: list[Error] = field(default_factory=list)


def _params_sizes_count(declaration: ProductFormatDeclaration) -> int:
    """Return the number of distinct sizes declared in ``params.sizes[]``.

    ``params`` is an open ``dict[str, Any]`` on the declaration (the
    canonical's own schema narrows it on construction; the wire-level
    type is permissive). ``sizes`` is canonical-image-specific but may
    appear on any multi-size canonical. Returns ``0`` when absent.
    """
    params = getattr(declaration, "params", None)
    if params is None:
        return 0
    if hasattr(params, "model_dump"):
        params_dict = params.model_dump(exclude_none=True)
    elif isinstance(params, dict):
        params_dict = params
    else:
        return 0
    sizes = params_dict.get("sizes")
    if not isinstance(sizes, list):
        return 0
    return len(sizes)


def project_declaration_to_v1(
    declaration: ProductFormatDeclaration,
    *,
    field_path: str = "format_options[]",
    product_id: str | None = None,
) -> V2ToV1Projection:
    """Project a single declaration to v1, emitting advisories per the
    resolution order documented at module level.

    Args:
        declaration: The v2 ``ProductFormatDeclaration`` to project.
        field_path: JSONPath-lite pointer surfaced on emitted advisories
            (e.g., ``products[0].format_options[2]``). The default points
            at the seller-published declaration without product context;
            callers wrapping a ``Product`` should pass the indexed form.
        product_id: Optional product identifier — surfaced in advisory
            ``details.product_id`` for buyer-side correlation.

    Returns:
        :class:`V2ToV1Projection` with the projected refs and any
        advisories the resolution order emitted.
    """
    kind = declaration.format_kind
    refs = list(declaration.legacy_format_refs)

    # Step 1: seller has explicitly opted out of v1 projection.
    # ``ProductFormatDeclaration`` enforces this is mutually exclusive
    # with ``v1_format_ref[]``, so we can't reach step 2 from here.
    if declaration.canonical_formats_only:
        return V2ToV1Projection()

    # Step 2: seller-asserted v1 link — emit refs, check multi-size fan-out.
    if refs:
        advisories: list[Error] = []
        sizes_n = _params_sizes_count(declaration)
        if sizes_n > len(refs):
            details: dict[str, Any] = {
                "format_kind": kind.value,
                "v1_format_ref_count": len(refs),
                "sizes_count": sizes_n,
            }
            if product_id is not None:
                details["product_id"] = _echo_identifier(product_id)
            advisories.append(
                make_sdk_advisory(
                    code="FORMAT_DECLARATION_V1_LOSSY_MULTI_SIZE",
                    message=(
                        f"v1_format_ref[] has {len(refs)} entries but params.sizes[] "
                        f"declares {sizes_n} sizes — the partial v1 emission covers "
                        f"only the referenced sizes. Seller SHOULD author one "
                        f"v1_format_ref entry per size."
                    ),
                    field=field_path,
                    details=details,
                    suggestion=(
                        "Add per-size v1_format_ref[] entries (one per params.sizes "
                        "entry) to give v1-only buyers full size coverage."
                    ),
                )
            )
        return V2ToV1Projection(format_ids=refs, advisories=advisories)

    # Step 3: canonical is not v1-translatable — silent.
    if not V1_TRANSLATABLE.get(kind, True):
        return V2ToV1Projection()

    # Step 4: canonical IS v1-translatable but seller didn't author refs.
    details = {
        "format_kind": kind.value,
        "reason": "no_v1_format_ref",
    }
    if product_id is not None:
        details["product_id"] = _echo_identifier(product_id)
    return V2ToV1Projection(
        advisories=[
            make_sdk_advisory(
                code="FORMAT_DECLARATION_V1_AMBIGUOUS",
                message=(
                    f"Canonical '{kind.value}' is normally v1-translatable but the "
                    f"declaration carries no v1_format_ref[] — SDK cannot synthesize "
                    f"a v1 format_id without seller assertion."
                ),
                field=field_path,
                details=details,
                suggestion=(
                    "Add v1_format_ref[] pointing at the v1 named format(s) this "
                    "declaration projects to (e.g., AAO-hosted formats at "
                    "https://creative.adcontextprotocol.org or a platform-published "
                    "adagents.json formats[] entry)."
                ),
            )
        ]
    )


def project_product_to_v1(
    product: Any,
    *,
    product_index: int | None = None,
) -> V2ToV1Projection:
    """Project every ``format_options[]`` entry on a ``Product`` to v1.

    Walks the product's declarations, applies
    :func:`project_declaration_to_v1` to each, and accumulates the
    aggregated refs + advisories. The product's existing v1 ``format_ids``
    field is preserved by the caller — this helper produces the *additive*
    set that the seller publishes alongside seller-declared v1 ids.

    Args:
        product: A ``Product`` instance carrying ``format_options[]``.
            Duck-typed so the helper works against the wire response,
            adopter-typed wrappers, or in-progress builders.
        product_index: Optional zero-based index of the product within the
            enclosing ``Products[]`` array. When provided, advisories
            carry the indexed field path (``products[N].format_options[K]``)
            so multi-product responses don't collapse to ambiguous pointers.

    Returns:
        :class:`V2ToV1Projection` with the union of per-declaration results.
    """
    declarations = getattr(product, "format_options", None) or []
    product_id = getattr(product, "product_id", None) or getattr(product, "id", None)

    out = V2ToV1Projection()
    for i, decl in enumerate(declarations):
        prefix = (
            f"products[{product_index}].format_options[{i}]"
            if product_index is not None
            else f"format_options[{i}]"
        )
        result = project_declaration_to_v1(
            decl,
            field_path=prefix,
            product_id=product_id,
        )
        out.format_ids.extend(result.format_ids)
        out.advisories.extend(result.advisories)
    return out


__all__ = [
    "V1_TRANSLATABLE",
    "V2ToV1Projection",
    "project_declaration_to_v1",
    "project_product_to_v1",
]
