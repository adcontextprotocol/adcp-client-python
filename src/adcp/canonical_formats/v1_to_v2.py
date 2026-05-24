"""v1 → v2 canonical-format projection.

Projects a v1 named-format declaration (``core/format.json`` shape)
into a v2 :class:`ProductFormatDeclaration`. Mirror image of
:mod:`adcp.canonical_formats.projection` (v2 → v1).

Resolution order per ``registries/v1-canonical-mapping.json``
"Resolution order (normative)" — items applied in order until a v2
canonical is identified:

1. **Seller-asserted on the v1 file.** ``v1_format.canonical`` is a
   :class:`CanonicalProjectionReference` carrying ``kind``,
   ``asset_source``, and ``slots_override[]``. Highest priority.
2. **Registry glob match.** Look up ``v1_format.format_id.id`` in the
   bundled registry's ``format_id_glob`` entries.
3. **Registry structural match.** Match ``v1_format.assets[*].asset_type``
   + VAST/DAAST versions + dimensions against the registry's
   ``structural`` entries. Yields a *family-level* identification only.
4. **Family-level structural match** (sub-case of 3) — emit
   ``FORMAT_DECLARATION_V1_AMBIGUOUS`` because the registry's
   structural patterns are all pure-structural family matches that
   can't be inverted back to a specific v1 format_id without seller
   assertion. The v2 declaration still gets a ``format_kind`` and
   ``params`` skeleton; the advisory notifies the consumer that the
   pairing is a family guess.
5. **Fail closed.** No match in steps 1-4 — emit
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
        except Exception:
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
        except Exception:
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

    # --- Step 1: seller-asserted ``canonical`` annotation ---
    annotation = _v1_canonical_annotation(v1_format)
    if annotation is not None:
        return V1ToV2Projection(
            declaration=_build_declaration(
                kind=annotation.kind,
                v1_format_id=fid,
                canonical_ref=annotation,
            )
        )

    # --- Step 2 + 3: registry lookup (glob, then structural) ---
    registry = load_default_registry()
    for mapping in registry.mappings:
        pattern = mapping.v1_pattern
        if hasattr(pattern, "format_id_glob"):
            if glob_match(fid.id, pattern.format_id_glob):
                return V1ToV2Projection(
                    declaration=_build_declaration(
                        kind=mapping.v2.canonical,
                        v1_format_id=fid,
                        params=dict(mapping.v2.parameters or {}),
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


__all__ = [
    "V1CatalogProjection",
    "V1ToV2Projection",
    "project_v1_catalog_to_v2",
    "project_v1_format_to_declaration",
]
