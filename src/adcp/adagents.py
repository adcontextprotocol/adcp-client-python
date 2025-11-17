from __future__ import annotations

"""
Utilities for fetching, parsing, and validating adagents.json files per the AdCP specification.

Publishers declare authorized sales agents via adagents.json files hosted at
https://{publisher_domain}/.well-known/adagents.json. This module provides utilities
for sales agents to verify they are authorized for specific properties.
"""

from typing import Any
from urllib.parse import urlparse

import httpx

from adcp.exceptions import AdagentsNotFoundError, AdagentsTimeoutError, AdagentsValidationError
from adcp.validation import ValidationError, validate_adagents


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


async def fetch_adagents(
    publisher_domain: str,
    timeout: float = 10.0,
    user_agent: str = "AdCP-Client/1.0",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch and parse adagents.json from publisher domain.

    Args:
        publisher_domain: Domain hosting the adagents.json file
        timeout: Request timeout in seconds
        user_agent: User-Agent header for HTTP request
        client: Optional httpx.AsyncClient for connection pooling.
            If provided, caller is responsible for client lifecycle.
            If None, a new client is created for this request.

    Returns:
        Parsed adagents.json data

    Raises:
        AdagentsNotFoundError: If adagents.json not found (404)
        AdagentsValidationError: If JSON is invalid or malformed
        AdagentsTimeoutError: If request times out

    Notes:
        For production use with multiple requests, pass a shared httpx.AsyncClient
        to enable connection pooling and improve performance.
    """
    # Validate and normalize domain for security
    publisher_domain = _validate_publisher_domain(publisher_domain)

    # Construct URL
    url = f"https://{publisher_domain}/.well-known/adagents.json"

    try:
        # Use provided client or create a new one
        if client is not None:
            # Reuse provided client (connection pooling)
            response = await client.get(
                url,
                headers={"User-Agent": user_agent},
                timeout=timeout,
                follow_redirects=True,
            )
        else:
            # Create new client for single request
            async with httpx.AsyncClient() as new_client:
                response = await new_client.get(
                    url,
                    headers={"User-Agent": user_agent},
                    timeout=timeout,
                    follow_redirects=True,
                )

        # Process response (same for both paths)
        if response.status_code == 404:
            raise AdagentsNotFoundError(publisher_domain)

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

        if "authorized_agents" not in data:
            raise AdagentsValidationError("adagents.json must have 'authorized_agents' field")

        if not isinstance(data["authorized_agents"], list):
            raise AdagentsValidationError("'authorized_agents' must be an array")

        # Validate mutual exclusivity constraints
        try:
            validate_adagents(data)
        except ValidationError as e:
            raise AdagentsValidationError(f"Invalid adagents.json structure: {e}") from e

        return data

    except httpx.TimeoutException as e:
        raise AdagentsTimeoutError(publisher_domain, timeout) from e
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


def get_all_properties(adagents_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all properties from adagents.json data.

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

    properties = []
    for agent in authorized_agents:
        if not isinstance(agent, dict):
            continue

        agent_url = agent.get("url", "")
        if not agent_url:
            continue

        agent_properties = agent.get("properties", [])
        if not isinstance(agent_properties, list):
            continue

        # Add each property with the agent URL for reference
        for prop in agent_properties:
            if isinstance(prop, dict):
                # Create a copy and add agent_url
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

    Args:
        adagents_data: Parsed adagents.json data
        agent_url: URL of the agent to filter by

    Returns:
        List of properties for the specified agent (empty if agent not found or no properties)

    Raises:
        AdagentsValidationError: If adagents_data is malformed
    """
    if not isinstance(adagents_data, dict):
        raise AdagentsValidationError("adagents_data must be a dictionary")

    authorized_agents = adagents_data.get("authorized_agents")
    if not isinstance(authorized_agents, list):
        raise AdagentsValidationError("adagents.json must have 'authorized_agents' array")

    # Normalize the agent URL for comparison
    normalized_agent_url = normalize_url(agent_url)

    for agent in authorized_agents:
        if not isinstance(agent, dict):
            continue

        agent_url_from_json = agent.get("url", "")
        if not agent_url_from_json:
            continue

        # Match agent URL (protocol-agnostic)
        if normalize_url(agent_url_from_json) != normalized_agent_url:
            continue

        # Found the agent - return their properties
        properties = agent.get("properties", [])
        if not isinstance(properties, list):
            return []

        return [p for p in properties if isinstance(p, dict)]

    return []


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

            # Extract property ID
            prop_id = prop.get("id")
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
    For the "push" approach where the agent tells you what it's authorized for,
    use the agent's list_authorized_properties endpoint via ADCPClient.

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

    See Also:
        ADCPClient.list_authorized_properties: "Push" approach using the agent's API

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
