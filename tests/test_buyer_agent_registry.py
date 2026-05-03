"""Tests for :mod:`adcp.decisioning.registry` — BuyerAgentRegistry
factory pattern + billing_capabilities enforcement.

Behavior under test:

* Three implementer postures (signing-only / bearer-only / mixed)
  reject the off-posture credential type by returning ``None``.
* :func:`validate_billing_for_agent` accepts permitted modes and
  raises ``X_BILLING_NOT_PERMITTED_FOR_AGENT`` on others, with details
  scoped to ``rejected_billing`` (and optional ``suggested_billing``)
  — the full ``permitted_billing`` subset is never leaked.
* ``BuyerAgent`` defaults match the pre-trust beta passthrough-only
  posture (no payments relationship — accounts must be operator-billed).
* Discriminated :data:`Credential` union pattern-matches cleanly.
* Frozen dataclasses reject mutation.
"""

from __future__ import annotations

import pytest

from adcp.decisioning.registry import (
    ApiKeyCredential,
    BuyerAgent,
    BuyerAgentDefaultTerms,
    BuyerAgentRegistry,
    Credential,
    HttpSigCredential,
    OAuthCredential,
    bearer_only_registry,
    mixed_registry,
    signing_only_registry,
    validate_billing_for_agent,
)
from adcp.decisioning.types import AdcpError

# ----- Discriminated Credential union -----


def test_api_key_credential_kind_literal() -> None:
    cred = ApiKeyCredential(kind="api_key", key_id="k1")
    assert cred.kind == "api_key"
    assert cred.key_id == "k1"


def test_oauth_credential_with_scopes() -> None:
    cred = OAuthCredential(
        kind="oauth",
        client_id="c1",
        scopes=("read:products", "write:media_buys"),
    )
    assert cred.kind == "oauth"
    assert "read:products" in cred.scopes


def test_http_sig_credential_carries_verified_agent_url() -> None:
    """``agent_url`` on HttpSigCredential is cryptographically verified —
    the framework only constructs this credential after validating the
    signature against the agent's published JWK."""
    cred = HttpSigCredential(
        kind="http_sig",
        keyid="kid-1",
        agent_url="https://agent.example.com/",
        verified_at=1700000000.0,
    )
    assert cred.kind == "http_sig"
    assert cred.agent_url == "https://agent.example.com/"


def test_credential_pattern_match() -> None:
    """The discriminated union pattern-matches on ``.kind`` cleanly."""
    creds: list[Credential] = [
        ApiKeyCredential(kind="api_key", key_id="k"),
        OAuthCredential(kind="oauth", client_id="c"),
        HttpSigCredential(
            kind="http_sig",
            keyid="kid",
            agent_url="https://x/",
            verified_at=0.0,
        ),
    ]
    kinds = [c.kind for c in creds]
    assert kinds == ["api_key", "oauth", "http_sig"]


# ----- BuyerAgent + defaults -----


def test_buyer_agent_default_billing_capabilities_is_passthrough_only() -> None:
    """Pre-trust beta default: passthrough-only. Sellers without a
    payments relationship configured must explicitly opt in to
    agent-billable by setting the capabilities frozenset."""
    agent = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme Buyer Agent",
        status="active",
    )
    assert agent.billing_capabilities == frozenset({"operator"})


def test_buyer_agent_billing_capabilities_set_supports_multiple_modes() -> None:
    """Real seller business model: agency that's direct-billed for
    owned brands but operator-passthrough for agency-mediated brands.
    Set shape preserves both."""
    agent = BuyerAgent(
        agent_url="https://agency.example/",
        display_name="Hybrid Agency",
        status="active",
        billing_capabilities=frozenset({"operator", "agent"}),
    )
    assert "operator" in agent.billing_capabilities
    assert "agent" in agent.billing_capabilities
    assert "advertiser" not in agent.billing_capabilities


def test_buyer_agent_is_frozen() -> None:
    """Frozen — registry returns immutable snapshots; mutation in-place
    would create cross-request leakage."""
    agent = BuyerAgent(
        agent_url="https://x/",
        display_name="x",
        status="active",
    )
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        agent.status = "suspended"  # type: ignore[misc]


def test_default_terms_optional_fields_default_to_none() -> None:
    terms = BuyerAgentDefaultTerms()
    assert terms.rate_card is None
    assert terms.payment_terms is None
    assert terms.credit_limit is None
    assert terms.billing_entity is None


# ----- Factory pattern: signing-only -----


@pytest.mark.asyncio
async def test_signing_only_registry_resolves_signed_traffic() -> None:
    expected = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )

    async def lookup(agent_url: str) -> BuyerAgent | None:
        return expected if agent_url == "https://agent.example/" else None

    registry = signing_only_registry(lookup)
    assert await registry.resolve_by_agent_url("https://agent.example/") is expected
    assert await registry.resolve_by_agent_url("https://unknown/") is None


@pytest.mark.asyncio
async def test_signing_only_registry_rejects_bearer() -> None:
    """Production-target posture: bearer is refused at the registry
    layer with no extra config — adopter explicitly chose signing-only."""

    async def lookup(_: str) -> BuyerAgent | None:
        raise AssertionError("should not be called for bearer")

    registry = signing_only_registry(lookup)
    cred = ApiKeyCredential(kind="api_key", key_id="k1")
    assert await registry.resolve_by_credential(cred) is None


# ----- Factory pattern: bearer-only -----


@pytest.mark.asyncio
async def test_bearer_only_registry_resolves_credential() -> None:
    expected = BuyerAgent(
        agent_url="https://legacy/",
        display_name="Legacy",
        status="active",
    )

    async def lookup(
        cred: ApiKeyCredential | OAuthCredential,
    ) -> BuyerAgent | None:
        if isinstance(cred, ApiKeyCredential) and cred.key_id == "k1":
            return expected
        return None

    registry = bearer_only_registry(lookup)
    cred = ApiKeyCredential(kind="api_key", key_id="k1")
    assert await registry.resolve_by_credential(cred) is expected


@pytest.mark.asyncio
async def test_bearer_only_registry_rejects_signed_traffic() -> None:
    """Pre-trust beta posture: signed traffic is refused. Migration
    path: adopt mixed_registry once signed onboarding is wired."""

    async def lookup(_: ApiKeyCredential | OAuthCredential) -> BuyerAgent | None:
        return None

    registry = bearer_only_registry(lookup)
    assert await registry.resolve_by_agent_url("https://signed/") is None


# ----- Factory pattern: mixed -----


@pytest.mark.asyncio
async def test_mixed_registry_routes_both_paths() -> None:
    signed_agent = BuyerAgent(
        agent_url="https://signed/",
        display_name="Signed",
        status="active",
    )
    bearer_agent = BuyerAgent(
        agent_url="https://bearer/",
        display_name="Bearer",
        status="active",
    )

    async def by_agent_url(url: str) -> BuyerAgent | None:
        return signed_agent if url == "https://signed/" else None

    async def by_credential(
        _: ApiKeyCredential | OAuthCredential,
    ) -> BuyerAgent | None:
        return bearer_agent

    registry = mixed_registry(
        resolve_by_agent_url=by_agent_url,
        resolve_by_credential=by_credential,
    )
    assert await registry.resolve_by_agent_url("https://signed/") is signed_agent
    cred = ApiKeyCredential(kind="api_key", key_id="k1")
    assert await registry.resolve_by_credential(cred) is bearer_agent


# ----- Protocol conformance -----


def test_signing_only_registry_satisfies_protocol() -> None:
    async def stub(_: str) -> BuyerAgent | None:
        return None

    registry = signing_only_registry(stub)
    assert isinstance(registry, BuyerAgentRegistry)


def test_bearer_only_registry_satisfies_protocol() -> None:
    async def stub(_: ApiKeyCredential | OAuthCredential) -> BuyerAgent | None:
        return None

    registry = bearer_only_registry(stub)
    assert isinstance(registry, BuyerAgentRegistry)


def test_mixed_registry_satisfies_protocol() -> None:
    async def signed_stub(_: str) -> BuyerAgent | None:
        return None

    async def cred_stub(
        _: ApiKeyCredential | OAuthCredential,
    ) -> BuyerAgent | None:
        return None

    registry = mixed_registry(
        resolve_by_agent_url=signed_stub,
        resolve_by_credential=cred_stub,
    )
    assert isinstance(registry, BuyerAgentRegistry)


# ----- billing_capabilities enforcement -----


def test_validate_billing_accepts_permitted_mode() -> None:
    agent = BuyerAgent(
        agent_url="https://agency/",
        display_name="Agency",
        status="active",
        billing_capabilities=frozenset({"operator", "agent"}),
    )
    # Both permitted — must not raise.
    validate_billing_for_agent(requested_billing="operator", agent=agent)
    validate_billing_for_agent(requested_billing="agent", agent=agent)


def test_validate_billing_rejects_passthrough_only_with_agent_billing() -> None:
    """Passthrough-only agent (default) — must reject ``billing="agent"``
    with structured error including the diagnostic detail."""
    agent = BuyerAgent(
        agent_url="https://passthrough/",
        display_name="Passthrough",
        status="active",
        # default billing_capabilities = frozenset({"operator"})
    )
    with pytest.raises(AdcpError) as exc:
        validate_billing_for_agent(requested_billing="agent", agent=agent)
    assert exc.value.code == "X_BILLING_NOT_PERMITTED_FOR_AGENT"
    assert exc.value.field == "billing"
    assert exc.value.recovery == "correctable"
    details = exc.value.details
    # ``rejected_billing`` is required.
    assert details["rejected_billing"] == "agent"
    # Suggested mode is the alphabetically-first permitted mode.
    assert details["suggested_billing"] == "operator"
    # Critical: the full ``permitted_billing`` subset MUST NOT leak —
    # surfacing it on every rejected request would let a misconfigured
    # buyer probe and exfiltrate the matrix one mode at a time.
    assert "permitted_billing" not in details
    # The agent_url is also a leak vector and is not echoed back.
    assert "agent_url" not in details


def test_validate_billing_rejects_advertiser_when_not_in_capabilities() -> None:
    agent = BuyerAgent(
        agent_url="https://x/",
        display_name="x",
        status="active",
        billing_capabilities=frozenset({"operator", "agent"}),
    )
    with pytest.raises(AdcpError) as exc:
        validate_billing_for_agent(requested_billing="advertiser", agent=agent)
    assert exc.value.code == "X_BILLING_NOT_PERMITTED_FOR_AGENT"
    assert "advertiser" in str(exc.value)
    # Sanity: with a non-empty permitted set, suggested_billing is set.
    assert exc.value.details["suggested_billing"] in {"agent", "operator"}
