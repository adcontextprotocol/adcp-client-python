"""Types the AdCP ``trusted_match`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.trusted_match import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.trusted_match.<schema>``.
Nothing here is renamed.

This facade is LAZY (PEP 562). It binds no name at import time, because an
eager one makes every leaf of this domain depend on every other leaf: a
submodule import runs this ``__init__``, so one leaf's dependencies become
the whole domain's. That turned a benign submodule-level cycle between
``core`` and ``formats.canonical`` -- two domains whose schemas reference
each other, which Python handles -- into a package-level one that made all
16 modules under ``formats/canonical/`` unimportable on their own.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-16 22:14:49 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The whole importable surface, so type checkers and IDEs see it. Runtime
    # resolution is ``__getattr__`` below; mypy never sees that, so a typo'd
    # import is still an error rather than ``object``.
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

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "trusted_match.available_package": ("AvailablePackage",),
    "trusted_match.context_match_request": (
        "ArtifactRef",
        "ContextMatchRequest",
        "ContextSignals",
        "Geo",
        "Keyword",
        "Metro",
        "Sentiment",
        "Type",
    ),
    "trusted_match.context_match_response": (
        "ContextMatchResponseRouterPublisher",
        "SignalsByProvider",
    ),
    "trusted_match.error": (
        "Code",
        "TmpError",
    ),
    "trusted_match.identity_match_request": (
        "Attestation",
        "Consent",
        "Identity",
        "IdentityMatchRequest",
        "SealedCredential",
        "VerificationLevel",
    ),
    "trusted_match.identity_match_response": (
        "IdentityMatchResponse",
        "IdentityMatchResponseRouterPublisher",
        "TmpxProviders",
    ),
    "trusted_match.offer": ("Offer",),
    "trusted_match.offer_price": (
        "Model",
        "OfferPrice",
    ),
    "trusted_match.provider_context_match_response": ("ContextMatchResponseProviderRouter",),
    "trusted_match.provider_identity_match_response": ("IdentityMatchResponseProviderRouter",),
    "trusted_match.provider_registration": (
        "Country",
        "Status",
        "TmpProviderRegistration",
        "TmpProviderRegistration1",
        "TmpProviderRegistration2",
        "TmpxSlot",
    ),
    "trusted_match.publisher_targeting_kv_config": ("PublisherTargetingKvMapping",),
    "trusted_match.publisher_tmpx_config": ("PublisherTmpxMacroMapping",),
    "trusted_match.tmpx_chunk": ("TmpxChunk",),
}

if not TYPE_CHECKING:
    import importlib

    _DECLARING_MODULE = {name: module for module, names in _DECLARED_BY.items() for name in names}

    def __getattr__(name: str) -> object:
        """Resolve a re-exported name by importing the ONE module declaring it."""
        module = _DECLARING_MODULE.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(f"adcp.types.domains.{module}"), name)
        globals()[name] = value  # cache: __getattr__ fires once per name
        return value

    def __dir__() -> list[str]:
        return sorted(set(__all__) | set(globals()))


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
