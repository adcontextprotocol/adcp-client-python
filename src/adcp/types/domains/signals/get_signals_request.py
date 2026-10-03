"""Types declared by the AdCP ``signals/get_signals_request`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.signals.get_signals_request import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.signals.get_signals_request import (
    Country,
    DiscoveryMode,
    Field1,
    GetSignalsRequest,
)

# Explicit exports
__all__ = ["Country", "DiscoveryMode", "Field1", "GetSignalsRequest"]
