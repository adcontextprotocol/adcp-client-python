"""Tier 3 brand-authorization dispatch gate.

Composes the :class:`adcp.signing.BrandAuthorizationResolver` Protocol
(landed in v3-identity stages 1-3, issue #350 PR #770) with the
framework's dispatch path so adopters can opt into per-brand
authorization the same way they opt into Tier 2 commercial-identity
gating today (via ``serve(buyer_agent_registry=...)``).

The gate runs **after** Tier 2 buyer-agent resolution and
:meth:`AccountStore.resolve`, and **before** the platform method
executes. By that point the framework has:

* a verified, registry-recognized :class:`BuyerAgent` with an active
  status (else Tier 2 already raised), and
* a resolved :class:`Account` the request operates on.

What the gate is *not*: it is not the verifier-side
``request_signature_*`` rejection path (that lives in
:mod:`adcp.signing.key_origins` for the JWKS-origin check and in the
forthcoming verifier integration for the brand_json chain checks —
issue #776). This dispatch-layer gate emits the cross-cutting
``PERMISSION_DENIED`` auth-denial code regardless of credential kind,
matching the Tier 2 unrecognized-agent rejection surface. Per-purpose
spec-shape codes are reserved for the verifier path.

The gate is opt-in. Adopters wire it by passing **both**
``brand_authz_resolver`` and ``brand_identity_resolver`` to
:func:`adcp.decisioning.serve`. Wiring one without the other raises
at boot — partial wiring is almost always a misconfiguration: a
resolver without an extractor never has a brand to check, an extractor
without a resolver never has anything to do.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from adcp.decisioning.registry import BuyerAgent
    from adcp.decisioning.types import Account
    from adcp.signing.brand_authz import BrandAuthorizationResolver


@dataclass(frozen=True)
class BrandIdentity:
    """The brand a request operates against — the input to the Tier 3
    binding check.

    Mirrors the brand identifier shape carried on wire :class:`AccountReference`
    natural-key references (``account.brand.domain``) and the
    spec-defined ``brand_id`` field on
    ``brand.json/authorized_operators[].brands[]``.

    :param domain: The brand's domain (e.g. ``"nike.com"``). Required —
        eTLD+1 binding cannot proceed without it.
    :param id: Optional brand identifier from the seller's portfolio.
        When set, the operator-delegation check is scoped to this brand
        (``authorized_operators[i].brands[]`` must contain this ``id``
        or ``"*"``). When ``None``, only ``"*"``-scoped operators
        satisfy the delegation check — fail-closed on unscoped
        delegation per :class:`BrandJsonAuthorizationResolver`.
    """

    domain: str
    id: str | None = None


#: Adopter-provided callable that extracts the brand identity from a
#: resolved :class:`Account` (+ the resolved :class:`BuyerAgent`).
#:
#: Returns ``None`` when the request has no brand to bind against — the
#: gate skips on ``None`` (the adopter is telling the framework "this
#: request isn't brand-scoped, no Tier 3 check applies"). Returns a
#: :class:`BrandIdentity` when the request operates on behalf of a
#: brand — the framework runs the binding check.
#:
#: Adopters in ``'derived'`` resolution mode typically derive the brand
#: from ``buyer_agent.allowed_brands`` or the agent's onboarding record;
#: adopters in ``'explicit'`` mode read ``account.metadata`` populated
#: by their :meth:`AccountStore.resolve`. Either path is fine — the
#: framework does not constrain the source, only the return shape.
BrandIdentityResolver = Callable[
    ["Account[object]", Union["BuyerAgent", None]],
    BrandIdentity | None | Awaitable[BrandIdentity | None],
]


@dataclass(frozen=True)
class BrandAuthorizationGate:
    """Bundle of the Tier 3 dependencies — the authorization resolver
    plus the brand-identity extractor.

    Passed to the dispatch path as a single immutable bundle so the
    pairing rule (both wired or neither) holds by construction:
    constructing the bundle requires both, so the framework only ever
    sees a complete configuration or no configuration at all.
    """

    resolver: BrandAuthorizationResolver
    extract_identity: BrandIdentityResolver
