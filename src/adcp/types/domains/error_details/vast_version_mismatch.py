"""Types declared by the AdCP ``error_details/vast_version_mismatch`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.error_details.vast_version_mismatch import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.error_details.vast_version_mismatch import (
    DocumentRole,
    MismatchReason,
    ObservedDocumentVastVersion,
    VastVersionMismatchDetails,
    VastVersionMismatchDetails1,
    VastVersionMismatchDetails2,
    VastVersionMismatchDetails3,
)

# Explicit exports
__all__ = [
    "DocumentRole",
    "MismatchReason",
    "ObservedDocumentVastVersion",
    "VastVersionMismatchDetails",
    "VastVersionMismatchDetails1",
    "VastVersionMismatchDetails2",
    "VastVersionMismatchDetails3",
]
