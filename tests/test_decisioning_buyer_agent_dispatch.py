"""Tier 2 — :class:`BuyerAgentRegistry` dispatch wire-up.

Covers the seam between the framework's existing auth path and the
new commercial-identity registry layer:

* :class:`AuthInfo` synthesizes a typed bearer
  :class:`adcp.decisioning.Credential` from the flat
  ``kind`` / ``key_id`` / ``principal`` fields. Signed-request
  traffic requires an explicit :class:`HttpSigCredential` from the
  v3 verifier — the SDK refuses to mint one without a real
  ``verified_at`` timestamp because that would let any middleware
  setting ``kind="signed_request"`` escalate bearer traffic onto
  the signed path.
* :class:`PlatformHandler` calls the registry BEFORE
  :meth:`AccountStore.resolve` when one is wired; the resolved
  :class:`BuyerAgent` is threaded onto :attr:`RequestContext.buyer_agent`.
* Suspended / blocked / unknown-status agents reject with structured
  error codes (``AGENT_SUSPENDED`` transient, ``AGENT_BLOCKED``
  terminal, ``REQUEST_AUTH_UNRECOGNIZED_AGENT`` for missing /
  unknown-status) instead of leaking into ``ACCOUNT_NOT_FOUND``.
* No registry wired → existing dispatch path runs unchanged
  (back-compat for pre-trust beta adopters).
"""

from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.decisioning import (
    AdcpError,
    ApiKeyCredential,
    AuthInfo,
    BuyerAgent,
    DecisioningCapabilities,
    DecisioningPlatform,
    HttpSigCredential,
    InMemoryTaskRegistry,
    OAuthCredential,
    SingletonAccounts,
    bearer_only_registry,
    mixed_registry,
    signing_only_registry,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.server.base import ToolContext


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-buyer-agent-")
    yield pool
    pool.shutdown(wait=True)


def _signed_auth_info(agent_url: str, *, keyid: str = "kid-1") -> AuthInfo:
    """Construct an AuthInfo for verified signed-request traffic.

    Mirrors what an RFC 9421 verifier middleware would do: build the
    typed :class:`HttpSigCredential` with a real ``verified_at`` and
    pass it via ``credential=``. The SDK refuses to synthesize this
    from the flat fields, so tests covering the signed path use this
    helper.
    """
    return AuthInfo(
        kind="http_sig",
        credential=HttpSigCredential(
            kind="http_sig",
            keyid=keyid,
            agent_url=agent_url,
            verified_at=1700000000.0,
        ),
    )


# ---------------------------------------------------------------------------
# AuthInfo — credential synthesis from flat fields
# ---------------------------------------------------------------------------


def test_authinfo_signed_request_does_not_synthesize_credential() -> None:
    """Security boundary: synthesizing an :class:`HttpSigCredential`
    from the flat ``kind="signed_request"`` field would let any auth
    middleware that writes that string escalate bearer traffic onto
    the signed-verified path. The SDK refuses to mint a credential
    that claims cryptographic verification when no verification
    happened in this code path."""
    auth = AuthInfo(
        kind="signed_request",
        key_id="kid-1",
        principal="https://agent.example/",
    )
    assert auth.credential is None
    assert auth.agent_url is None


def test_authinfo_explicit_http_sig_credential_populates_agent_url() -> None:
    """The supported v3 path: the verifier constructs
    :class:`HttpSigCredential` with a real ``verified_at`` and passes
    it via ``credential=``. ``agent_url`` derives from the credential
    so adopters reading ``auth_info.agent_url`` get a single field
    regardless of construction style."""
    cred = HttpSigCredential(
        kind="http_sig",
        keyid="kid-1",
        agent_url="https://agent.example/",
        verified_at=1700000000.0,
    )
    auth = AuthInfo(kind="http_sig", credential=cred)
    assert auth.credential is cred
    assert auth.agent_url == "https://agent.example/"


def test_authinfo_bearer_synthesizes_api_key_credential() -> None:
    auth = AuthInfo(kind="bearer", key_id="bearer-token-1", principal="buyer-x")
    assert isinstance(auth.credential, ApiKeyCredential)
    assert auth.credential.kind == "api_key"
    assert auth.credential.key_id == "bearer-token-1"


def test_authinfo_oauth_synthesizes_oauth_credential() -> None:
    auth = AuthInfo(
        kind="oauth",
        key_id="client-1",
        principal="buyer-x",
        scopes=["read:products", "write:media_buys"],
    )
    assert isinstance(auth.credential, OAuthCredential)
    assert auth.credential.client_id == "client-1"
    assert auth.credential.scopes == ("read:products", "write:media_buys")


def test_authinfo_explicit_credential_wins_over_flat_fields() -> None:
    """Synthesis is one-way: explicit ``credential=...`` always wins."""
    explicit = HttpSigCredential(
        kind="http_sig",
        keyid="kid-explicit",
        agent_url="https://explicit/",
        verified_at=123.0,
    )
    auth = AuthInfo(
        kind="bearer",
        key_id="bearer-key",
        principal="buyer-y",
        credential=explicit,
    )
    assert auth.credential is explicit
    assert auth.agent_url == "https://explicit/"


def test_authinfo_derived_kind_yields_no_credential() -> None:
    """``derived`` / unauthenticated dev fixtures don't carry a real
    credential; synthesis stays None so the registry can reject them
    explicitly."""
    auth = AuthInfo(kind="derived", principal="anonymous")
    assert auth.credential is None
    assert auth.agent_url is None


def test_authinfo_back_compat_dict_preserves_existing_consumer() -> None:
    """``_auth_info_to_dict`` (in accounts.py) still emits the 4-key
    flat projection — adopter ``Account.auth_info`` consumers don't
    see the new fields and aren't broken."""
    from adcp.decisioning.accounts import _auth_info_to_dict

    auth = AuthInfo(
        kind="bearer",
        key_id="kid-1",
        principal="buyer-a",
        scopes=["read"],
    )
    assert _auth_info_to_dict(auth) == {
        "kind": "bearer",
        "key_id": "kid-1",
        "principal": "buyer-a",
        "scopes": ["read"],
    }


def test_authinfo_dataclass_replace_preserves_credential() -> None:
    """``dataclasses.replace`` re-runs ``__post_init__``. The synthesis
    branch only fires when ``credential is None``, so an existing
    credential survives a replace that doesn't touch it."""
    auth = AuthInfo(kind="bearer", key_id="bearer-1")
    assert isinstance(auth.credential, ApiKeyCredential)
    replaced = dataclasses.replace(auth, principal="new-principal")
    assert replaced.credential is auth.credential


# ---------------------------------------------------------------------------
# _extract_auth_info — dict-shape metadata path
# ---------------------------------------------------------------------------


def test_extract_auth_info_dict_passes_through_v3_fields() -> None:
    """Adopters whose middleware writes a v3-shape dict for
    ``ctx.metadata['adcp.auth_info']`` must get the typed credential
    + ``agent_url`` + ``operator`` + ``extra`` through to AuthInfo.
    Without this, the registry dispatch sees only the synthesized
    bearer credential and silently bypasses the verified signed path."""
    cred = HttpSigCredential(
        kind="http_sig",
        keyid="kid-1",
        agent_url="https://agent.example/",
        verified_at=1700000000.0,
    )
    ctx = ToolContext(
        metadata={
            "adcp.auth_info": {
                "kind": "http_sig",
                "credential": cred,
                "agent_url": "https://agent.example/",
                "operator": "operator-1",
                "extra": {"session_id": "s_42"},
            }
        }
    )
    result = PlatformHandler._extract_auth_info(ctx)
    assert result is not None
    assert result.credential is cred
    assert result.agent_url == "https://agent.example/"
    assert result.operator == "operator-1"
    assert result.extra == {"session_id": "s_42"}


def test_extract_auth_info_dict_back_compat_flat_fields_only() -> None:
    """v6.0-alpha middleware that only writes the flat 4-key dict still
    works — the v3 keys default to None / {}."""
    ctx = ToolContext(
        metadata={
            "adcp.auth_info": {
                "kind": "bearer",
                "key_id": "bearer-1",
                "principal": "buyer-x",
                "scopes": ["read"],
            }
        }
    )
    result = PlatformHandler._extract_auth_info(ctx)
    assert result is not None
    assert result.kind == "bearer"
    assert isinstance(result.credential, ApiKeyCredential)
    assert result.agent_url is None
    assert result.operator is None
    assert result.extra == {}


# ---------------------------------------------------------------------------
# PlatformHandler — registry dispatch
# ---------------------------------------------------------------------------


def _make_handler_with_registry(
    platform: DecisioningPlatform,
    executor: ThreadPoolExecutor,
    registry,
) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=registry,
    )


@pytest.mark.asyncio
async def test_signed_request_dispatches_through_registry_by_agent_url(
    executor,
) -> None:
    """Verified signed-request path: the framework calls
    :meth:`BuyerAgentRegistry.resolve_by_agent_url` with the
    cryptographically-verified agent_url — NOT
    :meth:`resolve_by_credential` (which is the bearer path).

    The verifier is responsible for constructing
    :class:`HttpSigCredential` with the real ``verified_at``; the
    framework trusts that as the cryptographic guarantee."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    expected_agent = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="Acme",
        status="active",
    )
    looked_up: list[str] = []

    async def lookup(agent_url: str) -> BuyerAgent | None:
        looked_up.append(agent_url)
        return expected_agent

    registry = signing_only_registry(lookup)
    received_buyer_agent: list[Any] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            received_buyer_agent.append(ctx.buyer_agent)
            return GetProductsResponse(products=[])

    handler = _make_handler_with_registry(_Platform(), executor, registry)
    tool_ctx = ToolContext(
        metadata={"adcp.auth_info": _signed_auth_info("https://agent.example/")},
    )
    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any inventory"),
        tool_ctx,
    )
    assert looked_up == ["https://agent.example/"]
    assert received_buyer_agent == [expected_agent]


@pytest.mark.asyncio
async def test_bearer_request_dispatches_through_registry_by_credential(
    executor,
) -> None:
    """Pre-trust beta path: ApiKeyCredential routes through
    :meth:`resolve_by_credential` against the adopter's existing key
    table."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    expected_agent = BuyerAgent(
        agent_url="https://legacy/",
        display_name="Bearer Buyer",
        status="active",
    )
    looked_up: list[str] = []

    async def lookup(cred):  # type: ignore[no-untyped-def]
        assert isinstance(cred, ApiKeyCredential)
        looked_up.append(cred.key_id)
        return expected_agent

    registry = bearer_only_registry(lookup)
    received_agent_url: list[str] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            received_agent_url.append(ctx.buyer_agent.agent_url)
            return GetProductsResponse(products=[])

    handler = _make_handler_with_registry(_Platform(), executor, registry)
    tool_ctx = ToolContext(
        metadata={
            "adcp.auth_info": AuthInfo(
                kind="bearer",
                key_id="bearer-1",
                principal="buyer-x",
            ),
        }
    )
    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any inventory"),
        tool_ctx,
    )
    assert looked_up == ["bearer-1"]
    assert received_agent_url == ["https://legacy/"]


@pytest.mark.asyncio
async def test_signed_kind_without_explicit_credential_is_rejected(
    executor,
) -> None:
    """Defense-in-depth: a request whose AuthInfo carries
    ``kind="signed_request"`` but no explicit ``credential`` cannot
    reach the signed-traffic registry path. Synthesis is disabled
    for this kind, so ``_resolve_buyer_agent`` sees ``credential is
    None`` and rejects with ``REQUEST_AUTH_UNRECOGNIZED_AGENT``.
    Without this, an upstream middleware that wrote
    ``kind="signed_request"`` without doing RFC 9421 verification
    would silently escalate to the verified path."""
    from adcp.types import GetProductsRequest

    async def lookup(_url: str) -> BuyerAgent | None:
        raise AssertionError("registry must not be called when synthesis is disabled")

    registry = signing_only_registry(lookup)

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AssertionError("platform method must not be called")

    handler = _make_handler_with_registry(_Platform(), executor, registry)
    tool_ctx = ToolContext(
        metadata={
            "adcp.auth_info": AuthInfo(
                kind="signed_request",
                key_id="kid-1",
                principal="https://agent.example/",
            ),
        }
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            tool_ctx,
        )
    assert exc_info.value.code == "REQUEST_AUTH_UNRECOGNIZED_AGENT"


@pytest.mark.asyncio
async def test_registry_miss_raises_request_auth_unrecognized_agent(
    executor,
) -> None:
    """Registry returns None → ``REQUEST_AUTH_UNRECOGNIZED_AGENT``,
    NOT ``ACCOUNT_NOT_FOUND`` (which would mask the commercial-allowlist
    miss as an account-resolution problem)."""
    from adcp.types import GetProductsRequest

    async def lookup(_url: str) -> BuyerAgent | None:
        return None

    registry = signing_only_registry(lookup)

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AssertionError("platform method must not be called on registry miss")

    handler = _make_handler_with_registry(_Platform(), executor, registry)
    tool_ctx = ToolContext(
        metadata={"adcp.auth_info": _signed_auth_info("https://unknown/")},
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            tool_ctx,
        )
    assert exc_info.value.code == "REQUEST_AUTH_UNRECOGNIZED_AGENT"
    assert exc_info.value.recovery == "terminal"


@pytest.mark.asyncio
async def test_suspended_agent_raises_agent_suspended_transient(executor) -> None:
    """Status=suspended is a *retryable* commercial pause
    (``recovery="transient"``). Buyer agents can retry once the
    seller restores the agent — distinct from blocked, which is
    terminal."""
    from adcp.types import GetProductsRequest

    suspended = BuyerAgent(
        agent_url="https://suspended/",
        display_name="Suspended",
        status="suspended",
    )

    async def lookup(_: str) -> BuyerAgent | None:
        return suspended

    registry = signing_only_registry(lookup)

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AssertionError("platform method must not be called when suspended")

    handler = _make_handler_with_registry(_Platform(), executor, registry)
    tool_ctx = ToolContext(
        metadata={"adcp.auth_info": _signed_auth_info("https://suspended/")},
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            tool_ctx,
        )
    assert exc_info.value.code == "AGENT_SUSPENDED"
    assert exc_info.value.recovery == "transient"
    assert exc_info.value.details["agent_url"] == "https://suspended/"


@pytest.mark.asyncio
async def test_blocked_agent_raises_agent_blocked_terminal(executor) -> None:
    """Status=blocked is a hard cutoff
    (``recovery="terminal"``) — buyer cannot retry their way out,
    must contact seller directly."""
    from adcp.types import GetProductsRequest

    blocked = BuyerAgent(
        agent_url="https://blocked/",
        display_name="Blocked",
        status="blocked",
    )

    async def lookup(_: str) -> BuyerAgent | None:
        return blocked

    registry = signing_only_registry(lookup)

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AssertionError("platform method must not be called when blocked")

    handler = _make_handler_with_registry(_Platform(), executor, registry)
    tool_ctx = ToolContext(
        metadata={"adcp.auth_info": _signed_auth_info("https://blocked/")},
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            tool_ctx,
        )
    assert exc_info.value.code == "AGENT_BLOCKED"
    assert exc_info.value.recovery == "terminal"


@pytest.mark.asyncio
async def test_unknown_agent_status_default_rejects(executor) -> None:
    """Defense-in-depth: a row with a typo'd or future-enum-value
    status must not silently fall through to ``active`` past the
    commercial-identity gate. Anything not in ``{active, suspended,
    blocked}`` raises ``REQUEST_AUTH_UNRECOGNIZED_AGENT``."""
    from adcp.types import GetProductsRequest

    weird = BuyerAgent.__new__(BuyerAgent)
    # BuyerAgent is frozen; bypass the BuyerAgentStatus literal check
    # for this defense-in-depth scenario (a custom registry impl
    # could surface a status string the framework doesn't enumerate).
    object.__setattr__(weird, "agent_url", "https://weird/")
    object.__setattr__(weird, "display_name", "Weird")
    object.__setattr__(weird, "status", "deleted")
    object.__setattr__(weird, "billing_capabilities", frozenset({"operator"}))
    object.__setattr__(weird, "default_account_terms", None)
    object.__setattr__(weird, "allowed_brands", None)
    object.__setattr__(weird, "ext", {})

    async def lookup(_: str) -> BuyerAgent | None:
        return weird

    registry = signing_only_registry(lookup)

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AssertionError("platform method must not be called for unknown status")

    handler = _make_handler_with_registry(_Platform(), executor, registry)
    tool_ctx = ToolContext(
        metadata={"adcp.auth_info": _signed_auth_info("https://weird/")},
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            tool_ctx,
        )
    assert exc_info.value.code == "REQUEST_AUTH_UNRECOGNIZED_AGENT"
    assert exc_info.value.details["status"] == "deleted"


@pytest.mark.asyncio
async def test_no_registry_wired_skips_buyer_agent_resolution(executor) -> None:
    """Pre-trust beta back-compat: adopters who don't pass
    ``buyer_agent_registry`` get the v6.0 dispatch path unchanged.
    ``ctx.buyer_agent`` is None; AccountStore.resolve runs as before."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    received_buyer_agent: list[Any] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            received_buyer_agent.append(ctx.buyer_agent)
            return GetProductsResponse(products=[])

    handler = PlatformHandler(
        _Platform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any inventory"),
        ToolContext(),
    )
    assert received_buyer_agent == [None]


@pytest.mark.asyncio
async def test_unauthenticated_request_with_registry_rejects(executor) -> None:
    """Adopter wired the registry but the request has no auth_info →
    no credential to dispatch on → registry rejects. Pre-trust adopters
    running a registry have implicitly opted out of unauthenticated
    traffic; the framework refuses to silently fall through to the
    AccountStore in that posture."""
    from adcp.types import GetProductsRequest

    async def lookup(_: str) -> BuyerAgent | None:
        return None

    registry = signing_only_registry(lookup)

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AssertionError("must not be called when no credential")

    handler = _make_handler_with_registry(_Platform(), executor, registry)
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(),
        )
    assert exc_info.value.code == "REQUEST_AUTH_UNRECOGNIZED_AGENT"


@pytest.mark.asyncio
async def test_mixed_registry_routes_signed_and_bearer_correctly(executor) -> None:
    """Migration posture: both methods. Signed traffic resolves
    cryptographically; bearer falls through to the legacy key table.
    The framework picks the right resolver based on the credential
    kind."""
    from adcp.types import GetProductsRequest, GetProductsResponse

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
    signed_calls: list[str] = []
    bearer_calls: list[str] = []

    async def by_url(url: str) -> BuyerAgent | None:
        signed_calls.append(url)
        return signed_agent if url == "https://signed/" else None

    async def by_cred(cred):  # type: ignore[no-untyped-def]
        bearer_calls.append(cred.key_id)
        return bearer_agent

    registry = mixed_registry(
        resolve_by_agent_url=by_url,
        resolve_by_credential=by_cred,
    )
    seen: list[str] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            seen.append(ctx.buyer_agent.agent_url)
            return GetProductsResponse(products=[])

    handler = _make_handler_with_registry(_Platform(), executor, registry)

    # Signed path.
    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any inventory"),
        ToolContext(metadata={"adcp.auth_info": _signed_auth_info("https://signed/")}),
    )
    # Bearer path.
    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any inventory"),
        ToolContext(
            metadata={
                "adcp.auth_info": AuthInfo(
                    kind="bearer",
                    key_id="bearer-1",
                    principal="buyer-x",
                ),
            }
        ),
    )

    assert signed_calls == ["https://signed/"]
    assert bearer_calls == ["bearer-1"]
    assert seen == ["https://signed/", "https://bearer/"]


@pytest.mark.asyncio
async def test_mixed_registry_signed_miss_rejects(executor) -> None:
    """Mixed-registry posture, signed traffic, agent_url not in the
    seller's allowlist → REQUEST_AUTH_UNRECOGNIZED_AGENT. The bearer
    path is never consulted (signed credentials don't fall through
    to bearer lookup)."""
    from adcp.types import GetProductsRequest

    bearer_consulted: list[Any] = []

    async def by_url(_url: str) -> BuyerAgent | None:
        return None

    async def by_cred(cred):  # type: ignore[no-untyped-def]
        bearer_consulted.append(cred)
        return BuyerAgent(
            agent_url="https://bearer/",
            display_name="Bearer",
            status="active",
        )

    registry = mixed_registry(
        resolve_by_agent_url=by_url,
        resolve_by_credential=by_cred,
    )

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AssertionError("must not be called on signed miss")

    handler = _make_handler_with_registry(_Platform(), executor, registry)
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth_info("https://unknown/")}),
        )
    assert exc_info.value.code == "REQUEST_AUTH_UNRECOGNIZED_AGENT"
    assert bearer_consulted == []
