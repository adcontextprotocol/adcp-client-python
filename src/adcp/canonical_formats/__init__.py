"""Pythonic v1↔v2 canonical-formats projection layer.

AdCP 3.1 introduced canonical formats as the v2 catalog-side vocabulary
that replaces v1's per-publisher format proliferation. A seller publishes
``Product.format_options[]`` carrying ``ProductFormatDeclaration`` entries
(one per accepted canonical kind); buyer agents reason about creative
compatibility against the closed canonical set rather than the open v1
``format_ids`` namespace.

During the migration window both wire shapes coexist: 3.0-era buyers
read ``Product.format_ids[]`` (v1) and 3.1-aware buyers read
``Product.format_options[]`` (v2). This module supplies the projection
layer the SDK needs to bridge them without adopters hand-rolling
translation per integration.

Public surface
==============

* :func:`project_declaration_to_v1` — single ``ProductFormatDeclaration``
  → ``format_ids[]`` (with advisory ``errors[]`` emission on ambiguity
  / multi-size lossy fan-out).
* :func:`project_product_to_v1` — fan-out helper across a product's
  ``format_options[]``, accumulating refs and advisories.
* :func:`validate_format_kind_in_options` — closed-set guard: rejects a
  ``format_kind`` that isn't published in the seller's ``format_options[]``.
  Sellers call this before accepting a ``create_media_buy``.
* :func:`find_declaration_by_kind` — looks up the matching declaration
  (with optional ``capability_id`` disambiguation).
* :func:`upgrade_legacy_format_id` — upgrades common legacy named
  format IDs such as ``display_300x250`` to parameterized canonical
  ``FormatId`` values.
* :func:`formats_are_equivalent` — family/equivalence comparison after
  legacy upgrade.
* :func:`format_is_supported` — stricter product/capability gating comparison
  after legacy upgrade.
* :class:`CanonicalReferenceResolver` — hardened fetch/cache helper for
  immutable ``format_schema`` and ``platform_extensions`` references.
* :func:`load_default_registry` — loads the AAO-published v1↔v2 mapping
  registry from the bundled schema cache.
* :class:`SdkAdvisory` — typed wrapper around the SDK-source ``Error``
  entries the projection emits on ``errors[]``.

Resolution-order semantics for v2 → v1 follow ``registries/v1-canonical-mapping.json``:

1. ``canonical_formats_only=True`` or ``format_kind=custom`` → no v1 emit, no advisory.
2. ``v1_format_ref[]`` set → emit those refs; if ``params.sizes[]`` count exceeds
   ``v1_format_ref[]`` count, emit ``FORMAT_DECLARATION_V1_LOSSY_MULTI_SIZE``.
3. Canonical's ``v1_translatable=False`` (``agent_placement``, ``sponsored_placement``,
   ``responsive_creative``, ``image_carousel``) → no v1 emit, no advisory — the
   canonical is structurally v1-unreachable by design.
4. Canonical's ``v1_translatable=True`` but no ``v1_format_ref[]`` → emit
   ``FORMAT_DECLARATION_V1_AMBIGUOUS``. SDKs MUST NOT synthesize a v1 ``format_id``
   from registry structural matches; the registry is authoritative for v1→v2
   projection only.
"""

from __future__ import annotations

from adcp.canonical_formats.advisory import SDK_ID, SdkAdvisory, make_sdk_advisory
from adcp.canonical_formats.compat_helpers import (
    CANONICAL_CREATIVE_AGENT_URL,
    format_is_supported,
    formats_are_equivalent,
    upgrade_legacy_format_id,
)
from adcp.canonical_formats.dialect import (
    CreativeDialect,
    CreativeDialectError,
    canonical_creatives_capability,
    resolve_creative_dialect,
)
from adcp.canonical_formats.format_options import (
    FormatKindNotInClosedSetError,
    find_declaration_by_kind,
    find_declaration_by_v1_format_id,
    validate_format_kind_in_options,
)
from adcp.canonical_formats.narrowing import (
    Divergence,
    DivergenceKind,
    check_narrows,
    narrowing_advisory,
)
from adcp.canonical_formats.pixel_tracker import (
    PixelTrackerBatchResult,
    PixelTrackerDowngrade,
    PixelTrackerUpgrade,
    V1UrlTracker,
    downgrade_pixel_tracker,
    downgrade_pixel_trackers,
    upgrade_v1_tracker,
    upgrade_v1_trackers,
)
from adcp.canonical_formats.projection import (
    AAO_CANONICAL_AGENT_URL,
    CanonicalFormatLegacyResolutionContext,
    CanonicalFormatLegacyResolutionError,
    CanonicalFormatLegacyResolver,
    CanonicalProductProjection,
    CatalogIndex,
    LegacyCreativeProjectionError,
    LegacyFormatConversionContext,
    LegacyFormatConverter,
    ProjectedFormat,
    ProjectionCatalogAdapters,
    ProjectionDiagnostic,
    build_catalog_index,
    canonical_format_legacy_resolver_from_catalog_snapshots,
    legacy_format_converter_from_catalog_snapshots,
    load_rc3_catalog_index,
    migrated_format_option_id,
    normalize_legacy_creative_request,
    project_canonical_response_to_legacy,
    project_legacy_format_id,
    project_legacy_product,
    projection_adapters_from_catalog_snapshots,
    resolve_legacy_format_refs,
)
from adcp.canonical_formats.references import (
    DEFAULT_REFERENCE_BODY_LIMIT_BYTES,
    DEFAULT_REFERENCE_TIMEOUT_SECONDS,
    CanonicalReference,
    CanonicalReferenceResolver,
    CanonicalReferenceResult,
    CanonicalReferenceStatus,
    parse_canonical_reference,
)
from adcp.canonical_formats.registry import (
    RegistryLoadError,
    glob_match,
    load_default_registry,
    structural_match,
)
from adcp.canonical_formats.v1_to_v2 import (
    V1CatalogProjection,
    V1ToV2Projection,
    group_declarations_by_product,
    project_v1_catalog_to_v2,
    project_v1_format_to_declaration,
)
from adcp.canonical_formats.v2_to_v1 import (
    V1_TRANSLATABLE,
    V2ToV1Projection,
    project_declaration_to_v1,
    project_product_to_v1,
)

__all__ = [
    "Divergence",
    "DivergenceKind",
    "AAO_CANONICAL_AGENT_URL",
    "CanonicalFormatLegacyResolutionContext",
    "CanonicalFormatLegacyResolutionError",
    "CanonicalFormatLegacyResolver",
    "CreativeDialect",
    "CreativeDialectError",
    "CanonicalProductProjection",
    "CatalogIndex",
    "FormatKindNotInClosedSetError",
    "LegacyFormatConversionContext",
    "LegacyFormatConverter",
    "LegacyCreativeProjectionError",
    "PixelTrackerBatchResult",
    "PixelTrackerDowngrade",
    "PixelTrackerUpgrade",
    "ProjectedFormat",
    "ProjectionCatalogAdapters",
    "ProjectionDiagnostic",
    "RegistryLoadError",
    "CANONICAL_CREATIVE_AGENT_URL",
    "SDK_ID",
    "SdkAdvisory",
    "V1CatalogProjection",
    "CanonicalReference",
    "CanonicalReferenceResolver",
    "CanonicalReferenceResult",
    "CanonicalReferenceStatus",
    "V1ToV2Projection",
    "V1UrlTracker",
    "V1_TRANSLATABLE",
    "V2ToV1Projection",
    "check_narrows",
    "build_catalog_index",
    "canonical_creatives_capability",
    "canonical_format_legacy_resolver_from_catalog_snapshots",
    "DEFAULT_REFERENCE_BODY_LIMIT_BYTES",
    "DEFAULT_REFERENCE_TIMEOUT_SECONDS",
    "downgrade_pixel_tracker",
    "downgrade_pixel_trackers",
    "format_is_supported",
    "find_declaration_by_kind",
    "find_declaration_by_v1_format_id",
    "formats_are_equivalent",
    "glob_match",
    "group_declarations_by_product",
    "load_default_registry",
    "load_rc3_catalog_index",
    "legacy_format_converter_from_catalog_snapshots",
    "make_sdk_advisory",
    "narrowing_advisory",
    "migrated_format_option_id",
    "normalize_legacy_creative_request",
    "parse_canonical_reference",
    "project_declaration_to_v1",
    "project_legacy_format_id",
    "project_legacy_product",
    "project_canonical_response_to_legacy",
    "projection_adapters_from_catalog_snapshots",
    "project_product_to_v1",
    "project_v1_catalog_to_v2",
    "project_v1_format_to_declaration",
    "structural_match",
    "resolve_legacy_format_refs",
    "resolve_creative_dialect",
    "upgrade_v1_tracker",
    "upgrade_v1_trackers",
    "upgrade_legacy_format_id",
    "validate_format_kind_in_options",
]
