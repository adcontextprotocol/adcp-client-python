"""Tier 2 part 2 — :class:`BuyerAgentRegistry` dispatch wire-up.

Covers the seam between the framework's existing auth path and the
new commercial-identity registry layer:

* :class:`AuthInfo` synthesizes a typed
  :class:`adcp.decisioning.Credential` from legacy
  ``kind`` / ``key_id`` / ``principal`` fields so adopters built against
  the v6.0 alpha get registry dispatch with zero code change.
* :class:`PlatformHandler` calls the registry BEFORE
  :meth:`AccountStore.resolve` when one is wired; the resolved
  :class:`BuyerAgent` is threaded onto :attr:`RequestContext.buyer_agent`.
* Suspended / blocked / unknown agents reject with structured error
  codes (``AGENT_SUSPENDED`` / ``AGENT_BLOCKED`` /
  ``REQUEST_AUTH_UNRECOGNIZED_AGENT``) instead of leaking into
  ``ACCOUNT_NOT_FOUND``.
* No registry wired → existing dispatch path runs unchanged
  (back-compat for pre-trust beta adopters).
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# AuthInfo — credential synthesis from legacy fields
# ---------------------------------------------------------------------------


def test_authinfo_signed_request_synthesizes_http_sig_credential() -> None:
    """v6.0-alpha pattern: ``AuthInfo(kind="signed_request", principal="...",
    key_id="...")`` — ``__post_init__`` produces a typed
    :class:`HttpSigCredential` so the registry dispatch path works
    without adopter code change."""
    auth = AuthInfo(
        kind="signed_request",
        key_id="kid-1",
        principal="https://agent.example/",
    )
    assert isinstance(auth.credential, HttpSigCredential)
    assert auth.credential.kind == "http_sig"
    assert auth.credential.keyid == "kid-1"
    assert auth.credential.agent_url == "https://agent.example/"
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


def test_authinfo_explicit_credential_wins_over_legacy() -> None:
    """Adopters wiring v3 directly construct the credential explicitly.
    Synthesis is one-way: explicit ``credential=...`` always wins, the
    legacy fields are ignored as a synthesis source."""
    explicit = HttpSigCredential(
        kind="http_sig",
        keyid="kid-explicit",
        agent_url="https://explicit/",
        verified_at=123.0,
    )
    auth = AuthInfo(
        kind="signed_request",
        key_id="kid-legacy",
        principal="https://legacy/",
        credential=explicit,
    )
    assert auth.credential is explicit
    # agent_url derives from credential, not legacy principal.
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
    legacy projection — adopter ``Account.auth_info`` consumers don't
    see the new fields and aren't broken."""
    from adcp.decisioning.accounts import _auth_info_to_dict

    auth = AuthInfo(
        kind="signed_request",
        key_id="kid-1",
        principal="buyer-a",
        scopes=["read"],
    )
    assert _auth_info_to_dict(auth) == {
        "kind": "signed_request",
        "key_id": "kid-1",
        "principal": "buyer-a",
        "scopes": ["read"],
    }


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
    :meth:`resolve_by_credential` (which is the bearer path)."""
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
        metadata={
            "adcp.auth_info": AuthInfo(
                kind="signed_request",
                key_id="kid-1",
                principal="https://agent.example/",
            ),
        }
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
        display_name="Legacy Bearer Buyer",
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
        metadata={
            "adcp.auth_info": AuthInfo(
                kind="signed_request",
                key_id="kid-1",
                principal="https://unknown/",
            ),
        }
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            tool_ctx,
        )
    assert exc_info.value.code == "REQUEST_AUTH_UNRECOGNIZED_AGENT"
    assert exc_info.value.recovery == "terminal"


@pytest.mark.asyncio
async def test_suspended_agent_raises_agent_suspended(executor) -> None:
    """Status=suspended is a temporary commercial pause — distinct
    error code so buyer agents can branch on retry vs escalate."""
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
        metadata={
            "adcp.auth_info": AuthInfo(
                kind="signed_request",
                key_id="kid-1",
                principal="https://suspended/",
            ),
        }
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            tool_ctx,
        )
    assert exc_info.value.code == "AGENT_SUSPENDED"
    assert exc_info.value.details["agent_url"] == "https://suspended/"


@pytest.mark.asyncio
async def test_blocked_agent_raises_agent_blocked(executor) -> None:
    """Status=blocked is a hard cutoff — buyer cannot retry their way
    out, must contact seller directly."""
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
        metadata={
            "adcp.auth_info": AuthInfo(
                kind="signed_request",
                key_id="kid-1",
                principal="https://blocked/",
            ),
        }
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            tool_ctx,
        )
    assert exc_info.value.code == "AGENT_BLOCKED"


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
        # No buyer_agent_registry wired.
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
    The framework picks the right resolver based on the verified
    credential kind."""
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
        ToolContext(
            metadata={
                "adcp.auth_info": AuthInfo(
                    kind="signed_request",
                    key_id="kid-1",
                    principal="https://signed/",
                ),
            }
        ),
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
