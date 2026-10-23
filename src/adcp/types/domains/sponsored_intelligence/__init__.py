"""Types the AdCP ``sponsored_intelligence`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.sponsored_intelligence import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.sponsored_intelligence.<schema>``.
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

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "sponsored_intelligence.si_capabilities": (
        "A2ui",
        "Avatar",
        "Commerce",
        "Components",
        "Modalities",
        "SiCapabilities",
        "StandardEnum",
        "Video",
        "Voice",
    ),
    "sponsored_intelligence.si_context_use": ("SiContextUse",),
    "sponsored_intelligence.si_get_offering_request": ("SiGetOfferingRequest",),
    "sponsored_intelligence.si_get_offering_response": (
        "MatchingProduct",
        "Offering",
        "SiGetOfferingResponse",
    ),
    "sponsored_intelligence.si_identity": (
        "ConsentScopeEnum",
        "PrivacyPolicyAcknowledged",
        "ShippingAddress",
        "SiIdentity",
        "User",
    ),
    "sponsored_intelligence.si_initiate_session_request": ("SiInitiateSessionRequest",),
    "sponsored_intelligence.si_initiate_session_response": ("SiInitiateSessionResponse",),
    "sponsored_intelligence.si_send_message_request": (
        "ActionResponse",
        "SiSendMessageRequest",
    ),
    "sponsored_intelligence.si_send_message_response": (
        "ContextForCheckout",
        "Handoff",
        "Intent",
        "Price",
        "SiSendMessageResponse",
    ),
    "sponsored_intelligence.si_sponsored_context": (
        "Account",
        "DeclaredBy",
        "DisclosureObligation",
        "Jurisdiction",
        "PayingPrincipal",
        "Proximity",
        "Role",
        "SiSponsoredContext",
        "Timing",
    ),
    "sponsored_intelligence.si_sponsored_context_receipt": (
        "DisclosureCommitment",
        "HostReceipt",
        "SiSponsoredContextReceipt",
        "Status",
        "Status46",
    ),
    "sponsored_intelligence.si_terminate_session_request": (
        "Reason",
        "SiTerminateSessionRequest",
        "TerminationContext",
        "TransactionIntent",
    ),
    "sponsored_intelligence.si_terminate_session_response": (
        "AcpHandoff",
        "FollowUp",
        "SiTerminateSessionResponse",
    ),
    "sponsored_intelligence.si_ui_element": ("SiUiElement",),
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
