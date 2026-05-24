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
from adcp.canonical_formats.format_options import (
    FormatKindNotInClosedSetError,
    find_declaration_by_kind,
    validate_format_kind_in_options,
)
from adcp.canonical_formats.projection import (
    V1_TRANSLATABLE,
    V2ToV1Projection,
    project_declaration_to_v1,
    project_product_to_v1,
)
from adcp.canonical_formats.registry import (
    glob_match,
    load_default_registry,
    structural_match,
)

__all__ = [
    "FormatKindNotInClosedSetError",
    "SDK_ID",
    "SdkAdvisory",
    "V1_TRANSLATABLE",
    "V2ToV1Projection",
    "find_declaration_by_kind",
    "glob_match",
    "load_default_registry",
    "make_sdk_advisory",
    "project_declaration_to_v1",
    "project_product_to_v1",
    "structural_match",
    "validate_format_kind_in_options",
]
