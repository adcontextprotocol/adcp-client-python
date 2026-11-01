"""Types the AdCP ``trusted_match`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.trusted_match import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.trusted_match.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:49:37 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.types.domains.trusted_match.available_package import AvailablePackage
    from adcp.types.domains.trusted_match.context_match_request import (
        ArtifactRef,
        ContextMatchRequest,
        ContextSignals,
        Geo,
        Keyword,
        Metro,
        Sentiment,
        Type,
    )
    from adcp.types.domains.trusted_match.context_match_response import (
        ContextMatchResponseRouterPublisher,
        SignalsByProvider,
    )
    from adcp.types.domains.trusted_match.error import Code, TmpError
    from adcp.types.domains.trusted_match.identity_match_request import (
        Attestation,
        Consent,
        Identity,
        IdentityMatchRequest,
        SealedCredential,
        VerificationLevel,
    )
    from adcp.types.domains.trusted_match.identity_match_response import (
        IdentityMatchResponse,
        IdentityMatchResponseRouterPublisher,
        TmpxProviders,
    )
    from adcp.types.domains.trusted_match.offer import Offer
    from adcp.types.domains.trusted_match.offer_price import Model, OfferPrice
    from adcp.types.domains.trusted_match.provider_context_match_response import (
        ContextMatchResponseProviderRouter,
    )
    from adcp.types.domains.trusted_match.provider_identity_match_response import (
        IdentityMatchResponseProviderRouter,
    )
    from adcp.types.domains.trusted_match.provider_registration import (
        Country,
        Status,
        TmpProviderRegistration,
        TmpProviderRegistration1,
        TmpProviderRegistration2,
        TmpxSlot,
    )
    from adcp.types.domains.trusted_match.publisher_targeting_kv_config import (
        PublisherTargetingKvMapping,
    )
    from adcp.types.domains.trusted_match.publisher_tmpx_config import PublisherTmpxMacroMapping
    from adcp.types.domains.trusted_match.tmpx_chunk import TmpxChunk

# Explicit exports
__all__ = [
    "ArtifactRef",
    "Attestation",
    "AvailablePackage",
    "Code",
    "Consent",
    "ContextMatchRequest",
    "ContextMatchResponseProviderRouter",
    "ContextMatchResponseRouterPublisher",
    "ContextSignals",
    "Country",
    "Geo",
    "Identity",
    "IdentityMatchRequest",
    "IdentityMatchResponse",
    "IdentityMatchResponseProviderRouter",
    "IdentityMatchResponseRouterPublisher",
    "Keyword",
    "Metro",
    "Model",
    "Offer",
    "OfferPrice",
    "PublisherTargetingKvMapping",
    "PublisherTmpxMacroMapping",
    "SealedCredential",
    "Sentiment",
    "SignalsByProvider",
    "Status",
    "TmpError",
    "TmpProviderRegistration",
    "TmpProviderRegistration1",
    "TmpProviderRegistration2",
    "TmpxChunk",
    "TmpxProviders",
    "TmpxSlot",
    "Type",
    "VerificationLevel",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "ArtifactRef": "adcp.types.domains.trusted_match.context_match_request",
    "Attestation": "adcp.types.domains.trusted_match.identity_match_request",
    "AvailablePackage": "adcp.types.domains.trusted_match.available_package",
    "Code": "adcp.types.domains.trusted_match.error",
    "Consent": "adcp.types.domains.trusted_match.identity_match_request",
    "ContextMatchRequest": "adcp.types.domains.trusted_match.context_match_request",
    "ContextMatchResponseProviderRouter": "adcp.types.domains.trusted_match.provider_context_match_response",
    "ContextMatchResponseRouterPublisher": "adcp.types.domains.trusted_match.context_match_response",
    "ContextSignals": "adcp.types.domains.trusted_match.context_match_request",
    "Country": "adcp.types.domains.trusted_match.provider_registration",
    "Geo": "adcp.types.domains.trusted_match.context_match_request",
    "Identity": "adcp.types.domains.trusted_match.identity_match_request",
    "IdentityMatchRequest": "adcp.types.domains.trusted_match.identity_match_request",
    "IdentityMatchResponse": "adcp.types.domains.trusted_match.identity_match_response",
    "IdentityMatchResponseProviderRouter": "adcp.types.domains.trusted_match.provider_identity_match_response",
    "IdentityMatchResponseRouterPublisher": "adcp.types.domains.trusted_match.identity_match_response",
    "Keyword": "adcp.types.domains.trusted_match.context_match_request",
    "Metro": "adcp.types.domains.trusted_match.context_match_request",
    "Model": "adcp.types.domains.trusted_match.offer_price",
    "Offer": "adcp.types.domains.trusted_match.offer",
    "OfferPrice": "adcp.types.domains.trusted_match.offer_price",
    "PublisherTargetingKvMapping": "adcp.types.domains.trusted_match.publisher_targeting_kv_config",
    "PublisherTmpxMacroMapping": "adcp.types.domains.trusted_match.publisher_tmpx_config",
    "SealedCredential": "adcp.types.domains.trusted_match.identity_match_request",
    "Sentiment": "adcp.types.domains.trusted_match.context_match_request",
    "SignalsByProvider": "adcp.types.domains.trusted_match.context_match_response",
    "Status": "adcp.types.domains.trusted_match.provider_registration",
    "TmpError": "adcp.types.domains.trusted_match.error",
    "TmpProviderRegistration": "adcp.types.domains.trusted_match.provider_registration",
    "TmpProviderRegistration1": "adcp.types.domains.trusted_match.provider_registration",
    "TmpProviderRegistration2": "adcp.types.domains.trusted_match.provider_registration",
    "TmpxChunk": "adcp.types.domains.trusted_match.tmpx_chunk",
    "TmpxProviders": "adcp.types.domains.trusted_match.identity_match_response",
    "TmpxSlot": "adcp.types.domains.trusted_match.provider_registration",
    "Type": "adcp.types.domains.trusted_match.context_match_request",
    "VerificationLevel": "adcp.types.domains.trusted_match.identity_match_request",
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
