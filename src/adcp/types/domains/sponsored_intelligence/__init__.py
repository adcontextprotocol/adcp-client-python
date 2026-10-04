"""Types the AdCP ``sponsored_intelligence`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.sponsored_intelligence import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.sponsored_intelligence.<schema>``.
Nothing here is renamed.

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:33:59 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.sponsored_intelligence.si_capabilities import (
    A2ui,
    Avatar,
    Commerce,
    Components,
    Modalities,
    SiCapabilities,
    StandardEnum,
    Video,
    Voice,
)
from adcp.types.generated_poc.sponsored_intelligence.si_context_use import SiContextUse
from adcp.types.generated_poc.sponsored_intelligence.si_get_offering_request import (
    SiGetOfferingRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_get_offering_response import (
    MatchingProduct,
    Offering,
    SiGetOfferingResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_identity import (
    ConsentScopeEnum,
    PrivacyPolicyAcknowledged,
    ShippingAddress,
    SiIdentity,
    User,
)
from adcp.types.generated_poc.sponsored_intelligence.si_initiate_session_request import (
    SiInitiateSessionRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_initiate_session_response import (
    SiInitiateSessionResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_send_message_request import (
    ActionResponse,
    SiSendMessageRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_send_message_response import (
    ContextForCheckout,
    Handoff,
    Intent,
    Price,
    SiSendMessageResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_sponsored_context import (
    Account,
    DeclaredBy,
    DisclosureObligation,
    Jurisdiction,
    PayingPrincipal,
    Proximity,
    Role,
    SiSponsoredContext,
    Timing,
)
from adcp.types.generated_poc.sponsored_intelligence.si_sponsored_context_receipt import (
    DisclosureCommitment,
    HostReceipt,
    SiSponsoredContextReceipt,
    Status,
    Status46,
)
from adcp.types.generated_poc.sponsored_intelligence.si_terminate_session_request import (
    Reason,
    SiTerminateSessionRequest,
    TerminationContext,
    TransactionIntent,
)
from adcp.types.generated_poc.sponsored_intelligence.si_terminate_session_response import (
    AcpHandoff,
    FollowUp,
    SiTerminateSessionResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_ui_element import SiUiElement

# Explicit exports
__all__ = [
    "A2ui",
    "Account",
    "AcpHandoff",
    "ActionResponse",
    "Avatar",
    "Commerce",
    "Components",
    "ConsentScopeEnum",
    "ContextForCheckout",
    "DeclaredBy",
    "DisclosureCommitment",
    "DisclosureObligation",
    "FollowUp",
    "Handoff",
    "HostReceipt",
    "Intent",
    "Jurisdiction",
    "MatchingProduct",
    "Modalities",
    "Offering",
    "PayingPrincipal",
    "Price",
    "PrivacyPolicyAcknowledged",
    "Proximity",
    "Reason",
    "Role",
    "ShippingAddress",
    "SiCapabilities",
    "SiContextUse",
    "SiGetOfferingRequest",
    "SiGetOfferingResponse",
    "SiIdentity",
    "SiInitiateSessionRequest",
    "SiInitiateSessionResponse",
    "SiSendMessageRequest",
    "SiSendMessageResponse",
    "SiSponsoredContext",
    "SiSponsoredContextReceipt",
    "SiTerminateSessionRequest",
    "SiTerminateSessionResponse",
    "SiUiElement",
    "StandardEnum",
    "Status",
    "Status46",
    "TerminationContext",
    "Timing",
    "TransactionIntent",
    "User",
    "Video",
    "Voice",
]
