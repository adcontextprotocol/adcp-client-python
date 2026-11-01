"""Types the AdCP ``sponsored_intelligence`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.sponsored_intelligence import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.sponsored_intelligence.<schema>``.
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
    from adcp.types.domains.sponsored_intelligence.si_capabilities import (
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
    from adcp.types.domains.sponsored_intelligence.si_context_use import SiContextUse
    from adcp.types.domains.sponsored_intelligence.si_get_offering_request import (
        SiGetOfferingRequest,
    )
    from adcp.types.domains.sponsored_intelligence.si_get_offering_response import (
        MatchingProduct,
        Offering,
        SiGetOfferingResponse,
    )
    from adcp.types.domains.sponsored_intelligence.si_identity import (
        ConsentScopeEnum,
        PrivacyPolicyAcknowledged,
        ShippingAddress,
        SiIdentity,
        User,
    )
    from adcp.types.domains.sponsored_intelligence.si_initiate_session_request import (
        SiInitiateSessionRequest,
    )
    from adcp.types.domains.sponsored_intelligence.si_initiate_session_response import (
        SiInitiateSessionResponse,
    )
    from adcp.types.domains.sponsored_intelligence.si_send_message_request import (
        ActionResponse,
        SiSendMessageRequest,
    )
    from adcp.types.domains.sponsored_intelligence.si_send_message_response import (
        ContextForCheckout,
        Handoff,
        Intent,
        Price,
        SiSendMessageResponse,
    )
    from adcp.types.domains.sponsored_intelligence.si_sponsored_context import (
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
    from adcp.types.domains.sponsored_intelligence.si_sponsored_context_receipt import (
        DisclosureCommitment,
        HostReceipt,
        SiSponsoredContextReceipt,
        Status,
        Status46,
    )
    from adcp.types.domains.sponsored_intelligence.si_terminate_session_request import (
        Reason,
        SiTerminateSessionRequest,
        TerminationContext,
        TransactionIntent,
    )
    from adcp.types.domains.sponsored_intelligence.si_terminate_session_response import (
        AcpHandoff,
        FollowUp,
        SiTerminateSessionResponse,
    )
    from adcp.types.domains.sponsored_intelligence.si_ui_element import SiUiElement

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


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "A2ui": "adcp.types.domains.sponsored_intelligence.si_capabilities",
    "Account": "adcp.types.domains.sponsored_intelligence.si_sponsored_context",
    "AcpHandoff": "adcp.types.domains.sponsored_intelligence.si_terminate_session_response",
    "ActionResponse": "adcp.types.domains.sponsored_intelligence.si_send_message_request",
    "Avatar": "adcp.types.domains.sponsored_intelligence.si_capabilities",
    "Commerce": "adcp.types.domains.sponsored_intelligence.si_capabilities",
    "Components": "adcp.types.domains.sponsored_intelligence.si_capabilities",
    "ConsentScopeEnum": "adcp.types.domains.sponsored_intelligence.si_identity",
    "ContextForCheckout": "adcp.types.domains.sponsored_intelligence.si_send_message_response",
    "DeclaredBy": "adcp.types.domains.sponsored_intelligence.si_sponsored_context",
    "DisclosureCommitment": "adcp.types.domains.sponsored_intelligence.si_sponsored_context_receipt",
    "DisclosureObligation": "adcp.types.domains.sponsored_intelligence.si_sponsored_context",
    "FollowUp": "adcp.types.domains.sponsored_intelligence.si_terminate_session_response",
    "Handoff": "adcp.types.domains.sponsored_intelligence.si_send_message_response",
    "HostReceipt": "adcp.types.domains.sponsored_intelligence.si_sponsored_context_receipt",
    "Intent": "adcp.types.domains.sponsored_intelligence.si_send_message_response",
    "Jurisdiction": "adcp.types.domains.sponsored_intelligence.si_sponsored_context",
    "MatchingProduct": "adcp.types.domains.sponsored_intelligence.si_get_offering_response",
    "Modalities": "adcp.types.domains.sponsored_intelligence.si_capabilities",
    "Offering": "adcp.types.domains.sponsored_intelligence.si_get_offering_response",
    "PayingPrincipal": "adcp.types.domains.sponsored_intelligence.si_sponsored_context",
    "Price": "adcp.types.domains.sponsored_intelligence.si_send_message_response",
    "PrivacyPolicyAcknowledged": "adcp.types.domains.sponsored_intelligence.si_identity",
    "Proximity": "adcp.types.domains.sponsored_intelligence.si_sponsored_context",
    "Reason": "adcp.types.domains.sponsored_intelligence.si_terminate_session_request",
    "Role": "adcp.types.domains.sponsored_intelligence.si_sponsored_context",
    "ShippingAddress": "adcp.types.domains.sponsored_intelligence.si_identity",
    "SiCapabilities": "adcp.types.domains.sponsored_intelligence.si_capabilities",
    "SiContextUse": "adcp.types.domains.sponsored_intelligence.si_context_use",
    "SiGetOfferingRequest": "adcp.types.domains.sponsored_intelligence.si_get_offering_request",
    "SiGetOfferingResponse": "adcp.types.domains.sponsored_intelligence.si_get_offering_response",
    "SiIdentity": "adcp.types.domains.sponsored_intelligence.si_identity",
    "SiInitiateSessionRequest": "adcp.types.domains.sponsored_intelligence.si_initiate_session_request",
    "SiInitiateSessionResponse": "adcp.types.domains.sponsored_intelligence.si_initiate_session_response",
    "SiSendMessageRequest": "adcp.types.domains.sponsored_intelligence.si_send_message_request",
    "SiSendMessageResponse": "adcp.types.domains.sponsored_intelligence.si_send_message_response",
    "SiSponsoredContext": "adcp.types.domains.sponsored_intelligence.si_sponsored_context",
    "SiSponsoredContextReceipt": "adcp.types.domains.sponsored_intelligence.si_sponsored_context_receipt",
    "SiTerminateSessionRequest": "adcp.types.domains.sponsored_intelligence.si_terminate_session_request",
    "SiTerminateSessionResponse": "adcp.types.domains.sponsored_intelligence.si_terminate_session_response",
    "SiUiElement": "adcp.types.domains.sponsored_intelligence.si_ui_element",
    "StandardEnum": "adcp.types.domains.sponsored_intelligence.si_capabilities",
    "Status": "adcp.types.domains.sponsored_intelligence.si_sponsored_context_receipt",
    "Status46": "adcp.types.domains.sponsored_intelligence.si_sponsored_context_receipt",
    "TerminationContext": "adcp.types.domains.sponsored_intelligence.si_terminate_session_request",
    "Timing": "adcp.types.domains.sponsored_intelligence.si_sponsored_context",
    "TransactionIntent": "adcp.types.domains.sponsored_intelligence.si_terminate_session_request",
    "User": "adcp.types.domains.sponsored_intelligence.si_identity",
    "Video": "adcp.types.domains.sponsored_intelligence.si_capabilities",
    "Voice": "adcp.types.domains.sponsored_intelligence.si_capabilities",
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
