from __future__ import annotations

"""
Utilities for fetching, parsing, and validating adagents.json files per the AdCP specification.

Publishers declare authorized sales agents via adagents.json files hosted at
https://{publisher_domain}/.well-known/adagents.json. This module provides utilities
for sales agents to verify they are authorized for specific properties.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from adcp.exceptions import AdagentsNotFoundError, AdagentsTimeoutError, AdagentsValidationError
from adcp.validation import ValidationError, validate_adagents

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
]


@dataclass(frozen=True)
class AdagentsEntryError:
    """A single per-entry schema violation in ``authorized_agents``.

    ``kind`` identifies the rule that failed so callers can branch (e.g.,
    distinguish a publisher who shipped bare entries from one who picked
    an unknown authorization_type). ``message`` is a human-readable
    explanation suitable for surfacing to publishers.
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
    """

    schema_valid: bool
    errors: list[AdagentsEntryError]
    authorized_agents_count: int
    properties_count: int


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
    hostname = parsed.hostname or ""

    # Reject localhost by name
    if hostname in ("localhost", "localhost.localdomain") or hostname.endswith(".local"):
        raise AdagentsValidationError("authoritative_location must not target localhost")

    # Reject private/reserved IP addresses
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise AdagentsValidationError(
                "authoritative_location must not target private/reserved addresses"
            )
    except ValueError:
        pass  # Not an IP literal — hostname is fine


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


async def _resolve_direct(
    publisher_domain: str,
    timeout: float,
    user_agent: str,
    client: httpx.AsyncClient | None,
) -> tuple[dict[str, Any], DiscoveryMethod]:
    """Direct fetch with authoritative_location redirect following.

    Returns ``(data, discovery_method)`` where ``discovery_method`` is
    ``'direct'`` if no redirect was followed, ``'authoritative_location'``
    otherwise. Raises :class:`AdagentsNotFoundError` on 404 so callers
    can attempt the ads.txt MANAGERDOMAIN fallback.
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

        try:
            data = await _fetch_adagents_url(url, timeout, user_agent, fetch_client)
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

        return data, ("authoritative_location" if is_redirect else "direct")

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
    """
    url = f"https://{publisher_domain}/ads.txt"
    headers = {"User-Agent": user_agent, "Accept": "text/plain"}
    try:
        if client is not None:
            response = await client.get(
                url, headers=headers, timeout=timeout, follow_redirects=True
            )
        else:
            async with httpx.AsyncClient() as new_client:
                response = await new_client.get(
                    url, headers=headers, timeout=timeout, follow_redirects=True
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
    """
    publisher_domain = _validate_publisher_domain(publisher_domain)

    try:
        data, _ = await _resolve_direct(publisher_domain, timeout, user_agent, client)
        return data
    except AdagentsNotFoundError:
        manager_data = await _try_managerdomain_fallback(
            publisher_domain, timeout, user_agent, client
        )
        if manager_data is not None:
            return manager_data
        raise


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
        data, _ = await _resolve_direct(manager_domain_normalized, timeout, user_agent, client=None)
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
        data, discovery = await _resolve_direct(normalized, timeout, user_agent, client)
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
        manager_data, _ = await _resolve_direct(
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
) -> dict[str, Any]:
    """Fetch and parse adagents.json from a specific URL.

    This is the core fetch logic, separated to support redirect following.
    """
    try:
        # Use provided client or create a new one
        if client is not None:
            response = await client.get(
                url,
                headers={"User-Agent": user_agent},
                timeout=timeout,
                follow_redirects=True,
            )
        else:
            async with httpx.AsyncClient() as new_client:
                response = await new_client.get(
                    url,
                    headers={"User-Agent": user_agent},
                    timeout=timeout,
                    follow_redirects=True,
                )

        # Process response
        if response.status_code == 404:
            # Extract domain from URL for error message
            parsed = urlparse(url)
            raise AdagentsNotFoundError(parsed.netloc)

        if response.status_code != 200:
            raise AdagentsValidationError(
                f"Failed to fetch adagents.json: HTTP {response.status_code}"
            )

        # Parse JSON
        try:
            data = response.json()
        except Exception as e:
            raise AdagentsValidationError(f"Invalid JSON in adagents.json: {e}") from e

        # Validate basic structure
        if not isinstance(data, dict):
            raise AdagentsValidationError("adagents.json must be a JSON object")

        # If this has authorized_agents, validate it
        if "authorized_agents" in data:
            if not isinstance(data["authorized_agents"], list):
                raise AdagentsValidationError("'authorized_agents' must be an array")

            # Validate mutual exclusivity constraints
            try:
                validate_adagents(data)
            except ValidationError as e:
                raise AdagentsValidationError(f"Invalid adagents.json structure: {e}") from e
        elif "authoritative_location" not in data:
            # Neither authorized_agents nor authoritative_location
            raise AdagentsValidationError(
                "adagents.json must have either 'authorized_agents' or 'authoritative_location'"
            )

        return data

    except httpx.TimeoutException as e:
        parsed = urlparse(url)
        raise AdagentsTimeoutError(parsed.netloc, timeout) from e
    except httpx.RequestError as e:
        raise AdagentsValidationError(f"Failed to fetch adagents.json: {e}") from e


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
) -> list[dict[str, Any]]:
    """Resolve properties for a single agent entry based on its authorization_type.

    Args:
        agent: An authorized_agents entry
        top_level_properties: The top-level properties array from adagents.json

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
        authorized_ids = set(agent.get("property_ids", []))
        return [
            p
            for p in top_level_properties
            if isinstance(p, dict) and p.get("property_id") in authorized_ids
        ]

    # Handle property_tags (filter top-level properties by tags)
    if authorization_type == "property_tags":
        authorized_tags = {t for t in agent.get("property_tags", []) if isinstance(t, str)}
        return [
            p
            for p in top_level_properties
            if isinstance(p, dict)
            and {t for t in p.get("tags", []) if isinstance(t, str)} & authorized_tags
        ]

    # Handle publisher_properties (cross-domain references)
    if authorization_type == "publisher_properties":
        publisher_props = agent.get("publisher_properties", [])
        if not isinstance(publisher_props, list):
            return []
        return [p for p in publisher_props if isinstance(p, dict)]

    return []


def get_all_properties(adagents_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all properties from adagents.json data.

    Handles all authorization types: inline_properties, property_ids,
    property_tags, and publisher_properties.

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

    properties = []
    for agent in authorized_agents:
        if not isinstance(agent, dict):
            continue

        agent_url = agent.get("url", "")
        if not agent_url:
            continue

        agent_properties = _resolve_agent_properties(agent, top_level_properties)

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
    - publisher_properties: References properties from other publisher domains
      (returns the selector objects, not resolved properties)

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

    normalized_agent_url = normalize_url(agent_url)

    for agent in authorized_agents:
        if not isinstance(agent, dict):
            continue

        agent_url_from_json = agent.get("url", "")
        if not agent_url_from_json:
            continue

        if normalize_url(agent_url_from_json) != normalized_agent_url:
            continue

        return _resolve_agent_properties(agent, top_level_properties)

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
          no ``authorized_agents`` array and are reported as
          ``authorized_agents_count == 0`` with ``schema_valid`` True
          and no errors — they're a valid file with nothing to validate
          per-entry.
        * The validator does not fetch ``authoritative_location``
          targets. Callers should follow redirects first
          (e.g., via :func:`fetch_adagents`) and validate the resolved
          data.
    """
    if not isinstance(adagents_data, dict):
        raise AdagentsValidationError("adagents_data must be a dictionary")

    authorized_agents = adagents_data.get("authorized_agents")
    if authorized_agents is None:
        properties = adagents_data.get("properties", [])
        return AdagentsValidationReport(
            schema_valid=True,
            errors=[],
            authorized_agents_count=0,
            properties_count=len(properties) if isinstance(properties, list) else 0,
        )

    if not isinstance(authorized_agents, list):
        raise AdagentsValidationError("'authorized_agents' must be an array")

    properties = adagents_data.get("properties", [])
    properties_count = len(properties) if isinstance(properties, list) else 0

    errors: list[AdagentsEntryError] = []

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

        url = entry.get("url") if isinstance(entry.get("url"), str) else None

        if not url:
            errors.append(
                AdagentsEntryError(
                    index=index,
                    kind="missing_url",
                    message=f"authorized_agents[{index}] is missing required 'url'",
                )
            )

        if not entry.get("authorized_for"):
            errors.append(
                AdagentsEntryError(
                    index=index,
                    kind="missing_authorized_for",
                    message=(
                        f"authorized_agents[{index}] is missing required "
                        "'authorized_for' description"
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
