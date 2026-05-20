"""Opt-in v3.1.x types and helpers.

The SDK's primary pin stays on v3.0.x; everything in this module is opt-in
for adopters working against v3.1.x agents during the beta. It re-exports
alongside the default types from :mod:`adcp.types` without replacing them.

Three feature surfaces, one per v3.1 catalog-sync issue:

* :class:`GetSignalsRequestPreview` / :class:`GetProductsRequestPreview` —
  v3.0 request subclasses that promote the v3.1 conditional-fetch tokens
  (``if_catalog_version`` / ``if_pricing_version``) and ``discovery_mode``
  (#4761, #4762) to typed slots. The parent models declare ``extra='allow'``
  so wire shape is unchanged — these subclasses just give callers IDE-
  visible parameter names and client-side validation for the schema's
  cross-field constraints.

* :class:`CatalogVersionCache` — opt-in cache that tracks the most recent
  ``catalog_version`` / ``pricing_version`` token per ``(agent, account,
  filters)`` tuple, auto-attaches ``if_catalog_version`` on follow-up
  identical calls, and returns the cached payload on ``unchanged: true``
  responses (#4761 phase 3). Adopter passes a cache instance to the
  client; clients without one pre-pay nothing.

* :class:`CatalogChangeFeedClient` — wraps the per-agent ``GET /catalog/events``
  cursor poll and ``POST /catalog/subscriptions`` webhook registration
  endpoints from the per-agent change-feed proposal (#4763). Capability
  probe goes through ``get_adcp_capabilities`` and the new
  ``catalog_change_feed`` stanza.

Wire shape matches the v3.1.0-beta.1 schemas. Schema validation against
v3.1 responses routes automatically when the wire ``adcp_version`` declares
a 3.1 release — :func:`adcp.validation.schema_loader.get_validator` finds
the 3.1.0-beta.1 bundle in the cache and uses it instead of the v3.0 pin.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field, model_validator

from adcp.types import (
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
)
from adcp.types.core import Activity, ActivityType
from adcp.utils.operation_id import create_operation_id

if TYPE_CHECKING:
    from adcp.client import ADCPClient
    from adcp.types.core import TaskResult

__all__ = [
    "GetSignalsRequestPreview",
    "GetSignalsResponsePreview",
    "GetProductsRequestPreview",
    "GetProductsResponsePreview",
    "CatalogVersionCache",
    "CatalogVersionEntry",
    "get_products_with_cache",
    "get_signals_with_cache",
    "CatalogChangeFeedCapabilities",
    "CatalogChangeFeedClient",
    "CatalogChangeFeedError",
    "CatalogEvent",
    "CatalogEventType",
    "CatalogEventsPage",
    "catalog_change_feed_from_capabilities",
]


# ---------------------------------------------------------------------------
# get_signals — #4762 (discovery_mode=wholesale) + #4761 (if_*_version tokens)
# ---------------------------------------------------------------------------


class GetSignalsRequestPreview(GetSignalsRequest):
    """v3.1 get_signals request: adds ``discovery_mode`` and conditional-fetch tokens.

    ``discovery_mode='wholesale'`` enumerates the agent's full priced catalog
    (paginated) — symmetric with ``get_products buying_mode='wholesale'``.
    Wholesale mode bans ``signal_spec`` and ``signal_ids`` (this class enforces
    the mutex client-side so the agent doesn't have to round-trip a rejection).
    """

    discovery_mode: Annotated[
        Literal["brief", "wholesale"] | None,
        Field(
            description=(
                "Declares caller intent. 'brief' (default): semantic discovery — "
                "signal_spec or signal_ids required. 'wholesale': raw catalog "
                "enumeration — signal_spec and signal_ids MUST NOT be provided "
                "and the agent returns its full priced catalog, paginated."
            ),
        ),
    ] = None

    if_catalog_version: Annotated[
        str | None,
        Field(
            description=(
                "Opaque ``catalog_version`` token from a prior response. When the "
                "agent's current version matches, it MAY return ``unchanged: true`` "
                "with ``signals`` omitted. Scope-keyed: cache the token under "
                "(cache_scope, filters, destinations, countries[, account])."
            ),
        ),
    ] = None

    if_pricing_version: Annotated[
        str | None,
        Field(
            description=(
                "Opaque ``pricing_version`` token from a prior response. MUST be "
                "sent together with ``if_catalog_version`` — pricing has no "
                "structural baseline of its own."
            ),
        ),
    ] = None

    @model_validator(mode="after")
    def _enforce_wholesale_mutex(self) -> GetSignalsRequestPreview:
        if self.discovery_mode == "wholesale":
            if self.signal_spec is not None or self.signal_ids is not None:
                raise ValueError(
                    "discovery_mode='wholesale' MUST NOT be combined with "
                    "signal_spec or signal_ids (per v3.1 schema). Use 'brief' "
                    "for discovery, 'wholesale' for catalog enumeration."
                )
        if self.if_pricing_version is not None and self.if_catalog_version is None:
            raise ValueError(
                "if_pricing_version requires if_catalog_version — pricing version "
                "has no structural baseline to compare against on its own."
            )
        return self


class GetSignalsResponsePreview(GetSignalsResponse):
    """v3.1 get_signals response: adds catalog/pricing version tokens and cache_scope.

    The parent model already accepts these as Pydantic extras; this subclass
    promotes them to typed attributes so callers can read
    ``response.catalog_version`` directly.

    ``signals`` is overridden to be optional — the v3.1 spec MUSTs omission
    when ``unchanged: true``, and the v3.0 generated model declares it as
    required (correct for v3.0 traffic). Parsing v3.1 unchanged-shaped
    responses requires this relaxation.

    The override uses ``list[Any]`` rather than the parent's element type to
    avoid pulling deep generated-module imports into this opt-in surface;
    structural validation (list-of-objects) still applies. Callers who need
    per-element typing should round-trip through the v3.0 model.

    **Not substitutable** for the v3.0 ``GetSignalsResponse``: code that
    indexes ``signals[0]`` will crash with ``TypeError`` on an
    ``unchanged: true`` response. Branch on
    :attr:`unchanged` first.
    """

    signals: Annotated[
        list[Any] | None,
        Field(
            description=(
                "Returned list. MUST be omitted by v3.1 sellers when " "``unchanged: true`` is set."
            ),
        ),
    ] = None  # type: ignore[assignment]

    catalog_version: Annotated[
        str | None,
        Field(
            description=(
                "Opaque version token. Agents implementing conditional fetch "
                "return this on every response."
            ),
        ),
    ] = None

    pricing_version: Annotated[
        str | None,
        Field(
            description=(
                "Opaque pricing-layer version token, when the agent tracks "
                "pricing separately from structure."
            ),
        ),
    ] = None

    cache_scope: Annotated[
        Literal["public", "account"] | None,
        Field(
            description=(
                "Declares whether ``catalog_version`` describes the public "
                "rate card or an account-specific overlay. REQUIRED on every "
                "3.1+ response — the two-layer cache safety property depends on it."
            ),
        ),
    ] = None

    unchanged: Annotated[
        bool | None,
        Field(
            description=(
                "Present and ``true`` ONLY when the request's ``if_catalog_version`` "
                "(and ``if_pricing_version`` if sent) matches the agent's current "
                "version. ``signals`` is omitted in that case."
            ),
        ),
    ] = None


# ---------------------------------------------------------------------------
# get_products — #4761 (if_*_version tokens)
# ---------------------------------------------------------------------------


class GetProductsRequestPreview(GetProductsRequest):
    """v3.1 get_products request: adds conditional-fetch tokens.

    ``buying_mode='wholesale'`` already exists in v3.0; the v3.1 addition is
    the conditional-fetch token pair, identical in semantics to get_signals.
    """

    if_catalog_version: Annotated[
        str | None,
        Field(
            description=(
                "Opaque ``catalog_version`` token from a prior response. When the "
                "seller's current version matches, it MAY return ``unchanged: true`` "
                "with ``products`` omitted."
            ),
        ),
    ] = None

    if_pricing_version: Annotated[
        str | None,
        Field(
            description=(
                "Opaque ``pricing_version`` token. MUST be sent together with "
                "``if_catalog_version`` — pricing has no structural baseline of its own."
            ),
        ),
    ] = None

    @model_validator(mode="after")
    def _enforce_pricing_dep(self) -> GetProductsRequestPreview:
        if self.if_pricing_version is not None and self.if_catalog_version is None:
            raise ValueError(
                "if_pricing_version requires if_catalog_version — pricing version "
                "has no structural baseline to compare against on its own."
            )
        return self


class GetProductsResponsePreview(GetProductsResponse):
    """v3.1 get_products response: adds catalog/pricing version tokens and cache_scope.

    ``products`` is overridden to be optional — the v3.1 spec MUSTs omission
    when ``unchanged: true``, and the v3.0 generated model declares it as
    required. Parsing v3.1 unchanged-shaped responses requires this
    relaxation.

    The override uses ``list[Any]`` rather than the parent's element type to
    avoid deep generated-module imports; structural validation (list-of-objects)
    still applies. Callers who need per-element typing should round-trip
    through the v3.0 model.

    **Not substitutable** for the v3.0 ``GetProductsResponse``: code that
    indexes ``products[0]`` will crash with ``TypeError`` on an
    ``unchanged: true`` response. Branch on :attr:`unchanged` first.
    """

    products: Annotated[
        list[Any] | None,
        Field(
            description=(
                "Returned list. MUST be omitted by v3.1 sellers when " "``unchanged: true`` is set."
            ),
        ),
    ] = None  # type: ignore[assignment]

    catalog_version: Annotated[
        str | None,
        Field(
            description=(
                "Opaque version token. Sellers implementing conditional fetch "
                "return this on every response."
            ),
        ),
    ] = None

    pricing_version: Annotated[
        str | None,
        Field(
            description=(
                "Opaque pricing-layer version token, when the seller tracks " "pricing separately."
            ),
        ),
    ] = None

    cache_scope: Annotated[
        Literal["public", "account"] | None,
        Field(
            description=(
                "Whether ``catalog_version`` describes the public rate card or "
                "an account-specific overlay. REQUIRED on every 3.1+ response."
            ),
        ),
    ] = None

    unchanged: Annotated[
        bool | None,
        Field(
            description=(
                "Present and ``true`` ONLY when the request's conditional-fetch "
                "tokens match the seller's current version. ``products`` is "
                "omitted in that case."
            ),
        ),
    ] = None


# ---------------------------------------------------------------------------
# CatalogVersionCache — #4761 phase 3
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogVersionEntry:
    """Cached version state for one ``(agent, tool, scope_key)`` triple.

    ``payload`` is the last full response — returned verbatim on a cache hit
    when the agent confirms ``unchanged: true``.
    """

    catalog_version: str
    pricing_version: str | None
    cache_scope: Literal["public", "account"]
    payload: Any


# Scope-tuple derivation. The 3.1 spec defines distinct scoping dimensions
# per tool; both collapse to a stable hashable tuple here. ``pagination`` is
# intentionally NOT part of the key — the version describes the catalog,
# not a page within it.
_GET_PRODUCTS_SCOPE_FIELDS = (
    "buying_mode",
    "filters",
    "property_list",
    "catalog",
)
_GET_SIGNALS_SCOPE_FIELDS = (
    "discovery_mode",
    "filters",
    "destinations",
    "countries",
)


def _freeze(value: Any) -> Any:
    """Recursively coerce dict / list values into hashable tuples.

    The cache key has to be hashable — Pydantic ``model_dump`` produces
    dicts and lists, which aren't. ``frozenset`` of items handles dicts;
    ``tuple`` handles lists.
    """
    if isinstance(value, dict):
        return frozenset((k, _freeze(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _scope_key_for_request(
    tool: Literal["get_products", "get_signals"],
    request_dump: dict[str, Any],
) -> tuple[Any, ...]:
    """Build the cache scope tuple for a request.

    ``account`` is folded into the tuple when present — the spec keys the
    overlay layer on (public-dims + account_id). When absent, the request is
    inherently public-scoped.
    """
    fields = _GET_PRODUCTS_SCOPE_FIELDS if tool == "get_products" else _GET_SIGNALS_SCOPE_FIELDS
    scope: list[Any] = [tool]
    for f in fields:
        scope.append((f, _freeze(request_dump.get(f))))
    account = request_dump.get("account")
    scope.append(("account", _freeze(account)))
    return tuple(scope)


class CatalogVersionCache:
    """Per-(agent, tool, scope) cache of the last-seen catalog_version token.

    Adopters pass a single instance to :func:`get_products_with_cache` /
    :func:`get_signals_with_cache`; the helpers attach ``if_catalog_version``
    on subsequent identical calls and short-circuit ``unchanged: true``
    responses by returning the cached payload verbatim.

    The cache is thread-safe. Entries are keyed by ``(agent_id, scope_key)``;
    ``scope_key`` is derived from the request's scope-relevant fields per
    the 3.1 spec.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, tuple[Any, ...]], CatalogVersionEntry] = {}

    def lookup(self, agent_id: str, scope_key: tuple[Any, ...]) -> CatalogVersionEntry | None:
        with self._lock:
            return self._entries.get((agent_id, scope_key))

    def store(
        self,
        agent_id: str,
        scope_key: tuple[Any, ...],
        entry: CatalogVersionEntry,
    ) -> None:
        with self._lock:
            self._entries[(agent_id, scope_key)] = entry

    def invalidate(self, agent_id: str, scope_key: tuple[Any, ...] | None = None) -> None:
        """Drop a single entry, or every entry for ``agent_id`` when ``scope_key`` is ``None``."""
        with self._lock:
            if scope_key is None:
                self._entries = {k: v for k, v in self._entries.items() if k[0] != agent_id}
            else:
                self._entries.pop((agent_id, scope_key), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _extract_version_fields(response: Any) -> tuple[str | None, str | None, str | None, bool]:
    """Pull (catalog_version, pricing_version, cache_scope, unchanged) off a response.

    Works against both the v3.0 model (where these arrive via Pydantic
    extras) and the v3.1 preview subclass (typed attributes). Returns
    ``unchanged=False`` and ``None`` versions when the agent omits them
    (pre-v3.1 seller).

    Type-narrows each version field to ``str`` — a non-conformant agent
    that returns an int / object / bool token gets a ``None`` here rather
    than a downstream surprise.
    """
    if response is None:
        return None, None, None, False
    extras = getattr(response, "model_extra", None) or {}

    def _field(name: str) -> Any:
        value = getattr(response, name, None)
        if value is None:
            value = extras.get(name)
        return value

    def _as_str(value: Any) -> str | None:
        return value if isinstance(value, str) else None

    catalog_version = _as_str(_field("catalog_version"))
    pricing_version = _as_str(_field("pricing_version"))
    cache_scope = _as_str(_field("cache_scope"))
    unchanged = bool(_field("unchanged"))
    return catalog_version, pricing_version, cache_scope, unchanged


async def _get_with_cache(
    client: ADCPClient,
    request: GetProductsRequest | GetSignalsRequest,
    cache: CatalogVersionCache,
    *,
    tool: Literal["get_products", "get_signals"],
) -> TaskResult[Any]:
    """Shared wrapper logic for both tools.

    1. Compute the scope key from the request.
    2. If we have a cached entry, attach ``if_catalog_version`` (+ pricing)
       before sending.
    3. On ``unchanged: true``, return a ``TaskResult`` wrapping the cached
       payload.
    4. On a fresh payload, store the new version tokens and return as-is.

    The wrapper calls ``client.adapter.get_products`` / ``get_signals``
    directly rather than ``client.get_products`` / ``get_signals`` because
    those parse with the v3.0 response model, which rejects v3.1
    unchanged-shaped responses (where ``products`` / ``signals`` is
    omitted per the v3.1 spec). The bypass means activity emission, which
    ``ADCPClient`` does in the public method, must be replicated here —
    otherwise adopters with ``on_activity`` wired up for audit lose
    visibility into cached calls (including the 304-equivalent path that
    is the whole point of conditional fetch).
    """
    agent_id = client.agent_config.id
    request_dump = request.model_dump(mode="json", exclude_none=True)
    scope_key = _scope_key_for_request(tool, request_dump)
    cached = cache.lookup(agent_id, scope_key)

    # Attach conditional-fetch tokens to a copy of the request so the
    # caller's instance is left untouched.
    if cached is not None:
        request = request.model_copy(
            update={
                "if_catalog_version": cached.catalog_version,
                "if_pricing_version": cached.pricing_version,
            },
        )

    operation_id = create_operation_id()
    params = request.model_dump(mode="json", exclude_none=True)

    client._emit_activity(
        Activity(
            type=ActivityType.PROTOCOL_REQUEST,
            operation_id=operation_id,
            agent_id=client.agent_config.id,
            task_type=tool,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ),
    )

    if tool == "get_products":
        raw_result = await client.adapter.get_products(params)
        response_type: type = GetProductsResponsePreview
    else:
        raw_result = await client.adapter.get_signals(params)
        response_type = GetSignalsResponsePreview

    client._emit_activity(
        Activity(
            type=ActivityType.PROTOCOL_RESPONSE,
            operation_id=operation_id,
            agent_id=client.agent_config.id,
            task_type=tool,
            status=raw_result.status,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ),
    )

    result: TaskResult[Any] = client.adapter._parse_response(raw_result, response_type)

    if not result.success or result.data is None:
        return result

    catalog_version, pricing_version, cache_scope, unchanged = _extract_version_fields(
        result.data,
    )

    if unchanged and cached is not None:
        # Short-circuit: agent confirmed the version match. Hand back the
        # cached payload — caller MUST NOT mutate their local mirror.
        result.data = cached.payload
        return result

    if catalog_version is not None and cache_scope in ("public", "account"):
        # cache_scope is narrowed to "public"|"account" by the membership
        # test; the assignment is statically safe.
        cache.store(
            agent_id,
            scope_key,
            CatalogVersionEntry(
                catalog_version=catalog_version,
                pricing_version=pricing_version,
                cache_scope=cache_scope,  # type: ignore[arg-type]
                payload=result.data,
            ),
        )
    elif catalog_version is not None and cache_scope is None:
        # v3.1 schema makes cache_scope REQUIRED — a token without it
        # violates the two-layer cache safety invariant. Drop the entry
        # rather than risk mis-keying an account-overlay across accounts.
        import logging

        logging.getLogger(__name__).warning(
            "Agent %s returned catalog_version=%r without cache_scope on "
            "%s — non-conformant under v3.1; not caching to preserve the "
            "two-layer cache safety invariant.",
            agent_id,
            catalog_version,
            tool,
        )

    return result


async def get_products_with_cache(
    client: ADCPClient,
    request: GetProductsRequest,
    cache: CatalogVersionCache,
) -> TaskResult[Any]:
    """Call ``client.get_products`` with conditional-fetch caching.

    Auto-attaches ``if_catalog_version`` (and ``if_pricing_version`` when the
    cached entry has one) when a prior response is cached for the request's
    scope. Returns the cached payload verbatim on ``unchanged: true`` —
    callers MUST NOT mutate their local mirror in that case.

    Against a pre-v3.1 seller that ignores the conditional tokens, this is
    semantically a no-op: the full payload comes back and refreshes the
    cache entry. No cost paid except a one-trip warm-up.
    """
    return await _get_with_cache(client, request, cache, tool="get_products")


async def get_signals_with_cache(
    client: ADCPClient,
    request: GetSignalsRequest,
    cache: CatalogVersionCache,
) -> TaskResult[Any]:
    """Call ``client.get_signals`` with conditional-fetch caching.

    See :func:`get_products_with_cache` — same behavior, different tool.
    """
    return await _get_with_cache(client, request, cache, tool="get_signals")


# ---------------------------------------------------------------------------
# Catalog change feed — #4763
# ---------------------------------------------------------------------------


# Event-type literal aligned with the v3.1.0-beta.1 schema enum on
# ``core/catalog-event.json``. Used to type-check ``event_types`` filtering
# on poll() and subscribe(); kept open for forward-compat with future event
# types (the discriminated union is closed on the wire but adopters may
# encounter agent-emitted types ahead of an SDK release).
CatalogEventType = Literal[
    "product.created",
    "product.updated",
    "product.priced",
    "product.removed",
    "signal.created",
    "signal.updated",
    "signal.priced",
    "signal.removed",
    "catalog.bulk_change",
]


@dataclass(frozen=True)
class CatalogChangeFeedCapabilities:
    """Decoded ``catalog_change_feed`` stanza from ``get_adcp_capabilities``.

    Use :func:`catalog_change_feed_from_capabilities` to extract from a
    response object — that helper also handles agents that omit the stanza
    (pre-3.1 or 3.1 agents that don't implement the feed).
    """

    supported: bool
    retention_window_days: int
    webhooks_supported: bool
    event_types: tuple[str, ...]


def catalog_change_feed_from_capabilities(
    capabilities_response: Any,
) -> CatalogChangeFeedCapabilities | None:
    """Extract the ``catalog_change_feed`` stanza from a capabilities response.

    Returns ``None`` when the stanza is missing or declares ``supported: false``.
    Reads through Pydantic ``model_extra`` so this works against the v3.0
    capabilities response model (where the field arrives as an extra) and
    against any future v3.1 typed surface.
    """
    if capabilities_response is None:
        return None
    extras = getattr(capabilities_response, "model_extra", None) or {}
    stanza = getattr(capabilities_response, "catalog_change_feed", None) or extras.get(
        "catalog_change_feed",
    )
    if not isinstance(stanza, dict):
        return None
    if not stanza.get("supported"):
        return None
    return CatalogChangeFeedCapabilities(
        supported=True,
        retention_window_days=int(stanza.get("retention_window_days", 7)),
        webhooks_supported=bool(stanza.get("webhooks_supported", False)),
        event_types=tuple(stanza.get("event_types", ())),
    )


@dataclass(frozen=True)
class CatalogEvent:
    """One event from ``GET /catalog/events``.

    Payload shape varies by ``event_type``; the SDK leaves it as a dict so
    adopters can dispatch on the discriminator without paying a Pydantic
    rebuild cost. See ``schemas/cache/3.1.0-beta.1/core/catalog-event.json``
    for the per-type payload contracts.
    """

    event_id: str
    event_type: str
    entity_type: str
    entity_id: str
    created_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CatalogEventsPage:
    """One page from ``GET /catalog/events``.

    ``next_cursor`` is the value to pass on the next poll — write it
    unconditionally (the agent echoes the prior cursor when ``events`` is
    empty, so callers never need a null-check branch).
    """

    events: tuple[CatalogEvent, ...]
    has_more: bool
    next_cursor: str | None
    retention_window_days: int | None


class CatalogChangeFeedError(RuntimeError):
    """Raised when the change-feed endpoint returns a non-2xx response.

    ``status_code`` is the HTTP status, ``error_code`` is the agent's
    AdCP-level error code when one was returned (e.g. ``RETENTION_EXPIRED``
    when the consumer's cursor is older than the agent's retention window).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class CatalogChangeFeedClient:
    """Thin async wrapper around the agent's ``/catalog/events`` and
    ``/catalog/subscriptions`` endpoints.

    The change feed is an HTTP surface on the agent — it is NOT an MCP tool
    or an A2A skill, so this client speaks HTTP directly rather than going
    through the SDK's protocol adapter. The agent's base URL is derived
    from :class:`adcp.types.core.AgentConfig` (the ``agent_uri`` field),
    and the configured ``auth_header`` / ``auth_token`` is attached
    automatically.

    **Auth header convention.** When ``auth_type == "bearer"`` the token
    ships in the standard HTTP ``Authorization: Bearer …`` header (matching
    the A2A transport). When ``auth_type == "token"`` (the SDK default)
    the token ships in ``auth_header`` — by default ``x-adcp-auth``. If
    your agent expects ``Bearer`` semantics on every request, set
    ``auth_type="bearer"`` on the ``AgentConfig``; otherwise the change-feed
    client sends to the same header as the MCP transport.

    **Lifecycle.** Use as an async context manager so the underlying
    ``httpx.AsyncClient`` is closed deterministically::

        async with CatalogChangeFeedClient(config) as feed:
            page = await feed.poll(cursor=last_seen)

    Calling ``poll`` / ``subscribe`` outside ``async with`` is supported
    but leaks the ``httpx.AsyncClient`` unless you call ``aclose()``
    explicitly. Pass your own ``httpx.AsyncClient`` to share connection
    pools with other HTTP traffic — the wrapper will not close clients
    it didn't construct.
    """

    def __init__(
        self,
        agent_config: Any,
        *,
        http_client: Any | None = None,
        base_path: str = "",
    ) -> None:
        self.agent_config = agent_config
        # Trim trailing slash from agent_uri and append base_path so URLs
        # like https://agent.example/catalog/events compose cleanly even
        # when the agent_uri carries a trailing /.
        uri = str(getattr(agent_config, "agent_uri", "")).rstrip("/")
        self._base_url = uri + base_path
        self._http_client = http_client
        self._owns_client = http_client is None
        # Guards concurrent ``_get_client()`` calls — without it two
        # concurrent polls can each see ``_http_client is None`` and each
        # construct an ``httpx.AsyncClient``, leaving one to leak when
        # the second assignment overwrites the first.
        self._client_lock = asyncio.Lock()

    async def __aenter__(self) -> CatalogChangeFeedClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _get_client(self) -> Any:
        if self._http_client is not None:
            return self._http_client
        async with self._client_lock:
            # Double-check under the lock — another coroutine may have
            # constructed the client while we were waiting.
            if self._http_client is None:
                import httpx

                self._http_client = httpx.AsyncClient(
                    timeout=getattr(self.agent_config, "timeout", 30.0),
                )
            return self._http_client

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = dict(getattr(self.agent_config, "extra_headers", {}) or {})
        token = getattr(self.agent_config, "auth_token", None)
        if not token:
            return headers
        auth_type = getattr(self.agent_config, "auth_type", "token")
        auth_header = getattr(self.agent_config, "auth_header", "x-adcp-auth")
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers[auth_header] = token
        return headers

    async def poll(
        self,
        cursor: str | None = None,
        *,
        max_events: int | None = None,
        event_types: list[CatalogEventType] | None = None,
    ) -> CatalogEventsPage:
        """Poll ``GET /catalog/events`` and return one page of events.

        Args:
            cursor: UUID v7 cursor from a prior page's ``next_cursor``. Omit
                on the first poll to start from the agent's retention edge.
                Per spec, ``next_cursor`` echoes the request cursor when
                ``events`` is empty, so callers can write
                ``cursor = response.next_cursor`` without a null-check branch.
            max_events: Optional max page size; agents declare their own
                upper bound (default 1000, max 10000 per spec). Maps to the
                wire ``limit`` query parameter.
            event_types: Filter to a subset of event types; supports glob
                (``"product.*"``). Unknown / unsupported types are ignored
                silently per spec — pre-filter against the agent's declared
                :attr:`CatalogChangeFeedCapabilities.event_types` rather
                than relying on server-side validation. Maps to the wire
                ``types`` query parameter.

        Raises:
            CatalogChangeFeedError: On non-2xx response, including the
                ``RETENTION_EXPIRED`` error when the cursor is older than
                the agent's retention window (caller MUST resync via
                wholesale enumeration).
        """
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if max_events is not None:
            params["limit"] = max_events
        if event_types:
            params["types"] = ",".join(event_types)

        client = await self._get_client()
        response = await client.get(
            self._base_url + "/catalog/events",
            params=params,
            headers=self._headers(),
        )
        if response.status_code >= 400:
            self._raise_for_error(response)

        body = response.json()
        events = tuple(
            CatalogEvent(
                event_id=ev["event_id"],
                event_type=ev["event_type"],
                entity_type=ev["entity_type"],
                entity_id=ev["entity_id"],
                created_at=ev["created_at"],
                payload=ev.get("payload", {}),
            )
            for ev in body.get("events", [])
        )
        return CatalogEventsPage(
            events=events,
            has_more=bool(body.get("has_more", False)),
            next_cursor=body.get("next_cursor"),
            retention_window_days=body.get("retention_window_days"),
        )

    async def subscribe(
        self,
        webhook_url: str,
        event_types: list[CatalogEventType],
        *,
        subscription_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register a webhook subscription via ``POST /catalog/subscriptions``.

        Returns the agent's subscription confirmation as a raw dict — the
        wire shape for the subscription response is defined in
        ``specs/catalog-change-feed.md`` and is not bundled as a JSON Schema
        in v3.1.0-beta.1, so this method is intentionally untyped pending
        the GA schema. Callers SHOULD persist the returned ``subscription_id``
        for later deletion.

        Polling the feed is always the source of truth — webhooks are
        best-effort notifications. A subscriber that misses a webhook
        catches up on the next poll. Per the spec, receivers MUST NOT
        consume the webhook body as a source of state; they re-poll the
        feed_url instead.

        Args:
            extra: Vendor-specific or spec-extension fields. Raises
                ``ValueError`` if it collides with a reserved key
                (``webhook_url``, ``event_types``, ``subscription_id``) —
                silent overwrite would corrupt the subscription request.
        """
        body: dict[str, Any] = {
            "webhook_url": webhook_url,
            "event_types": list(event_types),
        }
        if subscription_id is not None:
            body["subscription_id"] = subscription_id
        if extra:
            reserved = {"webhook_url", "event_types", "subscription_id"}
            collisions = reserved & extra.keys()
            if collisions:
                raise ValueError(
                    f"subscribe(extra=...) cannot overwrite reserved keys: "
                    f"{sorted(collisions)}. Pass these as explicit arguments instead."
                )
            body.update(extra)

        client = await self._get_client()
        response = await client.post(
            self._base_url + "/catalog/subscriptions",
            json=body,
            headers=self._headers(),
        )
        if response.status_code >= 400:
            self._raise_for_error(response)
        return response.json()  # type: ignore[no-any-return]

    @staticmethod
    def _raise_for_error(response: Any) -> None:
        status_code = response.status_code
        error_code: str | None = None
        message = f"Catalog change feed returned HTTP {status_code}"
        try:
            body = response.json()
            if isinstance(body, dict):
                error_code = body.get("error_code") or body.get("code")
                if "message" in body:
                    message = f"{message}: {body['message']}"
        except ValueError:
            pass
        raise CatalogChangeFeedError(message, status_code=status_code, error_code=error_code)
