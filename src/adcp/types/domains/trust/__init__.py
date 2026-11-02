"""Types the AdCP ``trust`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.trust import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.trust.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:44:03 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.types.domains.trust.v1.trust import (
        Acknowledgement,
        Adcp,
        Adcp2,
        Adcp4,
        Adcp5,
        Agent,
        Brand,
        Country,
        DelegationType,
        Domain,
        Grant,
        GrantSource,
        GrantSource1,
        GrantedAgent,
        HttpsUrl,
        NamespacedToken,
        OpenProfiles,
        Profiles,
        Profiles1,
        Profiles2,
        Protocol,
        RecordProfiles,
        Role,
        Role11,
        Scope,
        Scope24,
        TrustJson,
        TrustJson1,
        TrustJson2,
    )
    from adcp.types.domains.trust.v1.trust_acknowledgements import (
        TrustJsonAcknowledgementsSubDocument,
    )

# Explicit exports
__all__ = [
    "Acknowledgement",
    "Adcp",
    "Adcp2",
    "Adcp4",
    "Adcp5",
    "Agent",
    "Brand",
    "Country",
    "DelegationType",
    "Domain",
    "Grant",
    "GrantSource",
    "GrantSource1",
    "GrantedAgent",
    "HttpsUrl",
    "NamespacedToken",
    "OpenProfiles",
    "Profiles",
    "Profiles1",
    "Profiles2",
    "Protocol",
    "RecordProfiles",
    "Role",
    "Role11",
    "Scope",
    "Scope24",
    "TrustJson",
    "TrustJson1",
    "TrustJson2",
    "TrustJsonAcknowledgementsSubDocument",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "Acknowledgement": "adcp.types.domains.trust.v1.trust",
    "Adcp": "adcp.types.domains.trust.v1.trust",
    "Adcp2": "adcp.types.domains.trust.v1.trust",
    "Adcp4": "adcp.types.domains.trust.v1.trust",
    "Adcp5": "adcp.types.domains.trust.v1.trust",
    "Agent": "adcp.types.domains.trust.v1.trust",
    "Brand": "adcp.types.domains.trust.v1.trust",
    "Country": "adcp.types.domains.trust.v1.trust",
    "DelegationType": "adcp.types.domains.trust.v1.trust",
    "Domain": "adcp.types.domains.trust.v1.trust",
    "Grant": "adcp.types.domains.trust.v1.trust",
    "GrantSource": "adcp.types.domains.trust.v1.trust",
    "GrantSource1": "adcp.types.domains.trust.v1.trust",
    "GrantedAgent": "adcp.types.domains.trust.v1.trust",
    "HttpsUrl": "adcp.types.domains.trust.v1.trust",
    "NamespacedToken": "adcp.types.domains.trust.v1.trust",
    "OpenProfiles": "adcp.types.domains.trust.v1.trust",
    "Profiles": "adcp.types.domains.trust.v1.trust",
    "Profiles1": "adcp.types.domains.trust.v1.trust",
    "Profiles2": "adcp.types.domains.trust.v1.trust",
    "Protocol": "adcp.types.domains.trust.v1.trust",
    "RecordProfiles": "adcp.types.domains.trust.v1.trust",
    "Role": "adcp.types.domains.trust.v1.trust",
    "Role11": "adcp.types.domains.trust.v1.trust",
    "Scope": "adcp.types.domains.trust.v1.trust",
    "Scope24": "adcp.types.domains.trust.v1.trust",
    "TrustJson": "adcp.types.domains.trust.v1.trust",
    "TrustJson1": "adcp.types.domains.trust.v1.trust",
    "TrustJson2": "adcp.types.domains.trust.v1.trust",
    "TrustJsonAcknowledgementsSubDocument": "adcp.types.domains.trust.v1.trust_acknowledgements",
}

if not TYPE_CHECKING:
    import importlib as _importlib

    def __getattr__(name: str) -> object:
        module_name = _DECLARED_IN.get(name)
        if module_name is None:
            # Not a declared name: let the import system try a submodule, or fail.
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(_importlib.import_module(module_name), name)
        globals()[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted(set(globals()) | set(__all__))
