"""MCP property-list resolution and opt-in buyer product checks."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal, TypeVar, cast

from pydantic import RootModel

from adcp.identifiers import (
    IdentifierValidationError,
    NormalizedIdentifier,
    normalize_domain,
    normalize_identifier,
)

logger = logging.getLogger(__name__)

_Request = TypeVar("_Request")

# ``core/identifier.json`` (AdCP 3.3): "Domain and subdomain values MUST be
# lowercase ASCII hostnames (IDNs use lowercase A-label/punycode form), without
# scheme, path, port, whitespace, or trailing dot. Domain permits one leading
# '*.'; subdomain does not. [...] Invalid values MUST cause rejection of the
# containing request; receivers MUST NOT silently discard identifiers." The
# patterns and length limits below are the schema's.
_HOSTNAME = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
_DOMAIN_IDENTIFIER_RE = re.compile(rf"(?:\*\.)?{_HOSTNAME}")
_SUBDOMAIN_IDENTIFIER_RE = re.compile(_HOSTNAME)


def is_valid_domain_identifier(identifier_type: str, value: Any) -> bool:
    """Whether a ``domain`` / ``subdomain`` identifier value has the 3.3 format.

    Checks exactly what ``core/identifier.json`` requires; no value is
    rewritten. Identifier types other than ``domain`` and ``subdomain`` are not
    constrained by this rule and return ``True``.
    """
    if identifier_type not in {"domain", "subdomain"}:
        return True
    if not isinstance(value, str):
        return False
    if identifier_type == "subdomain":
        return len(value) <= 253 and _SUBDOMAIN_IDENTIFIER_RE.fullmatch(value) is not None
    limit = 255 if value.startswith("*.") else 253
    return len(value) <= limit and _DOMAIN_IDENTIFIER_RE.fullmatch(value) is not None


class PropertyListIdentifierError(ValueError):
    """A property-list domain identifier cannot be converted for submission.

    ``field`` is the request path (``base_properties[0].identifiers[2].value``)
    and ``reason`` the :class:`~adcp.identifiers.IdentifierValidationError`
    reason. The message excludes the supplied value.
    """

    def __init__(self, field: str, identifier_type: str, reason: str):
        self.field = field
        self.identifier_type = identifier_type
        self.reason = reason
        super().__init__(f"Invalid {identifier_type} identifier at {field}: {reason}")


def _wire_request(request: Any) -> dict[str, Any]:
    if hasattr(request, "model_dump"):
        return dict(request.model_dump(mode="json", exclude_none=True))
    if isinstance(request, Mapping):
        return deepcopy(dict(request))
    raise TypeError("expected a property-list request model or mapping")


def _identifier_members(request: Any) -> dict[str, Any]:
    """Dump only the members that carry identifiers (``base_properties``, ``filters``).

    The receiver check reads these alone, so it never serializes the rest of
    the request (for example a buyer's ``push_notification_config``).
    """

    def dump(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        if isinstance(value, (list, tuple)):
            return [dump(item) for item in value]
        return value

    return {
        name: dump(field(request, name))
        for name in ("base_properties", "filters")
        if field(request, name) is not None
    }


def _identifier_slots(wire: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(path, identifier)`` for every identifier object in the request.

    Covers direct-identifier ``base_properties[]`` sources and
    ``filters.exclude_identifiers[]``, the identifier slots of
    ``create_property_list`` / ``update_property_list``.
    """
    for index, source in enumerate(wire.get("base_properties") or ()):
        if isinstance(source, dict) and source.get("selection_type") == "identifiers":
            for position, item in enumerate(source.get("identifiers") or ()):
                if isinstance(item, dict):
                    yield f"base_properties[{index}].identifiers[{position}]", item
    filters = wire.get("filters")
    if isinstance(filters, dict):
        for position, item in enumerate(filters.get("exclude_identifiers") or ()):
            if isinstance(item, dict):
                yield f"filters.exclude_identifiers[{position}]", item


def normalize_property_list_identifiers(request: _Request) -> _Request:
    """Explicitly convert a property-list request's domain identifiers.

    The sender-side conversion AdCP 3.3 asks for before submission: every
    ``domain`` / ``subdomain`` identifier in ``base_properties[]`` and
    ``filters.exclude_identifiers[]``, and every ``base_properties[]``
    ``publisher_domain``, is lowercased, converted to its IDNA A-label and
    stripped of surrounding whitespace and a trailing dot (``Example.com.``
    becomes ``example.com``). A value that cannot be converted (a URL, a port,
    an IP address, a misplaced wildcard, an invalid IDN) raises
    :class:`PropertyListIdentifierError`; it is never silently reinterpreted.
    In particular a pasted URL is refused rather than reduced to its host:
    extract the hostname yourself once you have confirmed that is intended.

    Returns a new request of the same kind (a generated request model or a
    mapping); other identifier types are left unchanged. Receivers enforce the
    format without rewriting, so convert before calling ``create_property_list``
    or ``update_property_list``.
    """
    wire = _wire_request(request)
    for path, item in _identifier_slots(wire):
        kind, value = item.get("type"), item.get("value")
        if kind not in {"domain", "subdomain"}:
            continue
        try:
            # normalize_identifier refuses a non-string value (expected_string).
            item["value"] = normalize_identifier(kind, cast(str, value)).value
        except IdentifierValidationError as exc:
            raise PropertyListIdentifierError(f"{path}.value", str(kind), exc.reason) from None
    for index, source in enumerate(wire.get("base_properties") or ()):
        if isinstance(source, dict) and "publisher_domain" in source:
            try:
                source["publisher_domain"] = normalize_domain(
                    source["publisher_domain"], allow_wildcard=False
                ).value
            except IdentifierValidationError as exc:
                raise PropertyListIdentifierError(
                    f"base_properties[{index}].publisher_domain", "domain", exc.reason
                ) from None
    if hasattr(request, "model_validate"):
        return type(request).model_validate(wire)  # type: ignore[no-any-return]
    return wire  # type: ignore[return-value]


def validate_property_list_identifiers(request: Any) -> None:
    """Reject a property-list request whose domain identifiers are malformed.

    Receiver side of the AdCP 3.3 rule: an invalid ``domain`` / ``subdomain``
    value in ``base_properties[]`` or ``filters.exclude_identifiers[]`` fails
    the whole request with ``INVALID_REQUEST`` (``field`` names the first
    offending value); nothing is rewritten or dropped. The decisioning
    framework runs this before ``create_property_list`` / ``update_property_list``
    reach the platform.
    """
    from adcp.decisioning.types import AdcpError

    for path, item in _identifier_slots(_identifier_members(request)):
        kind = item.get("type")
        if isinstance(kind, str) and not is_valid_domain_identifier(kind, item.get("value")):
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    f"{kind} identifiers must be lowercase ASCII hostnames (IDNs in "
                    "A-label form) without scheme, path, port, whitespace or trailing "
                    "dot; only domain permits a leading '*.'."
                ),
                recovery="correctable",
                field=f"{path}.value",
                suggestion=(
                    "convert the value before submission, e.g. with "
                    "adcp.property_lists.normalize_property_list_identifiers"
                ),
            )


def field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def request_property_list(request: Any) -> Any:
    """Read compact 3.2 offer filters first, falling back to legacy discovery."""
    criteria = field(request, "criteria")
    # Request criteria are wire mappings or generated request models.
    if isinstance(criteria, Mapping) or hasattr(type(criteria), "model_fields"):
        ref = field(field(criteria, "offer_filters"), "property_list")
        if ref is not None:
            return ref
    return field(request, "property_list")


@dataclass(frozen=True)
class ResolvedPropertyList:
    identifiers: tuple[NormalizedIdentifier, ...]
    cache_valid_until: datetime | None = None
    pages: int = 1


class MCPPropertyListFetcher:
    """Resolve get_property_list over MCP, including pagination and caching.

    Uses the reference URL directly, never appends a REST path. Bearer tokens
    are scoped to that connection. Destination checks, IP pinning, disabled
    redirects and disabled environment proxies apply before every connection.
    Cache entries are isolated by URL, list ID and a digest of the credential;
    every page must provide a future expiry for the result to be cached.
    """

    def __init__(
        self,
        *,
        max_cache_entries: int = 128,
        max_pages: int = 10_000,
        max_identifiers: int = 1_000_000,
        max_resolution_bytes: int = 64 * 1024 * 1024,
        max_cached_identifiers: int = 250_000,
        max_cache_bytes: int = 16 * 1024 * 1024,
    ):
        if min(max_cache_entries, max_cached_identifiers, max_cache_bytes) < 0:
            raise ValueError("cache budgets must be non-negative")
        if min(max_pages, max_identifiers, max_resolution_bytes) < 1:
            raise ValueError("resolution budgets must be positive")
        self.max_cache_entries = max_cache_entries
        self.max_pages = max_pages
        self.max_identifiers = max_identifiers
        self.max_resolution_bytes = max_resolution_bytes
        self.max_cached_identifiers = max_cached_identifiers
        self.max_cache_bytes = max_cache_bytes
        self._cache: OrderedDict[tuple[str, str, str], ResolvedPropertyList] = OrderedDict()
        self._cache_weights: dict[tuple[str, str, str], tuple[int, int]] = {}
        self._cached_identifiers = 0
        self._cache_bytes = 0

    def clear_cache(self) -> None:
        """Invalidate cached resolutions, for example after a list-change webhook."""
        self._cache.clear()
        self._cache_weights.clear()
        self._cached_identifiers = 0
        self._cache_bytes = 0

    def _discard(self, key: tuple[str, str, str]) -> None:
        self._cache.pop(key, None)
        count, size = self._cache_weights.pop(key, (0, 0))
        self._cached_identifiers -= count
        self._cache_bytes -= size

    def _create_client(self, agent_url: str, auth_token: str | None) -> Any:
        import httpx2

        from adcp._version import resolve_adcp_version
        from adcp.client import ADCPClient
        from adcp.signing import build_async_ip_pinned_transport2
        from adcp.types.core import AgentConfig, Protocol

        def pinned_factory(**kwargs: Any) -> Any:
            return httpx2.AsyncClient(
                **{
                    **kwargs,
                    "transport": build_async_ip_pinned_transport2(agent_url),
                    "follow_redirects": False,
                    "trust_env": False,
                }
            )

        return ADCPClient(
            AgentConfig(
                id="property-list",
                agent_uri=agent_url,
                protocol=Protocol.MCP,
                auth_token=auth_token,
                auth_header="Authorization",
                auth_type="bearer",
            ),
            httpx_client_factory=pinned_factory,
            account_policy="off",
            # Shared-list credentials authorize this task alone. Keep the
            # reference's existing SDK contract without a discovery request
            # that the issuer must reject for the scoped bearer credential.
            server_version=resolve_adcp_version(None),
        )

    async def resolve(self, ref: Any) -> ResolvedPropertyList:
        from adcp.decisioning.types import AdcpError
        from adcp.types.domains.property.get_property_list_request import GetPropertyListRequest
        from adcp.webhooks import (
            WebhookDestinationPolicy,
            WebhookDestinationValidationError,
            validate_webhook_destination_url,
        )

        agent_url, list_id = str(field(ref, "agent_url")), field(ref, "list_id")
        auth_token = field(ref, "auth_token")
        if not isinstance(list_id, str) or not list_id:
            raise AdcpError(
                "INVALID_REQUEST", message="Property list_id is required", recovery="correctable"
            )
        try:
            validate_webhook_destination_url(
                agent_url,
                policy=WebhookDestinationPolicy.production(),
                field="property_list.agent_url",
            )
        except WebhookDestinationValidationError:
            raise AdcpError(
                "INVALID_REQUEST",
                message="Property list destination is invalid",
                recovery="correctable",
            ) from None
        token_digest = hashlib.sha256((auth_token or "").encode()).hexdigest()
        key = (agent_url, list_id, token_digest)
        now = datetime.now(timezone.utc)
        # Purge expired entries so credential-scoped caches stay bounded.
        for old_key, entry in list(self._cache.items()):
            if entry.cache_valid_until is None or entry.cache_valid_until <= now:
                self._discard(old_key)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        identifiers: list[NormalizedIdentifier] = []
        resolution_bytes = 0
        expiries: list[datetime] = []
        all_cacheable = True
        cursors: set[str] = set()
        cursor: str | None = None
        try:
            async with self._create_client(agent_url, auth_token) as client:
                for page_number in range(1, self.max_pages + 1):
                    request = GetPropertyListRequest.model_validate(
                        {
                            "list_id": list_id,
                            "resolve": True,
                            "pagination": {"max_results": 10000, "cursor": cursor},
                        }
                    )
                    result = await client.get_property_list(request)
                    if not result.success or result.data is None:
                        raise ValueError("property list resolution failed")
                    page = result.data
                    if field(field(page, "list"), "list_id") != list_id:
                        raise ValueError("property list response ID does not match")
                    raw_identifiers = field(page, "identifiers")
                    if raw_identifiers is None:
                        raise ValueError("property list was not resolved")
                    if len(identifiers) + len(raw_identifiers) > self.max_identifiers:
                        raise ValueError("property list exceeded max_identifiers")
                    for item in raw_identifiers:
                        identifier = normalize_identifier(field(item, "type"), field(item, "value"))
                        # AdCP 3.3: an invalid domain identifier in a fetched
                        # page invalidates the whole snapshot; never rewrite it.
                        if not is_valid_domain_identifier(
                            identifier.type, identifier.original_value
                        ):
                            raise ValueError("property list identifier is not a valid hostname")
                        # Include source/canonical strings and conservative object overhead
                        # so tiny pages cannot evade the aggregate response budget.
                        resolution_bytes += (
                            len(identifier.value.encode("utf-8"))
                            + len(identifier.original_value.encode("utf-8"))
                            + len(identifier.type.encode("utf-8"))
                            + 128
                        )
                        if resolution_bytes > self.max_resolution_bytes:
                            raise ValueError("property list exceeded max_resolution_bytes")
                        identifiers.append(identifier)
                    expiry = field(page, "cache_valid_until")
                    if isinstance(expiry, str):
                        expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                    if expiry is None:
                        all_cacheable = False
                    elif expiry.tzinfo is None:
                        raise ValueError("cache expiry must include a timezone")
                    else:
                        expiries.append(expiry)
                    pagination = field(page, "pagination")
                    if not field(pagination, "has_more", False):
                        break
                    cursor = field(pagination, "cursor")
                    if not isinstance(cursor, str) or not cursor:
                        raise ValueError("invalid property list cursor")
                    # Bound cycle-detection memory even for large opaque cursors.
                    cursor_digest = hashlib.sha256(cursor.encode("utf-8")).hexdigest()
                    if cursor_digest in cursors:
                        raise ValueError("repeated property list cursor")
                    cursors.add(cursor_digest)
                else:
                    raise ValueError("property list exceeded max_pages")
        except Exception as exc:
            # Upstream messages and exception chains can contain bearer credentials.
            logger.warning("Property list resolution failed (%s)", type(exc).__name__)
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Property list resolution failed",
                recovery="transient",
            ) from None
        expiry = min(expiries) if all_cacheable and expiries else None
        resolved = ResolvedPropertyList(tuple(identifiers), expiry, page_number)
        cache_weight = resolution_bytes + sum(len(part.encode("utf-8")) for part in key) + 128
        if (
            self.max_cache_entries
            and expiry is not None
            and expiry > datetime.now(timezone.utc)
            and len(identifiers) <= self.max_cached_identifiers
            and cache_weight <= self.max_cache_bytes
        ):
            self._discard(key)
            while self._cache and (
                len(self._cache) >= self.max_cache_entries
                or self._cached_identifiers + len(identifiers) > self.max_cached_identifiers
                or self._cache_bytes + cache_weight > self.max_cache_bytes
            ):
                self._discard(next(iter(self._cache)))
            self._cache[key] = resolved
            self._cache_weights[key] = (len(identifiers), cache_weight)
            self._cached_identifiers += len(identifiers)
            self._cache_bytes += cache_weight
        return resolved

    async def fetch(
        self, agent_url: str, list_id: str, *, auth_token: str | None = None
    ) -> list[str]:
        """Compatibility surface returning canonical values; prefer resolve for types."""
        result = await self.resolve(
            {"agent_url": agent_url, "list_id": list_id, "auth_token": auth_token}
        )
        return [identifier.value for identifier in result.identifiers]


@lru_cache(maxsize=1)
def _default_property_list_fetcher() -> MCPPropertyListFetcher:
    return MCPPropertyListFetcher()


async def _expand_publisher_selectors(_seller_url: str, selectors: list[Any]) -> dict[str, Any]:
    """Expand seller-owned selectors against publishers' public catalogs."""
    from adcp.adagents import fetch_adagents

    manifests: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for selector in selectors:
        if field(selector, "selection_type") not in {"all", "by_id", "by_tag"}:
            raise ValueError("Unknown publisher property selector")
        domains = field(selector, "publisher_domains") or [field(selector, "publisher_domain")]
        for domain in domains:
            if domain not in manifests:
                manifests[domain] = await fetch_adagents(domain)
            for prop in manifests[domain].get("properties", []):
                owner = prop.get("publisher_domain", domain)
                if owner != domain:
                    continue
                selection = field(selector, "selection_type")
                if selection == "by_id" and prop.get("property_id") not in field(
                    selector, "property_ids", []
                ):
                    continue
                if selection == "by_tag" and not set(prop.get("tags", [])) & set(
                    field(selector, "property_tags", [])
                ):
                    continue
                if not prop.get("property_id") or not prop.get("identifiers"):
                    raise ValueError("Selected property has no usable identifiers")
                for identifier in prop["identifiers"]:
                    rows.append(
                        {
                            **identifier,
                            "publisher_domain": domain,
                            "property_id": prop.get("property_id"),
                        }
                    )
    return {"identifiers": rows}


@dataclass(frozen=True)
class ProductPropertyAssessment:
    product_id: str
    status: Literal["match", "mismatch", "unjudgeable"]


def _identifier_matches(actual: NormalizedIdentifier, allowed: dict[str, set[str]]) -> bool:
    values = allowed.get(actual.type, set())
    if actual.value in values:
        return True
    if actual.type != "domain":
        return False
    # Index lists once, rather than scanning up to 100k allowed identifiers
    # for every inventory property. Wildcards exclude the base hostname.
    labels = actual.value.split(".")
    if any("*." + ".".join(labels[index:]) in values for index in range(1, len(labels))):
        return True
    if labels[0] in {"www", "m"}:
        base = ".".join(labels[1:])
        if base in values:
            from adcp.adagents import domain_matches

            return domain_matches(actual.value, base)
    return False


class ProductPropertyPolicy:
    """Opt-in buyer check: filter mismatches, or flag products in TaskResult.metadata.

    Products whose selectors cannot be expanded are marked unjudgeable and
    retained, including app/CTV inventory with incomplete registry coverage.
    Filtering never claims to change seller pagination totals or to enforce
    delivery targeting. Registry expansion is read-only.
    """

    def __init__(
        self,
        *,
        mode: Literal["filter", "flag"] = "filter",
        fetcher: MCPPropertyListFetcher | None = None,
        registry: Any = None,
        expand_identifiers: Callable[..., Any] | None = None,
    ):
        if mode not in {"filter", "flag"}:
            raise ValueError("mode must be 'filter' or 'flag'")
        self.mode = mode
        self.fetcher = fetcher if fetcher is not None else MCPPropertyListFetcher()
        self.registry = registry
        self.expand_identifiers = expand_identifiers

    async def assess(
        self, products: list[Any], allowed: ResolvedPropertyList, *, seller_url: str
    ) -> list[ProductPropertyAssessment]:
        from adcp.registry import RegistryClient

        registry = self.registry if self.registry is not None else RegistryClient()
        cache: dict[str, Any] = {}
        allowed_values: dict[str, set[str]] = {}
        for identifier in allowed.identifiers:
            allowed_values.setdefault(identifier.type, set()).add(identifier.value)
        assessments: list[ProductPropertyAssessment] = []
        try:
            for product in products:
                product_id = str(field(product, "product_id", "?"))
                selectors = [
                    item.root if isinstance(item, RootModel) else item
                    for item in (field(product, "publisher_properties") or [])
                ]
                wire = [
                    (
                        item.model_dump(mode="json", exclude_none=True)
                        if hasattr(item, "model_dump")
                        else item
                    )
                    for item in selectors
                ]
                status: Literal["match", "mismatch", "unjudgeable"] = "unjudgeable"
                try:
                    key = json.dumps(wire, sort_keys=True)
                    if key not in cache:
                        expand = self.expand_identifiers or (
                            registry.expand_product_identifiers
                            if seller_url
                            else _expand_publisher_selectors
                        )
                        cache[key] = await expand(seller_url, wire)
                    expanded = cache[key]
                    rows = field(expanded, "identifiers") or []
                    groups: dict[tuple[str, str], list[NormalizedIdentifier]] = {}
                    for index, row in enumerate(rows):
                        group = (
                            str(field(row, "publisher_domain", "")),
                            str(field(row, "property_id", index)),
                        )
                        groups.setdefault(group, []).append(
                            normalize_identifier(field(row, "type"), field(row, "value"))
                        )
                    requested_ids = {
                        (str(field(selector, "publisher_domain")), str(pid))
                        for selector in selectors
                        if field(selector, "selection_type") == "by_id"
                        for pid in field(selector, "property_ids", [])
                    }
                    property_count = field(expanded, "property_count", len(groups))
                    if groups and requested_ids.issubset(groups) and len(groups) == property_count:
                        matches = [
                            any(_identifier_matches(actual, allowed_values) for actual in ids)
                            for ids in groups.values()
                        ]
                        overlay = field(field(product, "overlay_support"), "property_list")
                        if isinstance(overlay, RootModel):
                            overlay = overlay.root
                        targetable = (
                            overlay is True
                            or field(product, "property_targeting_allowed", False) is True
                        )
                        matches_list = any(matches) if targetable else all(matches)
                        status = "match" if matches_list else "mismatch"
                except Exception:
                    # Failure to expand one product must not reject the entire response.
                    status = "unjudgeable"
                assessments.append(ProductPropertyAssessment(product_id, status))
        finally:
            if self.registry is None:
                await registry.close()
        return assessments

    async def apply(self, request: Any, result: Any, *, seller_url: str) -> Any:
        ref = request_property_list(request)
        if (
            ref is None
            or not result.success
            or result.data is None
            or field(result.data, "products") is None
        ):
            return result
        try:
            allowed = await self.fetcher.resolve(ref)
            products = list(field(result.data, "products") or [])
            assessments = await self.assess(products, allowed, seller_url=seller_url)
        except Exception:
            products = list(field(result.data, "products") or [])
            assessments = [
                ProductPropertyAssessment(str(field(p, "product_id", "?")), "unjudgeable")
                for p in products
            ]
        metadata = {
            **(result.metadata or {}),
            "product_property_policy": [
                {"product_id": item.product_id, "status": item.status} for item in assessments
            ],
        }
        data = result.data
        if self.mode == "filter":
            filtered = [
                product for product, item in zip(products, assessments) if item.status != "mismatch"
            ]
            data = (
                {**data, "products": filtered}
                if isinstance(data, dict)
                else data.model_copy(update={"products": filtered})
            )
        return result.model_copy(update={"data": data, "metadata": metadata})


async def check_property_list_reference(
    ref: Any, *, fetcher: MCPPropertyListFetcher | None = None
) -> ResolvedPropertyList:
    """Compliance probe: resolve a reference over MCP and exhaust its pages.

    Disable caching so every probe exercises the issuing agent. The returned
    ``pages`` and identifiers let a caller assert its expected list contents.
    """
    resolver = fetcher if fetcher is not None else MCPPropertyListFetcher(max_cache_entries=0)
    resolver.clear_cache()
    return await resolver.resolve(ref)


__all__ = [
    "MCPPropertyListFetcher",
    "PropertyListIdentifierError",
    "ResolvedPropertyList",
    "ProductPropertyPolicy",
    "ProductPropertyAssessment",
    "check_property_list_reference",
    "is_valid_domain_identifier",
    "normalize_property_list_identifiers",
    "request_property_list",
    "validate_property_list_identifiers",
]
