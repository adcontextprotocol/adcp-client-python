"""Types declared by the AdCP ``core/audience_source`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.core.audience_source import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.audience_source import (
    AudienceSource,
    AudienceSource1,
    AudienceSource2,
)

# Explicit exports
__all__ = ["AudienceSource", "AudienceSource1", "AudienceSource2"]
