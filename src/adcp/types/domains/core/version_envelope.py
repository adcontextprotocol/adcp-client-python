"""Types declared by the AdCP ``core/version_envelope`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.core.version_envelope import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-04 01:19:07 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.version_envelope import AdcpVersionEnvelope

# Explicit exports
__all__ = ["AdcpVersionEnvelope"]
