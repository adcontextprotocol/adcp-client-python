"""Types the AdCP ``trusted_match`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.trusted_match import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.trusted_match.<schema>``.
Nothing here is renamed.

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:33:59 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.trusted_match.available_package import AvailablePackage
from adcp.types.generated_poc.trusted_match.context_match_request import (
    ArtifactRef,
    ContextMatchRequest,
    ContextSignals,
    Geo,
    Keyword,
    Metro,
    Sentiment,
    Type,
)
from adcp.types.generated_poc.trusted_match.context_match_response import (
    ContextMatchResponseRouterPublisher,
    SignalsByProvider,
)
from adcp.types.generated_poc.trusted_match.error import Code, TmpError
from adcp.types.generated_poc.trusted_match.identity_match_request import (
    Attestation,
    Consent,
    Identity,
    IdentityMatchRequest,
    SealedCredential,
    VerificationLevel,
)
from adcp.types.generated_poc.trusted_match.identity_match_response import (
    IdentityMatchResponse,
    IdentityMatchResponseRouterPublisher,
    TmpxProviders,
)
from adcp.types.generated_poc.trusted_match.offer import Offer
from adcp.types.generated_poc.trusted_match.offer_price import Model, OfferPrice
from adcp.types.generated_poc.trusted_match.provider_context_match_response import (
    ContextMatchResponseProviderRouter,
)
from adcp.types.generated_poc.trusted_match.provider_identity_match_response import (
    IdentityMatchResponseProviderRouter,
)
from adcp.types.generated_poc.trusted_match.provider_registration import (
    Country,
    Status,
    TmpProviderRegistration,
    TmpProviderRegistration1,
    TmpProviderRegistration2,
    TmpxSlot,
)
from adcp.types.generated_poc.trusted_match.publisher_targeting_kv_config import (
    PublisherTargetingKvMapping,
)
from adcp.types.generated_poc.trusted_match.publisher_tmpx_config import PublisherTmpxMacroMapping
from adcp.types.generated_poc.trusted_match.tmpx_chunk import TmpxChunk

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
