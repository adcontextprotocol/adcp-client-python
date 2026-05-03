"""Buyer-agent registry — commercial identity layer for v3 sellers.

The registry sits between the framework's authentication layer (which
proves *cryptographic* identity — "this signed request is from
agent_url X") and :func:`adcp.decisioning.AccountStore.resolve` (which
projects the request's account reference into a typed account).
It answers two questions that today fall on adopter code with no SDK
help:

1. **Is this agent authenticated?** Do we recognize this counterparty
   commercially at all (allowlist + onboarding state)?
2. **Can this agent be billed?** Does the seller have a payments
   relationship with the agent itself, or are accounts under this
   agent always operator-billed (passthrough)?

Question 2 has wire-level consequences — a passthrough-only agent's
:meth:`sync_accounts` request must always carry ``billing: "operator"``.
Today the framework can't enforce that and the resulting commercial
drift is invisible to buyers. With :class:`BuyerAgent.billing_capabilities`
the framework rejects mismatched ``billing`` values with a structured
:class:`adcp.decisioning.AdcpError`
``code="BILLING_NOT_PERMITTED_FOR_AGENT"``.

Per :issue:`adcp-client#1269` and the v3-identity-bundle RFC, the
registry is *durable* infrastructure — it stays useful even after
full HTTP-Signatures + brand.json verification ships. The OpenRTB
analogy: an SSP doesn't just verify a DSP's auth — it has a
``buyer_id`` row with rate cards, credit, payment status, suspension
flags. Token proves identity; row drives commercial behavior.

**Three implementer postures.** Adopters pick by which factory they
construct:

* :func:`signing_only_registry` — production target. Accept
  cryptographically-verified agent_url only; bearer traffic refused.
* :func:`mixed_registry` — transition. Signed traffic resolves
  cryptographically; bearer falls through to the legacy key table.
* :func:`bearer_only_registry` — pre-trust beta. Existing world,
  just typed. Migration path: switch to mixed later.

The factory pattern (over a Protocol with two optional methods) makes
"unimplemented" unambiguous: you build the registry that matches the
posture you've adopted; calling on a credential the registry doesn't
handle returns ``None`` deliberately.

**Salesagent migration mapping.** Their ``Principal`` table is a
primitive ``BuyerAgent`` table. Pre-trust posture is one method
(``resolve_by_credential``) against the existing rows — no new tables,
gets framework-enforced ``billing_capabilities`` validation as soon as
they add a ``billing_capability`` column.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

#: Billing modes a seller may apply to an :class:`Account`. Mirrors the
#: AdCP wire enum on ``schemas/cache/enums/billing-party.json``. The
#: registry's :attr:`BuyerAgent.billing_capabilities` is a SET of
#: these — a single agent may permit multiple modes (e.g., an agency
#: that's billed direct for owned brands but passes through for
#: agency-mediated brands).
BillingMode = Literal["operator", "agent", "advertiser"]

#: Lifecycle state of a recognized buyer agent. Sellers transition an
#: agent to ``"suspended"`` for credit / compliance pauses, ``"blocked"``
#: for hard cutoffs (terms violation, fraud).
BuyerAgentStatus = Literal["active", "suspended", "blocked"]


# ----- Credential discriminated union -----


@dataclass(frozen=True)
class ApiKeyCredential:
    """Bearer / API-key credential. The framework's authentication
    layer extracts the ``key_id`` from a header before this point;
    the registry's :meth:`BuyerAgentRegistry.resolve_by_credential`
    looks it up against the adopter's existing key table.
    """

    kind: Literal["api_key"]
    key_id: str


@dataclass(frozen=True)
class OAuthCredential:
    """OAuth client-credentials grant. Includes the verified scopes."""

    kind: Literal["oauth"]
    client_id: str
    scopes: tuple[str, ...] = ()
    expires_at: float | None = None


@dataclass(frozen=True)
class HttpSigCredential:
    """RFC 9421 HTTP-Signatures credential. ``agent_url`` is
    *cryptographically verified* — the framework has already validated
    the signature against the agent's published JWK before this
    credential is constructed.
    """

    kind: Literal["http_sig"]
    keyid: str
    agent_url: str
    verified_at: float


#: Discriminated union of supported credential kinds. Pattern-match
#: on ``credential.kind`` to extract the right sub-type.
Credential = ApiKeyCredential | OAuthCredential | HttpSigCredential


# ----- BuyerAgent + default terms -----


@dataclass(frozen=True)
class BuyerAgentDefaultTerms:
    """Commercial defaults applied when accounts are provisioned under
    an agent. Each field optional; the framework merges these with
    per-request overrides under sparse-merge semantics: explicit
    non-null per-request fields override; ``None`` fields fall through
    to the agent's default.

    Mirrors the spec ``Account`` shape on ``schemas/cache/core/account.json``
    (3.0-compliant, 3.1-ready — ``billing_entity`` is the 3.1 field
    that 3.0-only adopters can leave ``None``).
    """

    rate_card: str | None = None
    payment_terms: str | None = None
    credit_limit: Mapping[str, Any] | None = None
    billing_entity: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class BuyerAgent:
    """Commercial identity for a buyer agent we recognize.

    ``agent_url`` is the canonical, on-the-wire identifier — treat
    like a public key: stable enough that rotation requires explicit
    re-onboarding. No separate internal id is exposed; adopters
    attaching internal ids do so via :attr:`ext`.

    .. note::
        Frozen — the registry returns immutable snapshots. To mutate
        (status change, terms update), the adopter writes to their
        durable store and re-resolves on the next request. Mutation
        in-place would create cross-request leakage between concurrent
        platform method invocations sharing the same registry cache.
    """

    agent_url: str
    display_name: str
    status: BuyerAgentStatus

    #: Set of legal ``billing`` values for accounts under this agent.
    #: Pre-trust beta default: ``frozenset({"operator"})`` (passthrough
    #: only — agent has no payments relationship). Agent-billable adopters
    #: include ``"agent"`` and/or ``"advertiser"``.
    #:
    #: Real seller business models can permit MULTIPLE modes — e.g., an
    #: agency that's direct-billed for owned brands but
    #: ``operator``-passthrough for agency-mediated brands. The set
    #: shape preserves that.
    billing_capabilities: frozenset[BillingMode] = frozenset({"operator"})

    default_account_terms: BuyerAgentDefaultTerms | None = None

    #: Pre-RFC allowlist of brand domains this agent can transact for.
    #: Once :class:`BrandAuthorizationResolver` lands (Tier 3, gated on
    #: ADCP #3690), this becomes a static fallback layered on top of
    #: per-request authz against ``brand.json``. Both checks AND when
    #: both are configured.
    allowed_brands: frozenset[str] | None = None

    #: Adopter passthrough for internal ids, audit metadata, anything
    #: the SDK doesn't model.
    ext: Mapping[str, Any] = field(default_factory=dict)


# ----- Registry Protocol + factory pattern -----


@runtime_checkable
class BuyerAgentRegistry(Protocol):
    """Adopter-implemented mapping from credential → :class:`BuyerAgent`.

    The framework calls one method per request, dispatched by
    credential kind:

    * :meth:`resolve_by_agent_url` for cryptographically-verified
      signed traffic — the framework already validated the RFC 9421
      signature and extracted ``agent_url`` from the JWK claim.
    * :meth:`resolve_by_credential` for bearer / API-key / OAuth —
      the adopter looks up against their existing key table.

    Returning ``None`` rejects the request with ``PERMISSION_DENIED``
    (``details`` omitted — the unrecognized-agent path MUST be
    indistinguishable on the wire from the recognized-but-denied path
    to prevent cross-tenant onboarding enumeration). Adopters
    typically construct
    via the :func:`signing_only_registry` / :func:`bearer_only_registry`
    / :func:`mixed_registry` factories rather than implementing the
    Protocol directly — the factories carry the posture choice in
    their construction so it's never ambiguous which method is
    intentionally unimplemented.
    """

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        """Resolve a verified ``agent_url``. Adopters do NOT re-verify
        the signature here — the framework has already done so. This
        method just looks up the counterparty row in the adopter's
        commercial registry."""

    async def resolve_by_credential(
        self, credential: ApiKeyCredential | OAuthCredential
    ) -> BuyerAgent | None:
        """Resolve a bearer / API-key / OAuth credential. For
        pre-trust beta sellers, this IS the existing key table just
        exposed through a typed surface."""


# Factory call signatures — adopters provide the inner async lookup
# functions; the factory returns a registry with the right posture.
_SignedResolver = Callable[[str], Awaitable[BuyerAgent | None]]
_CredentialResolver = Callable[[ApiKeyCredential | OAuthCredential], Awaitable[BuyerAgent | None]]


@dataclass(frozen=True)
class _SigningOnlyRegistry:
    """Production-target posture: signed traffic only. Bearer requests
    are refused at the registry layer with no extra config."""

    _resolve_by_agent_url: _SignedResolver

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        return await self._resolve_by_agent_url(agent_url)

    async def resolve_by_credential(
        self, credential: ApiKeyCredential | OAuthCredential
    ) -> BuyerAgent | None:
        # Intentional reject — adopter chose signing-only.
        return None


@dataclass(frozen=True)
class _BearerOnlyRegistry:
    """Pre-trust beta posture: existing key table, just typed.
    Migration path: switch to :func:`mixed_registry` once signed
    onboarding is wired."""

    _resolve_by_credential: _CredentialResolver

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        # Intentional reject — adopter is pre-trust beta and doesn't
        # accept signed traffic yet.
        return None

    async def resolve_by_credential(
        self, credential: ApiKeyCredential | OAuthCredential
    ) -> BuyerAgent | None:
        return await self._resolve_by_credential(credential)


@dataclass(frozen=True)
class _MixedRegistry:
    """Transition posture: both methods. Signed traffic resolves
    cryptographically; bearer falls through to the legacy key table."""

    _resolve_by_agent_url: _SignedResolver
    _resolve_by_credential: _CredentialResolver

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        return await self._resolve_by_agent_url(agent_url)

    async def resolve_by_credential(
        self, credential: ApiKeyCredential | OAuthCredential
    ) -> BuyerAgent | None:
        return await self._resolve_by_credential(credential)


def signing_only_registry(
    resolve_by_agent_url: _SignedResolver,
) -> BuyerAgentRegistry:
    """Production-target: accept signed traffic only.

    Adopter supplies an async function that maps a verified
    ``agent_url`` to a :class:`BuyerAgent` (or ``None`` to reject).
    Bearer traffic gets ``PERMISSION_DENIED`` (with ``details``
    omitted — wire-indistinguishable from any other denial) at the
    framework's dispatch layer — the registry deliberately doesn't
    implement bearer lookup.
    """
    return _SigningOnlyRegistry(_resolve_by_agent_url=resolve_by_agent_url)


def bearer_only_registry(
    resolve_by_credential: _CredentialResolver,
) -> BuyerAgentRegistry:
    """Pre-trust beta: accept bearer / API-key / OAuth traffic only.

    Adopter supplies an async function that maps an
    :class:`ApiKeyCredential` or :class:`OAuthCredential` to a
    :class:`BuyerAgent` (or ``None`` to reject). Signed traffic gets
    ``PERMISSION_DENIED`` — adopt :func:`mixed_registry` once signed
    onboarding is wired.
    """
    return _BearerOnlyRegistry(_resolve_by_credential=resolve_by_credential)


def mixed_registry(
    *,
    resolve_by_agent_url: _SignedResolver,
    resolve_by_credential: _CredentialResolver,
) -> BuyerAgentRegistry:
    """Transition: accept both signed and bearer traffic.

    Used during the migration window when buyers are upgrading from
    pre-trust bearer auth to signed requests. The framework picks
    the right resolver based on the verified credential kind.
    """
    return _MixedRegistry(
        _resolve_by_agent_url=resolve_by_agent_url,
        _resolve_by_credential=resolve_by_credential,
    )


# ----- billing-capability validation helper -----


def validate_billing_for_agent(
    *,
    requested_billing: BillingMode,
    agent: BuyerAgent,
) -> None:
    """Raise :class:`adcp.decisioning.AdcpError`
    ``BILLING_NOT_PERMITTED_FOR_AGENT`` when ``requested_billing`` is
    not in ``agent.billing_capabilities``.

    Called by the framework's ``sync_accounts`` shim before invoking
    the platform method. Adopters needn't call this directly; the
    framework enforces. Re-exported so platform methods that branch
    on billing mode can short-circuit to the same structured error.

    The wire ``details`` payload deliberately carries only
    ``rejected_billing`` (and an optional ``suggested_billing``) — it
    MUST NOT carry the agent's full ``permitted_billing`` subset. The
    full subset is the agent's commercial relationship with the
    seller; surfacing it on every rejected request would let a
    misconfigured buyer probe and exfiltrate the matrix one mode at
    a time.
    """
    if requested_billing in agent.billing_capabilities:
        return
    # Local import to avoid a cycle (types.py → registry.py would
    # close on import-load order).
    from adcp.decisioning.types import AdcpError

    # Suggest a single permitted mode (deterministic — the
    # alphabetically-first permitted mode) when the agent has any
    # capability at all. We do NOT enumerate the full set; suggesting
    # one mode is sufficient remediation hint without leaking the
    # subset shape on every failed request.
    suggested = sorted(agent.billing_capabilities)[0] if agent.billing_capabilities else None
    details: dict[str, Any] = {"rejected_billing": requested_billing}
    if suggested is not None:
        details["suggested_billing"] = suggested

    raise AdcpError(
        "BILLING_NOT_PERMITTED_FOR_AGENT",
        message=(
            f"Buyer agent {agent.agent_url!r} is not authorized for "
            f"billing={requested_billing!r}. Common cause: this agent "
            "has no payments relationship with the seller (passthrough "
            "only) — accounts under this agent must be operator-billed. "
            "Sellers extending the agent's billing capabilities update "
            "the BuyerAgent.billing_capabilities frozenset in their "
            "durable store."
        ),
        field="billing",
        recovery="correctable",
        details=details,
    )


__all__ = [
    "ApiKeyCredential",
    "BillingMode",
    "BuyerAgent",
    "BuyerAgentDefaultTerms",
    "BuyerAgentRegistry",
    "BuyerAgentStatus",
    "Credential",
    "HttpSigCredential",
    "OAuthCredential",
    "bearer_only_registry",
    "mixed_registry",
    "signing_only_registry",
    "validate_billing_for_agent",
]
