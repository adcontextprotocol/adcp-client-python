from __future__ import annotations

"""
Utilities for fetching, parsing, and validating adagents.json files per the AdCP specification.

Publishers declare authorized sales agents via adagents.json files hosted at
https://{publisher_domain}/.well-known/adagents.json. This module provides utilities
for sales agents to verify they are authorized for specific properties.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx
from pydantic import Field

from adcp.exceptions import AdagentsNotFoundError, AdagentsTimeoutError, AdagentsValidationError
from adcp.types.base import AdCPBaseModel
from adcp.validation import ValidationError, validate_adagents

logger = logging.getLogger(__name__)

DiscoveryMethod = Literal["direct", "authoritative_location", "ads_txt_managerdomain"]


# authorization_type discriminator -> required selector field, per the AdCP
# adagents.json JSON Schema (every authorized_agents entry must satisfy one
# of the oneOf variants with the matching selector array).
_AUTHORIZATION_TYPE_TO_SELECTOR: dict[str, str] = {
    "property_ids": "property_ids",
    "property_tags": "property_tags",
    "inline_properties": "properties",
    "publisher_properties": "publisher_properties",
    "signal_ids": "signal_ids",
    "signal_tags": "signal_tags",
}

EntryErrorKind = Literal[
    "missing_url",
    "missing_authorized_for",
    "missing_authorization_type",
    "unknown_authorization_type",
    "missing_selector_for_type",
    "not_an_object",
    "empty_authorized_agents",
]


@dataclass(frozen=True)
class AdagentsEntryError:
    """A single schema violation found in an adagents.json file.

    ``kind`` is a stable string literal callers can branch on (e.g.,
    distinguish a publisher who shipped bare entries from one who picked
    an unknown authorization_type). ``message`` is developer-facing and
    its wording may change between releases — pattern-match on ``kind``
    when surfacing publisher-facing diagnostics.

    For file-level errors (e.g., ``empty_authorized_agents``) ``index``
    is ``-1`` and ``url`` is ``None``.
    """

    index: int
    kind: EntryErrorKind
    message: str
    url: str | None = None


@dataclass(frozen=True)
class AdagentsValidationReport:
    """Result of structurally validating a parsed adagents.json.

    Distinguishes the two failure modes that
    :func:`get_properties_by_agent` collapses into an empty list:
    a schema-invalid file (``schema_valid`` is False, ``errors`` populated)
    versus a valid file that simply doesn't list the caller's agent.

    ``authorized_agents_count`` and ``properties_count`` reflect the
    array lengths as observed in the input — they are reported regardless
    of ``schema_valid`` so callers can show "0 agents listed" diagnostics
    on partially-broken files.

    ``is_reference`` is True for the URL-reference variant of the schema
    (an ``authoritative_location`` pointer with no inline
    ``authorized_agents`` array). Callers that received a report with
    ``is_reference=True`` should follow the redirect (e.g., via
    :func:`fetch_adagents`) and validate the resolved file. This flag
    lets callers distinguish a legitimate URL-reference file from an
    inline file that happens to have zero entries (which is itself
    invalid per the schema's ``minItems: 1`` constraint on
    ``authorized_agents``).
    """

    schema_valid: bool
    errors: list[AdagentsEntryError]
    authorized_agents_count: int
    properties_count: int
    is_reference: bool = False


@dataclass
class AdAgentsValidationResult:
    """Result of discovering and validating a publisher's adagents.json.

    ``discovery_method`` records which path produced ``data``:
    ``direct`` for ``/.well-known/adagents.json`` on the publisher,
    ``authoritative_location`` for a URL-reference redirect, and
    ``ads_txt_managerdomain`` for the one-hop ads.txt MANAGERDOMAIN
    fallback (RFC 4175). ``manager_domain`` is set only on the
    managerdomain path.
    """

    domain: str
    url: str
    discovery_method: DiscoveryMethod = "direct"
    manager_domain: str | None = None
    data: dict[str, Any] | None = None
    valid: bool = False
    errors: list[str] = field(default_factory=list)


_MANAGERDOMAIN_RE = re.compile(
    r"^\s*managerdomain\s*=\s*([A-Za-z0-9.\-]+)\s*(?:#.*)?$",
    re.IGNORECASE,
)


def _parse_managerdomains(ads_txt_content: str) -> list[str]:
    """Extract MANAGERDOMAIN= directives from ads.txt content.

    Per RFC 4175 / IAB ads.txt: only directive-form lines
    (``MANAGERDOMAIN=value``) count — pure comment lines beginning with
    ``#`` are rejected. Order in source is preserved so callers can
    apply last-wins.
    """
    managers: list[str] = []
    for line in ads_txt_content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        match = _MANAGERDOMAIN_RE.match(line)
        if match:
            managers.append(match.group(1).lower())
    return managers


def _normalize_domain(domain: str) -> str:
    """Normalize domain for comparison - strip, lowercase, remove trailing dots/slashes.

    Args:
        domain: Domain to normalize

    Returns:
        Normalized domain string

    Raises:
        AdagentsValidationError: If domain contains invalid patterns
    """
    domain = domain.strip().lower()
    # Remove both trailing slashes and dots iteratively
    while domain.endswith("/") or domain.endswith("."):
        domain = domain.rstrip("/").rstrip(".")

    # Check for invalid patterns
    if not domain or ".." in domain:
        raise AdagentsValidationError(f"Invalid domain format: {domain!r}")

    return domain


# Hostnames that resolve to cloud metadata services or local-only namespaces.
# `.internal` is the GCP convention; `.local` is RFC 6762 mDNS.
_INTERNAL_HOSTNAMES: frozenset[str] = frozenset(
    {"localhost", "localhost.localdomain", "metadata.google.internal"}
)


def _check_safe_host(hostname: str, context: str) -> None:
    """Reject hostnames that target loopback, link-local, private, or metadata services.

    Used for every outbound HTTP target derived from publisher-controlled
    input (publisher_domain, authoritative_location, MANAGERDOMAIN). This
    is a string-level gate — it catches IP literals and well-known
    private hostnames, but does not pin DNS resolution. A hostile DNS
    server that returns a public IP on first lookup and a private IP on
    connect (DNS rebinding) is out of scope; see security follow-up.
    """
    if not hostname:
        raise AdagentsValidationError(f"{context} must have a hostname")
    if hostname in _INTERNAL_HOSTNAMES or hostname.endswith(".local"):
        raise AdagentsValidationError(f"{context} must not target localhost or internal hostnames")
    if hostname.endswith(".internal"):
        raise AdagentsValidationError(f"{context} must not target an .internal hostname")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise AdagentsValidationError(f"{context} must not target a private/reserved address")


# Reserved TLDs (RFC 6761) and second-level domains (RFC 2606) that never
# resolve in the public DNS. Tests use these consistently; skipping the
# resolve gate on them saves every test from having to mock
# socket.getaddrinfo while still applying the gate to real-world hostnames.
_NON_RESOLVING_SUFFIXES: tuple[str, ...] = (
    ".example",
    ".test",
    ".invalid",
    ".localhost",
    ".example.com",
    ".example.net",
    ".example.org",
)
_NON_RESOLVING_HOSTS: frozenset[str] = frozenset({"example.com", "example.net", "example.org"})


async def _dns_validate_host(host: str, port: int) -> None:
    """Resolve a hostname once and gate every returned address.

    Closes the common SSRF path where a public-looking hostname's DNS
    points at a private address (e.g. ``metadata.example.com`` →
    ``169.254.169.254``). ``_check_safe_host`` only sees the string;
    this helper sees what the resolver returns.

    Residual rebinding window: a determined attacker controlling the
    authoritative DNS can still return a public IP on this lookup and
    a private IP on httpx's connect lookup milliseconds later. Closing
    that window requires intercepting httpx's network backend to pin
    the connection IP — tracked separately.
    """
    if not host:
        return
    try:
        ipaddress.ip_address(host)
        return  # IP literal — already gated by _check_safe_host
    except ValueError:
        pass
    if host in _NON_RESOLVING_HOSTS or host.endswith(_NON_RESOLVING_SUFFIXES):
        return
    loop = asyncio.get_running_loop()
    try:
        # Run via executor (not loop.getaddrinfo) so tests can mock
        # socket.getaddrinfo at the C-level entry point — patching the
        # concrete event loop's bound method doesn't intercept reliably.
        infos = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        )
    except socket.gaierror as e:
        raise AdagentsValidationError(f"DNS resolution failed for {host!r}: {e}") from e
    for info in infos:
        addr = info[4][0]
        if isinstance(addr, str):
            _check_safe_host(addr, "resolved address")


def _validate_publisher_domain(domain: str) -> str:
    """Validate and sanitize publisher domain for security.

    Args:
        domain: Publisher domain to validate

    Returns:
        Validated and normalized domain

    Raises:
        AdagentsValidationError: If domain is invalid or contains suspicious characters
    """
    # Check for suspicious characters BEFORE stripping (to catch injection attempts)
    suspicious_chars = ["\\", "@", "\n", "\r", "\t"]
    for char in suspicious_chars:
        if char in domain:
            raise AdagentsValidationError(f"Invalid character in publisher domain: {char!r}")

    domain = domain.strip()

    # Check basic constraints
    if not domain:
        raise AdagentsValidationError("Publisher domain cannot be empty")
    if len(domain) > 253:  # DNS maximum length
        raise AdagentsValidationError(f"Publisher domain too long: {len(domain)} chars (max 253)")

    # Check for spaces after stripping leading/trailing whitespace
    if " " in domain:
        raise AdagentsValidationError("Invalid character in publisher domain: ' '")

    # Remove protocol if present (common user error) - do this BEFORE checking for slashes
    if "://" in domain:
        domain = domain.split("://", 1)[1]

    # Remove path if present (should only be domain) - do this BEFORE checking for slashes
    if "/" in domain:
        domain = domain.split("/", 1)[0]

    # Normalize
    domain = _normalize_domain(domain)

    # Final validation - must look like a domain
    if "." not in domain:
        raise AdagentsValidationError(f"Publisher domain must contain at least one dot: {domain!r}")

    # SSRF gate: reject IP literals and internal hostnames.
    _check_safe_host(domain, "publisher_domain")

    return domain


def _validate_redirect_url(url: str) -> None:
    """Validate an authoritative_location URL is safe to follow.

    Rejects private/reserved IP addresses and localhost to prevent SSRF attacks
    where a malicious publisher redirects the SDK to internal services.

    Args:
        url: The HTTPS URL to validate

    Raises:
        AdagentsValidationError: If the URL targets a private/reserved address
    """
    parsed = urlparse(url)
    _check_safe_host(parsed.hostname or "", "authoritative_location")


def normalize_url(url: str) -> str:
    """Normalize URL by removing protocol and trailing slash.

    Args:
        url: URL to normalize

    Returns:
        Normalized URL (domain/path without protocol or trailing slash)
    """
    parsed = urlparse(url)
    normalized = parsed.netloc + parsed.path
    return normalized.rstrip("/")


def domain_matches(property_domain: str, agent_domain_pattern: str) -> bool:
    """Check if domains match per AdCP rules.

    Rules:
    - Exact match always succeeds
    - 'example.com' matches www.example.com, m.example.com (common subdomains)
    - 'subdomain.example.com' matches that specific subdomain only
    - '*.example.com' matches all subdomains

    Args:
        property_domain: Domain from property
        agent_domain_pattern: Domain pattern from adagents.json

    Returns:
        True if domains match per AdCP rules
    """
    # Normalize both domains for comparison
    try:
        property_domain = _normalize_domain(property_domain)
        agent_domain_pattern = _normalize_domain(agent_domain_pattern)
    except AdagentsValidationError:
        # Invalid domain format - no match
        return False

    # Exact match
    if property_domain == agent_domain_pattern:
        return True

    # Wildcard pattern (*.example.com)
    if agent_domain_pattern.startswith("*."):
        base_domain = agent_domain_pattern[2:]
        return property_domain.endswith(f".{base_domain}")

    # Bare domain matches common subdomains (www, m)
    # If agent pattern is a bare domain (no subdomain), match www/m subdomains
    if "." in agent_domain_pattern and not agent_domain_pattern.startswith("www."):
        # Check if this looks like a bare domain (e.g., example.com)
        parts = agent_domain_pattern.split(".")
        if len(parts) == 2:  # Looks like bare domain
            common_subdomains = ["www", "m"]
            for subdomain in common_subdomains:
                if property_domain == f"{subdomain}.{agent_domain_pattern}":
                    return True

    return False


def identifiers_match(
    property_identifiers: list[dict[str, str]],
    agent_identifiers: list[dict[str, str]],
) -> bool:
    """Check if any property identifier matches agent's authorized identifiers.

    Args:
        property_identifiers: Identifiers from property
            (e.g., [{"type": "domain", "value": "cnn.com"}])
        agent_identifiers: Identifiers from adagents.json

    Returns:
        True if any identifier matches

    Notes:
        - Domain identifiers use AdCP domain matching rules
        - Other identifiers (bundle_id, roku_store_id, etc.) require exact match
    """
    for prop_id in property_identifiers:
        prop_type = prop_id.get("type", "")
        prop_value = prop_id.get("value", "")

        for agent_id in agent_identifiers:
            agent_type = agent_id.get("type", "")
            agent_value = agent_id.get("value", "")

            # Type must match
            if prop_type != agent_type:
                continue

            # Domain identifiers use special matching rules
            if prop_type == "domain":
                if domain_matches(prop_value, agent_value):
                    return True
            else:
                # Other identifier types require exact match
                if prop_value == agent_value:
                    return True

    return False


def verify_agent_authorization(
    adagents_data: dict[str, Any],
    agent_url: str,
    property_type: str | None = None,
    property_identifiers: list[dict[str, str]] | None = None,
) -> bool:
    """Check if agent is authorized for a property.

    Args:
        adagents_data: Parsed adagents.json data
        agent_url: URL of the sales agent to verify
        property_type: Type of property (website, app, etc.) - optional
        property_identifiers: List of identifiers to match - optional

    Returns:
        True if agent is authorized, False otherwise

    Raises:
        AdagentsValidationError: If adagents_data is malformed

    Notes:
        - If property_type/identifiers are None, checks if agent is authorized
          for ANY property on this domain
        - Implements AdCP domain matching rules
        - Agent URLs are matched ignoring protocol and trailing slash
    """
    # Validate structure
    if not isinstance(adagents_data, dict):
        raise AdagentsValidationError("adagents_data must be a dictionary")

    authorized_agents = adagents_data.get("authorized_agents")
    if not isinstance(authorized_agents, list):
        raise AdagentsValidationError("adagents.json must have 'authorized_agents' array")

    # Normalize the agent URL for comparison
    normalized_agent_url = normalize_url(agent_url)

    # Check each authorized agent
    for agent in authorized_agents:
        if not isinstance(agent, dict):
            continue

        agent_url_from_json = agent.get("url", "")
        if not agent_url_from_json:
            continue

        # Match agent URL (protocol-agnostic)
        if normalize_url(agent_url_from_json) != normalized_agent_url:
            continue

        # Found matching agent - now check properties
        properties = agent.get("properties")

        # If properties field is missing or empty, agent is authorized for all properties
        if properties is None or (isinstance(properties, list) and len(properties) == 0):
            return True

        # If no property filters specified, we found the agent - authorized
        if property_type is None and property_identifiers is None:
            return True

        # Check specific property authorization
        if isinstance(properties, list):
            for prop in properties:
                if not isinstance(prop, dict):
                    continue

                # Check property type if specified
                if property_type is not None:
                    prop_type = prop.get("property_type", "")
                    if prop_type != property_type:
                        continue

                # Check identifiers if specified
                if property_identifiers is not None:
                    prop_identifiers = prop.get("identifiers", [])
                    if not isinstance(prop_identifiers, list):
                        continue

                    if identifiers_match(property_identifiers, prop_identifiers):
                        return True
                else:
                    # Property type matched and no identifier check needed
                    return True

    return False


# Maximum number of authoritative_location redirects to follow
MAX_REDIRECT_DEPTH = 5

# Maximum size of a publisher's ads.txt file. IAB practice caps real
# ads.txt files in the low MB range; this gives plenty of headroom while
# preventing a hostile publisher from forcing the SDK to buffer an
# arbitrarily large body during the MANAGERDOMAIN fallback.
MAX_ADS_TXT_BYTES = 1_048_576  # 1 MiB

# Two-tier size caps for adagents.json fetches (adcp#4504). The pointer
# file served at /.well-known/adagents.json is small (URL reference or
# small inline file); the dereferenced authoritative file behind
# ``authoritative_location`` can be much larger for managed networks
# enumerating thousands of publishers, so a higher cap applies on the
# second hop.
MAX_POINTER_BYTES = 5 * 1024 * 1024  # 5 MiB — first hop SSRF cap
MAX_AUTHORITATIVE_BYTES = 20 * 1024 * 1024  # 20 MiB — second hop


@dataclass(frozen=True)
class AdagentsCacheEntry:
    """Conditional-refresh cache state for an adagents.json URL.

    Pass an entry into :func:`fetch_adagents_with_cache` to send
    ``If-None-Match`` (preferred) and ``If-Modified-Since`` validators
    on the next fetch. A 304 from the publisher is treated as a
    successful cache-lifetime refresh — the ``body`` is returned
    unchanged with refreshed timing, per the adcp#4504 fetch contract.
    """

    body: dict[str, Any]
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class AdagentsFetchResult:
    """Result of a fetch, including refreshed cache validators.

    ``not_modified`` is True when the server returned 304 and ``data``
    came from the supplied cache entry. ``etag`` / ``last_modified`` are
    the validators to persist for the next fetch — on 304 they come
    from the 304 response headers if present, falling back to the
    supplied entry's values.
    """

    data: dict[str, Any]
    discovery_method: DiscoveryMethod
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


async def _resolve_direct(
    publisher_domain: str,
    timeout: float,
    user_agent: str,
    client: httpx.AsyncClient | None,
    cache_entry: AdagentsCacheEntry | None = None,
) -> tuple[dict[str, Any], DiscoveryMethod, str | None, str | None, bool]:
    """Direct fetch with authoritative_location redirect following.

    Returns ``(data, discovery_method, etag, last_modified, not_modified)``.
    ``discovery_method`` is ``'direct'`` if no redirect was followed,
    ``'authoritative_location'`` otherwise. The cache validators come
    from the hop that produced ``data`` (the authoritative file when
    redirected, the publisher otherwise). Raises
    :class:`AdagentsNotFoundError` on 404 so callers can attempt the
    ads.txt MANAGERDOMAIN fallback.

    The first hop uses :data:`MAX_POINTER_BYTES` (5 MiB) and any
    dereferenced ``authoritative_location`` hop uses
    :data:`MAX_AUTHORITATIVE_BYTES` (20 MiB) per adcp#4504.
    """
    url = f"https://{publisher_domain}/.well-known/adagents.json"
    visited_urls: set[str] = set()
    is_redirect = False

    for depth in range(MAX_REDIRECT_DEPTH + 1):
        if url in visited_urls:
            raise AdagentsValidationError(
                "Circular redirect detected in authoritative_location chain"
            )
        visited_urls.add(url)

        # Caller's client is only used on the initial publisher fetch; redirect
        # targets are third-party origins, so use a fresh client per hop.
        fetch_client = None if is_redirect else client
        max_bytes = MAX_AUTHORITATIVE_BYTES if is_redirect else MAX_POINTER_BYTES
        # Conditional refresh only applies to the hop that actually produced
        # the cached body. For an SDK-level cache, that's whichever hop the
        # caller fetched last. The simplest correct behavior is to apply the
        # validators on the first hop only — a 304 there short-circuits the
        # redirect chain. Pointer files rarely change anyway.
        hop_cache = cache_entry if not is_redirect else None

        try:
            data, etag, last_modified, not_modified = await _fetch_adagents_url(
                url,
                timeout,
                user_agent,
                fetch_client,
                max_bytes=max_bytes,
                cache_entry=hop_cache,
            )
        except AdagentsNotFoundError:
            # A 404 on a followed authoritative_location target is a broken
            # redirect chain, not a missing publisher manifest. Surface it as
            # a validation error so the MANAGERDOMAIN fallback (which keys off
            # the publisher's own 404) is not falsely triggered for what is
            # really an upstream pointer failure.
            if is_redirect:
                raise AdagentsValidationError(
                    f"authoritative_location target returned 404: {url}"
                ) from None
            raise

        if not_modified:
            # 304 is only ever returned on the first hop: hop_cache is None
            # on the redirected hop (see above), and _fetch_adagents_url
            # raises if a server returns 304 without a cache_entry. So the
            # discovery method here is always "direct".
            return data, "direct", etag, last_modified, True

        if "authoritative_location" in data and "authorized_agents" not in data:
            authoritative_url = data["authoritative_location"]

            if not isinstance(authoritative_url, str) or not authoritative_url.startswith(
                "https://"
            ):
                raise AdagentsValidationError(
                    f"authoritative_location must be an HTTPS URL, got: {authoritative_url!r}"
                )

            _validate_redirect_url(authoritative_url)

            if depth >= MAX_REDIRECT_DEPTH:
                raise AdagentsValidationError(
                    f"Maximum redirect depth ({MAX_REDIRECT_DEPTH}) exceeded"
                )

            url = authoritative_url
            is_redirect = True
            continue

        return (
            data,
            ("authoritative_location" if is_redirect else "direct"),
            etag,
            last_modified,
            False,
        )

    raise AssertionError("Unreachable")  # pragma: no cover


async def _fetch_ads_txt_managerdomains(
    publisher_domain: str,
    timeout: float,
    user_agent: str,
    client: httpx.AsyncClient | None,
) -> list[str]:
    """Fetch /ads.txt for publisher and return MANAGERDOMAIN= directives in order.

    Returns an empty list on any failure (non-200, network error, timeout,
    or oversized body) — the fallback is best-effort and absence is not
    an error. Bodies larger than :data:`MAX_ADS_TXT_BYTES` are discarded
    so a hostile publisher can't force the SDK to buffer arbitrary data.

    ``follow_redirects=False`` matches the adagents.json streaming path:
    HTTP 30x is not a sanctioned cross-host delegation mechanism for
    ads.txt either, and following one transparently would bypass the
    SSRF gate on the resolved Location host. Publishers who serve
    ads.txt behind a redirect will fall through to "no MANAGERDOMAIN
    found" — the SDK then surfaces the publisher's original 404,
    which is the correct outcome for a best-effort fallback.
    """
    url = f"https://{publisher_domain}/ads.txt"
    headers = {"User-Agent": user_agent, "Accept": "text/plain"}
    try:
        await _dns_validate_host(publisher_domain, 443)
    except AdagentsValidationError:
        return []
    try:
        if client is not None:
            response = await client.get(
                url, headers=headers, timeout=timeout, follow_redirects=False
            )
        else:
            async with httpx.AsyncClient() as new_client:
                response = await new_client.get(
                    url, headers=headers, timeout=timeout, follow_redirects=False
                )
        if response.status_code != 200:
            return []
        if len(response.content) > MAX_ADS_TXT_BYTES:
            return []
        return _parse_managerdomains(response.text)
    except (httpx.TimeoutException, httpx.RequestError):
        return []


def _ensure_safe_manager_domain(manager_domain: str) -> str | None:
    """Validate that a manager domain from publisher-controlled ads.txt is safe to fetch.

    Returns the normalized domain on success, ``None`` if the input is
    malformed or targets a private/reserved address. The ads.txt body is
    publisher-controlled, so this gate matters: without it a malicious
    publisher could declare ``MANAGERDOMAIN=169.254.169.254`` (AWS IMDS)
    or other internal addresses and force the SDK into an SSRF.
    """
    try:
        normalized = _validate_publisher_domain(manager_domain)
    except AdagentsValidationError:
        return None
    try:
        _validate_redirect_url(f"https://{normalized}/.well-known/adagents.json")
    except AdagentsValidationError:
        return None
    return normalized


async def fetch_adagents(
    publisher_domain: str,
    timeout: float = 10.0,
    user_agent: str = "AdCP-Client/1.0",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch and parse adagents.json from publisher domain.

    Discovery order:

    1. ``https://{publisher}/.well-known/adagents.json`` (direct).
    2. ``authoritative_location`` redirect, if the direct response is a
       URL reference.
    3. RFC 4175 ads.txt MANAGERDOMAIN fallback, on direct 404 only:
       fetches ``https://{publisher}/ads.txt`` for a
       ``MANAGERDOMAIN=`` directive and, if present, tries
       ``https://{manager}/.well-known/adagents.json``.

    The fallback is one-hop only. If the manager domain also 404s,
    this raises :class:`AdagentsNotFoundError` for the original
    publisher — not a silent pass.

    Args:
        publisher_domain: Domain hosting the adagents.json file.
        timeout: Request timeout in seconds.
        user_agent: User-Agent header for HTTP request.
        client: Optional httpx.AsyncClient for connection pooling.
            If provided, caller is responsible for client lifecycle.
            If None, a new client is created for this request.

    Returns:
        Parsed adagents.json data (resolved via authoritative_location
        or ads.txt MANAGERDOMAIN if applicable).

    Raises:
        AdagentsNotFoundError: If adagents.json was not found via any
            discovery path.
        AdagentsValidationError: If JSON is invalid, malformed, or
            redirects exceed maximum depth or form a loop.
        AdagentsTimeoutError: If request times out.

    Notes:
        For production use with multiple requests, pass a shared
        httpx.AsyncClient to enable connection pooling.

        Callers who need to know which discovery path produced the
        data (direct, authoritative_location, or ads_txt_managerdomain)
        should call :func:`validate_adagents_domain` instead.

        ``fetch_adagents`` performs only minimal structural checks. To
        report per-entry schema violations (e.g., bare entries missing
        ``authorization_type``) without raising, pass the returned data
        to :func:`validate_adagents_structure`.
    """
    publisher_domain = _validate_publisher_domain(publisher_domain)

    try:
        data, *_ = await _resolve_direct(publisher_domain, timeout, user_agent, client)
        return data
    except AdagentsNotFoundError:
        manager_data = await _try_managerdomain_fallback(
            publisher_domain, timeout, user_agent, client
        )
        if manager_data is not None:
            return manager_data
        raise


async def fetch_adagents_with_cache(
    publisher_domain: str,
    cache_entry: AdagentsCacheEntry | None = None,
    timeout: float = 10.0,
    user_agent: str = "AdCP-Client/1.0",
    client: httpx.AsyncClient | None = None,
) -> AdagentsFetchResult:
    """Fetch with conditional refresh — returns body plus refreshed validators.

    Pass the previous fetch's :class:`AdagentsCacheEntry` to send
    ``If-None-Match`` / ``If-Modified-Since`` on the next fetch. A 304
    from the publisher is treated as a successful refresh: the cached
    ``body`` is returned with ``not_modified=True``, satisfying the
    7-day cache window described in adcp#4504.

    The first hop (``/.well-known/adagents.json``) is capped at 5 MiB;
    a dereferenced ``authoritative_location`` file is capped at 20 MiB.
    Both caps fail closed — oversized responses raise
    :class:`AdagentsValidationError` rather than truncate.

    Does NOT perform the ads.txt ``managerdomain`` fallback; the
    fallback is best-effort discovery, not cache-aware refresh, and
    bypassing it on 304 keeps the path simple. Callers that need both
    behaviors should compose this helper with
    :func:`validate_adagents_domain`.
    """
    publisher_domain = _validate_publisher_domain(publisher_domain)
    data, discovery, etag, last_modified, not_modified = await _resolve_direct(
        publisher_domain, timeout, user_agent, client, cache_entry=cache_entry
    )
    return AdagentsFetchResult(
        data=data,
        discovery_method=discovery,
        etag=etag,
        last_modified=last_modified,
        not_modified=not_modified,
    )


async def _try_managerdomain_fallback(
    publisher_domain: str,
    timeout: float,
    user_agent: str,
    client: httpx.AsyncClient | None,
) -> dict[str, Any] | None:
    """One-hop ads.txt MANAGERDOMAIN fallback. Returns data on success.

    Returns None when no MANAGERDOMAIN is published, when the directive
    points back at the source publisher (cycle), or when the manager
    domain's adagents.json cannot be fetched. Callers translate ``None``
    into the publisher's original 404.
    """
    managers = await _fetch_ads_txt_managerdomains(publisher_domain, timeout, user_agent, client)
    if not managers:
        return None

    # Last-wins per IAB resolution (adcp#4173): later directive
    # overrides earlier ones in the same file.
    manager_domain = managers[-1]

    if manager_domain == publisher_domain:
        return None

    manager_domain_normalized = _ensure_safe_manager_domain(manager_domain)
    if manager_domain_normalized is None:
        return None

    try:
        # Manager domain is a different origin from the publisher; use a fresh
        # client rather than the caller's so credentials don't leak across origins.
        data, *_ = await _resolve_direct(
            manager_domain_normalized, timeout, user_agent, client=None
        )
        return data
    except (AdagentsNotFoundError, AdagentsValidationError, AdagentsTimeoutError):
        return None


async def validate_adagents_domain(
    publisher_domain: str,
    timeout: float = 10.0,
    user_agent: str = "AdCP-Client/1.0",
    client: httpx.AsyncClient | None = None,
) -> AdAgentsValidationResult:
    """Discover and validate a publisher's adagents.json with provenance.

    Mirrors :func:`fetch_adagents` discovery semantics but returns a
    typed :class:`AdAgentsValidationResult` exposing which path
    produced the data (``discovery_method``) and the manager domain
    used for the RFC 4175 fallback (``manager_domain``), if any.

    Errors are reported on the result rather than raised. A manager
    domain 404 is a terminal failure: ``valid`` is False and
    ``manager_domain`` is recorded for diagnostics.

    .. warning::

        When ``discovery_method == 'ads_txt_managerdomain'`` the data
        came from the manager, not the publisher. Callers wiring this
        into authorization decisions must verify that the source
        publisher is explicitly named in the manager's adagents.json
        (e.g., via ``publisher_properties.publisher_domain`` on the
        relevant authorized_agents entry) before trusting an agent
        claim — otherwise a manager that lists agent A unconditionally
        implicitly authorizes A for every publisher pointing
        MANAGERDOMAIN at the manager.
    """
    try:
        normalized = _validate_publisher_domain(publisher_domain)
    except AdagentsValidationError as e:
        return AdAgentsValidationResult(
            domain=publisher_domain,
            url="",
            errors=[str(e)],
        )

    url = f"https://{normalized}/.well-known/adagents.json"

    try:
        data, discovery, *_ = await _resolve_direct(normalized, timeout, user_agent, client)
        return AdAgentsValidationResult(
            domain=normalized,
            url=url,
            discovery_method=discovery,
            data=data,
            valid=True,
        )
    except AdagentsNotFoundError as direct_error:
        direct_error_msg = str(direct_error)
    except (AdagentsValidationError, AdagentsTimeoutError) as e:
        return AdAgentsValidationResult(
            domain=normalized,
            url=url,
            errors=[str(e)],
        )

    managers = await _fetch_ads_txt_managerdomains(normalized, timeout, user_agent, client)
    if not managers:
        return AdAgentsValidationResult(
            domain=normalized,
            url=url,
            errors=[direct_error_msg],
        )

    manager_domain = managers[-1]

    if manager_domain == normalized:
        return AdAgentsValidationResult(
            domain=normalized,
            url=url,
            errors=[
                direct_error_msg,
                f"ads.txt managerdomain {manager_domain} points back to source publisher",
            ],
        )

    manager_normalized = _ensure_safe_manager_domain(manager_domain)
    if manager_normalized is None:
        return AdAgentsValidationResult(
            domain=normalized,
            url=url,
            errors=[
                direct_error_msg,
                f"ads.txt managerdomain {manager_domain!r} is malformed or "
                "targets a private/reserved address",
            ],
        )

    try:
        manager_data, *_ = await _resolve_direct(
            manager_normalized, timeout, user_agent, client=None
        )
    except AdagentsNotFoundError:
        return AdAgentsValidationResult(
            domain=normalized,
            url=url,
            discovery_method="ads_txt_managerdomain",
            manager_domain=manager_normalized,
            errors=[
                direct_error_msg,
                f"manager domain {manager_normalized} did not serve adagents.json",
            ],
        )
    except (AdagentsValidationError, AdagentsTimeoutError) as e:
        return AdAgentsValidationResult(
            domain=normalized,
            url=url,
            discovery_method="ads_txt_managerdomain",
            manager_domain=manager_normalized,
            errors=[direct_error_msg, str(e)],
        )

    return AdAgentsValidationResult(
        domain=normalized,
        url=url,
        discovery_method="ads_txt_managerdomain",
        manager_domain=manager_normalized,
        data=manager_data,
        valid=True,
    )


async def _fetch_adagents_url(
    url: str,
    timeout: float,
    user_agent: str,
    client: httpx.AsyncClient | None,
    max_bytes: int = MAX_POINTER_BYTES,
    cache_entry: AdagentsCacheEntry | None = None,
) -> tuple[dict[str, Any], str | None, str | None, bool]:
    """Fetch and parse adagents.json from a specific URL.

    Returns a 4-tuple ``(data, etag, last_modified, not_modified)``.
    ``not_modified`` is True only when ``cache_entry`` was supplied and
    the origin responded with 304 — in that case ``data`` is the cached
    body. Response bodies larger than ``max_bytes`` are rejected (use
    :data:`MAX_POINTER_BYTES` for the first hop and
    :data:`MAX_AUTHORITATIVE_BYTES` for dereferenced authoritative files
    per adcp#4504).
    """
    headers: dict[str, str] = {"User-Agent": user_agent}
    if cache_entry is not None:
        if cache_entry.etag:
            headers["If-None-Match"] = cache_entry.etag
        if cache_entry.last_modified:
            headers["If-Modified-Since"] = cache_entry.last_modified

    parsed = urlparse(url)
    await _dns_validate_host(
        parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80)
    )

    try:
        if client is not None:
            body, status_code, response_headers = await _stream_capped(
                client, url, headers, timeout, max_bytes
            )
        else:
            async with httpx.AsyncClient() as new_client:
                body, status_code, response_headers = await _stream_capped(
                    new_client, url, headers, timeout, max_bytes
                )
    except httpx.TimeoutException as e:
        parsed = urlparse(url)
        raise AdagentsTimeoutError(parsed.netloc, timeout) from e
    except httpx.RequestError as e:
        raise AdagentsValidationError(f"Failed to fetch adagents.json: {e}") from e

    if status_code == 304:
        if cache_entry is None:
            # The server should not return 304 without a conditional
            # request; treat as an error rather than silently returning
            # nothing.
            raise AdagentsValidationError(
                "Received 304 Not Modified without a cache entry to serve"
            )
        return (
            cache_entry.body,
            _safe_validator(response_headers.get("etag")) or cache_entry.etag,
            _safe_validator(response_headers.get("last-modified")) or cache_entry.last_modified,
            True,
        )

    if status_code == 404:
        parsed = urlparse(url)
        raise AdagentsNotFoundError(parsed.netloc)

    if status_code != 200:
        raise AdagentsValidationError(f"Failed to fetch adagents.json: HTTP {status_code}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        # Truncate the upstream-derived error to bound log volume — a
        # malicious server can otherwise force unbounded `str(e)` content
        # into caller logs by sending a large unparsable body.
        raise AdagentsValidationError(f"Invalid JSON in adagents.json: {str(e)[:200]}") from e

    if not isinstance(data, dict):
        raise AdagentsValidationError("adagents.json must be a JSON object")

    if "authorized_agents" in data:
        if not isinstance(data["authorized_agents"], list):
            raise AdagentsValidationError("'authorized_agents' must be an array")

        try:
            validate_adagents(data)
        except ValidationError as e:
            raise AdagentsValidationError(f"Invalid adagents.json structure: {e}") from e
    elif "authoritative_location" not in data:
        raise AdagentsValidationError(
            "adagents.json must have either 'authorized_agents' or 'authoritative_location'"
        )

    return (
        data,
        _safe_validator(response_headers.get("etag")),
        _safe_validator(response_headers.get("last-modified")),
        False,
    )


# Cache validators (ETag / Last-Modified) are replayed on the next fetch, so
# an unbounded value sent back by a hostile server would balloon every future
# request. RFC 9110 doesn't cap either header; this is purely defensive.
_MAX_VALIDATOR_LEN = 256


def _safe_validator(value: str | None) -> str | None:
    if value is None or len(value) > _MAX_VALIDATOR_LEN:
        return None
    return value


async def _stream_capped(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> tuple[bytes, int, httpx.Headers]:
    """Stream a GET and abort if the body exceeds ``max_bytes``.

    Reading the body via ``iter_bytes`` lets us bail before buffering an
    oversized response. A ``Content-Length`` larger than the cap is
    rejected up-front; servers that omit the header (or lie) are still
    caught by the running total inside the loop.
    """
    # follow_redirects=False: HTTP 30x is not how adagents.json delegates.
    # Cross-host delegation goes through the explicit `authoritative_location`
    # field, which passes through _validate_redirect_url. Allowing httpx to
    # transparently follow 30x would bypass that SSRF gate.
    async with client.stream(
        "GET", url, headers=headers, timeout=timeout, follow_redirects=False
    ) as response:
        if response.status_code == 304:
            return b"", 304, response.headers

        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise AdagentsValidationError(
                        f"adagents.json body Content-Length {content_length} exceeds "
                        f"size cap of {max_bytes} bytes"
                    )
            except ValueError:
                # malformed Content-Length — fall through to streaming cap
                pass

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise AdagentsValidationError(
                    f"adagents.json body exceeds size cap of {max_bytes} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks), response.status_code, response.headers


async def verify_agent_for_property(
    publisher_domain: str,
    agent_url: str,
    property_identifiers: list[dict[str, str]],
    property_type: str | None = None,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Convenience wrapper to fetch adagents.json and verify authorization in one call.

    Args:
        publisher_domain: Domain hosting the adagents.json file
        agent_url: URL of the sales agent to verify
        property_identifiers: List of identifiers to match
        property_type: Type of property (website, app, etc.) - optional
        timeout: Request timeout in seconds
        client: Optional httpx.AsyncClient for connection pooling

    Returns:
        True if agent is authorized, False otherwise

    Raises:
        AdagentsNotFoundError: If adagents.json not found (404)
        AdagentsValidationError: If JSON is invalid or malformed
        AdagentsTimeoutError: If request times out
    """
    adagents_data = await fetch_adagents(publisher_domain, timeout=timeout, client=client)
    return verify_agent_authorization(
        adagents_data=adagents_data,
        agent_url=agent_url,
        property_type=property_type,
        property_identifiers=property_identifiers,
    )


def _resolve_agent_properties(
    agent: dict[str, Any],
    top_level_properties: list[dict[str, Any]],
    domain_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Resolve properties for a single agent entry based on its authorization_type.

    Args:
        agent: An authorized_agents entry
        top_level_properties: The top-level properties array from adagents.json
        domain_index: Pre-built ``publisher_domain → [property, ...]`` index
            over ``top_level_properties`` (built once per file by the caller
            via :func:`_build_domain_index`).

    Returns:
        List of resolved property dicts for this agent
    """
    authorization_type = agent.get("authorization_type", "")

    # Handle inline_properties, or legacy entries with properties array but no authorization_type
    if authorization_type == "inline_properties" or (
        not authorization_type and "properties" in agent
    ):
        properties = agent.get("properties", [])
        if not isinstance(properties, list):
            return []
        return [p for p in properties if isinstance(p, dict)]

    # Handle property_ids (filter top-level properties by property_id)
    if authorization_type == "property_ids":
        raw_ids = agent.get("property_ids")
        if not isinstance(raw_ids, list):
            return []
        authorized_ids = {i for i in raw_ids if isinstance(i, str)}
        return [
            p
            for p in top_level_properties
            if isinstance(p, dict) and p.get("property_id") in authorized_ids
        ]

    # Handle property_tags (filter top-level properties by tags)
    if authorization_type == "property_tags":
        raw_tags = agent.get("property_tags")
        if not isinstance(raw_tags, list):
            return []
        authorized_tags = {t for t in raw_tags if isinstance(t, str)}
        return [
            p
            for p in top_level_properties
            if isinstance(p, dict)
            and {t for t in p.get("tags", []) if isinstance(t, str)} & authorized_tags
        ]

    # Handle publisher_properties (cross-domain references).
    # Each entry with publisher_domains[a,b,c] fans out to one selector per
    # listed domain — the compact form is exactly equivalent to repeating
    # the entry once per publisher per adcp#4504. Selectors are then
    # resolved inline against the parent file's top-level properties[]
    # array, indexed by publisher_domain, per adcp#4827.
    if authorization_type == "publisher_properties":
        publisher_props = agent.get("publisher_properties", [])
        if not isinstance(publisher_props, list):
            return []
        selectors = _fanout_publisher_properties(
            [p for p in publisher_props if isinstance(p, dict)]
        )
        return _resolve_publisher_property_selectors(selectors, domain_index)

    return []


def _build_domain_index(
    properties: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build a ``publisher_domain → [property, ...]`` index.

    O(N) up-front cost; reused across every selector for that file so the
    per-selector resolution cost drops from O(properties) to O(1) lookup
    plus O(matches) filtering. Malformed entries (non-dict, missing or
    non-string ``publisher_domain``) are skipped.
    """
    domain_index: dict[str, list[dict[str, Any]]] = {}
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        domain = prop.get("publisher_domain")
        if not isinstance(domain, str) or not domain:
            continue
        domain_index.setdefault(domain, []).append(prop)
    return domain_index


def _resolve_publisher_property_selectors(
    selectors: list[dict[str, Any]],
    domain_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Resolve fanned-out publisher_properties selectors against inline data.

    Resolves selectors per adcp#4827 §Resolution-paths (inline path
    against the parent file's top-level properties[] indexed by
    publisher_domain).

    For each selector (one per publisher_domain), look up the matching
    properties in ``domain_index`` by ``publisher_domain`` and apply the
    selector's ``selection_type``:

    - ``"all"``: every property under that domain
    - ``"by_tag"``: properties whose ``tags`` intersect ``property_tags``
      (empty ``property_tags`` resolves to ``[]`` — fail-closed, no
      "tag list omitted means everything")
    - ``"by_id"``: properties whose ``property_id`` is in ``property_ids``
      (empty ``property_ids`` resolves to ``[]`` — same fail-closed rule)
    - Anything else: ``[]`` (fail-closed; unknown selection_type does
      not authorize anything — see CLAUDE.md "no fallbacks" on
      authorization decisions)

    Selectors whose domain has no entries in the index are skipped —
    federated fallback (fetching the publisher's own adagents.json) is
    out of scope for this resolver and lives in companion helpers.

    ``domain_index`` is built once per file by :func:`_build_domain_index`
    and reused across every agent's selectors in that file.

    Results are deduplicated by ``(publisher_domain, property_id)``.
    Raises :class:`AdagentsValidationError` if any matching property is
    missing the required ``property_id`` field (fail-fast per CLAUDE.md).
    """
    if not selectors:
        return []

    resolved: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for selector in selectors:
        domain = selector.get("publisher_domain")
        if not isinstance(domain, str) or not domain:
            continue
        candidates = domain_index.get(domain)
        if not candidates:
            continue

        selection_type = selector.get("selection_type")
        matched: list[dict[str, Any]]
        if selection_type == "all":
            matched = list(candidates)
        elif selection_type == "by_tag":
            raw_tags = selector.get("property_tags")
            if not isinstance(raw_tags, list):
                continue
            wanted_tags = {t for t in raw_tags if isinstance(t, str)}
            if not wanted_tags:
                continue
            matched = [
                p
                for p in candidates
                if {t for t in p.get("tags", []) or [] if isinstance(t, str)} & wanted_tags
            ]
        elif selection_type == "by_id":
            raw_ids = selector.get("property_ids")
            if not isinstance(raw_ids, list):
                continue
            wanted_ids = {i for i in raw_ids if isinstance(i, str)}
            if not wanted_ids:
                continue
            matched = [p for p in candidates if p.get("property_id") in wanted_ids]
        else:
            continue

        for prop in matched:
            prop_id = prop.get("property_id")
            if not isinstance(prop_id, str) or not prop_id:
                raise AdagentsValidationError(
                    f"property under domain={domain!r} missing required 'property_id'"
                )
            key = (domain, prop_id)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(prop)
    return resolved


def _fanout_publisher_properties(
    publisher_props: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expand ``publisher_domains[]`` compact entries into one selector per domain.

    For each entry that uses the compact form, emits one selector per
    listed domain with ``publisher_domain`` set and ``publisher_domains``
    stripped — preserving every other key (``selection_type``,
    ``property_tags``, custom extensions). Entries that already use the
    singular ``publisher_domain`` form pass through unchanged.

    Malformed entries (compact form with non-list / empty
    ``publisher_domains``) are dropped silently: structural validation
    happens at :func:`validate_publisher_properties_item`, which is the
    right layer to raise. The resolver is best-effort and stays useful
    on partially-broken files.
    """
    out: list[dict[str, Any]] = []
    for entry in publisher_props:
        domains = entry.get("publisher_domains")
        if domains is None:
            out.append(entry)
            continue

        if not isinstance(domains, list) or not domains:
            continue

        for domain in domains:
            if not isinstance(domain, str) or not domain:
                continue
            expanded = {k: v for k, v in entry.items() if k != "publisher_domains"}
            expanded["publisher_domain"] = domain
            out.append(expanded)
    return out


def _get_revoked_publisher_domains(adagents_data: dict[str, Any]) -> set[str]:
    """Return the set of publisher domains revoked by this file.

    Validators MUST treat any publisher domain listed in
    ``revoked_publisher_domains[]`` as no-longer-authorized regardless of
    where else it appears (per adcp#4504). Malformed entries are skipped
    — structural validation is the validator's job, not the index's.
    """
    revoked_raw = adagents_data.get("revoked_publisher_domains")
    if not isinstance(revoked_raw, list):
        return set()
    revoked: set[str] = set()
    for entry in revoked_raw:
        if not isinstance(entry, dict):
            continue
        publisher_domain = entry.get("publisher_domain")
        if isinstance(publisher_domain, str) and publisher_domain:
            revoked.add(publisher_domain)
    return revoked


def filter_revoked_selectors(
    selectors: list[dict[str, Any]],
    revoked_domains: set[str],
) -> list[dict[str, Any]]:
    """Strip selectors whose ``publisher_domain`` is revoked.

    Apply this AFTER the compact-form fan-out so each remaining selector
    addresses exactly one publisher, then drop any whose domain is in
    ``revoked_domains``. Revocation takes precedence over every other
    listing of that domain in the file (selectors, top-level properties,
    etc.) per adcp#4504.
    """
    if not revoked_domains:
        return selectors
    return [s for s in selectors if s.get("publisher_domain") not in revoked_domains]


def get_all_properties(adagents_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all properties from adagents.json data.

    Handles all authorization types: inline_properties, property_ids,
    property_tags, and publisher_properties.

    For ``publisher_properties`` selectors whose target ``publisher_domain``
    is NOT present inline in this file's top-level ``properties[]`` array,
    this function returns no properties for that selector. Federated
    fallback (fetching the child publisher's own adagents.json to resolve
    the selector remotely) is out of scope here and lives in
    :func:`fetch_agent_authorizations_from_directory` and
    :func:`detect_publisher_properties_divergence` from companion PR #752.
    Wire-only authorization checks that assume federated resolution will
    under-authorize against managed-network parent files that only inline
    a subset of their child domains.

    Args:
        adagents_data: Parsed adagents.json data

    Returns:
        List of all properties across all authorized agents, with agent_url added

    Raises:
        AdagentsValidationError: If adagents_data is malformed
    """
    if not isinstance(adagents_data, dict):
        raise AdagentsValidationError("adagents_data must be a dictionary")

    authorized_agents = adagents_data.get("authorized_agents")
    if not isinstance(authorized_agents, list):
        raise AdagentsValidationError("adagents.json must have 'authorized_agents' array")

    top_level_properties = adagents_data.get("properties", [])
    if not isinstance(top_level_properties, list):
        top_level_properties = []

    revoked = _get_revoked_publisher_domains(adagents_data)
    revoked_top_level = [
        p
        for p in top_level_properties
        if not (
            isinstance(p, dict)
            and isinstance(p.get("publisher_domain"), str)
            and p["publisher_domain"] in revoked
        )
    ]

    # Build the domain index once per file — _resolve_agent_properties is
    # called per-agent, and at cafemedia scale (thousands of properties ×
    # multiple agents) rebuilding it inside each call is O(agents × N).
    domain_index = _build_domain_index(revoked_top_level)

    properties = []
    for agent in authorized_agents:
        if not isinstance(agent, dict):
            continue

        agent_url = agent.get("url", "")
        if not agent_url:
            continue

        # revoked_top_level pre-filters revoked domains from the per-domain
        # index, so inline resolution honors revocation transparently.
        agent_properties = _resolve_agent_properties(agent, revoked_top_level, domain_index)

        for prop in agent_properties:
            prop_with_agent = {**prop, "agent_url": agent_url}
            properties.append(prop_with_agent)

    return properties


def get_all_tags(adagents_data: dict[str, Any]) -> set[str]:
    """Extract all unique tags from properties in adagents.json data.

    Args:
        adagents_data: Parsed adagents.json data

    Returns:
        Set of all unique tags across all properties

    Raises:
        AdagentsValidationError: If adagents_data is malformed
    """
    properties = get_all_properties(adagents_data)
    tags = set()

    for prop in properties:
        prop_tags = prop.get("tags", [])
        if isinstance(prop_tags, list):
            for tag in prop_tags:
                if isinstance(tag, str):
                    tags.add(tag)

    return tags


def get_properties_by_agent(adagents_data: dict[str, Any], agent_url: str) -> list[dict[str, Any]]:
    """Get all properties authorized for a specific agent.

    Handles all authorization types per the AdCP specification:
    - inline_properties: Properties defined directly in the agent's properties array
    - property_ids: Filter top-level properties by property_id
    - property_tags: Filter top-level properties by tags
    - publisher_properties: Inline-resolved properties from cross-publisher
      selectors (resolved from the parent file's top-level properties[]
      array per adcp#4827)

    For ``publisher_properties`` selectors whose target ``publisher_domain``
    is NOT present inline in this file's top-level ``properties[]`` array,
    this function returns no properties for that selector. Federated
    fallback (fetching the child publisher's own adagents.json to resolve
    the selector remotely) is out of scope here and lives in
    :func:`fetch_agent_authorizations_from_directory` and
    :func:`detect_publisher_properties_divergence` from companion PR #752.
    Wire-only authorization checks that assume federated resolution will
    under-authorize against managed-network parent files that only inline
    a subset of their child domains.

    Args:
        adagents_data: Parsed adagents.json data
        agent_url: URL of the agent to filter by

    Returns:
        List of properties for the specified agent (empty if agent not found)

    Raises:
        AdagentsValidationError: If adagents_data is malformed
    """
    if not isinstance(adagents_data, dict):
        raise AdagentsValidationError("adagents_data must be a dictionary")

    authorized_agents = adagents_data.get("authorized_agents")
    if not isinstance(authorized_agents, list):
        raise AdagentsValidationError("adagents.json must have 'authorized_agents' array")

    top_level_properties = adagents_data.get("properties", [])
    if not isinstance(top_level_properties, list):
        top_level_properties = []

    revoked = _get_revoked_publisher_domains(adagents_data)
    revoked_top_level = [
        p
        for p in top_level_properties
        if not (
            isinstance(p, dict)
            and isinstance(p.get("publisher_domain"), str)
            and p["publisher_domain"] in revoked
        )
    ]

    normalized_agent_url = normalize_url(agent_url)

    domain_index = _build_domain_index(revoked_top_level)

    for agent in authorized_agents:
        if not isinstance(agent, dict):
            continue

        agent_url_from_json = agent.get("url", "")
        if not agent_url_from_json:
            continue

        if normalize_url(agent_url_from_json) != normalized_agent_url:
            continue

        # revoked_top_level pre-filters revoked domains from the per-domain
        # index, so inline resolution honors revocation transparently.
        resolved = _resolve_agent_properties(agent, revoked_top_level, domain_index)
        return resolved

    return []


def validate_adagents_structure(adagents_data: dict[str, Any]) -> AdagentsValidationReport:
    """Structurally validate a parsed adagents.json against the AdCP schema.

    Use this to distinguish a schema-invalid file from a valid file that
    doesn't list a particular agent. :func:`get_properties_by_agent`
    returns ``[]`` for both cases, which makes "publisher hasn't
    authorized us yet" indistinguishable from "publisher's file is
    structurally broken." This helper reports per-entry violations
    against the authoritative ``authorized_agents`` oneOf in the AdCP
    adagents.json schema.

    The two real-world failure modes this catches in production
    publisher files are:

    * **Bare entries** — ``{url, authorized_for}`` with no
      ``authorization_type``. The agent looks listed, but matches no
      schema variant, so the SDK treats the entry as authorizing
      nothing.
    * **Wrong selector for type** — e.g.,
      ``{authorization_type: "property_ids", property_tags: [...]}``,
      where the discriminator and selector array disagree.

    Args:
        adagents_data: Parsed adagents.json (the dict returned by
            :func:`fetch_adagents` or loaded directly from JSON).

    Returns:
        :class:`AdagentsValidationReport`. ``schema_valid`` is True only
        when every entry in ``authorized_agents`` satisfies the schema.

    Raises:
        AdagentsValidationError: If ``adagents_data`` is not a dict, or
            ``authorized_agents`` is not a list. These are
            input-shape errors, not per-entry schema violations.

    Notes:
        * URL-reference variants (``authoritative_location`` form) have
          no inline ``authorized_agents`` array. They're reported with
          ``is_reference=True``, ``authorized_agents_count == 0``, and
          ``schema_valid=True``. Callers should follow the redirect
          (e.g., via :func:`fetch_adagents`, which resolves it
          automatically) and re-validate the resolved file.
        * The schema targets AdCP 3.0. Files written against 2.5 (no
          signal_ids / signal_tags variants) will flag those entries as
          ``unknown_authorization_type`` — correct for the 3.0 target,
          but worth knowing if you're validating mixed-version traffic.
        * Selector-array *item* patterns (e.g., the
          ``^[a-zA-Z0-9_-]+$`` constraint on each signal_id) are out of
          scope. This helper validates the discriminator + required
          selector array; it does not deep-validate selector contents.
    """
    if not isinstance(adagents_data, dict):
        raise AdagentsValidationError("adagents_data must be a dictionary")

    authorized_agents = adagents_data.get("authorized_agents")
    if authorized_agents is None:
        # URL-reference variant: file points at an authoritative_location
        # rather than carrying an inline authorized_agents array.
        properties = adagents_data.get("properties", [])
        is_reference = isinstance(adagents_data.get("authoritative_location"), str)
        return AdagentsValidationReport(
            schema_valid=True,
            errors=[],
            authorized_agents_count=0,
            properties_count=len(properties) if isinstance(properties, list) else 0,
            is_reference=is_reference,
        )

    if not isinstance(authorized_agents, list):
        raise AdagentsValidationError("'authorized_agents' must be an array")

    properties = adagents_data.get("properties", [])
    properties_count = len(properties) if isinstance(properties, list) else 0

    errors: list[AdagentsEntryError] = []

    if len(authorized_agents) == 0:
        # Inline variant requires minItems: 1 on authorized_agents.
        errors.append(
            AdagentsEntryError(
                index=-1,
                kind="empty_authorized_agents",
                message=(
                    "adagents.json inline variant requires at least one entry "
                    "in 'authorized_agents' (schema minItems: 1)"
                ),
            )
        )

    for index, entry in enumerate(authorized_agents):
        if not isinstance(entry, dict):
            errors.append(
                AdagentsEntryError(
                    index=index,
                    kind="not_an_object",
                    message=f"authorized_agents[{index}] is not a JSON object",
                )
            )
            continue

        raw_url = entry.get("url")
        url = raw_url if isinstance(raw_url, str) and raw_url else None

        if url is None:
            errors.append(
                AdagentsEntryError(
                    index=index,
                    kind="missing_url",
                    message=f"authorized_agents[{index}] is missing required 'url'",
                )
            )

        authorized_for = entry.get("authorized_for")
        if not isinstance(authorized_for, str) or not authorized_for:
            errors.append(
                AdagentsEntryError(
                    index=index,
                    kind="missing_authorized_for",
                    message=(
                        f"authorized_agents[{index}] is missing required "
                        "'authorized_for' description (string, minLength 1)"
                    ),
                    url=url,
                )
            )

        authorization_type = entry.get("authorization_type")
        if authorization_type is None:
            errors.append(
                AdagentsEntryError(
                    index=index,
                    kind="missing_authorization_type",
                    message=(
                        f"authorized_agents[{index}] is missing required "
                        "'authorization_type' discriminator (expected one of: "
                        f"{', '.join(sorted(_AUTHORIZATION_TYPE_TO_SELECTOR))})"
                    ),
                    url=url,
                )
            )
            continue

        if authorization_type not in _AUTHORIZATION_TYPE_TO_SELECTOR:
            errors.append(
                AdagentsEntryError(
                    index=index,
                    kind="unknown_authorization_type",
                    message=(
                        f"authorized_agents[{index}] has unknown "
                        f"authorization_type={authorization_type!r} "
                        f"(expected one of: "
                        f"{', '.join(sorted(_AUTHORIZATION_TYPE_TO_SELECTOR))})"
                    ),
                    url=url,
                )
            )
            continue

        required_selector = _AUTHORIZATION_TYPE_TO_SELECTOR[authorization_type]
        selector_value = entry.get(required_selector)
        if not isinstance(selector_value, list) or len(selector_value) == 0:
            errors.append(
                AdagentsEntryError(
                    index=index,
                    kind="missing_selector_for_type",
                    message=(
                        f"authorized_agents[{index}] has "
                        f"authorization_type={authorization_type!r} but is "
                        f"missing required non-empty {required_selector!r} array"
                    ),
                    url=url,
                )
            )

    return AdagentsValidationReport(
        schema_valid=not errors,
        errors=errors,
        authorized_agents_count=len(authorized_agents),
        properties_count=properties_count,
    )


class AuthorizationContext:
    """Authorization context for a publisher domain.

    Attributes:
        property_ids: List of property IDs the agent is authorized for
        property_tags: List of property tags the agent is authorized for
        raw_properties: Raw property data from adagents.json
    """

    def __init__(self, properties: list[dict[str, Any]]):
        """Initialize from list of properties.

        Args:
            properties: List of property dictionaries from adagents.json
        """
        self.property_ids: list[str] = []
        self.property_tags: list[str] = []
        self.raw_properties = properties

        # Extract property IDs and tags
        for prop in properties:
            if not isinstance(prop, dict):
                continue

            # Extract property ID (per AdCP v2 schema, the field is "property_id")
            prop_id = prop.get("property_id")
            if prop_id and isinstance(prop_id, str):
                self.property_ids.append(prop_id)

            # Extract tags
            tags = prop.get("tags", [])
            if isinstance(tags, list):
                for tag in tags:
                    if isinstance(tag, str) and tag not in self.property_tags:
                        self.property_tags.append(tag)

    def __repr__(self) -> str:
        return (
            f"AuthorizationContext("
            f"property_ids={self.property_ids}, "
            f"property_tags={self.property_tags})"
        )


async def fetch_agent_authorizations(
    agent_url: str,
    publisher_domains: list[str],
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, AuthorizationContext]:
    """Fetch authorization contexts by checking publisher adagents.json files.

    This function discovers what publishers have authorized your agent by fetching
    their adagents.json files from the .well-known directory and extracting the
    properties your agent can access.

    This is the "pull" approach - you query publishers to see if they've authorized you.

    Args:
        agent_url: URL of your sales agent
        publisher_domains: List of publisher domains to check (e.g., ["nytimes.com", "wsj.com"])
        timeout: Request timeout in seconds for each fetch
        client: Optional httpx.AsyncClient for connection pooling

    Returns:
        Dictionary mapping publisher domain to AuthorizationContext.
        Only includes domains where the agent is authorized.

    Example:
        >>> # "Pull" approach - check what publishers have authorized you
        >>> contexts = await fetch_agent_authorizations(
        ...     "https://our-sales-agent.com",
        ...     ["nytimes.com", "wsj.com", "cnn.com"]
        ... )
        >>> for domain, ctx in contexts.items():
        ...     print(f"{domain}:")
        ...     print(f"  Property IDs: {ctx.property_ids}")
        ...     print(f"  Tags: {ctx.property_tags}")

    Notes:
        - Silently skips domains where adagents.json is not found or invalid
        - Only returns domains where the agent is explicitly authorized
        - For production use with many domains, pass a shared httpx.AsyncClient
          to enable connection pooling
    """
    import asyncio

    # Create tasks to fetch all adagents.json files in parallel
    async def fetch_authorization_for_domain(
        domain: str,
    ) -> tuple[str, AuthorizationContext | None]:
        """Fetch authorization context for a single domain."""
        try:
            adagents_data = await fetch_adagents(domain, timeout=timeout, client=client)

            # Check if agent is authorized
            if not verify_agent_authorization(adagents_data, agent_url):
                return (domain, None)

            # Get properties for this agent
            properties = get_properties_by_agent(adagents_data, agent_url)

            # Create authorization context
            return (domain, AuthorizationContext(properties))

        except (AdagentsNotFoundError, AdagentsValidationError, AdagentsTimeoutError):
            # Silently skip domains with missing or invalid adagents.json
            return (domain, None)

    # Fetch all domains in parallel
    tasks = [fetch_authorization_for_domain(domain) for domain in publisher_domains]
    results = await asyncio.gather(*tasks)

    # Build result dictionary, filtering out None values
    return {domain: ctx for domain, ctx in results if ctx is not None}


# Wire schema for the AAO agent → publishers inverse-lookup endpoint
# (`schemas/aao/agent-publishers.json`, adcp#4828). The publisher's own
# adagents.json remains the trust root — these models describe a *discovery*
# response, and callers SHOULD verify each `publisher_domain` against its
# adagents.json via :func:`fetch_adagents` before trusting an authorization.
DirectoryDiscoveryMethod = Literal[
    "direct",
    "authoritative_location",
    "adagents_authoritative",
    "ads_txt_managerdomain",
]

DirectoryEdgeStatus = Literal["authorized", "revoked"]


class DirectoryPublisherEntry(AdCPBaseModel):
    """One publisher row in an AAO directory inverse-lookup response."""

    publisher_domain: str
    discovery_method: DirectoryDiscoveryMethod
    manager_domain: str | None = None
    properties_authorized: int = Field(ge=0)
    properties_total: int = Field(ge=0)
    signing_keys_pinned: bool | None = None
    status: DirectoryEdgeStatus
    last_verified_at: datetime
    property_ids: list[str] | None = Field(
        default=None,
        description=(
            "Canonical property IDs the agent's selectors resolve to under "
            "this publisher. Present iff the request was made with "
            "include=['properties'] AND the directory server supports it "
            "(per adcp#4894). None signals count-only mode for downstream "
            "consumers."
        ),
    )


class AgentAuthorizationsDirectoryResult(AdCPBaseModel):
    """Response envelope for ``GET /v1/agents/{agent_url}/publishers``.

    Maps directly to ``schemas/aao/agent-publishers.json`` in the AdCP
    bundle (adcp#4828). The directory is a discovery accelerator — each
    ``publisher_domain`` row tells callers where to look; they SHOULD
    verify the publisher's adagents.json directly before treating an
    authorization as trusted.
    """

    agent_url: str
    directory_indexed_at: datetime | None
    publishers: list[DirectoryPublisherEntry] = Field(default_factory=list)
    next_cursor: str | None = None


# Per-page response cap. Matches MAX_POINTER_BYTES (5 MiB) — a directory
# page is a small envelope; pagination handles bulk responses.
MAX_DIRECTORY_PAGE_BYTES = 5 * 1024 * 1024

# Hard cap on directory pagination iterations. A misbehaving directory that
# never returns an empty next_cursor would otherwise hang the sweep
# indefinitely; this cap fail-closes the loop.
MAX_DIRECTORY_PAGES = 1000


async def fetch_agent_authorizations_from_directory(
    agent_url: str,
    *,
    directory_url: str,
    since: str | None = None,
    cursor: str | None = None,
    include: list[str] | None = None,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> AgentAuthorizationsDirectoryResult:
    """Query an AAO directory for publishers that authorize ``agent_url``.

    Calls ``GET {directory_url}/v1/agents/{agent_url}/publishers`` per the
    AAO inverse-lookup contract (adcp#4823 / #4828) and returns the parsed
    response. The directory's answer is *discovery*, not authorization:
    callers should still verify each returned ``publisher_domain`` via
    :func:`fetch_adagents` before treating an edge as trusted.

    Args:
        agent_url: The agent whose publisher authorizations are being
            queried. Passed verbatim in the path; the directory echoes
            back a canonicalized form on the response.
        directory_url: HTTPS base URL of the AAO directory
            (e.g. ``"https://aao.example.com"``). The ``/v1/agents/...``
            path is appended; pass the directory's root, not a
            request-specific path.
        since: Optional RFC 3339 / ISO 8601 timestamp for incremental
            sync — sent as ``?since=<timestamp>`` and instructs the
            directory to return only publishers whose ``last_verified_at``
            is at or after this value. Use this for time-windowed polling,
            not for pagination. To page through a result set, use
            ``cursor`` instead.
        cursor: Optional opaque pagination cursor returned by a prior
            response as ``next_cursor`` — sent as ``?cursor=<value>``
            per the AAO directory spec (adcp#4823). Pass
            ``result.next_cursor`` from the previous page here; do not
            pass it to ``since``. ``since`` and ``cursor`` are
            independent query parameters and may be passed together when
            resuming a time-windowed paginated sweep.
        include: Optional list of expansion keys per the AAO directory
            API spec (adcp#4894). Each value is emitted as a separate
            ``?include=<value>`` query parameter (repeated-key form, not
            comma-joined). Pass ``["properties"]`` against directories
            that support it to receive per-publisher ``property_ids[]``
            on each row, enabling full set-diff against the publisher's
            own adagents.json. Directories that don't support a given
            expansion key simply omit the corresponding fields from the
            response; callers should treat absence as count-only mode.
        timeout: Request timeout in seconds.
        client: Optional shared ``httpx.AsyncClient`` for connection
            pooling. Caller owns the client lifecycle.

    Returns:
        :class:`AgentAuthorizationsDirectoryResult`. On 404 from the
        directory the function returns a result with ``publishers=[]``
        and ``directory_indexed_at=None`` — directories MUST be allowed
        to answer "I do not index this agent" without callers needing
        to branch on exception type.

    Raises:
        AdagentsValidationError: If ``directory_url`` is malformed, the
            response status is non-200/non-404, the body is not valid
            JSON, or the body does not match the directory result schema.
        AdagentsTimeoutError: If the request times out.

    Notes:
        - ``directory_url`` is gated through the same SSRF protection
          (HTTPS only, DNS pre-check, private/reserved address ban) as
          publisher-side fetches.
        - Response bodies are capped at 5 MiB. Paginate via
          ``next_cursor`` by passing it as ``cursor=`` on the next call.
    """
    if not isinstance(agent_url, str) or not agent_url:
        raise AdagentsValidationError("agent_url must be a non-empty string")
    if not isinstance(directory_url, str) or not directory_url:
        raise AdagentsValidationError("directory_url must be a non-empty string")

    base = directory_url.rstrip("/")
    if not base.startswith("https://"):
        raise AdagentsValidationError(f"directory_url must be an HTTPS URL, got: {directory_url!r}")
    _validate_redirect_url(f"{base}/v1/agents/_/publishers")

    request_url = f"{base}/v1/agents/{quote(agent_url, safe='')}/publishers"
    query_pairs: list[tuple[str, str]] = []
    if since is not None:
        query_pairs.append(("since", since))
    if cursor is not None:
        query_pairs.append(("cursor", cursor))
    if include:
        # Repeated-key form per docs/aao/directory-api.mdx (style: form,
        # explode: true). Comma-joined NOT accepted by spec-conformant
        # directories.
        for value in include:
            query_pairs.append(("include", value))
    if query_pairs:
        query_string = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in query_pairs)
        request_url = f"{request_url}?{query_string}"

    parsed = urlparse(request_url)
    await _dns_validate_host(
        parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80)
    )

    headers = {"User-Agent": "AdCP-Client/1.0", "Accept": "application/json"}

    try:
        if client is not None:
            body, status_code, _ = await _stream_capped(
                client, request_url, headers, timeout, MAX_DIRECTORY_PAGE_BYTES
            )
        else:
            async with httpx.AsyncClient() as new_client:
                body, status_code, _ = await _stream_capped(
                    new_client, request_url, headers, timeout, MAX_DIRECTORY_PAGE_BYTES
                )
    except httpx.TimeoutException as e:
        raise AdagentsTimeoutError(parsed.netloc, timeout) from e
    except httpx.RequestError as e:
        raise AdagentsValidationError(f"Failed to fetch agent-publishers directory: {e}") from e

    if status_code == 404:
        # Per adcp#4828, a directory that has not indexed this agent
        # answers 404. Surface as an empty result so callers don't need
        # to special-case the exception path for "no edges" — the
        # protocol is intentionally permissive here.
        return AgentAuthorizationsDirectoryResult(
            agent_url=agent_url,
            directory_indexed_at=None,
            publishers=[],
            next_cursor=None,
        )

    if status_code != 200:
        raise AdagentsValidationError(f"Agent-publishers directory returned HTTP {status_code}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise AdagentsValidationError(
            f"Invalid JSON in agent-publishers directory response: {str(e)[:200]}"
        ) from e

    try:
        return AgentAuthorizationsDirectoryResult.model_validate(data)
    except Exception as e:  # pydantic.ValidationError + any coercion failure
        raise AdagentsValidationError(
            f"Agent-publishers directory response failed schema validation: {e}"
        ) from e


class PublisherDivergence(AdCPBaseModel):
    """Divergence record for a single publisher domain.

    ``missing_in_inline``: property IDs the federated fetch found in the
    publisher's own adagents.json that the directory did not surface
    (publisher has properties the directory doesn't know about yet).

    ``missing_in_federated``: property IDs the directory claims the agent
    is authorized for but the publisher's own adagents.json does not
    include (stale directory entry or publisher revocation).

    Both fields are None in count-only fallback mode (directory did
    not return ``property_ids[]``). In count-only mode, count-equality
    does NOT guarantee set-equality — same-count substitutions are
    undetectable. Use ``?include=properties`` (adcp#4894) on directories
    that support it for full set-diff precision.

    ``child_fetch_error`` is non-None when the publisher's adagents.json
    could not be fetched or parsed; other fields carry no meaning.
    """

    publisher_domain: str
    directory_properties_authorized: int = Field(ge=0)
    federated_properties_found: int = Field(ge=0)
    missing_in_inline: list[str] | None = None
    missing_in_federated: list[str] | None = None
    child_fetch_error: str | None = None


DivergenceReport = list[PublisherDivergence]


async def detect_publisher_properties_divergence(
    agent_url: str,
    *,
    directory_url: str,
    sample_size: int | None = 200,
    max_concurrency: int = 20,
    timeout: float = 30.0,
    client: httpx.AsyncClient | None = None,
) -> DivergenceReport:
    """Compare directory's inline resolution against per-publisher federated fetches.

    For each publisher the directory lists under ``agent_url``, fetches
    that publisher's own ``adagents.json`` and compares the property set
    against the directory's claim. Returns only publishers where the two
    paths disagree (or where the child fetch failed).

    Always requests ``include=["properties"]`` from the directory so the
    full ``(publisher_domain, property_id)`` set-diff lights up on
    directories that support adcp#4894. Against older directories that
    return only ``properties_authorized`` counts, falls back to count-
    comparison; ``missing_in_inline`` / ``missing_in_federated`` are
    None in that fallback path.

    Per adcp#4827 §Resolution-paths, the federated result is
    authoritative when the two paths disagree.

    Args:
        agent_url: agent to check.
        directory_url: AAO directory base URL (HTTPS only — same SSRF
            gate as :func:`fetch_agent_authorizations_from_directory`).
        sample_size: cap the sweep at N publishers (drawn from the first
            page of directory results). None opts into a full sweep
            across all pages — only do this for small networks. Default
            200 keeps the divergence sweep bounded by default.
        max_concurrency: semaphore-capped concurrent federated fetches.
            Default 20 — caps the burst against publisher origins.
        timeout: per-request timeout (directory + child fetches).
        client: optional shared ``httpx.AsyncClient``.

    Returns:
        :data:`DivergenceReport` (``list[PublisherDivergence]``). Empty
        list = no divergence detected. Note in count-only fallback mode,
        an empty list means counts agree but set-equality is not
        guaranteed.
    """
    own_client = client is None
    http = client or httpx.AsyncClient()
    try:
        collected: list[DirectoryPublisherEntry] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_count = 0
        while True:
            page = await fetch_agent_authorizations_from_directory(
                agent_url,
                directory_url=directory_url,
                cursor=cursor,
                include=["properties"],
                timeout=timeout,
                client=http,
            )
            page_count += 1
            collected.extend(page.publishers)
            if sample_size is not None and len(collected) >= sample_size:
                collected = collected[:sample_size]
                break
            cursor = page.next_cursor
            if not cursor:
                break
            if cursor in seen_cursors:
                raise AdagentsValidationError(
                    f"Directory page cursor {cursor!r} repeated — refusing to loop forever."
                )
            seen_cursors.add(cursor)
            if page_count >= MAX_DIRECTORY_PAGES:
                raise AdagentsValidationError(
                    f"Directory pagination exceeded {MAX_DIRECTORY_PAGES} pages — aborting sweep."
                )

        # Dedupe by publisher_domain before fan-out: a hostile directory
        # returning N rows for the same publisher would otherwise amplify
        # into N concurrent fetches against a single victim host. First
        # occurrence wins (deterministic) — conflicting property_ids /
        # properties_authorized across duplicates are dropped here; the
        # directory's behavior is itself a divergence signal for ops.
        seen_domains: set[str] = set()
        deduped: list[DirectoryPublisherEntry] = []
        for entry in collected:
            if entry.publisher_domain in seen_domains:
                continue
            seen_domains.add(entry.publisher_domain)
            deduped.append(entry)
        collected = deduped

        # Emit a one-shot warning when the entire sample comes back without
        # property_ids[]. In count-only mode, same-count substitutions are
        # undetectable — adopters should pin include=["properties"] support
        # on directories that offer it.
        if collected and all(e.property_ids is None for e in collected):
            logger.warning(
                "AAO directory %s did not return property_ids[] on any publisher "
                "entry — falling back to count-only divergence detection. Same-count "
                "substitutions are undetectable in this mode. Upgrade the directory "
                "or pin include=['properties'] support.",
                directory_url,
            )

        sem = asyncio.Semaphore(max_concurrency)

        async def _probe(entry: DirectoryPublisherEntry) -> PublisherDivergence | None:
            async with sem:
                try:
                    data = await fetch_adagents(
                        entry.publisher_domain, timeout=timeout, client=http
                    )
                    federated_props = get_properties_by_agent(data, agent_url)
                    # Falsy/empty property_id is silently dropped: upstream
                    # schema requires a non-empty string, so an empty value
                    # is a structural violation that belongs in
                    # validate_adagents, not a divergence signal. Federated
                    # properties with valid IDs only.
                    federated_ids = {
                        str(p.get("property_id")) for p in federated_props if p.get("property_id")
                    }
                except (
                    AdagentsNotFoundError,
                    AdagentsValidationError,
                    AdagentsTimeoutError,
                    httpx.HTTPError,
                    OSError,
                    ValueError,
                ) as exc:
                    return PublisherDivergence(
                        publisher_domain=entry.publisher_domain,
                        directory_properties_authorized=entry.properties_authorized,
                        federated_properties_found=0,
                        missing_in_inline=None,
                        missing_in_federated=None,
                        child_fetch_error=str(exc),
                    )

            if entry.property_ids is not None:
                # Full set-diff path (adcp#4894).
                dir_ids = set(entry.property_ids)
                missing_in_inline = sorted(federated_ids - dir_ids)
                missing_in_federated = sorted(dir_ids - federated_ids)
                if not missing_in_inline and not missing_in_federated:
                    return None
                return PublisherDivergence(
                    publisher_domain=entry.publisher_domain,
                    directory_properties_authorized=entry.properties_authorized,
                    federated_properties_found=len(federated_ids),
                    missing_in_inline=missing_in_inline,
                    missing_in_federated=missing_in_federated,
                )

            # Count-only fallback (older directories).
            if len(federated_ids) == entry.properties_authorized:
                return None
            return PublisherDivergence(
                publisher_domain=entry.publisher_domain,
                directory_properties_authorized=entry.properties_authorized,
                federated_properties_found=len(federated_ids),
                missing_in_inline=None,
                missing_in_federated=None,
            )

        probes = await asyncio.gather(*[_probe(e) for e in collected])
    finally:
        if own_client:
            await http.aclose()

    return [p for p in probes if p is not None]
