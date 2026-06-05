"""Client for the AdCP registry API (brand, property, member, and policy lookups)."""

from __future__ import annotations

import asyncio
import re
from typing import Any, TypeVar, cast
from urllib.parse import quote as url_quote

import httpx
from pydantic import BaseModel, ValidationError

from adcp.exceptions import RegistryError

_T = TypeVar("_T", bound=BaseModel)
from adcp.types.core import (
    Member,
    Policy,
    PolicyHistory,
    PolicySummary,
    ResolvedBrand,
    ResolvedProperty,
)
from adcp.types.registry import (
    BrandActivity,
    BrandRegistryItem,
    DomainLookupResult,
    FederatedAgentWithDetails,
    FederatedPublisher,
    FeedPage,
    PropertyActivity,
    PropertyRegistryItem,
    ValidationResult,
)

DEFAULT_REGISTRY_URL = "https://agenticadvertising.org"
MAX_BULK_DOMAINS = 100
MAX_BULK_POLICIES = 100


class RegistryClient:
    """Client for the AdCP registry API.

    Provides brand, property, and member lookups against the central AdCP registry.

    Args:
        base_url: Registry API base URL.
        timeout: Request timeout in seconds.
        client: Optional httpx.AsyncClient for connection pooling.
            If provided, caller is responsible for client lifecycle.
        user_agent: User-Agent header for requests.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_REGISTRY_URL,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
        user_agent: str = "adcp-client-python",
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._user_agent = user_agent

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create httpx client."""
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                ),
            )
        return self._owned_client

    async def close(self) -> None:
        """Close owned HTTP client. No-op if using external client."""
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    async def __aenter__(self) -> RegistryClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth_token: str | None = None,
        operation: str = "Registry request",
        allow_404: bool = False,
        expected_status: int | set[int] = 200,
    ) -> httpx.Response | None:
        """Execute a registry API request with standard error handling.

        Returns None if allow_404=True and the server returns 404.
        Raises RegistryError for all other non-expected status codes.
        """
        client = await self._get_client()
        headers: dict[str, str] = {"User-Agent": self._user_agent}
        if auth_token is not None:
            headers["Authorization"] = f"Bearer {auth_token}"

        expected = {expected_status} if isinstance(expected_status, int) else expected_status

        try:
            url = f"{self._base_url}{path}"
            if method == "GET":
                response = await client.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self._timeout,
                )
            elif method == "POST":
                response = await client.post(
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self._timeout,
                )
            else:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self._timeout,
                )

            if allow_404 and response.status_code == 404:
                return None
            if response.status_code not in expected:
                raise RegistryError(
                    f"{operation} failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            return response
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"{operation} timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"{operation} failed: {e}") from e

    async def _request_ok(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Like _request but guarantees a non-None response.

        Use for endpoints that never return 404-as-None.
        """
        resp = await self._request(method, path, **kwargs)
        if resp is None:
            raise RegistryError(
                f"{kwargs.get('operation', 'Request')} failed: unexpected empty response"
            )
        return resp

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth_token: str | None = None,
        operation: str = "Registry request",
        expected_status: int | set[int] = 200,
    ) -> dict[str, Any]:
        """Execute a registry request and return the JSON object body."""
        resp = await self._request_ok(
            method,
            path,
            params=params,
            json_body=json_body,
            auth_token=auth_token,
            operation=operation,
            expected_status=expected_status,
        )
        return cast(dict[str, Any], resp.json())

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth_token: str | None = None,
        operation: str = "Registry request",
        expected_status: int | set[int] = 200,
    ) -> str:
        """Execute a registry request and return the text body."""
        resp = await self._request_ok(
            method,
            path,
            params=params,
            json_body=json_body,
            auth_token=auth_token,
            operation=operation,
            expected_status=expected_status,
        )
        return resp.text

    @staticmethod
    def _parse(model_cls: type[_T], data: Any, operation: str) -> _T:
        """Validate data against a Pydantic model, wrapping errors."""
        try:
            return model_cls.model_validate(data)
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"{operation} failed: invalid response: {e}") from e

    async def lookup_brand(self, domain: str) -> ResolvedBrand | None:
        """Resolve a domain to its brand identity.

        Works for any domain — brand houses, sub-brands, and operators
        (agencies, DSPs) are all brands in the registry.

        Args:
            domain: Domain to resolve (e.g., "nike.com", "wpp.com").

        Returns:
            ResolvedBrand if found, None if not in the registry.

        Raises:
            RegistryError: On HTTP or parsing errors.

        Example:
            brand = await registry.lookup_brand(request.brand.domain)
        """
        client = await self._get_client()
        try:
            response = await client.get(
                f"{self._base_url}/api/brands/resolve",
                params={"domain": domain},
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise RegistryError(
                    f"Brand lookup failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            if data is None:
                return None
            return ResolvedBrand.model_validate(data)
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Brand lookup timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Brand lookup failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Brand lookup failed: invalid response: {e}") from e

    async def lookup_brands(self, domains: list[str]) -> dict[str, ResolvedBrand | None]:
        """Bulk resolve domains to brand identities.

        Automatically chunks requests exceeding 100 domains.

        Args:
            domains: List of domains to resolve.

        Returns:
            Dict mapping each domain to its ResolvedBrand, or None if not found.

        Raises:
            RegistryError: On HTTP or parsing errors.
        """
        if not domains:
            return {}

        chunks = [
            domains[i : i + MAX_BULK_DOMAINS] for i in range(0, len(domains), MAX_BULK_DOMAINS)
        ]

        chunk_results = await asyncio.gather(
            *[self._lookup_brands_chunk(chunk) for chunk in chunks]
        )

        merged: dict[str, ResolvedBrand | None] = {}
        for result in chunk_results:
            merged.update(result)
        return merged

    async def _lookup_brands_chunk(self, domains: list[str]) -> dict[str, ResolvedBrand | None]:
        """Resolve a single chunk of brand domains (max 100)."""
        client = await self._get_client()
        try:
            response = await client.post(
                f"{self._base_url}/api/brands/resolve/bulk",
                json={"domains": domains},
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise RegistryError(
                    f"Bulk brand lookup failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            results_raw = data.get("results", {})
            results: dict[str, ResolvedBrand | None] = {d: None for d in domains}
            for domain, brand_data in results_raw.items():
                if brand_data is not None:
                    results[domain] = ResolvedBrand.model_validate(brand_data)
            return results
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Bulk brand lookup timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Bulk brand lookup failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Bulk brand lookup failed: invalid response: {e}") from e

    async def lookup_property(self, domain: str) -> ResolvedProperty | None:
        """Resolve a publisher domain to its property info.

        Args:
            domain: Publisher domain to resolve (e.g., "nytimes.com").

        Returns:
            ResolvedProperty if found, None if the domain is not in the registry.

        Raises:
            RegistryError: On HTTP or parsing errors.
        """
        client = await self._get_client()
        try:
            response = await client.get(
                f"{self._base_url}/api/properties/resolve",
                params={"domain": domain},
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise RegistryError(
                    f"Property lookup failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            if data is None:
                return None
            return ResolvedProperty.model_validate(data)
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Property lookup timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Property lookup failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Property lookup failed: invalid response: {e}") from e

    async def lookup_properties(self, domains: list[str]) -> dict[str, ResolvedProperty | None]:
        """Bulk resolve publisher domains to property info.

        Automatically chunks requests exceeding 100 domains.

        Args:
            domains: List of publisher domains to resolve.

        Returns:
            Dict mapping each domain to its ResolvedProperty, or None if not found.

        Raises:
            RegistryError: On HTTP or parsing errors.
        """
        if not domains:
            return {}

        chunks = [
            domains[i : i + MAX_BULK_DOMAINS] for i in range(0, len(domains), MAX_BULK_DOMAINS)
        ]

        chunk_results = await asyncio.gather(
            *[self._lookup_properties_chunk(chunk) for chunk in chunks]
        )

        merged: dict[str, ResolvedProperty | None] = {}
        for result in chunk_results:
            merged.update(result)
        return merged

    async def _lookup_properties_chunk(
        self, domains: list[str]
    ) -> dict[str, ResolvedProperty | None]:
        """Resolve a single chunk of property domains (max 100)."""
        client = await self._get_client()
        try:
            response = await client.post(
                f"{self._base_url}/api/properties/resolve/bulk",
                json={"domains": domains},
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise RegistryError(
                    f"Bulk property lookup failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            results_raw = data.get("results", {})
            results: dict[str, ResolvedProperty | None] = {d: None for d in domains}
            for domain, prop_data in results_raw.items():
                if prop_data is not None:
                    results[domain] = ResolvedProperty.model_validate(prop_data)
            return results
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Bulk property lookup timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Bulk property lookup failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Bulk property lookup failed: invalid response: {e}") from e

    async def list_members(self, limit: int = 100) -> list[Member]:
        """List organizations registered in the AAO member directory.

        Args:
            limit: Maximum number of members to return.

        Returns:
            List of Member objects.

        Raises:
            RegistryError: On HTTP or parsing errors.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")

        client = await self._get_client()
        try:
            response = await client.get(
                f"{self._base_url}/api/members",
                params={"limit": limit},
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise RegistryError(
                    f"Member list failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            return [Member.model_validate(m) for m in data.get("members", [])]
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Member list timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Member list failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Member list failed: invalid response: {e}") from e

    async def get_member(self, slug: str) -> Member | None:
        """Get a single AAO member by their slug.

        Args:
            slug: Member slug (e.g., "adgentek").

        Returns:
            Member if found, None if not in the registry.

        Raises:
            RegistryError: On HTTP or parsing errors.
            ValueError: If slug contains path-traversal characters.
        """
        if not slug or not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
            raise ValueError(f"Invalid member slug: {slug!r}")
        client = await self._get_client()
        try:
            response = await client.get(
                f"{self._base_url}/api/members/{slug}",
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise RegistryError(
                    f"Member lookup failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            if data is None:
                return None
            return Member.model_validate(data)
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Member lookup timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Member lookup failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Member lookup failed: invalid response: {e}") from e

    # ========================================================================
    # Policy Registry Operations
    # ========================================================================

    async def list_policies(
        self,
        search: str | None = None,
        category: str | None = None,
        enforcement: str | None = None,
        jurisdiction: str | None = None,
        vertical: str | None = None,
        domain: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[PolicySummary]:
        """List governance policies with optional filtering.

        Args:
            search: Full-text search on policy name and description.
            category: Filter by category ("regulation" or "standard").
            enforcement: Filter by enforcement level ("must", "should", "may").
            jurisdiction: Filter by jurisdiction with region alias matching.
            vertical: Filter by industry vertical.
            domain: Filter by governance domain ("campaign", "creative", etc.).
            limit: Results per page (default 20, max 1000).
            offset: Pagination offset.

        Returns:
            List of PolicySummary objects.

        Raises:
            RegistryError: On HTTP or parsing errors.
        """
        client = await self._get_client()
        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        if search is not None:
            params["search"] = search
        if category is not None:
            params["category"] = category
        if enforcement is not None:
            params["enforcement"] = enforcement
        if jurisdiction is not None:
            params["jurisdiction"] = jurisdiction
        if vertical is not None:
            params["vertical"] = vertical
        if domain is not None:
            params["domain"] = domain

        try:
            response = await client.get(
                f"{self._base_url}/api/policies/registry",
                params=params,
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise RegistryError(
                    f"Policy list failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            return [PolicySummary.model_validate(p) for p in data.get("policies", [])]
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Policy list timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Policy list failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Policy list failed: invalid response: {e}") from e

    async def resolve_policy(
        self,
        policy_id: str,
        version: str | None = None,
    ) -> Policy | None:
        """Resolve a single policy by ID.

        Args:
            policy_id: Policy identifier (e.g., "gdpr_consent").
            version: Optional version pin; returns None if current version differs.

        Returns:
            Policy if found, None if not in the registry.

        Raises:
            RegistryError: On HTTP or parsing errors.
        """
        client = await self._get_client()
        params: dict[str, str] = {"policy_id": policy_id}
        if version is not None:
            params["version"] = version

        try:
            response = await client.get(
                f"{self._base_url}/api/policies/resolve",
                params=params,
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise RegistryError(
                    f"Policy resolve failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            if data is None:
                return None
            return Policy.model_validate(data)
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Policy resolve timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Policy resolve failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Policy resolve failed: invalid response: {e}") from e

    async def resolve_policies(
        self,
        policy_ids: list[str],
    ) -> dict[str, Policy | None]:
        """Bulk resolve policies by ID.

        Automatically chunks requests exceeding 100 policy IDs.

        Args:
            policy_ids: List of policy identifiers to resolve.

        Returns:
            Dict mapping each policy_id to its Policy, or None if not found.

        Raises:
            RegistryError: On HTTP or parsing errors.
        """
        if not policy_ids:
            return {}

        chunks = [
            policy_ids[i : i + MAX_BULK_POLICIES]
            for i in range(0, len(policy_ids), MAX_BULK_POLICIES)
        ]

        chunk_results = await asyncio.gather(
            *[self._resolve_policies_chunk(chunk) for chunk in chunks]
        )

        merged: dict[str, Policy | None] = {}
        for result in chunk_results:
            merged.update(result)
        return merged

    async def _resolve_policies_chunk(self, policy_ids: list[str]) -> dict[str, Policy | None]:
        """Resolve a single chunk of policy IDs (max 100)."""
        client = await self._get_client()
        try:
            response = await client.post(
                f"{self._base_url}/api/policies/resolve/bulk",
                json={"policy_ids": policy_ids},
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise RegistryError(
                    f"Bulk policy resolve failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            results_raw = data.get("results", {})
            results: dict[str, Policy | None] = {pid: None for pid in policy_ids}
            for pid, policy_data in results_raw.items():
                if policy_data is not None:
                    results[pid] = Policy.model_validate(policy_data)
            return results
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Bulk policy resolve timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Bulk policy resolve failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Bulk policy resolve failed: invalid response: {e}") from e

    async def policy_history(
        self,
        policy_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> PolicyHistory | None:
        """Retrieve edit history for a policy.

        Args:
            policy_id: Policy identifier.
            limit: Maximum revisions to return (default 20, max 100).
            offset: Pagination offset.

        Returns:
            PolicyHistory if found, None if the policy doesn't exist.

        Raises:
            RegistryError: On HTTP or parsing errors.
        """
        client = await self._get_client()
        try:
            response = await client.get(
                f"{self._base_url}/api/policies/history",
                params={"policy_id": policy_id, "limit": limit, "offset": offset},
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise RegistryError(
                    f"Policy history failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json()
            if data is None:
                return None
            return PolicyHistory.model_validate(data)
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Policy history timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Policy history failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Policy history failed: invalid response: {e}") from e

    async def save_policy(
        self,
        policy_id: str,
        version: str,
        name: str,
        category: str,
        enforcement: str,
        policy: str,
        *,
        auth_token: str,
        description: str | None = None,
        jurisdictions: list[str] | None = None,
        region_aliases: dict[str, list[str]] | None = None,
        verticals: list[str] | None = None,
        channels: list[str] | None = None,
        effective_date: str | None = None,
        sunset_date: str | None = None,
        governance_domains: list[str] | None = None,
        source_url: str | None = None,
        source_name: str | None = None,
        guidance: str | None = None,
        exemplars: dict[str, Any] | None = None,
        ext: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a community-contributed policy.

        Requires authentication. Cannot edit registry-sourced or pending policies.

        Args:
            policy_id: Policy identifier (lowercase alphanumeric with underscores).
            version: Semantic version string.
            name: Human-readable policy name.
            category: "regulation" or "standard".
            enforcement: "must", "should", or "may".
            policy: Natural language policy text.
            auth_token: API key for authentication.
            description: Policy description.
            jurisdictions: ISO jurisdiction codes.
            region_aliases: Region alias mappings (e.g., {"EU": ["DE", "FR"]}).
            verticals: Industry verticals.
            channels: Media channels.
            effective_date: ISO 8601 date when enforcement begins.
            sunset_date: ISO 8601 date when enforcement ends.
            governance_domains: Applicable domains ("campaign", "creative", etc.).
            source_url: URL of the source regulation/standard.
            source_name: Name of the source.
            guidance: Implementation guidance text.
            exemplars: Pass/fail calibration scenarios.
            ext: Extension data.

        Returns:
            Dict with success, message, policy_id, and revision_number.

        Raises:
            RegistryError: On HTTP or parsing errors (400, 401, 409, 429).
        """
        client = await self._get_client()
        body: dict[str, Any] = {
            "policy_id": policy_id,
            "version": version,
            "name": name,
            "category": category,
            "enforcement": enforcement,
            "policy": policy,
        }
        for key, value in [
            ("description", description),
            ("jurisdictions", jurisdictions),
            ("region_aliases", region_aliases),
            ("verticals", verticals),
            ("channels", channels),
            ("effective_date", effective_date),
            ("sunset_date", sunset_date),
            ("governance_domains", governance_domains),
            ("source_url", source_url),
            ("source_name", source_name),
            ("guidance", guidance),
            ("exemplars", exemplars),
            ("ext", ext),
        ]:
            if value is not None:
                body[key] = value

        try:
            response = await client.post(
                f"{self._base_url}/api/policies/save",
                json=body,
                headers={
                    "User-Agent": self._user_agent,
                    "Authorization": f"Bearer {auth_token}",
                },
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise RegistryError(
                    f"Policy save failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            result: dict[str, Any] = response.json()
            return result
        except RegistryError:
            raise
        except httpx.TimeoutException as e:
            raise RegistryError(f"Policy save timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Policy save failed: {e}") from e

    # ========================================================================
    # Brand Registry Operations
    # ========================================================================

    async def get_brand_json(self, domain: str, *, fresh: bool = False) -> dict[str, Any] | None:
        """Fetch raw brand.json for a domain."""
        params: dict[str, Any] = {"domain": domain}
        if fresh:
            params["fresh"] = "true"
        resp = await self._request(
            "GET",
            "/api/brands/brand-json",
            params=params,
            allow_404=True,
            operation="Brand JSON fetch",
        )
        if resp is None:
            return None
        return cast(dict[str, Any], resp.json())

    async def save_brand(
        self,
        domain: str,
        brand_name: str,
        *,
        auth_token: str,
        brand_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save or update a brand in the registry (auth required)."""
        body: dict[str, Any] = {"domain": domain, "brand_name": brand_name}
        if brand_manifest is not None:
            body["brand_manifest"] = brand_manifest
        resp = await self._request_ok(
            "POST",
            "/api/brands/save",
            json_body=body,
            auth_token=auth_token,
            operation="Brand save",
        )

        return cast(dict[str, Any], resp.json())

    async def list_brands(
        self,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[BrandRegistryItem]:
        """List brands in the registry."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if search is not None:
            params["search"] = search
        resp = await self._request_ok(
            "GET",
            "/api/brands/registry",
            params=params,
            operation="Brand list",
        )

        data = resp.json()
        return [self._parse(BrandRegistryItem, b, "Brand list") for b in data.get("brands", [])]

    async def brand_history(
        self,
        domain: str,
        limit: int = 20,
        offset: int = 0,
    ) -> BrandActivity | None:
        """Get edit history for a brand."""
        resp = await self._request(
            "GET",
            "/api/brands/history",
            params={"domain": domain, "limit": limit, "offset": offset},
            allow_404=True,
            operation="Brand history",
        )
        if resp is None:
            return None
        return self._parse(BrandActivity, resp.json(), "Brand history")

    async def enrich_brand(self, domain: str) -> dict[str, Any]:
        """Enrich brand data using Brandfetch."""
        resp = await self._request_ok(
            "GET",
            "/api/brands/enrich",
            params={"domain": domain},
            operation="Brand enrich",
        )

        return cast(dict[str, Any], resp.json())

    # ========================================================================
    # Property Registry Operations
    # ========================================================================

    async def list_properties(
        self,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PropertyRegistryItem]:
        """List properties in the registry."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if search is not None:
            params["search"] = search
        resp = await self._request_ok(
            "GET",
            "/api/properties/registry",
            params=params,
            operation="Property list",
        )

        data = resp.json()
        return [
            self._parse(PropertyRegistryItem, p, "Property list")
            for p in data.get("properties", [])
        ]

    async def validate_property(self, domain: str) -> ValidationResult:
        """Validate a domain's adagents.json configuration."""
        resp = await self._request_ok(
            "GET",
            "/api/properties/validate",
            params={"domain": domain},
            operation="Property validate",
        )

        return self._parse(ValidationResult, resp.json(), "Property validate")

    async def save_property(
        self,
        publisher_domain: str,
        authorized_agents: list[dict[str, Any]],
        *,
        auth_token: str,
        properties: list[dict[str, Any]] | None = None,
        contact: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Save or update a hosted property (auth required)."""
        body: dict[str, Any] = {
            "publisher_domain": publisher_domain,
            "authorized_agents": authorized_agents,
        }
        if properties is not None:
            body["properties"] = properties
        if contact is not None:
            body["contact"] = contact
        resp = await self._request_ok(
            "POST",
            "/api/properties/save",
            json_body=body,
            auth_token=auth_token,
            operation="Property save",
        )

        return cast(dict[str, Any], resp.json())

    async def property_history(
        self,
        domain: str,
        limit: int = 20,
        offset: int = 0,
    ) -> PropertyActivity | None:
        """Get edit history for a property."""
        resp = await self._request(
            "GET",
            "/api/properties/history",
            params={"domain": domain, "limit": limit, "offset": offset},
            allow_404=True,
            operation="Property history",
        )
        if resp is None:
            return None
        return self._parse(PropertyActivity, resp.json(), "Property history")

    async def check_property_list(self, domains: list[str]) -> dict[str, Any]:
        """Check publisher domains against the registry."""
        resp = await self._request_ok(
            "POST",
            "/api/properties/check",
            json_body={"domains": domains},
            operation="Property check",
        )

        return cast(dict[str, Any], resp.json())

    async def get_property_check_report(self, report_id: str) -> dict[str, Any] | None:
        """Retrieve a property check report by ID."""
        resp = await self._request(
            "GET",
            f"/api/properties/check/{url_quote(report_id, safe='')}",
            allow_404=True,
            operation="Property check report",
        )
        if resp is None:
            return None
        return cast(dict[str, Any], resp.json())

    async def verify_hosted_property_origin(
        self,
        domain: str,
        *,
        auth_token: str | None = None,
    ) -> dict[str, Any]:
        """Verify a hosted property's origin adagents.json delegation."""
        return await self._request_json(
            "POST",
            f"/api/properties/hosted/{url_quote(domain, safe='')}/verify-origin",
            auth_token=auth_token,
            operation="Hosted property origin verification",
        )

    # ========================================================================
    # Agent Discovery
    # ========================================================================

    async def list_agents(
        self,
        *,
        type: str | None = None,
        health: bool = False,
        capabilities: bool = False,
        properties: bool = False,
        compliance: bool = False,
        metric_id: str | list[str] | None = None,
        accreditation: str | list[str] | None = None,
        q: str | None = None,
        verification_mode: str | list[str] | None = None,
        verified: bool = False,
    ) -> list[FederatedAgentWithDetails]:
        """List registered and discovered agents.

        Measurement filters (``metric_id``, ``accreditation``, ``q``) imply
        ``type=measurement`` on the registry when ``type`` is omitted.
        """
        params: dict[str, Any] = {}
        if type is not None:
            params["type"] = type
        if health:
            params["health"] = "true"
        if capabilities:
            params["capabilities"] = "true"
        if properties:
            params["properties"] = "true"
        if compliance:
            params["compliance"] = "true"
        if metric_id is not None:
            params["metric_id"] = metric_id
        if accreditation is not None:
            params["accreditation"] = accreditation
        if q is not None:
            params["q"] = q
        if verification_mode is not None:
            params["verification_mode"] = verification_mode
        if verified:
            params["verified"] = "true"
        resp = await self._request_ok(
            "GET",
            "/api/registry/agents",
            params=params,
            operation="Agent list",
        )

        data = resp.json()
        return [
            self._parse(FederatedAgentWithDetails, a, "Agent list") for a in data.get("agents", [])
        ]

    async def list_publishers(self) -> list[FederatedPublisher]:
        """List publishers in the registry."""
        resp = await self._request_ok(
            "GET",
            "/api/registry/publishers",
            operation="Publisher list",
        )

        data = resp.json()
        return [
            self._parse(FederatedPublisher, p, "Publisher list") for p in data.get("publishers", [])
        ]

    async def get_registry_stats(self) -> dict[str, Any]:
        """Get aggregate registry statistics."""
        resp = await self._request_ok(
            "GET",
            "/api/registry/stats",
            operation="Registry stats",
        )

        return cast(dict[str, Any], resp.json())

    async def search_agents(
        self,
        *,
        auth_token: str,
        channels: str | None = None,
        property_types: str | None = None,
        markets: str | None = None,
        categories: str | None = None,
        tags: str | None = None,
        delivery_types: str | None = None,
        has_tmp: bool | None = None,
        min_properties: int | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search agents by inventory profile (auth required)."""
        params: dict[str, Any] = {"limit": limit}
        for key, val in [
            ("channels", channels),
            ("property_types", property_types),
            ("markets", markets),
            ("categories", categories),
            ("tags", tags),
            ("delivery_types", delivery_types),
            ("cursor", cursor),
        ]:
            if val is not None:
                params[key] = val
        if has_tmp is not None:
            params["has_tmp"] = str(has_tmp).lower()
        if min_properties is not None:
            params["min_properties"] = min_properties
        resp = await self._request_ok(
            "GET",
            "/api/registry/agents/search",
            params=params,
            auth_token=auth_token,
            operation="Agent search",
        )

        return cast(dict[str, Any], resp.json())

    async def request_crawl(self, domain: str, *, auth_token: str) -> dict[str, Any]:
        """Request a domain re-crawl (auth required)."""
        resp = await self._request_ok(
            "POST",
            "/api/registry/crawl-request",
            json_body={"domain": domain},
            auth_token=auth_token,
            operation="Crawl request",
            expected_status={200, 202},
        )

        return cast(dict[str, Any], resp.json())

    async def request_manager_revalidation(
        self,
        *,
        auth_token: str,
        **body: Any,
    ) -> dict[str, Any]:
        """Request manager revalidation for registry-managed data."""
        return await self._request_json(
            "POST",
            "/api/registry/manager-revalidation-request",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Manager revalidation request",
            expected_status={200, 202},
        )

    async def request_brand_crawl(
        self,
        *,
        auth_token: str,
        **body: Any,
    ) -> dict[str, Any]:
        """Request a brand crawl through the registry."""
        return await self._request_json(
            "POST",
            "/api/registry/brand-crawl-request",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Brand crawl request",
            expected_status={200, 202},
        )

    # ========================================================================
    # Lookups & Authorization
    # ========================================================================

    async def lookup_domain(self, domain: str) -> DomainLookupResult:
        """Find all agents authorized for a publisher domain."""
        resp = await self._request_ok(
            "GET",
            f"/api/registry/lookup/domain/{url_quote(domain, safe='')}",
            operation="Domain lookup",
        )

        return self._parse(DomainLookupResult, resp.json(), "Domain lookup")

    async def lookup_property_identifier(self, type: str, value: str) -> dict[str, Any]:
        """Find agents holding a specific property identifier."""
        resp = await self._request_ok(
            "GET",
            "/api/registry/lookup/property",
            params={"type": type, "value": value},
            operation="Property identifier lookup",
        )

        return cast(dict[str, Any], resp.json())

    async def get_agent_domains(self, agent_url: str) -> dict[str, Any]:
        """Get all publisher domains and identifiers for an agent."""
        encoded = url_quote(agent_url, safe="")
        resp = await self._request_ok(
            "GET",
            f"/api/registry/lookup/agent/{encoded}/domains",
            operation="Agent domains lookup",
        )

        return cast(dict[str, Any], resp.json())

    async def get_publishers_for_agent(
        self,
        agent_url: str,
        *,
        since: str | None = None,
        cursor: str | None = None,
        status: str | None = None,
        include: str | None = None,
        limit: int | None = None,
        legacy_api_prefix: bool = False,
    ) -> dict[str, Any]:
        """List publishers associated with an agent URL."""
        encoded = url_quote(agent_url, safe="")
        params = {
            k: v
            for k, v in {
                "since": since,
                "cursor": cursor,
                "status": status,
                "include": include,
                "limit": limit,
            }.items()
            if v is not None
        }
        prefix = "/api/v1" if legacy_api_prefix else "/v1"
        return await self._request_json(
            "GET",
            f"{prefix}/agents/{encoded}/publishers",
            params=params,
            operation="Publishers for agent",
        )

    async def lookup_operator(
        self,
        domain: str,
        *,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Resolve registry operator metadata for a domain."""
        params: dict[str, Any] = {"domain": domain}
        if scope is not None:
            params["scope"] = scope
        return await self._request_json(
            "GET",
            "/api/registry/operator",
            params=params,
            operation="Operator lookup",
        )

    async def lookup_publisher(self, domain: str) -> dict[str, Any]:
        """Resolve registry publisher metadata for a domain."""
        return await self._request_json(
            "GET",
            "/api/registry/publisher",
            params={"domain": domain},
            operation="Publisher lookup",
        )

    async def lookup_publisher_agent_authorization(
        self,
        domain: str,
        agent: str,
    ) -> dict[str, Any]:
        """Resolve whether a publisher authorizes an agent."""
        return await self._request_json(
            "GET",
            "/api/registry/publisher/authorization",
            params={"domain": domain, "agent": agent},
            operation="Publisher agent authorization lookup",
        )

    async def get_agent_authorizations(
        self,
        agent_url: str,
        *,
        auth_token: str | None = None,
        include: str | None = None,
        evidence: str | None = None,
    ) -> dict[str, Any]:
        """Fetch authorization rows for one agent."""
        params = {"agent_url": agent_url}
        if include is not None:
            params["include"] = include
        if evidence is not None:
            params["evidence"] = evidence
        return await self._request_json(
            "GET",
            "/api/registry/authorizations",
            params=params,
            auth_token=auth_token,
            operation="Agent authorizations",
        )

    async def get_agent_authorizations_snapshot(
        self,
        *,
        auth_token: str | None = None,
        include: str | None = None,
        evidence: str | None = None,
    ) -> dict[str, Any]:
        """Fetch the full authorization snapshot metadata."""
        params = {k: v for k, v in {"include": include, "evidence": evidence}.items() if v}
        return await self._request_json(
            "GET",
            "/api/registry/authorizations/snapshot",
            params=params,
            auth_token=auth_token,
            operation="Agent authorizations snapshot",
        )

    async def validate_product_authorization(
        self,
        agent_url: str,
        publisher_properties: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Check whether an agent is authorized to sell products."""
        resp = await self._request_ok(
            "POST",
            "/api/registry/validate/product-authorization",
            json_body={
                "agent_url": agent_url,
                "publisher_properties": publisher_properties,
            },
            operation="Product authorization",
        )

        return cast(dict[str, Any], resp.json())

    async def expand_product_identifiers(
        self,
        agent_url: str,
        publisher_properties: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Expand publisher_properties selectors into concrete identifiers."""
        resp = await self._request_ok(
            "POST",
            "/api/registry/expand/product-identifiers",
            json_body={
                "agent_url": agent_url,
                "publisher_properties": publisher_properties,
            },
            operation="Expand product identifiers",
        )

        return cast(dict[str, Any], resp.json())

    async def validate_property_authorization(
        self,
        agent_url: str,
        identifier_type: str,
        identifier_value: str,
    ) -> dict[str, Any]:
        """Quick check if a property identifier is authorized for an agent."""
        resp = await self._request_ok(
            "GET",
            "/api/registry/validate/property-authorization",
            params={
                "agent_url": agent_url,
                "identifier_type": identifier_type,
                "identifier_value": identifier_value,
            },
            operation="Property authorization",
        )

        return cast(dict[str, Any], resp.json())

    # ========================================================================
    # Validation Tools
    # ========================================================================

    async def validate_adagents(self, domain: str) -> dict[str, Any]:
        """Validate a domain's adagents.json via the registry API."""
        resp = await self._request_ok(
            "POST",
            "/api/adagents/validate",
            json_body={"domain": domain},
            operation="Adagents validate",
        )

        return cast(dict[str, Any], resp.json())

    async def create_adagents(
        self,
        authorized_agents: list[dict[str, Any]],
        *,
        include_schema: bool = False,
        include_timestamp: bool = False,
        properties: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Generate a valid adagents.json from authorized agents."""
        body: dict[str, Any] = {"authorized_agents": authorized_agents}
        if include_schema:
            body["include_schema"] = True
        if include_timestamp:
            body["include_timestamp"] = True
        if properties is not None:
            body["properties"] = properties
        resp = await self._request_ok(
            "POST",
            "/api/adagents/create",
            json_body=body,
            operation="Adagents create",
        )

        return cast(dict[str, Any], resp.json())

    # ========================================================================
    # Search
    # ========================================================================

    async def api_discovery(self) -> dict[str, Any]:
        """Get API discovery info (links to entry points and docs)."""
        resp = await self._request_ok(
            "GET",
            "/api",
            operation="API discovery",
        )

        return cast(dict[str, Any], resp.json())

    async def search(self, q: str) -> dict[str, Any]:
        """Search across brands, publishers, and properties."""
        resp = await self._request_ok(
            "GET",
            "/api/search",
            params={"q": q},
            operation="Search",
        )

        return cast(dict[str, Any], resp.json())

    async def lookup_manifest_ref(self, domain: str, *, type: str | None = None) -> dict[str, Any]:
        """Find the best manifest reference for a domain."""
        params: dict[str, Any] = {"domain": domain}
        if type is not None:
            params["type"] = type
        resp = await self._request_ok(
            "GET",
            "/api/manifest-refs/lookup",
            params=params,
            operation="Manifest ref lookup",
        )

        return cast(dict[str, Any], resp.json())

    # ========================================================================
    # Agent Probing
    # ========================================================================

    async def discover_agent(self, url: str) -> dict[str, Any]:
        """Probe an agent URL to discover its capabilities."""
        resp = await self._request_ok(
            "GET",
            "/api/public/discover-agent",
            params={"url": url},
            operation="Agent discovery",
        )

        return cast(dict[str, Any], resp.json())

    async def get_agent_formats(self, url: str) -> dict[str, Any]:
        """Fetch creative formats from an agent."""
        resp = await self._request_ok(
            "GET",
            "/api/public/agent-formats",
            params={"url": url},
            operation="Agent formats",
        )

        return cast(dict[str, Any], resp.json())

    async def get_agent_products(self, url: str) -> dict[str, Any]:
        """Fetch products from a sales agent."""
        resp = await self._request_ok(
            "GET",
            "/api/public/agent-products",
            params={"url": url},
            operation="Agent products",
        )

        return cast(dict[str, Any], resp.json())

    async def validate_publisher(self, domain: str) -> dict[str, Any]:
        """Validate a publisher domain's adagents.json and return stats."""
        resp = await self._request_ok(
            "GET",
            "/api/public/validate-publisher",
            params={"domain": domain},
            operation="Publisher validation",
        )

        return cast(dict[str, Any], resp.json())

    # ========================================================================
    # Compliance, Verification, and Member Management
    # ========================================================================

    @staticmethod
    def _encoded_agent_url(agent_url: str) -> str:
        return url_quote(agent_url, safe="")

    async def get_agent_compliance(self, agent_url: str) -> dict[str, Any]:
        """Fetch the latest compliance summary for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/compliance",
            operation="Agent compliance",
        )

    async def get_jwks(self) -> dict[str, Any]:
        """Fetch the registry JWKS document."""
        return await self._request_json(
            "GET",
            "/api/.well-known/jwks.json",
            operation="Registry JWKS",
        )

    async def get_agent_verification(self, agent_url: str) -> dict[str, Any]:
        """Fetch the active verification badges for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/verification",
            operation="Agent verification",
        )

    async def get_agent_badge_svg(self, agent_url: str, role: str) -> str:
        """Fetch an agent verification badge SVG."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_text(
            "GET",
            f"/api/registry/agents/{encoded}/badge/{url_quote(role, safe='')}.svg",
            operation="Agent badge SVG",
        )

    async def get_agent_badge_embed(self, agent_url: str, role: str) -> dict[str, Any]:
        """Fetch embeddable badge HTML/Markdown for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/badge/{url_quote(role, safe='')}/embed",
            operation="Agent badge embed",
        )

    async def get_agent_badge_versioned_svg(
        self,
        agent_url: str,
        role: str,
        version: str,
    ) -> str:
        """Fetch a version-pinned agent verification badge SVG."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_text(
            "GET",
            "/api/registry/agents/"
            f"{encoded}/badge/{url_quote(role, safe='')}/{url_quote(version, safe='')}.svg",
            operation="Versioned agent badge SVG",
        )

    async def get_agent_badge_versioned_embed(
        self,
        agent_url: str,
        role: str,
        version: str,
    ) -> dict[str, Any]:
        """Fetch version-pinned embeddable badge HTML/Markdown for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "GET",
            "/api/registry/agents/"
            f"{encoded}/badge/{url_quote(role, safe='')}/{url_quote(version, safe='')}/embed",
            operation="Versioned agent badge embed",
        )

    async def get_agent_storyboard_status(self, agent_url: str) -> dict[str, Any]:
        """Fetch latest storyboard status for one agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/storyboard-status",
            operation="Agent storyboard status",
        )

    async def bulk_agent_storyboard_status(
        self,
        *,
        auth_token: str | None = None,
        **body: Any,
    ) -> dict[str, Any]:
        """Fetch storyboard status for multiple agents."""
        return await self._request_json(
            "POST",
            "/api/registry/agents/storyboard-status",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Bulk agent storyboard status",
        )

    async def get_agent_compliance_history(
        self,
        agent_url: str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Fetch compliance run history for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        params = {"limit": limit} if limit is not None else None
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/compliance/history",
            params=params,
            operation="Agent compliance history",
        )

    async def update_agent_lifecycle(
        self,
        agent_url: str,
        *,
        auth_token: str,
        **body: Any,
    ) -> dict[str, Any]:
        """Update an agent lifecycle stage."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "PUT",
            f"/api/registry/agents/{encoded}/lifecycle",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Agent lifecycle update",
        )

    async def update_agent_compliance_opt_out(
        self,
        agent_url: str,
        *,
        auth_token: str,
        **body: Any,
    ) -> dict[str, Any]:
        """Update agent compliance opt-out state."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "PUT",
            f"/api/registry/agents/{encoded}/compliance/opt-out",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Agent compliance opt-out update",
        )

    async def get_agent_monitoring_settings(self, agent_url: str) -> dict[str, Any]:
        """Fetch monitoring settings for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/monitoring/settings",
            operation="Agent monitoring settings",
        )

    async def update_agent_monitoring_pause(
        self,
        agent_url: str,
        *,
        auth_token: str,
        **body: Any,
    ) -> dict[str, Any]:
        """Pause or resume monitoring for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "PUT",
            f"/api/registry/agents/{encoded}/monitoring/pause",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Agent monitoring pause update",
        )

    async def update_agent_monitoring_interval(
        self,
        agent_url: str,
        *,
        auth_token: str,
        **body: Any,
    ) -> dict[str, Any]:
        """Update monitoring interval for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "PUT",
            f"/api/registry/agents/{encoded}/monitoring/interval",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Agent monitoring interval update",
        )

    async def requeue_agent_for_heartbeat(
        self,
        agent_url: str,
        *,
        auth_token: str,
    ) -> dict[str, Any]:
        """Requeue an agent for heartbeat monitoring."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "POST",
            f"/api/registry/agents/{encoded}/monitoring/requeue",
            auth_token=auth_token,
            operation="Agent heartbeat requeue",
        )

    async def get_agent_compliance_step_diagnostics(
        self,
        agent_url: str,
        *,
        run_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Fetch compliance step diagnostics for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        params = {k: v for k, v in {"run_id": run_id, "limit": limit}.items() if v is not None}
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/compliance/diagnostics",
            params=params,
            operation="Agent compliance step diagnostics",
        )

    async def get_agent_monitoring_requests(
        self,
        agent_url: str,
        *,
        limit: int | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Fetch monitoring requests for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        params = {k: v for k, v in {"limit": limit, "since": since}.items() if v is not None}
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/monitoring/requests",
            params=params,
            operation="Agent monitoring requests",
        )

    async def refresh_agent(
        self,
        agent_url: str,
        *,
        auth_token: str,
    ) -> dict[str, Any]:
        """Refresh an agent's registry health/capability/compliance snapshot."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "POST",
            f"/api/registry/agents/{encoded}/refresh",
            auth_token=auth_token,
            operation="Agent refresh",
            expected_status={200, 202},
        )

    async def get_agent_auth_status(self, agent_url: str) -> dict[str, Any]:
        """Fetch saved authentication status for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/auth-status",
            operation="Agent auth status",
        )

    async def connect_agent(
        self,
        agent_url: str,
        *,
        auth_token: str,
        **body: Any,
    ) -> dict[str, Any]:
        """Connect registry-managed credentials for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "PUT",
            f"/api/registry/agents/{encoded}/connect",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Agent connect",
        )

    async def save_agent_oauth_client_credentials(
        self,
        agent_url: str,
        *,
        auth_token: str,
        **body: Any,
    ) -> dict[str, Any]:
        """Save OAuth client credentials for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "PUT",
            f"/api/registry/agents/{encoded}/oauth-client-credentials",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Agent OAuth client credentials save",
        )

    async def test_agent_oauth_client_credentials(
        self,
        agent_url: str,
        *,
        auth_token: str,
    ) -> dict[str, Any]:
        """Test saved OAuth client credentials for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "POST",
            f"/api/registry/agents/{encoded}/oauth-client-credentials/test",
            auth_token=auth_token,
            operation="Agent OAuth client credentials test",
        )

    async def get_applicable_storyboards(self, agent_url: str) -> dict[str, Any]:
        """Resolve compliance storyboards applicable to an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "GET",
            f"/api/registry/agents/{encoded}/applicable-storyboards",
            operation="Applicable storyboards",
        )

    async def list_storyboards(
        self,
        *,
        category: str | None = None,
        compliance_target: str | None = None,
    ) -> dict[str, Any]:
        """List available registry compliance storyboards."""
        params = {
            k: v
            for k, v in {"category": category, "compliance_target": compliance_target}.items()
            if v is not None
        }
        return await self._request_json(
            "GET",
            "/api/storyboards",
            params=params,
            operation="Storyboard list",
        )

    async def get_storyboard(
        self,
        storyboard_id: str,
        *,
        compliance_target: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one registry compliance storyboard."""
        params = {"compliance_target": compliance_target} if compliance_target is not None else None
        return await self._request_json(
            "GET",
            f"/api/storyboards/{url_quote(storyboard_id, safe='')}",
            params=params,
            operation="Storyboard get",
        )

    async def find_brand(self, q: str, *, limit: int | None = None) -> dict[str, Any]:
        """Find brands by name or domain."""
        params: dict[str, Any] = {"q": q}
        if limit is not None:
            params["limit"] = limit
        return await self._request_json(
            "GET",
            "/api/brands/find",
            params=params,
            operation="Brand find",
        )

    async def setup_my_brand(self, *, auth_token: str, **body: Any) -> dict[str, Any]:
        """Set up a brand record for the authenticated member."""
        return await self._request_json(
            "POST",
            "/api/brands/setup-my-brand",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Brand setup",
        )

    async def bulk_property_check(
        self,
        *,
        auth_token: str | None = None,
        **body: Any,
    ) -> dict[str, Any]:
        """Start a bulk property check."""
        return await self._request_json(
            "POST",
            "/api/properties/check/bulk",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Bulk property check",
            expected_status={200, 202},
        )

    async def get_bulk_property_check_report(self, report_id: str) -> dict[str, Any]:
        """Retrieve a bulk property check report by ID."""
        return await self._request_json(
            "GET",
            f"/api/properties/check/bulk/{url_quote(report_id, safe='')}",
            operation="Bulk property check report",
        )

    async def run_storyboard_step(
        self,
        agent_url: str,
        storyboard_id: str,
        step_id: str,
        *,
        auth_token: str,
        **body: Any,
    ) -> dict[str, Any]:
        """Run one storyboard step against an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "POST",
            "/api/registry/agents/"
            f"{encoded}/storyboard/{url_quote(storyboard_id, safe='')}/step/"
            f"{url_quote(step_id, safe='')}",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Storyboard step run",
        )

    async def get_storyboard_first_step(
        self,
        storyboard_id: str,
        *,
        compliance_target: str | None = None,
    ) -> dict[str, Any]:
        """Fetch the first runnable step for a storyboard."""
        params = {"compliance_target": compliance_target} if compliance_target is not None else None
        return await self._request_json(
            "GET",
            f"/api/storyboards/{url_quote(storyboard_id, safe='')}/first-step",
            params=params,
            operation="Storyboard first step",
        )

    async def run_storyboard(
        self,
        agent_url: str,
        storyboard_id: str,
        *,
        auth_token: str,
    ) -> dict[str, Any]:
        """Run a storyboard against an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "POST",
            f"/api/registry/agents/{encoded}/storyboard/{url_quote(storyboard_id, safe='')}/run",
            auth_token=auth_token,
            operation="Storyboard run",
            expected_status={200, 202},
        )

    async def compare_storyboard(
        self,
        agent_url: str,
        storyboard_id: str,
        *,
        auth_token: str,
    ) -> dict[str, Any]:
        """Compare storyboard runs for an agent."""
        encoded = self._encoded_agent_url(agent_url)
        return await self._request_json(
            "POST",
            "/api/registry/agents/"
            f"{encoded}/storyboard/{url_quote(storyboard_id, safe='')}/compare",
            auth_token=auth_token,
            operation="Storyboard compare",
        )

    async def list_member_agents(
        self,
        *,
        auth_token: str,
        org: str | None = None,
    ) -> dict[str, Any]:
        """List the authenticated member's registered agents."""
        params = {"org": org} if org is not None else None
        return await self._request_json(
            "GET",
            "/api/me/agents",
            params=params,
            auth_token=auth_token,
            operation="Member agent list",
        )

    async def register_member_agent(
        self,
        *,
        auth_token: str,
        org: str | None = None,
        **body: Any,
    ) -> dict[str, Any]:
        """Register an agent for the authenticated member."""
        params = {"org": org} if org is not None else None
        return await self._request_json(
            "POST",
            "/api/me/agents",
            params=params,
            json_body=dict(body),
            auth_token=auth_token,
            operation="Member agent register",
            expected_status={200, 201},
        )

    async def update_member_agent(
        self,
        url: str,
        *,
        auth_token: str,
        org: str | None = None,
        **body: Any,
    ) -> dict[str, Any]:
        """Update one member-owned agent."""
        params = {"org": org} if org is not None else None
        return await self._request_json(
            "PATCH",
            f"/api/me/agents/{url_quote(url, safe='')}",
            params=params,
            json_body=dict(body),
            auth_token=auth_token,
            operation="Member agent update",
        )

    async def remove_member_agent(
        self,
        url: str,
        *,
        auth_token: str,
        org: str | None = None,
    ) -> dict[str, Any]:
        """Remove one member-owned agent."""
        params = {"org": org} if org is not None else None
        return await self._request_json(
            "DELETE",
            f"/api/me/agents/{url_quote(url, safe='')}",
            params=params,
            auth_token=auth_token,
            operation="Member agent remove",
        )

    async def create_organization(self, *, auth_token: str, **body: Any) -> dict[str, Any]:
        """Create an organization for the authenticated member."""
        return await self._request_json(
            "POST",
            "/api/organizations",
            json_body=dict(body),
            auth_token=auth_token,
            operation="Organization create",
            expected_status={200, 201},
        )

    # ========================================================================
    # Change Feed
    # ========================================================================

    async def get_feed(
        self,
        *,
        auth_token: str,
        cursor: str | None = None,
        types: str | None = None,
        limit: int = 100,
    ) -> FeedPage:
        """Poll the registry change feed (auth required).

        Returns a FeedPage with events, cursor, and has_more.
        Pass cursor from previous response to resume.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if types is not None:
            params["types"] = types
        resp = await self._request_ok(
            "GET",
            "/api/registry/feed",
            params=params,
            auth_token=auth_token,
            operation="Feed poll",
        )

        return self._parse(FeedPage, resp.json(), "Feed poll")
