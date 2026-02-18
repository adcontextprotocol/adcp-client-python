"""Client for the AdCP registry API (brand, property, and member lookups)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import ValidationError

from adcp.exceptions import RegistryError
from adcp.types.core import Member, ResolvedBrand, ResolvedProperty

DEFAULT_REGISTRY_URL = "https://agenticadvertising.org"
MAX_BULK_DOMAINS = 100


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

    async def lookup_brands(
        self, domains: list[str]
    ) -> dict[str, ResolvedBrand | None]:
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
            domains[i : i + MAX_BULK_DOMAINS]
            for i in range(0, len(domains), MAX_BULK_DOMAINS)
        ]

        chunk_results = await asyncio.gather(
            *[self._lookup_brands_chunk(chunk) for chunk in chunks]
        )

        merged: dict[str, ResolvedBrand | None] = {}
        for result in chunk_results:
            merged.update(result)
        return merged

    async def _lookup_brands_chunk(
        self, domains: list[str]
    ) -> dict[str, ResolvedBrand | None]:
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
            raise RegistryError(
                f"Bulk brand lookup timed out after {self._timeout}s"
            ) from e
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
            raise RegistryError(
                f"Property lookup timed out after {self._timeout}s"
            ) from e
        except httpx.HTTPError as e:
            raise RegistryError(f"Property lookup failed: {e}") from e
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"Property lookup failed: invalid response: {e}") from e

    async def lookup_properties(
        self, domains: list[str]
    ) -> dict[str, ResolvedProperty | None]:
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
            domains[i : i + MAX_BULK_DOMAINS]
            for i in range(0, len(domains), MAX_BULK_DOMAINS)
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
            raise RegistryError(
                f"Bulk property lookup timed out after {self._timeout}s"
            ) from e
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
        """
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
