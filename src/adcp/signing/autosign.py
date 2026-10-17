"""Client-side orchestration for auto-signing outgoing AdCP requests.

`SigningConfig` bundles the private key and key-id a client uses to sign.
`operation_needs_signing` reads the seller's advertised ``request_signing``
capability block and classifies each outgoing operation as required,
optional, or skip — letting the caller decide whether to invoke
`sign_request` and what to do if no config is available.

The pure `sign_request` primitive lives in `adcp.signing.signer`; this
module is the thin glue between capabilities and that primitive.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import RootModel

from adcp.signing.constants import DEFAULT_TAG
from adcp.signing.crypto import ALG_ED25519, ALLOWED_ALGS, PrivateKey

if TYPE_CHECKING:
    from adcp.types.domains.protocol.get_adcp_capabilities_response import (
        RequestSigning,
    )


# The operation name the adapter is currently dispatching. The A2A / MCP
# adapters set this before invoking their respective SDKs so the shared
# httpx request event hook can look up the seller's signing policy for the
# right operation name. Unset (default None) during out-of-band HTTP calls
# like A2A agent-card fetches, which the hook then skips.
#
# Invariant: do NOT spawn background tasks that make unrelated httpx
# calls inside the scope where this var is set. ContextVar values copy
# into child tasks on ``asyncio.create_task``, so a background task
# spawned inside a signing scope would inherit the operation name and
# sign a request the caller didn't intend to sign.
current_operation: ContextVar[str | None] = ContextVar(
    "adcp_signing_current_operation", default=None
)


SigningDecision = Literal["required", "optional", "skip"]
"""Outcome of classifying an operation against a seller's signing policy.

* ``"required"`` — the seller has listed the operation in ``required_for``.
  The client MUST sign; if no ``SigningConfig`` is available the caller
  should raise rather than send an unsigned request the seller will reject.
* ``"optional"`` — the seller has listed the operation in ``warn_for`` or
  ``supported_for``. Sign if a ``SigningConfig`` is available; skip without
  error otherwise (per spec: ``warn_for`` logs failures but does not reject;
  ``supported_for`` accepts both signed and unsigned).
* ``"skip"`` — the operation is not in any list, or the seller does not
  advertise signing support at all. Do not sign.
"""

SigningProfileVersion = Literal["3.0", "3.1", "3.2"]


def signing_profile_for_adcp_version(adcp_version: str) -> SigningProfileVersion:
    """Map a trusted AdCP release pin to its request-signing profile.

    Prerelease and patch precision do not change the signing wire profile.
    Callers must derive this from trusted endpoint configuration, never an
    unbound request-body field.
    """
    release = adcp_version.split("+", 1)[0].split("-", 1)[0]
    parts = release.split(".")
    if len(parts) < 2:
        raise ValueError(f"invalid AdCP version for request signing: {adcp_version!r}")
    profile = f"{parts[0]}.{parts[1]}"
    if profile not in {"3.0", "3.1", "3.2"}:
        raise ValueError(f"AdCP {adcp_version!r} has no supported request-signing profile")
    return profile  # type: ignore[return-value]


def _rfc9421_deprecation_active() -> bool:
    from adcp.signing._profile_deprecation import rfc9421_deprecation_active

    return rfc9421_deprecation_active()


@dataclass(frozen=True)
class SigningConfig:
    """Client-side signing credentials for RFC 9421 request signing.

    Passed to ``ADCPClient`` / ``ADCPMultiAgentClient`` at construction.
    The same config signs traffic to every agent the client talks to —
    the buyer is one identity from the seller's point of view.

    Parameters
    ----------
    private_key:
        The signing key (``Ed25519PrivateKey`` or ``EllipticCurvePrivateKey``
        on P-256). Generated via the ``adcp-keygen`` CLI or loaded from an
        existing PEM via ``cryptography.hazmat.primitives.serialization``.
    key_id:
        The ``kid`` value the verifier will look up in the seller-side JWKS.
        Must match the ``kid`` of the public key published at the buyer's
        ``jwks_uri`` (advertised in the buyer's ``brand.json``).
    alg:
        RFC 9421 algorithm identifier. One of ``"ed25519"`` or
        ``"ecdsa-p256-sha256"``. Defaults to ``"ed25519"``.
    tag:
        Signature tag. Defaults to the AdCP request-signing tag and should
        not need to be overridden.
    signing_profile_version:
        Explicit signing wire profile. When omitted, ``ADCPClient`` derives
        it from the trusted effective wire pin (``server_version`` when set,
        otherwise ``adcp_version``). Set this only to override negotiation or
        when using the standalone event-hook helper, which has no client
        protocol pin to consult.
    """

    private_key: PrivateKey
    key_id: str
    alg: str = ALG_ED25519
    tag: str = DEFAULT_TAG
    signing_profile_version: SigningProfileVersion | None = None
    agent_origin: str | None = None

    def __post_init__(self) -> None:
        if self.alg not in ALLOWED_ALGS:
            raise ValueError(f"alg must be one of {sorted(ALLOWED_ALGS)}, got {self.alg!r}")
        if not self.key_id:
            raise ValueError("key_id must be a non-empty string")
        if self.signing_profile_version not in {None, "3.0", "3.1", "3.2"}:
            raise ValueError("signing_profile_version must be None, '3.0', '3.1', or '3.2'")
        if self.agent_origin is not None:
            from adcp.signing.wba import WBA_TAG, _https_origin, public_directory_key

            object.__setattr__(self, "agent_origin", _https_origin(self.agent_origin))
            if self.key_id != public_directory_key(self.private_key)["kid"]:
                raise ValueError("Web Bot Auth key_id must be the public key's thumbprint")
            object.__setattr__(self, "tag", WBA_TAG)
        elif _rfc9421_deprecation_active():
            warnings.warn(
                "RFC 9421-profile signing is deprecated in AdCP 3.3 and remains supported "
                "through 3.x; opt into Web Bot Auth with agent_origin when peers support it.",
                DeprecationWarning,
                stacklevel=2,
            )

    def __repr__(self) -> str:
        # Redact the private key from string representations so accidental
        # logging of the config (or of an exception that closes over it)
        # doesn't surface key material even in summary form.
        return (
            f"SigningConfig(key_id={self.key_id!r}, alg={self.alg!r}, "
            f"tag={self.tag!r}, signing_profile_version={self.signing_profile_version!r}, "
            f"agent_origin={self.agent_origin!r}, private_key=<redacted>)"
        )


@dataclass(frozen=True)
class _OperationLists:
    """Normalized operation lists extracted from a RequestSigning block.

    Generated models wrap operation names in ``RootModel[str]`` while older
    models may expose plain strings. This helper normalizes both forms and
    flattens ``None`` and ``[]`` to empty frozensets for O(1) membership.
    """

    required: frozenset[str] = field(default_factory=frozenset)
    warn: frozenset[str] = field(default_factory=frozenset)
    supported: frozenset[str] = field(default_factory=frozenset)


def _operation_names(
    values: Iterable[str | RootModel[str]] | None,
) -> frozenset[str]:
    return frozenset(value if isinstance(value, str) else value.root for value in values or ())


def _extract(capability: RequestSigning) -> _OperationLists:
    return _OperationLists(
        required=_operation_names(capability.required_for),
        warn=_operation_names(capability.warn_for),
        supported=_operation_names(capability.supported_for),
    )


def operation_needs_signing(
    capability: RequestSigning | None,
    operation: str,
) -> SigningDecision:
    """Classify whether to sign an outgoing operation against a seller's policy.

    Precedence follows the spec: ``required_for`` > ``warn_for`` >
    ``supported_for``. An operation named in ``required_for`` is classified
    ``"required"`` even if it also appears in the weaker lists.

    If ``capability`` is ``None`` (the seller doesn't advertise a
    ``request_signing`` block at all) or ``capability.supported`` is
    ``False``, always returns ``"skip"``.

    Parameters
    ----------
    capability:
        The ``request_signing`` block from the seller's
        ``get_adcp_capabilities`` response, or ``None``.
    operation:
        The AdCP protocol operation name, e.g. ``"create_media_buy"``. Not
        an MCP tool name or A2A skill name — the verifier compares against
        the protocol names.
    """
    if capability is None or not capability.supported:
        return "skip"

    lists = _extract(capability)
    if operation in lists.required:
        return "required"
    if operation in lists.warn or operation in lists.supported:
        return "optional"
    return "skip"


def require_signing_profile(capability: RequestSigning | None, signing: SigningConfig) -> None:
    """Reject unsupported opt-in profiles before sending or changing credentials."""
    profiles = getattr(capability, "profiles", None)
    accepted = (
        {DEFAULT_TAG}
        if profiles is None
        else {getattr(profile, "value", profile) for profile in profiles}
    )
    if signing.tag not in accepted:
        raise ValueError(
            f"Seller does not advertise request-signing profile {signing.tag!r}; "
            "automatic signing cannot downgrade the configured identity"
        )


__all__ = [
    "SigningConfig",
    "SigningDecision",
    "current_operation",
    "operation_needs_signing",
]
