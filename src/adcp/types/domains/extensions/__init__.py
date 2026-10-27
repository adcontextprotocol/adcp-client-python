"""Types the AdCP ``extensions`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.extensions import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.extensions.<schema>``.
Nothing here is renamed.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-26 23:56:31 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.domains.extensions.adcp import AdcpOwnedExtensions
from adcp.types.domains.extensions.extension_meta import AdcpExtensionFileSchema

# Explicit exports
__all__ = ["AdcpExtensionFileSchema", "AdcpOwnedExtensions"]
