"""An SSRF-refused JWKS URL rejects as terminal, not as a transient fetch failure.

The discovery chain's last hop fetches ``jwks_uri``, and two different things
can go wrong there. A transient failure (DNS, TCP, TLS, timeout, non-2xx) is
``request_signature_jwks_unavailable`` and the peer should retry. A destination
the SSRF gate refuses is ``request_signature_jwks_untrusted`` and the peer must
not: the gate refuses that address on every attempt, so a retry cannot succeed.

Both failures are raised with the same resolver code, ``jwks_fetch_failed``,
whose table row names the transient code. The SSRF site therefore carries its
own ``signature_code``, and this module grades that — plus the ``recovery``
classifications in the pinned bundle that are the reason the two cannot share
one code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from adcp.signing import agent_resolver
from adcp.signing.agent_resolver import AgentResolverError, request_signature_code
from adcp.signing.errors import (
    REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE,
    REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE,
    REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
    REQUEST_SIGNATURE_JWKS_UNTRUSTED,
)
from adcp.signing.jwks import SSRFValidationError
from adcp.validation.version import resolve_bundle_key

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLE_KEY = resolve_bundle_key((_REPO_ROOT / "src" / "adcp" / "ADCP_VERSION").read_text().strip())
_ENUM = json.loads(
    (
        _REPO_ROOT / "schemas" / "cache" / _BUNDLE_KEY / "enums" / "request-signing-error-code.json"
    ).read_text()
)

_AGENT = "https://seller.example.com/mcp"
_BRAND_JSON_URL = "https://seller.example.com/.well-known/brand.json"
_BRAND_JSON = {
    "agents": [
        {
            "url": _AGENT,
            "type": "sales",
            "jwks_uri": "https://keys.seller.example.com/.well-known/jwks.json",
        }
    ]
}


def test_the_two_jwks_codes_differ_in_retry_classification() -> None:
    """Why the SSRF hop cannot share a code with a transient fetch failure.

    If these two ever carry the same ``recovery`` value the distinction below
    stops mattering, and this fails rather than leaving the test looking like a
    style preference.
    """
    metadata = _ENUM["enumMetadata"]
    assert metadata[REQUEST_SIGNATURE_JWKS_UNTRUSTED]["recovery"] == "terminal"
    assert metadata[REQUEST_SIGNATURE_JWKS_UNAVAILABLE]["recovery"] == "transient"


@pytest.fixture
def _resolver_reaching_the_jwks_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two hops before ``jwks_uri`` so the JWKS hop is what runs."""

    async def fetch_capabilities(*args: Any, **kwargs: Any) -> Any:
        return agent_resolver._CapabilitiesPayload(
            body={"identity": {"brand_json_url": _BRAND_JSON_URL}}, final_url=_AGENT
        )

    monkeypatch.setattr(agent_resolver, "_fetch_capabilities", fetch_capabilities)


def _brand_json_client_factory(_url: str) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BRAND_JSON)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_ssrf_refused_jwks_url_is_untrusted_not_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    _resolver_reaching_the_jwks_hop: None,
) -> None:
    """The SSRF gate refusing ``jwks_uri`` reports the terminal code."""

    async def refuse_jwks(*args: Any, **kwargs: Any) -> Any:
        raise SSRFValidationError("resolves to a private address")

    monkeypatch.setattr(agent_resolver, "async_default_jwks_fetcher", refuse_jwks)

    with pytest.raises(AgentResolverError) as caught:
        await agent_resolver.async_resolve_agent(
            _AGENT,
            agent_type="sales",
            _brand_jwks_client_factory=_brand_json_client_factory,
        )

    # The resolver code stays the hop's code; only the spec code discriminates.
    assert caught.value.code == "jwks_fetch_failed"
    assert request_signature_code(caught.value) == REQUEST_SIGNATURE_JWKS_UNTRUSTED


@pytest.mark.asyncio
async def test_transient_jwks_fetch_failure_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    _resolver_reaching_the_jwks_hop: None,
) -> None:
    """The companion case, so the override above cannot swallow both.

    Without this, changing the table row to the terminal code would also pass
    the test above while telling every peer never to retry a timeout.
    """

    async def fail_jwks(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectTimeout("keys.seller.example.com timed out")

    monkeypatch.setattr(agent_resolver, "async_default_jwks_fetcher", fail_jwks)

    with pytest.raises(AgentResolverError) as caught:
        await agent_resolver.async_resolve_agent(
            _AGENT,
            agent_type="sales",
            _brand_jwks_client_factory=_brand_json_client_factory,
        )

    assert caught.value.code == "jwks_fetch_failed"
    assert request_signature_code(caught.value) == REQUEST_SIGNATURE_JWKS_UNAVAILABLE


@pytest.mark.asyncio
async def test_unresolvable_jwks_host_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    _resolver_reaching_the_jwks_hop: None,
) -> None:
    """The SSRF gate also raises when the host does not resolve, marked transient.

    A DNS failure is the transient row's own case ("DNS, TCP, TLS, timeout"), so
    it must not be promoted to the terminal code with the refused destinations.
    """

    async def unresolvable(*args: Any, **kwargs: Any) -> Any:
        raise SSRFValidationError("cannot resolve host", transient=True)

    monkeypatch.setattr(agent_resolver, "async_default_jwks_fetcher", unresolvable)

    with pytest.raises(AgentResolverError) as caught:
        await agent_resolver.async_resolve_agent(
            _AGENT,
            agent_type="sales",
            _brand_jwks_client_factory=_brand_json_client_factory,
        )

    assert caught.value.code == "jwks_fetch_failed"
    assert request_signature_code(caught.value) == REQUEST_SIGNATURE_JWKS_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transient", "spec_code"),
    [
        (False, REQUEST_SIGNATURE_JWKS_UNTRUSTED),
        (True, REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE),
    ],
    ids=["refused", "unresolvable"],
)
async def test_ssrf_refused_capabilities_destination(
    monkeypatch: pytest.MonkeyPatch, transient: bool, spec_code: str
) -> None:
    """``agent_url`` resolving to a refused address is terminal, like the JWKS hop.

    A literal reserved address is already refused before the fetch, as
    ``invalid_agent_url``; a hostname that resolves to one is refused when the
    pinned transport is built, and must report the same terminal code.
    """

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise SSRFValidationError("resolves to a private address", transient=transient)

    monkeypatch.setattr(agent_resolver, "build_async_ip_pinned_transport", refuse)

    with pytest.raises(AgentResolverError) as caught:
        await agent_resolver.async_resolve_agent(_AGENT, agent_type="sales")

    assert caught.value.code == "capabilities_unreachable"
    assert request_signature_code(caught.value) == spec_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transient", "spec_code"),
    [
        (False, REQUEST_SIGNATURE_JWKS_UNTRUSTED),
        (True, REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE),
    ],
    ids=["refused", "unresolvable"],
)
async def test_ssrf_refused_brand_json_destination(
    monkeypatch: pytest.MonkeyPatch,
    _resolver_reaching_the_jwks_hop: None,
    transient: bool,
    spec_code: str,
) -> None:
    """The brand.json resolver reports an SSRF refusal as ``fetch_failed``.

    That resolver code's row is the transient ``brand_json_unreachable``; a
    refused destination is terminal on this hop too.
    """
    from adcp.signing import ip_pinned_transport

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise SSRFValidationError("resolves to a private address", transient=transient)

    # brand_jwks imports the builder at call time, so patch its home module.
    monkeypatch.setattr(ip_pinned_transport, "build_async_ip_pinned_transport", refuse)

    with pytest.raises(AgentResolverError) as caught:
        await agent_resolver.async_resolve_agent(_AGENT, agent_type="sales")

    assert caught.value.code == "brand_json_resolution_failed"
    assert request_signature_code(caught.value) == spec_code
