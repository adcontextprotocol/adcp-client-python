"""Types the AdCP ``manifest`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.manifest import <Type>

This domain has one schema, so every type it declares is here.

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:33:59 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.manifest import Model

# Explicit exports
__all__ = ["Model"]
