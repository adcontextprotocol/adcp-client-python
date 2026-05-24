"""Response builder helpers for ADCP servers.

These functions produce correctly-shaped AdCP response dicts that match
the generated Pydantic response schemas. They reduce boilerplate and
ensure schema compliance.

Every builder here matches the field names in the corresponding
generated response type (e.g., SyncAccountsResponse uses "accounts",
SyncCreativesResponse uses "creatives").

Usage:
    from adcp.server.responses import capabilities_response, products_response

    @mcp.tool()
    async def get_adcp_capabilities():
        return capabilities_response(["media_buy"])

    @mcp.tool()
    async def get_products():
        return products_response(MY_PRODUCTS)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from adcp.server.helpers import valid_actions_for_status


def _rfc3339_now() -> str:
    """Current UTC time as an RFC 3339 timestamp with ``Z`` suffix.

    Python's :meth:`datetime.isoformat` emits ``+00:00`` for UTC, but
    several strict schema validators in the AdCP ecosystem — notably
    the ``zod.string().datetime()`` check that the AdCP storyboard
    runner uses — reject the offset form by default. Normalizing to
    the Zulu form (``...Z``) keeps response timestamps acceptable to
    every common validator without losing precision.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_logger = logging.getLogger("adcp.server")


def _strip_none_values(value: Any) -> Any:
    """Recursively strip None-valued keys from dicts and lists.

    Applied to loose-dict items in asset-bearing response builders so that
    optional Pydantic fields (e.g. ``ImageAsset.format``) which default to
    ``None`` in Python do not appear as ``null`` on the wire.  The bundled
    JSON schemas declare those fields as non-nullable (``"type": "string"``,
    not ``["string", "null"]``), so a null value causes ``oneOf``/discriminator
    validation to fail at the buyer's schema validator.

    Pydantic model items are not passed through this function — their
    ``model_dump(exclude_none=True)`` call in :func:`_serialize` already
    handles null exclusion.
    """
    if isinstance(value, dict):
        return {k: _strip_none_values(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none_values(v) for v in value]
    return value


def _strip_write_only_fields(value: Any) -> Any:
    """Recursively strip write-only credential fields from a wire dict.

    Mirrors :func:`adcp.decisioning.account_projection._project_governance_agent`
    at the response-builder layer. The decisioning dispatcher's strip
    runs at ``_invoke_platform_method`` for platform methods; this
    layer covers adopters who hand-build response payloads via the
    ``adcp.server.responses`` builders without going through the
    decisioning dispatcher.

    Strips:

    * ``governance_agents[i].authentication`` — write-only credential.
    * ``billing_entity.bank`` — write-only bank coordinates.

    Pydantic models are passed through unchanged — adopters using
    typed response models are responsible for the strip via
    :func:`adcp.decisioning.project_account_for_response` or
    equivalent. Loose dicts (the more common case for hand-built
    builder calls) get the recursive walk.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, sub in value.items():
            if key == "governance_agents" and isinstance(sub, list):
                projected: list[Any] = []
                for agent in sub:
                    if isinstance(agent, dict):
                        projected.append(
                            {
                                k: _strip_write_only_fields(v)
                                for k, v in agent.items()
                                if k != "authentication"
                            }
                        )
                    else:
                        projected.append(agent)
                out[key] = projected
            elif key == "billing_entity" and isinstance(sub, dict):
                out[key] = {k: _strip_write_only_fields(v) for k, v in sub.items() if k != "bank"}
            else:
                out[key] = _strip_write_only_fields(sub)
        return out
    if isinstance(value, list):
        return [_strip_write_only_fields(v) for v in value]
    return value


def _serialize(items: list[Any]) -> list[Any]:
    """Serialize a list of dicts or Pydantic models to plain dicts.

    Loose-dict items (adopters returning ``{**db_record, ...}`` from
    a hand-built response builder) get a recursive write-only-field
    strip via :func:`_strip_write_only_fields` so
    ``governance_agents[i].authentication`` and ``billing_entity.bank``
    can't smuggle through, followed by :func:`_strip_none_values` to
    remove ``null``-valued keys that the bundled JSON schemas declare as
    non-nullable (e.g. ``ImageAsset.format``).  Pydantic models are
    passed through their own ``model_dump(exclude_none=True)`` — the
    typed projections at :mod:`adcp.decisioning.account_projection` are
    responsible for the write-only strip on that path.
    """
    out: list[Any] = []
    for p in items:
        if hasattr(p, "model_dump"):
            out.append(p.model_dump(mode="json", exclude_none=True))
        elif isinstance(p, dict):
            out.append(_strip_none_values(_strip_write_only_fields(p)))
        else:
            out.append(p)
    return out


# ============================================================================
# Protocol Discovery
# ============================================================================


def capabilities_response(
    supported_protocols: list[str],
    *,
    major_versions: list[int] | None = None,
    adcp_version: str | None = None,
    supported_versions: list[str] | None = None,
    build_version: str | None = None,
    sandbox: bool = True,
    features: dict[str, Any] | None = None,
    idempotency: dict[str, Any] | None = None,
    compliance_testing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a get_adcp_capabilities response.

    Args:
        supported_protocols: e.g. ["media_buy"], ["media_buy", "signals"].
            Valid values: media_buy, signals, governance, creative, brand,
            sponsored_intelligence. ``compliance_testing`` is NOT a protocol —
            pass it via the ``compliance_testing`` kwarg.
        major_versions: AdCP major versions. Defaults to [3]. Deprecated in
            favor of ``supported_versions`` (release-precision); both are
            emitted through 3.x for backwards compatibility.
        adcp_version: Server's pinned release this response was built
            for (release-precision string, e.g. ``"3.1"``). When set,
            included on the response envelope so buyers can read what
            release the server actually served. Typically passed by
            ``ADCPServerBuilder``'s auto-capabilities handler from its
            per-instance pin.
        supported_versions: Release-precision versions this server speaks
            (e.g. ``["3.0", "3.1"]``). Authoritative for buyer-side
            release pinning per the version-negotiation RFC. When omitted
            and ``adcp_version`` is set, defaults to ``[adcp_version]``.
        build_version: Optional advisory metadata — full
            VERSION.RELEASE.PATCH of the server's build (e.g.
            ``"3.1.2"``). Useful for incident triage; not part of the
            wire negotiation contract.
        sandbox: Whether this is a sandbox agent. Defaults to True.
        features: Additional feature flags.
        idempotency: Optional idempotency declaration, nested under
            ``adcp.idempotency`` per AdCP #2315. Pass the output of
            :meth:`adcp.server.idempotency.IdempotencyStore.capability` here
            to declare the seller's ``replay_ttl_seconds``.
        compliance_testing: Optional top-level ``compliance_testing`` block
            to advertise compliance-testing capabilities. When provided,
            emitted as a sibling of ``adcp`` in the response.

    Example::

        from adcp.server.responses import capabilities_response
        from adcp.server.idempotency import IdempotencyStore, MemoryBackend

        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)
        return capabilities_response(
            ["media_buy"],
            idempotency=store.capability(),
        )
    """
    if compliance_testing is not None and not idempotency:
        _logger.warning(
            "capabilities_response: adcp.idempotency not declared. "
            "The AdCP 3.0.1 storyboard runner may downgrade to v2 mode and "
            "cascade failures across idempotency-dependent tracks. "
            "Pass idempotency={'supported': False} to declare non-support, "
            "or idempotency=store.capability() to declare support."
        )
    adcp_info: dict[str, Any] = {"major_versions": major_versions or [3]}
    if supported_versions is None and adcp_version is not None:
        supported_versions = [adcp_version]
    if supported_versions:
        adcp_info["supported_versions"] = supported_versions
    if build_version is not None:
        adcp_info["build_version"] = build_version
    if idempotency:
        adcp_info["idempotency"] = idempotency
    resp: dict[str, Any] = {
        "status": "completed",
        "adcp": adcp_info,
        "supported_protocols": supported_protocols,
        "sandbox": sandbox,
    }
    if adcp_version is not None:
        resp["adcp_version"] = adcp_version
    if features:
        resp["features"] = features
    if compliance_testing is not None:
        resp["compliance_testing"] = compliance_testing
    return resp


# ============================================================================
# Account Operations
# ============================================================================


def sync_accounts_response(
    accounts: list[dict[str, Any]],
    *,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a sync_accounts success response.

    Each account dict should include: account_id, brand, operator,
    action ("created"|"updated"), status ("active"|"pending_approval").

    Matches SyncAccountsResponse1 schema (field: "accounts").

    Items pass through :func:`_serialize` so loose-dict adopters who
    spread an input ``governance_agents`` (with ``authentication``)
    or ``billing_entity`` (with ``bank``) onto the response get the
    write-only credential strip.
    """
    return {"accounts": _serialize(accounts), "sandbox": sandbox}


def sync_governance_response(
    accounts: list[dict[str, Any]],
    *,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a sync_governance response.

    Each account dict should include: account, status ("synced"),
    governance_agents ([{url, categories}]).

    Items pass through :func:`_serialize` so loose-dict adopters who
    spread an input ``governance_agents`` (with ``authentication``)
    onto the response get the write-only credential strip.
    """
    return {"accounts": _serialize(accounts), "sandbox": sandbox}


# ============================================================================
# Product Catalog
# ============================================================================


def products_response(
    products: list[Any] | None = None,
    *,
    item_count: int | None = None,
    proposals: list[Any] | None = None,
    incomplete: list[Any] | None = None,
    pagination: dict[str, Any] | None = None,
    wholesale_feed_version: str | None = None,
    pricing_version: str | None = None,
    cache_scope: str | None = None,
    unchanged: bool | None = None,
    status: str = "completed",
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a get_products response.

    Matches GetProductsResponse schema, including beta 3 wholesale feed
    metadata for cache/version-aware enumeration. Pass ``cache_scope``
    explicitly for spec-valid wholesale responses; the dispatcher only infers
    ``public`` for request paths without an account.
    """
    serialized = _serialize(products) if products is not None else None
    resp: dict[str, Any] = {
        "status": status,
        "sandbox": sandbox,
    }
    if serialized is not None:
        resp["products"] = serialized
    if item_count is not None:
        resp["item_count"] = item_count
    elif serialized is not None:
        resp["item_count"] = len(serialized)
    if proposals is not None:
        resp["proposals"] = _serialize(proposals)
    if incomplete is not None:
        resp["incomplete"] = _serialize(incomplete)
    if pagination is not None:
        resp["pagination"] = pagination
    if wholesale_feed_version is not None:
        resp["wholesale_feed_version"] = wholesale_feed_version
    if pricing_version is not None:
        resp["pricing_version"] = pricing_version
    if cache_scope is not None:
        resp["cache_scope"] = cache_scope
    if unchanged is not None:
        resp["unchanged"] = unchanged
    return resp


# ============================================================================
# Media Buy Operations
# ============================================================================


def media_buy_response(
    media_buy_id: str,
    packages: list[Any],
    *,
    buyer_ref: str | None = None,
    status: str | None = None,
    valid_actions: list[str] | None = None,
    revision: int | None = None,
    confirmed_at: str | None = None,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a create_media_buy success response.

    Each package should include: package_id, product_id, pricing_option_id, budget.
    Matches CreateMediaBuyResponse1 (success) schema.

    Auto-populates valid_actions from status if not provided.
    Auto-sets revision to 1 and confirmed_at to now if not provided.
    """
    resp: dict[str, Any] = {
        "media_buy_id": media_buy_id,
        "packages": _serialize(packages),
        "revision": revision if revision is not None else 1,
        "confirmed_at": confirmed_at or _rfc3339_now(),
        "sandbox": sandbox,
    }
    if buyer_ref is not None:
        resp["buyer_ref"] = buyer_ref
    if status is not None:
        resp["media_buy_status"] = status
        if valid_actions is None:
            resp["valid_actions"] = valid_actions_for_status(status)
        else:
            resp["valid_actions"] = valid_actions
    elif valid_actions is not None:
        resp["valid_actions"] = valid_actions
    return resp


def media_buy_error_response(errors: list[dict[str, str]]) -> dict[str, Any]:
    """Build a create_media_buy error response.

    Each error dict: {"code": "...", "message": "..."}.
    Matches CreateMediaBuyResponse2 (error) schema.
    """
    return {"errors": errors}


def update_media_buy_response(
    media_buy_id: str,
    *,
    affected_packages: list[Any] | None = None,
    status: str | None = None,
    valid_actions: list[str] | None = None,
    revision: int | None = None,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build an update_media_buy success response.

    Matches UpdateMediaBuyResponse1 (success) schema.
    Auto-populates valid_actions from status if not provided.
    """
    resp: dict[str, Any] = {
        "media_buy_id": media_buy_id,
        "sandbox": sandbox,
    }
    if affected_packages is not None:
        resp["affected_packages"] = _serialize(affected_packages)
    if status is not None:
        resp["media_buy_status"] = status
        if valid_actions is None:
            resp["valid_actions"] = valid_actions_for_status(status)
        else:
            resp["valid_actions"] = valid_actions
    elif valid_actions is not None:
        resp["valid_actions"] = valid_actions
    if revision is not None:
        resp["revision"] = revision
    return resp


def media_buys_response(
    media_buys: list[Any],
    *,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a get_media_buys response.

    Each media buy should include: media_buy_id, status, currency, packages.
    Matches GetMediaBuysResponse schema.
    """
    return {
        "media_buys": _serialize(media_buys),
        "sandbox": sandbox,
    }


def delivery_response(
    media_buy_deliveries: list[dict[str, Any]],
    *,
    reporting_period: dict[str, str] | None = None,
    currency: str = "USD",
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a get_media_buy_delivery response.

    Each media_buy_delivery should include:
        media_buy_id, status, totals (impressions, spend, etc.), by_package.

    Matches GetMediaBuyDeliveryResponse schema.

    Args:
        media_buy_deliveries: Array of delivery data per media buy.
        reporting_period: {"start": ISO timestamp, "end": ISO timestamp}.
            Defaults to current timestamp for both.
        currency: ISO 4217 currency code.
        sandbox: Whether this is simulated data.
    """
    now = _rfc3339_now()
    return {
        "reporting_period": reporting_period or {"start": now, "end": now},
        "media_buy_deliveries": media_buy_deliveries,
        "currency": currency,
        "sandbox": sandbox,
    }


# ============================================================================
# Creative Operations
# ============================================================================


def creative_formats_response(
    formats: list[Any],
    *,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a list_creative_formats response.

    Each format should include: format_id ({agent_url, id}), name.
    Matches ListCreativeFormatsResponse schema.
    """
    return {
        "formats": _serialize(formats),
        "sandbox": sandbox,
    }


def sync_creatives_response(
    creatives: list[dict[str, Any]],
    *,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a sync_creatives success response.

    Each creative dict should include: creative_id, action ("created"|"updated").
    Optionally: status ("processing"|"pending_review"|"approved"|"rejected"|"archived").
    Matches SyncCreativesResponse1 schema (field: "creatives").
    """
    return {"creatives": _serialize(creatives), "sandbox": sandbox}


def list_creatives_response(
    creatives: list[Any],
    *,
    pagination: dict[str, Any] | None = None,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a list_creatives response.

    Each creative should include: creative_id, name, format_id, status.
    Matches ListCreativesResponse schema.

    Timestamp defaults: every Creative item in the spec requires
    ``created_date`` and ``updated_date`` (ISO 8601 UTC). For any dict
    item that omits either field, this helper fills it with the current
    UTC timestamp via :func:`_rfc3339_now` (Zulu suffix, RFC 3339).
    Both fields default to the same value when neither is provided,
    which matches the intuitive meaning for a freshly-listed item.
    Explicit caller-provided values are always preserved. Pydantic
    model items are passed through ``_serialize`` unchanged — callers
    using typed Creative models should set timestamps on the model.
    """
    now = _rfc3339_now()
    filled: list[Any] = []
    for item in creatives:
        if isinstance(item, dict):
            has_created = "created_date" in item and item["created_date"] is not None
            has_updated = "updated_date" in item and item["updated_date"] is not None
            if has_created and has_updated:
                filled.append(item)
                continue
            patched = dict(item)
            if not has_created:
                patched["created_date"] = now
            if not has_updated:
                patched["updated_date"] = now
            filled.append(patched)
        else:
            filled.append(item)

    count = len(filled)
    return {
        "creatives": _serialize(filled),
        "pagination": pagination or {"total": count, "has_more": False},
        "query_summary": {"total_results": count, "total_matching": count, "returned": count},
        "sandbox": sandbox,
    }


def preview_creative_response(
    previews: list[dict[str, Any]],
    *,
    expires_at: str | None = None,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a preview_creative single response.

    Each preview should include:
        preview_id, input ({format_id, name, assets}),
        renders ([{render_id, output_format, preview_url, role, dimensions}]).

    Matches PreviewCreativeResponse1 (single) schema.
    """
    return {
        "response_type": "single",
        "previews": _serialize(previews),
        "expires_at": expires_at or "2099-12-31T23:59:59Z",
        "sandbox": sandbox,
    }


def build_creative_response(
    creative_manifest: dict[str, Any] | list[dict[str, Any]],
    *,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a build_creative success response.

    Accepts either a single manifest dict or a list of manifests.
    Each manifest should include: format_id, name, assets.

    Single manifest matches BuildCreativeResponse1.
    List matches BuildCreativeResponse2 (multi-format).
    """
    if isinstance(creative_manifest, list):
        return {
            "creative_manifests": [_strip_none_values(m) for m in creative_manifest],
            "sandbox": sandbox,
        }
    return {
        "creative_manifest": _strip_none_values(creative_manifest),
        "sandbox": sandbox,
    }


# ============================================================================
# Signal Operations
# ============================================================================


def signals_response(
    signals: list[Any] | None = None,
    *,
    incomplete: list[Any] | None = None,
    pagination: dict[str, Any] | None = None,
    wholesale_feed_version: str | None = None,
    pricing_version: str | None = None,
    cache_scope: str | None = None,
    unchanged: bool | None = None,
    status: str = "completed",
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a get_signals response.

    Each signal should include: signal_agent_segment_id, name, description,
    signal_type, data_provider, coverage_percentage, deployments, pricing_options, signal_id.
    Matches GetSignalsResponse schema, including beta 3 wholesale feed
    metadata for cache/version-aware enumeration. Pass ``cache_scope``
    explicitly for spec-valid wholesale responses; the dispatcher only infers
    ``public`` for request paths without an account.
    """
    resp: dict[str, Any] = {"status": status, "sandbox": sandbox}
    if signals is not None:
        resp["signals"] = _serialize(signals)
    if incomplete is not None:
        resp["incomplete"] = _serialize(incomplete)
    if pagination is not None:
        resp["pagination"] = pagination
    if wholesale_feed_version is not None:
        resp["wholesale_feed_version"] = wholesale_feed_version
    if pricing_version is not None:
        resp["pricing_version"] = pricing_version
    if cache_scope is not None:
        resp["cache_scope"] = cache_scope
    if unchanged is not None:
        resp["unchanged"] = unchanged
    return resp


def activate_signal_response(
    deployments: list[dict[str, Any]],
    *,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build an activate_signal success response.

    Each deployment should include: type, is_live, activation_key.
    For platform: platform, account.
    For agent: agent_url.
    Matches ActivateSignalResponse1 (success) schema.
    """
    return {
        "deployments": deployments,
        "sandbox": sandbox,
    }


# ============================================================================
# Event & Catalog Operations
# ============================================================================


def log_event_response(
    events_received: int,
    events_processed: int,
    *,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a log_event success response.

    Matches LogEventResponse1 (success) schema.
    """
    return {
        "events_received": events_received,
        "events_processed": events_processed,
        "sandbox": sandbox,
    }


def sync_catalogs_response(
    catalogs: list[dict[str, Any]],
    *,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a sync_catalogs success response.

    Each catalog should include: catalog_id, action, item_count, items_approved.
    Matches SyncCatalogsResponse1 (success) schema.
    """
    return {
        "catalogs": catalogs,
        "sandbox": sandbox,
    }


# ============================================================================
# Generic Helpers
# ============================================================================


def error_response(code: str, message: str) -> dict[str, Any]:
    """Build a single AdCP error object (not a full error response).

    .. deprecated::
        Use ``adcp_error()`` from ``adcp.server.helpers`` instead.
        It returns a properly wrapped ``{"errors": [...]}`` response with
        auto-recovery classification. This function returns an unwrapped
        single error dict ``{"code": ..., "message": ...}`` which is not
        a valid ADCP error response on its own.
    """
    return {"code": code, "message": message}
