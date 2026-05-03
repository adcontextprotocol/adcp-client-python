"""Agent URL → signing-key resolution via identity.brand_json_url.

Implements §"Discovering an agent's signing keys via brand_json_url" from
security.mdx (8-step algorithm). Canonical entry-point: async_resolve_agent.
Sync convenience: resolve_agent. Composed verifier: verify_from_agent_url.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import replace as _replace
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field

from adcp.signing.brand_jwks import BrandJsonResolverError, _default_jwks_uri, _fetch_brand_json
from adcp.signing.capability_priming import _unwrap_response
from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport
from adcp.signing.jwks import SSRFValidationError, async_default_jwks_fetcher

if TYPE_CHECKING:
    from adcp.signing.verifier import VerifiedSigner, VerifyOptions

# Body caps (per design decisions in issue #344)
_CAPABILITIES_BODY_CAP = 64 * 1024  # 64 KiB
_CONNECT_TIMEOUT = 5.0
_TOTAL_TIMEOUT = 10.0

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

#: Test seam: factory that accepts a URL and returns an async context manager
#: wrapping an httpx.AsyncClient. Production path uses IP-pinned transport;
#: tests inject a factory wired to a mock transport.
_ClientFactory = Callable[[str], AbstractAsyncContextManager[httpx.AsyncClient]]

AgentResolverErrorCode = Literal[
    "invalid_url",
    "capability_fetch_failed",
    "brand_json_url_missing",
    "brand_json_origin_mismatch",
    "brand_json_fetch_failed",
    "brand_json_agent_not_found",
    "jwks_fetch_failed",
    "ssrf_blocked",
]


class AgentResolverError(Exception):
    """Raised by async_resolve_agent / resolve_agent on any failure."""

    def __init__(self, code: AgentResolverErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code: AgentResolverErrorCode = code
        self.detail: str = detail


class AgentResolutionFreshness(BaseModel):
    """Cache metadata captured at resolution time."""

    fetched_at: float
    cache_control: str | None = None


class AgentResolutionHop(BaseModel):
    """One step in the resolution trace (capabilities / brand_json / jwks)."""

    label: str
    url: str
    status: int | None = None
    elapsed_ms: float | None = None


class AgentResolution(BaseModel):
    """Result of a successful agent URL → signing-key walk.

    Fields are grounded in observable wire state per the design decisions in
    adcp-client-python#344. SDK-invented terms (identity_posture, consistency)
    are absent — ``--json`` output is cross-SDK interop surface.
    """

    agent_url: str
    brand_json_url: str
    agent_entry: dict[str, Any]
    jwks_uri: str
    jwks: dict[str, Any]
    freshness: AgentResolutionFreshness
    trace: list[AgentResolutionHop] = Field(default_factory=list)


async def async_resolve_agent(
    agent_url: str,
    *,
    allow_private: bool = False,
    capabilities_body_cap: int = _CAPABILITIES_BODY_CAP,
    connect_timeout: float = _CONNECT_TIMEOUT,
    total_timeout: float = _TOTAL_TIMEOUT,
    _client_factory: _ClientFactory | None = None,
) -> AgentResolution:
    """Resolve an agent URL to its signing keys via brand_json_url.

    Steps (per security.mdx §"Discovering an agent's signing keys"):
    1. Fetch get_adcp_capabilities with a SSRF-pinned transport — does NOT
       route through ADCPClient (threat model differs for attacker-supplied URLs).
    2. Extract identity.brand_json_url from the response.
    3. Fetch + walk brand.json to find the agent entry matching agent_url.
    4. Fetch JWKS from the agent entry's jwks_uri.

    ``_client_factory`` is a test seam; leave None in production.
    """
    trace: list[AgentResolutionHop] = []

    # Steps 1 + 2: capabilities fetch → brand_json_url
    raw_caps, caps_hop = await _fetch_capabilities(
        agent_url,
        allow_private=allow_private,
        body_cap=capabilities_body_cap,
        connect_timeout=connect_timeout,
        total_timeout=total_timeout,
        client_factory=_client_factory,
    )
    trace.append(caps_hop)

    # raw_caps is the outer JSON-RPC body; extract the tool result first so
    # _unwrap_response sees the MCP CallToolResult (structuredContent / content[])
    # rather than the JSON-RPC envelope.
    tool_result: Any = raw_caps
    if isinstance(raw_caps, dict) and isinstance(raw_caps.get("result"), dict):
        tool_result = raw_caps["result"]

    payload = _unwrap_response(tool_result)
    if not isinstance(payload, dict):
        raise AgentResolverError(
            "brand_json_url_missing",
            "get_adcp_capabilities response is not a dict",
        )
    identity = payload.get("identity")
    brand_json_url: str | None = (
        identity.get("brand_json_url") if isinstance(identity, dict) else None
    )
    if not isinstance(brand_json_url, str) or not brand_json_url:
        raise AgentResolverError(
            "brand_json_url_missing",
            "get_adcp_capabilities response has no identity.brand_json_url",
        )

    # Domain-binding guard: brand_json_url must be same-origin or parent-domain
    # of agent_url so a compromised agent cannot redirect key discovery to an
    # attacker-controlled public host (SSRF validation only blocks private IPs).
    _validate_brand_json_origin(brand_json_url, agent_url)

    # Step 3: fetch brand.json and locate the agent entry
    try:
        fetched = await _fetch_brand_json(
            start_url=brand_json_url,
            current_etag=None,
            max_redirects=3,
            allow_private=allow_private,
            timeout_seconds=total_timeout,
            max_body_bytes=256 * 1024,
            client_factory=_client_factory,
        )
    except BrandJsonResolverError as exc:
        raise AgentResolverError("brand_json_fetch_failed", str(exc)) from exc

    trace.append(
        AgentResolutionHop(
            label="brand_json",
            url=fetched.final_url,
            status=200 if fetched.status == "ok" else 304,
        )
    )

    if fetched.data is None:
        raise AgentResolverError("brand_json_fetch_failed", "brand.json response missing body")

    agent_entry, jwks_uri = _find_agent_by_url(fetched.data, agent_url, fetched.final_url)

    # Step 4: fetch JWKS (async_default_jwks_fetcher already uses IP-pinned transport)
    t0 = time.monotonic()
    try:
        jwks_data = await async_default_jwks_fetcher(jwks_uri, allow_private=allow_private)
    except SSRFValidationError as exc:
        raise AgentResolverError("ssrf_blocked", f"jwks_uri SSRF check failed: {exc}") from exc
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise AgentResolverError("jwks_fetch_failed", f"JWKS fetch failed: {exc}") from exc
    elapsed = (time.monotonic() - t0) * 1000
    trace.append(AgentResolutionHop(label="jwks", url=jwks_uri, elapsed_ms=round(elapsed, 1)))

    return AgentResolution(
        agent_url=agent_url,
        brand_json_url=brand_json_url,
        agent_entry=agent_entry,
        jwks_uri=jwks_uri,
        jwks=jwks_data,
        freshness=AgentResolutionFreshness(
            fetched_at=time.time(),
            cache_control=fetched.cache_control,
        ),
        trace=trace,
    )


def resolve_agent(agent_url: str, **kwargs: Any) -> AgentResolution:
    """Sync convenience wrapper around async_resolve_agent.

    Library code that runs on an asyncio event loop should call
    async_resolve_agent directly to avoid blocking the loop.
    """
    return asyncio.run(async_resolve_agent(agent_url, **kwargs))


async def verify_from_agent_url(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    agent_url: str,
    options: VerifyOptions,
    _client_factory: _ClientFactory | None = None,
) -> VerifiedSigner:
    """Resolve agent keys then verify the request signature.

    Composes async_resolve_agent with verify_request_signature:
    1. Walks the brand_json_url chain to build a JWKS snapshot.
    2. Injects a StaticJwksResolver backed by that snapshot into ``options``.
    3. Calls verify_request_signature with the updated options.

    Raises AgentResolverError on resolution failure, SignatureVerificationError
    on signature failure.

    ``_client_factory`` is a test seam forwarded to async_resolve_agent;
    leave None in production.
    """
    from adcp.signing.jwks import StaticJwksResolver
    from adcp.signing.verifier import verify_request_signature

    resolution = await async_resolve_agent(agent_url, _client_factory=_client_factory)
    jwks_resolver = StaticJwksResolver(resolution.jwks)
    pinned = _replace(options, jwks_resolver=jwks_resolver)
    return verify_request_signature(
        method=method, url=url, headers=headers, body=body, options=pinned
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_brand_json_origin(brand_json_url: str, agent_url: str) -> None:
    """Reject brand_json_url values not same-origin or parent-domain of agent_url.

    A compromised agent can advertise any brand_json_url; without this guard
    it could redirect key discovery to an attacker-controlled public host.
    SSRF validation on hop 2 blocks private IPs but not public attacker domains.

    Accepted relationships (brand_host → agent_host examples):
      example.com → buyer.example.com  (agent is a subdomain of brand domain)
      buyer.example.com → buyer.example.com  (exact match)

    Rejected (cross-domain trust pivot):
      evil.com → buyer.example.com

    Cross-subdomain cases (brand.example.com for buyer.example.com) require
    the [identity] tldextract extra for eTLD+1 comparison; those are gated
    on Tier-3 work tracked in adcp#3690.
    """
    try:
        brand_parts = urlsplit(brand_json_url)
        agent_parts = urlsplit(agent_url)
    except ValueError as exc:
        raise AgentResolverError("invalid_url", f"invalid URL in origin check: {exc}") from exc

    if brand_parts.scheme != "https":
        raise AgentResolverError(
            "brand_json_origin_mismatch",
            f"brand_json_url must use HTTPS (got scheme {brand_parts.scheme!r})",
        )

    brand_host = (brand_parts.hostname or "").lower()
    agent_host = (agent_parts.hostname or "").lower()

    if not brand_host:
        raise AgentResolverError("brand_json_origin_mismatch", "brand_json_url has no host")

    if brand_host == agent_host or agent_host.endswith("." + brand_host):
        return

    raise AgentResolverError(
        "brand_json_origin_mismatch",
        f"brand_json_url host ({brand_host!r}) must be the same as or a parent "
        f"domain of agent_url host ({agent_host!r}); use the [identity] extra "
        "for cross-subdomain brand.json (e.g. brand.acme.com for buyer.acme.com)",
    )


async def _fetch_capabilities(
    agent_url: str,
    *,
    allow_private: bool,
    body_cap: int,
    connect_timeout: float,
    total_timeout: float,
    client_factory: _ClientFactory | None,
) -> tuple[dict[str, Any], AgentResolutionHop]:
    """SSRF-pinned MCP tools/call fetch of get_adcp_capabilities."""
    mcp_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_adcp_capabilities", "arguments": {}},
    }
    t0 = time.monotonic()

    if client_factory is not None:
        client_cm: AbstractAsyncContextManager[httpx.AsyncClient] = client_factory(agent_url)
    else:
        try:
            transport = build_async_ip_pinned_transport(agent_url, allow_private=allow_private)
        except SSRFValidationError as exc:
            raise AgentResolverError(
                "ssrf_blocked", f"agent_url SSRF check failed: {exc}"
            ) from exc
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=total_timeout,
            write=total_timeout,
            pool=total_timeout,
        )
        client_cm = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    try:
        async with client_cm as client:
            try:
                response = await client.post(
                    agent_url,
                    json=mcp_body,
                    headers={"Content-Type": "application/json"},
                )
            except SSRFValidationError as exc:
                raise AgentResolverError(
                    "ssrf_blocked", f"agent_url SSRF check failed: {exc}"
                ) from exc
            except (httpx.HTTPError, OSError) as exc:
                raise AgentResolverError(
                    "capability_fetch_failed", f"get_adcp_capabilities failed: {exc}"
                ) from exc

            elapsed = (time.monotonic() - t0) * 1000
            hop = AgentResolutionHop(
                label="capabilities",
                url=agent_url,
                status=response.status_code,
                elapsed_ms=round(elapsed, 1),
            )

            if response.status_code != 200:
                raise AgentResolverError(
                    "capability_fetch_failed",
                    f"get_adcp_capabilities returned HTTP {response.status_code}",
                )

            body_bytes = response.content
            if len(body_bytes) > body_cap:
                raise AgentResolverError(
                    "capability_fetch_failed",
                    f"get_adcp_capabilities response exceeds {body_cap} bytes",
                )

            try:
                data: dict[str, Any] = response.json()
            except (ValueError, httpx.DecodingError) as exc:
                raise AgentResolverError(
                    "capability_fetch_failed",
                    "get_adcp_capabilities response is not valid JSON",
                ) from exc
    except AgentResolverError:
        raise

    return data, hop


def _norm_url(raw: str) -> str:
    """Normalize a URL for agent-entry matching (lowercase scheme+host, strip default port)."""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def _find_agent_by_url(
    data: dict[str, Any],
    agent_url: str,
    final_brand_url: str,
) -> tuple[dict[str, Any], str]:
    """Find the brand.json agent entry whose url matches agent_url.

    Walks the same structure as _select_agent in brand_jwks: portfolio
    brands[] first, then house.agents[].  Top-level agents[] is used when
    there is no ``house`` key (flat brand.json).
    """
    target = _norm_url(agent_url)

    def _search(agents: Any) -> tuple[dict[str, Any], str] | None:
        if not isinstance(agents, list):
            return None
        for entry in agents:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str):
                continue
            if _norm_url(url) != target:
                continue
            jwks_uri_raw = entry.get("jwks_uri")
            if isinstance(jwks_uri_raw, str):
                return entry, jwks_uri_raw
            # Fallback: spec default /.well-known/jwks.json on the agent origin.
            # _default_jwks_uri enforces that agent.url and brand.json share an
            # origin, preventing a malicious brand.json from directing JWKS fetch
            # to an attacker-controlled host (cross-origin trust pivot).
            try:
                return entry, _default_jwks_uri(url, final_brand_url)
            except BrandJsonResolverError as exc:
                raise AgentResolverError("brand_json_agent_not_found", str(exc)) from exc
        return None

    house = data.get("house")
    if isinstance(house, dict):
        brands = data.get("brands")
        if isinstance(brands, list):
            for brand in brands:
                if isinstance(brand, dict):
                    result = _search(brand.get("agents"))
                    if result is not None:
                        return result
        result = _search(house.get("agents"))
        if result is not None:
            return result
    else:
        result = _search(data.get("agents"))
        if result is not None:
            return result

    raise AgentResolverError(
        "brand_json_agent_not_found",
        f"brand.json has no agent entry matching {agent_url!r}",
    )


__all__ = [
    "AgentResolution",
    "AgentResolutionFreshness",
    "AgentResolutionHop",
    "AgentResolverError",
    "AgentResolverErrorCode",
    "async_resolve_agent",
    "resolve_agent",
    "verify_from_agent_url",
]
