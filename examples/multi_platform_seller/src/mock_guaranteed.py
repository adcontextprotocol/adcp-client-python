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
    IdempotencySupported,
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
class _MediaBuy:
    """One pre-booked media buy reserving capacity from a product."""

    media_buy_id: str
    buyer_ref: str
    product_id: str
    impressions_reserved: int
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
            idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
        ),
        account=CapabilitiesAccount(supported_billing=["operator"]),
        media_buy=MediaBuy(supported_pricing_models=["cpm"]),
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
        """Reserve capacity. Rejects ``INVENTORY_UNAVAILABLE`` when the
        requested impressions exceed remaining capacity for ANY of the
        requested products. Atomic per-call: either every package
        reserves or none does.
        """
        # Wire shape: req.packages = [{buyer_ref, products, budget, ...}]
        packages = _read_packages(req)
        if not packages:
            raise AdcpError(
                "INVALID_REQUEST",
                message="create_media_buy requires at least one package.",
                recovery="correctable",
                field="packages",
            )

        with self._lock:
            # Pre-flight every package's capacity ask before mutating
            # state. Two-phase reserve avoids partial writes when the
            # last package in the list trips capacity.
            reservations: list[tuple[str, int, float]] = []
            for pkg in packages:
                product_id, impressions, budget = _resolve_package_capacity(pkg, self._catalog)
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
                reservations.append((product_id, impressions, budget))

            # Commit phase — capacity already validated.
            media_buy_id = f"mb_g_{uuid.uuid4().hex[:12]}"
            total_impressions = 0
            total_budget = 0.0
            for product_id, impressions, budget in reservations:
                self._capacity[product_id] -= impressions
                total_impressions += impressions
                total_budget += budget

            buyer_ref = _read_buyer_ref(req)
            start_time, end_time = _read_window(req)
            self._buys[media_buy_id] = _MediaBuy(
                media_buy_id=media_buy_id,
                buyer_ref=buyer_ref,
                product_id=reservations[0][0],
                impressions_reserved=total_impressions,
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
                    "package_id": f"pkg_{i}",
                    "buyer_ref": _read_pkg_buyer_ref(pkg, i),
                    "status": "pending_creatives",
                }
                for i, pkg in enumerate(packages)
            ],
        }

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Drive the lifecycle: pause / resume / cancel. The state
        machine helper enforces legal transitions and raises
        ``INVALID_STATE`` otherwise."""
        with self._lock:
            buy = self._buys.get(media_buy_id)
            if buy is None:
                raise AdcpError(
                    "MEDIA_BUY_NOT_FOUND",
                    message=f"unknown media_buy_id={media_buy_id!r}",
                    recovery="terminal",
                    field="media_buy_id",
                )

            new_state = _read_target_state(patch)
            if new_state is not None:
                assert_media_buy_transition(buy.status, new_state, media_buy_id=media_buy_id)
                buy.status = new_state

        return {
            "media_buy_id": media_buy_id,
            "buyer_ref": buy.buyer_ref,
            "status": buy.status,
            "packages": [],
        }

    def sync_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Auto-approve every submitted creative and advance the buy
        from ``pending_creatives → pending_start`` once any creatives
        are attached. Real adopters route to standards-and-practices
        review; the mock skips that to keep the storyboard straight-
        through.
        """
        creatives = _read_creatives(req)
        media_buy_id = _read_media_buy_id(req)

        with self._lock:
            buy = self._buys.get(media_buy_id) if media_buy_id else None
            if buy is not None and creatives:
                buy.creatives_attached += len(creatives)
                if buy.status == "pending_creatives":
                    assert_media_buy_transition(
                        buy.status, "pending_start", media_buy_id=media_buy_id
                    )
                    buy.status = "pending_start"

        return {
            "creatives": [
                {
                    "creative_id": _creative_id(c, i),
                    "approval_status": "approved",
                }
                for i, c in enumerate(creatives)
            ],
        }

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
        if buy.status not in ("active", "paused", "completed"):
            served = 0
            spend = 0.0
        else:
            elapsed = (now - buy.start_time).total_seconds()
            window = max(1.0, (buy.end_time - buy.start_time).total_seconds())
            ratio = max(0.0, min(1.0, elapsed / window))
            served = int(buy.impressions_reserved * ratio)
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


def _resolve_package_capacity(
    pkg: Any,
    catalog: dict[str, _Product],
) -> tuple[str, int, float]:
    """Resolve a package to ``(product_id, impressions, budget_usd)``."""
    products = _attr(pkg, "products", []) or []
    if not products:
        raise AdcpError(
            "INVALID_REQUEST",
            message="package.products is empty",
            recovery="correctable",
            field="packages[].products",
        )
    product_id = str(products[0])
    if product_id not in catalog:
        raise AdcpError(
            "INVALID_REQUEST",
            message=f"unknown product {product_id!r}",
            recovery="correctable",
            field="packages[].products",
        )

    budget = _attr(pkg, "budget", {}) or {}
    total_budget = float(_attr(budget, "total", 0.0) or 0.0)
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


def _creative_id(creative: Any, idx: int) -> str:
    raw = _attr(creative, "creative_id", None)
    if raw:
        return str(raw)
    return f"cr_{idx}"


__all__ = ["MockGuaranteedPlatform"]
