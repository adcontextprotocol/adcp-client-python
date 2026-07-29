"""v1 → v2 canonical-format projection.

Projects a v1 named-format declaration (``core/format.json`` shape)
into a v2 :class:`ProductFormatDeclaration`. Mirror image of
:mod:`adcp.canonical_formats.v2_to_v1` (v2 → v1).

Resolution order — applies the *inbound* (v1→v2) portion of the
normative "Resolution order" in ``registries/v1-canonical-mapping.json``.
The registry numbers its 6-step contract from the perspective of the
full bidirectional graph: step 1 ("v2 → v1 link via v1_format_ref") is
the v2→v1 outbound case handled in :mod:`adcp.canonical_formats.v2_to_v1`,
and step 6 ("fail closed") is the universal terminal. The inbound
applicable steps are 2-6 in the registry's numbering, which we re-
number locally as 1-4 below for SDK-side clarity:

1. **Seller-asserted ``canonical`` annotation on the v1 file**
   (registry step 2). ``v1_format.canonical`` is a
   :class:`CanonicalProjectionReference` carrying ``kind``,
   ``asset_source``, and ``slots_override[]``. Highest priority on
   the v1→v2 path. Registry-published ``parameters`` for a matching
   glob still fill in anything the seller didn't restate.
2. **Registry glob match** (registry step 3). Look up
   ``v1_format.format_id.id`` in the bundled registry's
   ``format_id_glob`` entries. The bundled 3.1.8 registry includes
   literal entries for observed legacy duration, dimension, and VAST
   naming conventions.
3. **Registry structural match** (registry steps 4 + 5). Match
   ``v1_format.assets[*].asset_type`` + VAST/DAAST versions +
   dimensions against the registry's ``structural`` entries. Yields a
   *family-level* identification only — emit
   ``FORMAT_DECLARATION_V1_AMBIGUOUS`` because pure-structural
   patterns can't be inverted back to a specific v1 format_id without
   seller assertion. The v2 declaration still gets a ``format_kind``
   and ``params`` skeleton.
4. **Fail closed** (registry step 6). No match in steps 1-3 — emit
   ``FORMAT_PROJECTION_FAILED`` and emit no v2 declaration. The v1
   format remains valid on the v1 wire; the v2 projection is just
   absent for this entry.

The emitted v2 declaration always carries ``v1_format_ref`` pointing
back at the source v1 format_id, satisfying the v2→v1 reverse path
that half 1 implements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from adcp.canonical_formats.advisory import _echo_identifier, make_sdk_advisory
from adcp.canonical_formats.registry import (
    glob_match,
    load_default_registry,
    structural_match,
)
from adcp.types import (
    CanonicalFormatKind,
    CanonicalProjectionReference,
    Error,
    FormatId,
    ProductFormatDeclaration,
)


@dataclass
class V1ToV2Projection:
    """Result of projecting one v1 named format to a v2 declaration.

    Attributes:
        declaration: The projected ``ProductFormatDeclaration``, or
            ``None`` when projection failed closed (see step 5 above).
            When non-``None`` the declaration carries ``v1_format_ref``
            pointing back at the source v1 format.
        advisories: SDK-source ``errors[]`` entries the resolution
            order emitted. May include
            ``FORMAT_DECLARATION_V1_AMBIGUOUS`` (family-only structural
            match) or ``FORMAT_PROJECTION_FAILED`` (no match).
    """

    declaration: ProductFormatDeclaration | None = None
    advisories: list[Error] = field(default_factory=list)


def _v1_format_id(v1_format: Any) -> FormatId | None:
    """Extract the v1 ``format_id`` from a v1 format declaration.

    Tolerates a raw dict, a Pydantic ``Format``, or a duck-typed object
    carrying ``format_id``. Returns ``None`` when the shape doesn't
    expose a parseable id (the caller will then fail-closed).
    """
    fid = (
        v1_format.get("format_id")
        if isinstance(v1_format, dict)
        else getattr(v1_format, "format_id", None)
    )
    if fid is None:
        return None
    if isinstance(fid, FormatId):
        return fid
    if isinstance(fid, dict):
        try:
            return FormatId.model_validate(fid)
        except ValidationError:
            return None
    return None


def _v1_canonical_annotation(v1_format: Any) -> CanonicalProjectionReference | None:
    """Extract the v1 format's ``canonical`` annotation when present.

    Tolerates the same input shapes as :func:`_v1_format_id`. Returns
    ``None`` when the v1 format doesn't carry an explicit annotation
    (the caller falls through to the registry).
    """
    raw = (
        v1_format.get("canonical")
        if isinstance(v1_format, dict)
        else getattr(v1_format, "canonical", None)
    )
    if raw is None:
        return None
    if isinstance(raw, CanonicalProjectionReference):
        return raw
    if isinstance(raw, dict):
        try:
            return CanonicalProjectionReference.model_validate(raw)
        except ValidationError:
            return None
    return None


def _v1_asset_types(v1_format: Any) -> list[str]:
    """Collect the unique ``asset_type`` values from ``v1_format.assets[]``.

    Used by the structural-match step. Tolerates both individual assets
    (``asset_type`` on the slot) and repeatable groups (per-slot
    ``asset_type`` on each inner slot).
    """
    assets = (
        v1_format.get("assets")
        if isinstance(v1_format, dict)
        else getattr(v1_format, "assets", None)
    )
    if not isinstance(assets, list):
        return []
    out: list[str] = []
    for asset in assets:
        if isinstance(asset, dict):
            atype = asset.get("asset_type")
        else:
            atype = getattr(asset, "asset_type", None)
        if isinstance(atype, str) and atype not in out:
            out.append(atype)
    return out


def _v1_version_constraints(
    v1_format: Any,
    *,
    keys: tuple[str, ...],
) -> list[str]:
    """Collect VAST/DAAST version values from a v1 format.

    The version may live at the top level (a flat catalog entry) or
    inside per-asset requirements. Walks both. Returns an empty list
    when none are declared.
    """
    out: list[str] = []
    src = v1_format if isinstance(v1_format, dict) else None
    if src is None:
        return out
    for key in keys:
        v = src.get(key)
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out.extend(s for s in v if isinstance(s, str))
    # Walk per-asset requirements.
    assets = src.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            requirements = asset.get("requirements")
            if not isinstance(requirements, dict):
                continue
            for key in keys:
                v = requirements.get(key)
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, list):
                    out.extend(s for s in v if isinstance(s, str))
    return out


def _build_declaration(
    *,
    kind: CanonicalFormatKind,
    v1_format_id: FormatId,
    params: dict[str, Any] | None = None,
    canonical_ref: CanonicalProjectionReference | None = None,
) -> ProductFormatDeclaration:
    """Assemble the v2 declaration from resolved kind + source v1 ref.

    Threads ``asset_source`` and ``slots_override`` from the v1
    ``canonical`` annotation into ``params`` when present, so the
    seller's projection hints propagate cleanly.
    """
    body: dict[str, Any] = dict(params or {})
    if canonical_ref is not None:
        if canonical_ref.asset_source is not None and "asset_source" not in body:
            body["asset_source"] = canonical_ref.asset_source.value
        if canonical_ref.slots_override is not None and "slots" not in body:
            body["slots"] = [
                slot.model_dump(exclude_none=True) for slot in canonical_ref.slots_override
            ]
    return ProductFormatDeclaration(
        format_kind=kind,
        params=body,
        v1_format_ref=[v1_format_id],
    )


def project_v1_format_to_declaration(
    v1_format: Any,
    *,
    field_path: str = "formats[]",
) -> V1ToV2Projection:
    """Project a single v1 named format to a v2 ``ProductFormatDeclaration``.

    Walks the resolution order documented at module level. Tolerates
    both raw dicts (the common case when reading a v1 catalog from
    JSON) and Pydantic-validated v1 ``Format`` instances.

    Args:
        v1_format: The v1 format declaration to project. Dict or
            duck-typed object with ``format_id``, ``canonical``,
            ``assets`` accessors.
        field_path: JSONPath-lite pointer for emitted advisories
            (e.g., ``"formats[2]"``).

    Returns:
        :class:`V1ToV2Projection` carrying the declaration (when
        projection succeeded) and any advisories the resolution order
        emitted.
    """
    fid = _v1_format_id(v1_format)
    if fid is None:
        return V1ToV2Projection(
            advisories=[
                make_sdk_advisory(
                    code="FORMAT_PROJECTION_FAILED",
                    message="v1 format declaration carries no parseable format_id.",
                    field=field_path,
                    details={"resolution_failure": "missing_format_id"},
                )
            ]
        )

    registry = load_default_registry()

    # Look up the registry's matching glob mapping (if any) so partial
    # seller annotations can still pick up registry-published default
    # ``parameters`` without forcing the seller to restate them on the v1
    # file. Step 1 (seller annotation) overrides ``kind`` /
    # ``slots_override`` / ``asset_source`` but does NOT clobber the
    # registry's parametric defaults — that would lose every parameter
    # a registry glob carries (e.g., ``vast_version``, dimensions) when
    # a seller annotates only ``{kind: video_vast}``.
    registry_params: dict[str, Any] = {}
    registry_kind: CanonicalFormatKind | None = None
    for mapping in registry.mappings:
        pattern = mapping.v1_pattern
        glob = getattr(pattern, "format_id_glob", None)
        if isinstance(glob, str) and glob_match(fid.id, glob):
            registry_params = dict(mapping.v2.parameters or {})
            registry_kind = mapping.v2.canonical
            break

    # --- Step 1 (registry resolution-order step 2): seller-asserted
    # ``canonical`` annotation on the v1 file. Annotation wins on
    # ``kind`` + ``asset_source`` + ``slots_override``; registry
    # parameters fill in anything the seller didn't restate.
    annotation = _v1_canonical_annotation(v1_format)
    if annotation is not None:
        return V1ToV2Projection(
            declaration=_build_declaration(
                kind=annotation.kind,
                v1_format_id=fid,
                params=registry_params,
                canonical_ref=annotation,
            )
        )

    # --- Step 2 (registry resolution-order step 3): registry glob hit
    # without a seller annotation — emit the registry's pairing.
    if registry_kind is not None:
        return V1ToV2Projection(
            declaration=_build_declaration(
                kind=registry_kind,
                v1_format_id=fid,
                params=registry_params,
            )
        )

    # No literal-glob hit — try structural fallback.
    asset_types = _v1_asset_types(v1_format)
    vast_versions = _v1_version_constraints(v1_format, keys=("vast_version", "vast_versions"))
    daast_versions = _v1_version_constraints(v1_format, keys=("daast_version", "daast_versions"))

    structural_hits: list[Any] = []
    for mapping in registry.mappings:
        pattern = mapping.v1_pattern
        # The discriminated union distinguishes structural (``V1Pattern1``) from
        # glob (``V1Pattern``); only the structural branch is consultable here.
        structural = getattr(pattern, "structural", None)
        if structural is None:
            continue
        if structural_match(
            asset_types=asset_types,
            vast_versions=vast_versions or None,
            daast_versions=daast_versions or None,
            pattern=structural,
        ):
            structural_hits.append(mapping)

    if structural_hits:
        # Step 4: family-level match — emit AMBIGUOUS advisory but still
        # produce a usable declaration with the matched canonical so
        # consumers have a typed shape to work against.
        first = structural_hits[0]
        declaration = _build_declaration(
            kind=first.v2.canonical,
            v1_format_id=fid,
            params=dict(first.v2.parameters or {}),
        )
        advisory = make_sdk_advisory(
            code="FORMAT_DECLARATION_V1_AMBIGUOUS",
            message=(
                f"v1 format {fid.id!r} structurally matched the "
                f"{first.v2.canonical.value!r} family but the registry "
                f"entry is pure-structural — the projection is a "
                f"family-level guess. Seller SHOULD add an explicit "
                f"``canonical`` annotation on the v1 format."
            ),
            field=field_path,
            details={
                "v1_format_id": _echo_identifier(fid.id),
                "matched_canonical": first.v2.canonical.value,
                "match_kind": "structural_family",
                "candidate_count": len(structural_hits),
            },
            suggestion=(
                "Add a ``canonical: { kind: ..., asset_source?: ..., "
                "slots_override?: [...] }`` annotation on the v1 format "
                "file so the projection is seller-declared rather than "
                "family-inferred."
            ),
        )
        return V1ToV2Projection(declaration=declaration, advisories=[advisory])

    # --- Step 5: fail closed ---
    return V1ToV2Projection(
        advisories=[
            make_sdk_advisory(
                code="FORMAT_PROJECTION_FAILED",
                message=(
                    f"v1 format {fid.id!r} has no ``canonical`` annotation and "
                    f"no registry match — SDK cannot project it onto a v2 "
                    f"canonical."
                ),
                field=field_path,
                details={
                    "v1_format_id": _echo_identifier(fid.id),
                    "resolution_failure": "no_registry_match",
                    "asset_types": asset_types,
                },
                suggestion=(
                    "Add a ``canonical`` annotation to the v1 format file, "
                    "or file a registry PR adding a structural pattern "
                    "covering this format's shape."
                ),
            )
        ]
    )


@dataclass
class V1CatalogProjection:
    """Aggregate result of projecting a list of v1 formats to v2 declarations."""

    declarations: list[ProductFormatDeclaration] = field(default_factory=list)
    advisories: list[Error] = field(default_factory=list)


def project_v1_catalog_to_v2(
    v1_formats: list[Any],
    *,
    field_path_prefix: str = "formats",
) -> V1CatalogProjection:
    """Project a list of v1 named formats (a catalog) to v2 declarations.

    Aggregates per-format projection results — failed-closed entries
    contribute their advisory but no declaration. Useful for migrating
    an entire v1 ``reference-formats.json``-style catalog in one call.
    """
    out = V1CatalogProjection()
    for i, v1_format in enumerate(v1_formats):
        result = project_v1_format_to_declaration(
            v1_format,
            field_path=f"{field_path_prefix}[{i}]",
        )
        if result.declaration is not None:
            out.declarations.append(result.declaration)
        out.advisories.extend(result.advisories)
    return out


def group_declarations_by_product(
    declarations: list[ProductFormatDeclaration],
    mapping: dict[str, list[str]],
) -> dict[str, list[ProductFormatDeclaration]]:
    """Group projected v2 declarations into products by ``v1_format_ref`` id.

    A buyer-side adopter porting a v1 catalog onto v2 frequently has a
    pre-existing mapping of which v1 format ids belong to which product
    (e.g., from the seller's published product catalog or an internal
    routing table). After running
    :func:`project_v1_catalog_to_v2` over the flat v1 format list, this
    helper buckets the resulting declarations into per-product
    ``format_options[]`` lists.

    Args:
        declarations: Output of :func:`project_v1_catalog_to_v2` —
            every entry MUST carry a non-empty ``v1_format_ref[]``.
            Declarations whose ``v1_format_ref[0].id`` doesn't appear
            in ``mapping`` are silently skipped; pass-through is the
            adopter's choice.
        mapping: ``{product_id: [v1_format_id, ...]}`` describing
            which v1 format ids belong to which product.

    Returns:
        ``{product_id: [ProductFormatDeclaration, ...]}`` ready to
        drop into ``Product.format_options[]`` for each product.
        Order within each product preserves the input declaration
        order. Products with no matching declarations are omitted.

    Example::

        catalog = project_v1_catalog_to_v2(v1_formats)
        per_product = group_declarations_by_product(
            catalog.declarations,
            mapping={
                "homepage_mrec": ["display_300x250_image"],
                "homepage_billboard": ["display_970x250_image"],
            },
        )
        product = Product(
            product_id="homepage_mrec",
            format_options=per_product["homepage_mrec"],
            ...
        )
    """
    # Build a reverse index ``v1_id -> product_id`` once so the per-
    # declaration lookup is O(1). Earlier-occurring product_ids win on
    # collision — adopters with overlapping mappings get deterministic
    # behaviour matching the dict iteration order they passed in.
    reverse: dict[str, str] = {}
    for product_id, v1_ids in mapping.items():
        for v1_id in v1_ids:
            reverse.setdefault(v1_id, product_id)

    out: dict[str, list[ProductFormatDeclaration]] = {}
    for declaration in declarations:
        refs = declaration.v1_format_ref or []
        if not refs:
            continue
        # A declaration may carry multiple v1 refs (multi-size fan-out
        # of a single canonical onto several v1 sizes). The first ref
        # determines product membership; this matches the half-1
        # ``find_declaration_by_v1_format_id`` lookup semantics.
        matched_product = reverse.get(refs[0].id)
        if matched_product is None:
            continue
        out.setdefault(matched_product, []).append(declaration)
    return out


__all__ = [
    "V1CatalogProjection",
    "V1ToV2Projection",
    "group_declarations_by_product",
    "project_v1_catalog_to_v2",
    "project_v1_format_to_declaration",
]
