"""In-process mock guaranteed-inventory platform.

Models the canonical ``sales-guaranteed`` shape: pre-bookable
inventory, fixed CPM, capacity-bounded, exposes the four-state
lifecycle ``pending_creatives → pending_start → active → completed``.

Pure Python — no upstream HTTP, no external deps. The point is to
demonstrate how an adopter writes a :class:`DecisioningPlatform`
subclass that satisfies the ``sales-guaranteed`` Protocol; the
business logic is intentionally minimal so the platform-shape and
router-integration story stay legible.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SalesPlatform,
    assert_media_buy_transition,
)
from adcp.decisioning.capabilities import Account as CapabilitiesAccount
from adcp.decisioning.capabilities import (
    Adcp,
    IdempotencyUnsupported,
    MediaBuy,
    SupportedProtocol,
)

# ---------------------------------------------------------------------------
# In-memory inventory + buy state
# ---------------------------------------------------------------------------


@dataclass
class _Product:
    """One sellable inventory product."""

    product_id: str
    name: str
    description: str
    cpm_usd: float
    capacity_impressions: int


@dataclass
class _Package:
    """One package within a media buy. Persists fields the storyboard
    expects to round-trip through ``get_media_buys`` (targeting_overlay,
    measurement_terms) and to drive lifecycle transitions
    (creative_assignments)."""

    package_id: str
    buyer_ref: str
    product_id: str
    budget_usd: float
    impressions: int
    targeting_overlay: dict[str, Any] | None = None
    measurement_terms: dict[str, Any] | None = None
    creative_assignments: list[Any] | None = None


@dataclass
class _MediaBuy:
    """One pre-booked media buy reserving capacity from a product."""

    media_buy_id: str
    buyer_ref: str
    packages: list[_Package]
    total_budget_usd: float
    start_time: datetime
    end_time: datetime
    status: str = "pending_creatives"
    creatives_attached: int = 0


# Catalog defaults — the full storyboard runs against these. Adopters
# in production load from their own datastore; here a frozen list is
# more legible than a CMS round trip.
_DEFAULT_CATALOG: list[_Product] = [
    _Product(
        product_id="guaranteed-homepage-takeover",
        name="Guaranteed Homepage Takeover",
        description="Premium 970x250 above-the-fold inventory, fixed CPM.",
        cpm_usd=25.00,
        capacity_impressions=10_000_000,
    ),
    _Product(
        product_id="guaranteed-mobile-interstitial",
        name="Guaranteed Mobile Interstitial",
        description="Full-screen interstitial on owned mobile properties.",
        cpm_usd=18.00,
        capacity_impressions=5_000_000,
    ),
]


# ---------------------------------------------------------------------------
# The platform
# ---------------------------------------------------------------------------


class MockGuaranteedPlatform(DecisioningPlatform, SalesPlatform):
    """In-process ``sales-guaranteed`` platform.

    Each call resolves through ``ctx.account`` like any
    :class:`DecisioningPlatform` — the multi-tenant routing is the
    router's concern; this platform just serves the requests it
    receives. The ``accounts`` attribute is set by the example's
    ``app.py`` boot to a no-op store; the framework's
    :func:`validate_platform` requires every platform to carry one,
    even when (as in router-fronted deployments) the router's store
    is the active resolver.

    :attr:`upstream_url` is ``None`` — no real upstream. Adapter HTTP
    routing flows through :meth:`upstream_for` for live/sandbox, but
    here every storyboard step is satisfied in-process.
    """

    upstream_url = None

    capabilities = DecisioningCapabilities(
        specialisms=["sales-guaranteed"],
        adcp=Adcp(
            major_versions=[3],
            # Mock platform: no in-memory dedup wired. Honest declaration
            # over a silent-lie supported=True (the SDK's boot-time
            # validator at adcp.decisioning.validate_idempotency catches
            # the latter). Real adopters wrap mutating handlers with
            # @IdempotencyStore.wrap and declare supported=True.
            idempotency=IdempotencyUnsupported(supported=False),
        ),
        account=CapabilitiesAccount(supported_billing=["operator"]),
        media_buy=MediaBuy(
            supported_pricing_models=["cpm"],
        ),
        supported_protocols=[SupportedProtocol.media_buy],
    )

    def __init__(
        self,
        *,
        catalog: list[_Product] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        # The catalog is per-instance: tenants get their own product
        # list, capacity, and reservations. Multi-tenant adopters scale
        # this by giving each tenant its own MockGuaranteedPlatform
        # instance — exactly the router-fronted topology.
        self._catalog: dict[str, _Product] = {
            p.product_id: p for p in (catalog or list(_DEFAULT_CATALOG))
        }
        self._capacity: dict[str, int] = {
            p.product_id: p.capacity_impressions for p in self._catalog.values()
        }
        self._buys: dict[str, _MediaBuy] = {}
        # Creative library — populated by sync_creatives, read by
        # list_creatives. Wire-shape dicts keyed by creative_id so
        # list_creatives can return them without re-projecting.
        self._creatives: dict[str, dict[str, Any]] = {}

    # The router's AccountStore is what runtime dispatch threads
    # ctx.account through; this attribute exists only to satisfy
    # validate_platform's "accounts must be set" gate. The router never
    # consults it.
    accounts: Any = None  # type: ignore[assignment]

    # ----- sales-guaranteed required methods --------------------------

    def get_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Sync catalog read. Returns the full product list."""
        return {
            "products": [
                _project_product_to_wire(p, self._capacity[p.product_id])
                for p in self._catalog.values()
            ]
        }

    def create_media_buy(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Reserve capacity. Rejects ``PRODUCT_UNAVAILABLE`` when the
        requested impressions exceed remaining capacity for ANY of the
        requested products. Atomic per-call: either every package
        reserves or none does.

        Per AdCP wire contract, ``packages[].product_id`` (singular) is
        the buyer-supplied reference; the seller resolves to its
        in-memory catalog. Per-package ``budget`` is a flat number
        denominated in the buy's currency.

        Rejects ``TERMS_REJECTED`` when proposed measurement_terms are
        unworkable (variance < 5%, or measurement_window outside c3/c7).
        """
        wire_packages = _read_packages(req)
        if not wire_packages:
            raise AdcpError(
                "INVALID_REQUEST",
                message="create_media_buy requires at least one package.",
                recovery="correctable",
                field="packages",
            )

        with self._lock:
            # Pre-flight every package's capacity + terms before mutating
            # state. Two-phase reserve avoids partial writes when the
            # last package in the list trips a check.
            resolved: list[_Package] = []
            for i, wire_pkg in enumerate(wire_packages):
                product_id, impressions, budget = _resolve_package(wire_pkg, self._catalog)
                _check_measurement_terms(_attr(wire_pkg, "measurement_terms"))
                remaining = self._capacity.get(product_id, 0)
                if impressions > remaining:
                    raise AdcpError(
                        "PRODUCT_UNAVAILABLE",
                        message=(
                            f"product {product_id!r} has {remaining} "
                            f"impressions remaining; package requested "
                            f"{impressions}."
                        ),
                        recovery="correctable",
                        field="packages",
                        details={
                            "product_id": product_id,
                            "remaining": remaining,
                            "requested": impressions,
                        },
                    )
                resolved.append(
                    _Package(
                        package_id=f"pkg_g_{uuid.uuid4().hex[:8]}",
                        buyer_ref=_read_pkg_buyer_ref(wire_pkg, i),
                        product_id=product_id,
                        budget_usd=budget,
                        impressions=impressions,
                        targeting_overlay=_read_dict(_attr(wire_pkg, "targeting_overlay")),
                        measurement_terms=_read_dict(_attr(wire_pkg, "measurement_terms")),
                        creative_assignments=_read_list(_attr(wire_pkg, "creative_assignments")),
                    )
                )

            # Commit phase — capacity + terms validated.
            media_buy_id = f"mb_g_{uuid.uuid4().hex[:12]}"
            total_budget = 0.0
            for pkg in resolved:
                self._capacity[pkg.product_id] -= pkg.impressions
                total_budget += pkg.budget_usd

            buyer_ref = _read_buyer_ref(req)
            start_time, end_time = _read_window(req)
            self._buys[media_buy_id] = _MediaBuy(
                media_buy_id=media_buy_id,
                buyer_ref=buyer_ref,
                packages=resolved,
                total_budget_usd=total_budget,
                start_time=start_time,
                end_time=end_time,
            )

        return {
            "media_buy_id": media_buy_id,
            "buyer_ref": buyer_ref,
            "status": "pending_creatives",
            "packages": [
                {
                    "package_id": pkg.package_id,
                    "buyer_ref": pkg.buyer_ref,
                    "status": "pending_creatives",
                }
                for pkg in resolved
            ],
        }

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Drive the lifecycle: pause / resume / cancel; apply package
        patches.

        Errors:

        * ``MEDIA_BUY_NOT_FOUND`` when ``media_buy_id`` is unknown.
        * ``PACKAGE_NOT_FOUND`` when a referenced ``packages[].package_id``
          isn't on the buy. Recovery ``correctable`` for both — the
          buyer can retry with a fresh id.
        * ``INVALID_STATE`` when a status transition is illegal
          (raised by ``assert_media_buy_transition``).
        """
        with self._lock:
            buy = self._buys.get(media_buy_id)
            if buy is None:
                raise AdcpError(
                    "MEDIA_BUY_NOT_FOUND",
                    message=f"unknown media_buy_id={media_buy_id!r}",
                    recovery="correctable",
                    field="media_buy_id",
                )

            # Apply per-package patches if provided. Validate every
            # referenced package_id before mutating any of them — the
            # storyboard's invalid_transitions/update_unknown_package
            # asserts that an unknown id is rejected with
            # PACKAGE_NOT_FOUND, not silently absorbed.
            patch_packages = _read_packages(patch) or []
            existing_by_id = {p.package_id: p for p in buy.packages}
            for pkg_patch in patch_packages:
                pkg_id = _attr(pkg_patch, "package_id")
                if pkg_id is None:
                    continue
                if pkg_id not in existing_by_id:
                    raise AdcpError(
                        "PACKAGE_NOT_FOUND",
                        message=f"unknown package_id={pkg_id!r} on media_buy={media_buy_id!r}",
                        recovery="correctable",
                        field="packages[].package_id",
                    )
            for pkg_patch in patch_packages:
                pkg_id = _attr(pkg_patch, "package_id")
                if pkg_id is None or pkg_id not in existing_by_id:
                    continue
                target = existing_by_id[pkg_id]
                overlay = _read_dict(_attr(pkg_patch, "targeting_overlay"))
                if overlay is not None:
                    target.targeting_overlay = overlay
                terms = _read_dict(_attr(pkg_patch, "measurement_terms"))
                if terms is not None:
                    target.measurement_terms = terms
                assignments = _read_list(_attr(pkg_patch, "creative_assignments"))
                if assignments is not None:
                    target.creative_assignments = assignments

            new_state = _read_target_state(patch)
            if new_state is not None:
                assert_media_buy_transition(buy.status, new_state, media_buy_id=media_buy_id)
                buy.status = new_state

        return {
            "media_buy_id": media_buy_id,
            "buyer_ref": buy.buyer_ref,
            "status": buy.status,
            "packages": [
                {
                    "package_id": pkg.package_id,
                    "buyer_ref": pkg.buyer_ref,
                    "status": buy.status,
                }
                for pkg in buy.packages
            ],
        }

    def sync_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Auto-approve every submitted creative and advance any buys
        waiting on creatives from ``pending_creatives → pending_start``.

        Real adopters route to standards-and-practices review; the mock
        skips that to keep the storyboard straight-through.

        Per ``schemas/3.0.6/creative/sync-creatives-response.json`` the
        per-item shape is ``{creative_id, action, status?}`` where
        ``action`` is the lifecycle operation (``created`` for new
        creatives) and ``status`` is the optional review-state hint.
        """
        creatives = _read_creatives(req)

        with self._lock:
            for buy in self._buys.values():
                if buy.status == "pending_creatives" and creatives:
                    assert_media_buy_transition(
                        buy.status, "pending_start", media_buy_id=buy.media_buy_id
                    )
                    buy.status = "pending_start"
                    buy.creatives_attached += len(creatives)
            for i, c in enumerate(creatives):
                stored = _project_creative_to_wire(c, i)
                self._creatives[stored["creative_id"]] = stored

        return {
            "creatives": [
                {
                    "creative_id": _creative_id(c, i),
                    "action": "created",
                    "status": "approved",
                }
                for i, c in enumerate(creatives)
            ],
        }

    def list_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Return the seller's view of buyer-uploaded creatives.

        Returns the full library; pagination is not modeled (the mock
        runs against a small fixed-size storyboard catalog). The
        ``query_summary`` block is required by
        ``schemas/3.0.6/creative/list-creatives-response.json``.
        """
        with self._lock:
            creatives = list(self._creatives.values())
        total = len(creatives)
        return {
            "query_summary": {"total_matching": total, "returned": total},
            "pagination": {"has_more": False, "total_count": total},
            "creatives": creatives,
        }

    def get_media_buys(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Required by ``sales-*`` specialisms. Returns the seller's
        view of every media buy this account has booked.

        Echoes back ``targeting_overlay`` and ``measurement_terms``
        persisted at create / update time so the
        ``inventory_list_targeting`` storyboard can verify round-trip.
        """
        requested = _read_media_buy_ids(req)
        with self._lock:
            buys = list(self._buys.values())
        result: list[dict[str, Any]] = []
        for buy in buys:
            if requested and buy.media_buy_id not in requested:
                continue
            result.append(_project_media_buy_to_wire(buy))
        return {"media_buys": result}

    def get_media_buy_delivery(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Synthesize delivery proportional to elapsed flight time.

        Real adopters read from their reporting pipeline; the mock
        scales delivery linearly between ``start_time`` and
        ``end_time`` so storyboard assertions on a non-zero delivery
        response have something to bite on.
        """
        media_buy_id = _read_media_buy_id(req)
        with self._lock:
            buy = self._buys.get(media_buy_id) if media_buy_id else None

        if buy is None:
            return {"media_buy_deliveries": []}

        now = datetime.now(timezone.utc)
        total_impressions = sum(p.impressions for p in buy.packages)
        if buy.status not in ("active", "paused", "completed"):
            served = 0
            spend = 0.0
        else:
            elapsed = (now - buy.start_time).total_seconds()
            window = max(1.0, (buy.end_time - buy.start_time).total_seconds())
            ratio = max(0.0, min(1.0, elapsed / window))
            served = int(total_impressions * ratio)
            spend = buy.total_budget_usd * ratio

        return {
            "media_buy_deliveries": [
                {
                    "media_buy_id": media_buy_id,
                    "totals": {
                        "impressions": served,
                        "spend": round(spend, 2),
                    },
                },
            ],
        }


# ---------------------------------------------------------------------------
# Wire-shape projections (pure helpers)
# ---------------------------------------------------------------------------


def _project_product_to_wire(
    product: _Product,
    remaining_impressions: int,
) -> dict[str, Any]:
    """Project a :class:`_Product` to the AdCP product wire shape."""
    return {
        "product_id": product.product_id,
        "name": product.name,
        "description": product.description,
        "delivery_type": "guaranteed",
        "publisher_properties": [
            {"publisher_domain": "example.com", "selection_type": "all"},
        ],
        "format_ids": [
            {
                "agent_url": "https://creative.adcontextprotocol.org/",
                "id": "display_300x250",
            },
        ],
        "pricing_options": [
            {
                "pricing_option_id": "po-cpm-fixed",
                "pricing_model": "cpm",
                "fixed_price": product.cpm_usd,
                "currency": "USD",
            },
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend"],
            "available_reporting_frequencies": ["daily"],
            "date_range_support": "date_range",
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {"provider": "internal"},
        "available_inventory_impressions": remaining_impressions,
    }


# ---------------------------------------------------------------------------
# Wire-shape readers — accept either typed Pydantic or dict-shaped input
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a Pydantic model OR a dict, with default."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _read_packages(req: Any) -> list[Any]:
    raw = _attr(req, "packages", [])
    return list(raw) if raw else []


def _read_buyer_ref(req: Any) -> str:
    return str(_attr(req, "buyer_ref", "buyer_unknown"))


def _read_window(req: Any) -> tuple[datetime, datetime]:
    start = _attr(req, "start_time")
    end = _attr(req, "end_time")
    if isinstance(start, str):
        start = _parse_iso(start)
    if isinstance(end, str):
        end = _parse_iso(end)
    if not isinstance(start, datetime):
        start = datetime.now(timezone.utc)
    if not isinstance(end, datetime):
        end = start
    return start, end


def _parse_iso(value: str) -> datetime:
    """Parse a wire-shape ISO-8601 timestamp.

    Accepts both ``Z`` and explicit offset suffixes; falls back to the
    current time on parse failure (the buyer-side validator catches
    truly malformed timestamps before they reach the platform).
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def _read_pkg_buyer_ref(pkg: Any, idx: int) -> str:
    return str(_attr(pkg, "buyer_ref", f"pkg-{idx}"))


def _resolve_package(
    pkg: Any,
    catalog: dict[str, _Product],
) -> tuple[str, int, float]:
    """Resolve a wire package to ``(product_id, impressions, budget_usd)``.

    Per ``schemas/3.0.6/media-buy/package-request.json`` the wire shape
    carries ``product_id`` (singular, required) and ``budget`` (a flat
    number in the buy's currency). The seller resolves the product
    against its in-memory catalog.
    """
    product_id = _attr(pkg, "product_id")
    if product_id is None or not str(product_id):
        raise AdcpError(
            "INVALID_REQUEST",
            message="package.product_id is required",
            recovery="correctable",
            field="packages[].product_id",
        )
    product_id = str(product_id)
    if product_id not in catalog:
        raise AdcpError(
            "PRODUCT_NOT_FOUND",
            message=f"unknown product {product_id!r}",
            recovery="correctable",
            field="packages[].product_id",
        )

    raw_budget = _attr(pkg, "budget", 0.0)
    total_budget = float(raw_budget or 0.0)
    cpm = catalog[product_id].cpm_usd
    if cpm <= 0:
        raise AdcpError(
            "INTERNAL_ERROR",
            message=f"product {product_id!r} has non-positive CPM",
            recovery="terminal",
        )
    # Impressions = (budget / CPM) * 1000.
    impressions = int(total_budget / cpm * 1000)
    return product_id, impressions, total_budget


def _check_measurement_terms(terms: Any) -> None:
    """Reject buyer-proposed terms the seller can't accept.

    Storyboard ``measurement_terms_rejected/aggressive_terms`` sends
    zero-tolerance variance with a c30 window — outside what an
    operator-billing seller can underwrite. The relaxed retry sends
    variance >=5% with c3/c7 window. Mirror the v3 reference seller's
    policy (see ``examples/seller_agent.py``).
    """
    if terms is None:
        return
    pkg_terms = _read_dict(terms) or {}
    billing = _read_dict(pkg_terms.get("billing_measurement")) or {}
    window = billing.get("measurement_window")
    variance = billing.get("max_variance_percent")
    if (variance is not None and variance < 5) or (
        window is not None and window not in ("c3", "c7")
    ):
        raise AdcpError(
            "TERMS_REJECTED",
            message=(
                "Measurement terms unworkable: max_variance_percent must be >=5 "
                "and measurement_window must be c3 or c7."
            ),
            recovery="correctable",
            field="measurement_terms",
        )


def _project_creative_to_wire(creative: Any, idx: int) -> dict[str, Any]:
    """Project a sync_creatives input item to the
    ``schemas/3.0.6/creative/list-creatives-response.json`` Creative
    shape. Auto-approval mirrors the sync_creatives policy: every
    submitted creative comes back as ``approved``."""
    creative_id = _creative_id(creative, idx)
    name = _attr(creative, "name", None) or creative_id
    raw_format = _attr(creative, "format_id", None)
    if isinstance(raw_format, dict):
        format_id = raw_format
    elif raw_format is not None:
        format_id = {
            "agent_url": "https://creative.adcontextprotocol.org/",
            "id": str(raw_format),
        }
    else:
        format_id = {
            "agent_url": "https://creative.adcontextprotocol.org/",
            "id": "display_300x250",
        }
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "creative_id": creative_id,
        "name": str(name),
        "format_id": format_id,
        "status": "approved",
        "created_date": now_iso,
        "updated_date": now_iso,
    }


def _project_media_buy_to_wire(buy: _MediaBuy) -> dict[str, Any]:
    """Project an in-memory ``_MediaBuy`` to the
    ``schemas/3.0.6/media-buy/get-media-buys-response.json`` MediaBuy
    shape. Echoes targeting_overlay / measurement_terms persisted at
    create or update time for round-trip verification."""
    packages: list[dict[str, Any]] = []
    for pkg in buy.packages:
        wire_pkg: dict[str, Any] = {
            "package_id": pkg.package_id,
            "product_id": pkg.product_id,
            "budget": pkg.budget_usd,
            "currency": "USD",
            "impressions": pkg.impressions,
        }
        if pkg.targeting_overlay is not None:
            wire_pkg["targeting_overlay"] = pkg.targeting_overlay
        packages.append(wire_pkg)
    return {
        "media_buy_id": buy.media_buy_id,
        "status": buy.status,
        "currency": "USD",
        "total_budget": buy.total_budget_usd,
        "start_time": buy.start_time.isoformat(),
        "end_time": buy.end_time.isoformat(),
        "packages": packages,
    }


def _read_target_state(patch: Any) -> str | None:
    """Project an ``UpdateMediaBuyRequest`` onto a target lifecycle state."""
    if patch is None:
        return None
    active = _attr(patch, "active", None)
    if active is True:
        return "active"
    if active is False:
        return "paused"
    status = _attr(patch, "status", None)
    if isinstance(status, str):
        return status
    return None


def _read_creatives(req: Any) -> list[Any]:
    raw = _attr(req, "creatives", []) or []
    return list(raw)


def _read_media_buy_id(req: Any) -> str | None:
    raw = _attr(req, "media_buy_id", None)
    if raw is not None:
        return str(raw)
    # Some shapes nest under ``media_buy_ids[0]`` for batch reads.
    ids = _attr(req, "media_buy_ids", None)
    if isinstance(ids, list) and ids:
        return str(ids[0])
    return None


def _read_media_buy_ids(req: Any) -> set[str]:
    """Read the ``media_buy_ids`` filter from a get_media_buys request.

    Returns the empty set when no filter was supplied (caller treats
    that as "return all"). Tolerant of either typed Pydantic or
    dict-shaped input via :func:`_attr`.
    """
    ids = _attr(req, "media_buy_ids", None)
    if isinstance(ids, list):
        return {str(x) for x in ids if x is not None}
    return set()


def _read_dict(value: Any) -> dict[str, Any] | None:
    """Project a Pydantic-or-dict value to a plain dict.

    Returns ``None`` for ``None`` input; calls ``model_dump`` for
    Pydantic models so the persisted state survives a round-trip
    through ``json.dumps``. Non-dict-like values pass through
    unchanged inside ``{...}`` wrapping is NOT applied — caller
    decides whether to ignore.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)  # type: ignore[no-any-return]
    return None


def _read_list(value: Any) -> list[Any] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return list(value)
    return None


def _creative_id(creative: Any, idx: int) -> str:
    raw = _attr(creative, "creative_id", None)
    if raw:
        return str(raw)
    return f"cr_{idx}"


__all__ = ["MockGuaranteedPlatform"]
