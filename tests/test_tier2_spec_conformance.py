"""Tier 2 commercial-identity gate — AdCP error-code spec conformance.

The four denial paths in :func:`adcp.decisioning.handler._resolve_buyer_agent`
and :func:`adcp.decisioning.registry.validate_billing_for_agent` previously
raised four SDK-invented error codes (``AGENT_SUSPENDED``, ``AGENT_BLOCKED``,
``REQUEST_AUTH_UNRECOGNIZED_AGENT``, ``INVALID_BILLING_MODEL``) absent from
the spec's :file:`schemas/cache/enums/error-code.json` 51-entry vocabulary.

This file pins the spec-conformant wire shape:

* All four denial paths surface a code from the spec vocabulary
  (``PERMISSION_DENIED`` for the three commercial-identity paths;
  ``BILLING_NOT_PERMITTED_FOR_AGENT`` for the billing-capability path —
  see PR notes for the spec status of the billing code).
* Recognized-but-denied paths (suspended / blocked) carry
  ``details.scope="agent"`` + ``details.status``.
* Unrecognized paths (registry miss / no credential / unknown status)
  OMIT ``details`` so the wire shape is indistinguishable from a
  recognized-but-denied response per the cross-tenant onboarding-oracle
  clamp.
* The billing-capability path's ``details`` carries ``rejected_billing``
  (and an optional ``suggested_billing``) — the full
  ``permitted_billing`` subset MUST NOT leak.

The four old codes MUST NOT be raised by any framework path. A regression
test here is the load-bearing CI signal that an adopter doesn't
accidentally re-introduce them via copy-paste.

The latency / headers / side-effects parity contract between the
unrecognized-agent path and the recognized-but-denied path is tracked as
a separate follow-up (see issue #375 and the parity-contract follow-up
referenced in the PR body). This file pins the wire-shape conformance
only — the parity refactor needs a single emit point with deliberate
latency padding and identical audit/metric side-effects, which is a
larger dispatch-path refactor than fits in the rename PR.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from adcp.decisioning import (
    AdcpError,
    AuthInfo,
    BuyerAgent,
    DecisioningCapabilities,
    DecisioningPlatform,
    HttpSigCredential,
    InMemoryTaskRegistry,
    SingletonAccounts,
    signing_only_registry,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.registry import validate_billing_for_agent
from adcp.server.base import ToolContext

# Codes the framework MUST NOT raise from the Tier 2 commercial-identity
# gate after this PR. Adopters who match on these on the wire need to
# migrate to the new shape per the CHANGELOG.
_REMOVED_CODES = frozenset(
    {
        "AGENT_SUSPENDED",
        "AGENT_BLOCKED",
        "REQUEST_AUTH_UNRECOGNIZED_AGENT",
        "INVALID_BILLING_MODEL",
    }
)


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tier2-spec-")
    yield pool
    pool.shutdown(wait=True)


def _signed_auth_info(agent_url: str) -> AuthInfo:
    return AuthInfo(
        kind="http_sig",
        credential=HttpSigCredential(
            kind="http_sig",
            keyid="kid-1",
            agent_url=agent_url,
            verified_at=1700000000.0,
        ),
    )


def _make_handler(platform: DecisioningPlatform, executor, registry) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=registry,
    )


class _RejectingPlatform(DecisioningPlatform):
    """Test platform whose every method asserts it's not invoked.

    The Tier 2 gate runs BEFORE the platform method, so any platform
    method invocation here means the gate let the request through
    when it shouldn't have.
    """

    capabilities = DecisioningCapabilities()
    accounts = SingletonAccounts(account_id="acct-1")

    async def get_products(self, req, ctx):  # pragma: no cover - asserts not called
        raise AssertionError("Tier 2 gate should have rejected before this")


# ---------------------------------------------------------------------------
# All four removed codes are gone from the four denial paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_miss_does_not_raise_legacy_code(executor) -> None:
    async def lookup(_url: str) -> BuyerAgent | None:
        return None

    handler = _make_handler(_RejectingPlatform(), executor, signing_only_registry(lookup))
    from adcp.types import GetProductsRequest

    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth_info("https://x/")}),
        )
    assert exc.value.code not in _REMOVED_CODES


@pytest.mark.asyncio
async def test_suspended_agent_does_not_raise_legacy_code(executor) -> None:
    suspended = BuyerAgent(agent_url="https://s/", display_name="S", status="suspended")

    async def lookup(_: str) -> BuyerAgent | None:
        return suspended

    handler = _make_handler(_RejectingPlatform(), executor, signing_only_registry(lookup))
    from adcp.types import GetProductsRequest

    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth_info("https://s/")}),
        )
    assert exc.value.code not in _REMOVED_CODES


@pytest.mark.asyncio
async def test_blocked_agent_does_not_raise_legacy_code(executor) -> None:
    blocked = BuyerAgent(agent_url="https://b/", display_name="B", status="blocked")

    async def lookup(_: str) -> BuyerAgent | None:
        return blocked

    handler = _make_handler(_RejectingPlatform(), executor, signing_only_registry(lookup))
    from adcp.types import GetProductsRequest

    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth_info("https://b/")}),
        )
    assert exc.value.code not in _REMOVED_CODES


def test_billing_validation_does_not_raise_legacy_code() -> None:
    agent = BuyerAgent(
        agent_url="https://passthrough/",
        display_name="Passthrough",
        status="active",
    )
    with pytest.raises(AdcpError) as exc:
        validate_billing_for_agent(requested_billing="agent", agent=agent)
    assert exc.value.code not in _REMOVED_CODES


# ---------------------------------------------------------------------------
# Recognized-but-denied paths carry details.scope="agent" + details.status.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suspended_carries_scope_and_status_details(executor) -> None:
    suspended = BuyerAgent(agent_url="https://s/", display_name="S", status="suspended")

    async def lookup(_: str) -> BuyerAgent | None:
        return suspended

    handler = _make_handler(_RejectingPlatform(), executor, signing_only_registry(lookup))
    from adcp.types import GetProductsRequest

    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth_info("https://s/")}),
        )
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.recovery == "correctable"
    assert exc.value.details["scope"] == "agent"
    assert exc.value.details["status"] == "suspended"


@pytest.mark.asyncio
async def test_blocked_carries_scope_and_status_details(executor) -> None:
    blocked = BuyerAgent(agent_url="https://b/", display_name="B", status="blocked")

    async def lookup(_: str) -> BuyerAgent | None:
        return blocked

    handler = _make_handler(_RejectingPlatform(), executor, signing_only_registry(lookup))
    from adcp.types import GetProductsRequest

    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth_info("https://b/")}),
        )
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.recovery == "correctable"
    assert exc.value.details["scope"] == "agent"
    assert exc.value.details["status"] == "blocked"


# ---------------------------------------------------------------------------
# Unrecognized paths OMIT details (omit-on-unestablished-identity rule).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_miss_omits_details(executor) -> None:
    """Registry returned None — the unrecognized-agent path. ``details``
    MUST be empty so the wire shape is indistinguishable from the
    recognized-but-denied paths. ``scope`` would itself be the
    discriminator that leaks onboarding state to an external attacker.
    """

    async def lookup(_url: str) -> BuyerAgent | None:
        return None

    handler = _make_handler(_RejectingPlatform(), executor, signing_only_registry(lookup))
    from adcp.types import GetProductsRequest

    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth_info("https://x/")}),
        )
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.recovery == "correctable"
    # The critical no-leak property: NO scope, NO status, NO agent_url.
    assert exc.value.details == {}


@pytest.mark.asyncio
async def test_no_credential_omits_details(executor) -> None:
    """No credential at all on the request — the framework's
    unauthenticated-with-registry path. Same wire shape as registry
    miss; ``details`` empty."""

    async def lookup(_: str) -> BuyerAgent | None:
        return None

    handler = _make_handler(_RejectingPlatform(), executor, signing_only_registry(lookup))
    from adcp.types import GetProductsRequest

    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(),
        )
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.details == {}


@pytest.mark.asyncio
async def test_unknown_status_omits_details(executor) -> None:
    """Defense-in-depth: a registry row whose status the framework
    cannot interpret (typo, future enum value, adopter-custom string)
    is routed through the omit-on-unestablished-identity path. The
    framework cannot defensibly project the unknown status string on
    the wire (it might encode commercial state the framework doesn't
    understand), so ``details`` is omitted."""
    weird = BuyerAgent.__new__(BuyerAgent)
    object.__setattr__(weird, "agent_url", "https://w/")
    object.__setattr__(weird, "display_name", "W")
    object.__setattr__(weird, "status", "deleted")
    object.__setattr__(weird, "billing_capabilities", frozenset({"operator"}))
    object.__setattr__(weird, "default_account_terms", None)
    object.__setattr__(weird, "allowed_brands", None)
    object.__setattr__(weird, "ext", {})

    async def lookup(_: str) -> BuyerAgent | None:
        return weird

    handler = _make_handler(_RejectingPlatform(), executor, signing_only_registry(lookup))
    from adcp.types import GetProductsRequest

    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth_info("https://w/")}),
        )
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.details == {}


# ---------------------------------------------------------------------------
# Billing capability path — rejected_billing required, no permitted leak.
# ---------------------------------------------------------------------------


def test_billing_validation_carries_rejected_billing() -> None:
    agent = BuyerAgent(
        agent_url="https://passthrough/",
        display_name="Passthrough",
        status="active",
    )
    with pytest.raises(AdcpError) as exc:
        validate_billing_for_agent(requested_billing="agent", agent=agent)
    assert exc.value.code == "BILLING_NOT_PERMITTED_FOR_AGENT"
    assert exc.value.recovery == "correctable"
    assert exc.value.details["rejected_billing"] == "agent"


def test_billing_validation_carries_suggested_billing_when_permitted_nonempty() -> None:
    agent = BuyerAgent(
        agent_url="https://x/",
        display_name="X",
        status="active",
        billing_capabilities=frozenset({"operator", "agent"}),
    )
    with pytest.raises(AdcpError) as exc:
        validate_billing_for_agent(requested_billing="advertiser", agent=agent)
    assert exc.value.details["suggested_billing"] in {"agent", "operator"}


def test_billing_validation_does_not_leak_permitted_subset() -> None:
    """Critical no-leak property: the full ``permitted_billing`` subset
    is the agent's commercial relationship with the seller. Surfacing
    it on every rejected request would let a misconfigured buyer probe
    and exfiltrate the matrix one mode at a time. ``details`` carries
    ``rejected_billing`` and an optional ``suggested_billing`` — not
    the whole set."""
    agent = BuyerAgent(
        agent_url="https://x/",
        display_name="X",
        status="active",
        billing_capabilities=frozenset({"operator", "agent"}),
    )
    with pytest.raises(AdcpError) as exc:
        validate_billing_for_agent(requested_billing="advertiser", agent=agent)
    details = exc.value.details
    assert "permitted_billing" not in details
    # Defense-in-depth: agent_url is also a leak vector and is not echoed.
    assert "agent_url" not in details
