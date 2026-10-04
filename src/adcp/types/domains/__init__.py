"""Public types grouped by the AdCP schema that declares them.

The AdCP bundle is organised by domain — ``core/``, ``creative/``,
``media_buy/`` and the rest — and by schema within each. Codegen mirrors that
layout, and these modules re-export it, so a type name more than one schema
declares is unambiguous by path rather than by a mangled name:

    from adcp.types.domains.creative import QuerySummary
    from adcp.types.domains.core.audience_evidence import Unit
    from adcp.types.domains.core.canvas_constraint import Unit

A domain root carries the names that domain declares exactly once. For the
rest, import from the schema's own module, as the last two lines do.

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:33:59 UTC
"""

from __future__ import annotations

#: Every domain in this package.
DOMAINS = (
    "a2ui",
    "aao",
    "account",
    "adagents",
    "brand",
    "brand_discovery",
    "collection",
    "compliance",
    "content_standards",
    "core",
    "creative",
    "enums",
    "error_details",
    "extensions",
    "formats",
    "governance",
    "manifest",
    "manifest_schema",
    "media_buy",
    "pricing_options",
    "property",
    "protocol",
    "registries",
    "signals",
    "sponsored_intelligence",
    "trusted_match",
)

__all__ = ["DOMAINS"]
