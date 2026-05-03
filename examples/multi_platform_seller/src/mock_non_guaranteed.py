"""In-process mock non-guaranteed (programmatic remnant) platform.

Models the canonical ``sales-non-guaranteed`` shape: every
``create_media_buy`` succeeds, delivery scales with budget, status
starts ``active``. No capacity reservation — the auction-time
allocation runs at delivery, not at booking. Variable CPM with a
floor.

Pure Python — no upstream HTTP. Sibling to
:class:`MockGuaranteedPlatform`; the contrast between the two
illustrates why a multi-tenant seller needs a router (different
business model per tenant) rather than a single ``DecisioningPlatform``
that branches internally.
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
# In-memory model
# ---------------------------------------------------------------------------


@dataclass
class _Product:
    """One programmatic-remnant inventory product."""

    product_id: str
    name: str
    description: str
    floor_cpm_usd: float


@dataclass
class _MediaBuy:
    media_buy_id: str
    buyer_ref: str
    total_budget_usd: float
    start_time: datetime
    end_time: datetime
    status: str = "active"
    creatives_attached: int = 0


_DEFAULT_CATALOG: list[_Product] = [
    _Product(
        product_id="remnant-display-network",
        name="Programmatic Display Network",
        description="Run-of-network 300x250 with auction-time pricing.",
        floor_cpm_usd=2.00,
    ),
    _Product(
        product_id="remnant-video-rotation",
        name="Programmatic Video Rotation",
        description="Pre-roll 15s/30s with floor-based clearing.",
        floor_cpm_usd=8.00,
    ),
]


# ---------------------------------------------------------------------------
# The platform
# ---------------------------------------------------------------------------


class MockNonGuaranteedPlatform(DecisioningPlatform, SalesPlatform):
    """In-process ``sales-non-guaranteed`` platform.

    Always accepts ``create_media_buy``; the auction at serving time
    decides which impressions clear at the variable CPM. Delivery
    scales with budget instead of fixed reservations.

    :attr:`upstream_url` is ``None`` — the mock runs entirely in
    process.
    """

    upstream_url = None

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
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
        clearing_multiplier: float = 1.4,
    ) -> None:
        self._lock = threading.Lock()
        self._catalog: dict[str, _Product] = {
            p.product_id: p for p in (catalog or list(_DEFAULT_CATALOG))
        }
        # Multiplier on the floor CPM to simulate clearing prices in
        # the synthetic delivery projection. Real adopters read from
        # their auction logs; the mock uses a fixed multiplier so the
        # storyboard's delivery assertions stay deterministic.
        self._clearing_multiplier = clearing_multiplier
        self._buys: dict[str, _MediaBuy] = {}

    accounts: Any = None  # type: ignore[assignment]

    # ----- sales-non-guaranteed required methods ----------------------

    def get_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        return {"products": [_project_product_to_wire(p) for p in self._catalog.values()]}

    def create_media_buy(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Always succeed. No capacity reservation; auction at serving
        time decides clearing. The buy goes straight to ``active``."""
        packages = _read_packages(req)
        if not packages:
            raise AdcpError(
                "INVALID_REQUEST",
                message="create_media_buy requires at least one package.",
                recovery="correctable",
                field="packages",
            )

        # Validate referenced products exist before issuing the buy.
        # PRODUCT_NOT_FOUND is the spec error code for this case.
        total_budget = 0.0
        for pkg in packages:
            products = _attr(pkg, "products", []) or []
            for product_id in products:
                if str(product_id) not in self._catalog:
                    raise AdcpError(
                        "PRODUCT_NOT_FOUND",
                        message=f"unknown product {product_id!r}",
                        recovery="correctable",
                        field="packages[].products",
                    )
            budget = _attr(pkg, "budget", {}) or {}
            total_budget += float(_attr(budget, "total", 0.0) or 0.0)

        media_buy_id = f"mb_n_{uuid.uuid4().hex[:12]}"
        buyer_ref = _read_buyer_ref(req)
        start_time, end_time = _read_window(req)

        with self._lock:
            self._buys[media_buy_id] = _MediaBuy(
                media_buy_id=media_buy_id,
                buyer_ref=buyer_ref,
                total_budget_usd=total_budget,
                start_time=start_time,
                end_time=end_time,
            )

        return {
            "media_buy_id": media_buy_id,
            "buyer_ref": buyer_ref,
            "status": "active",
            "packages": [
                {
                    "package_id": f"pkg_{i}",
                    "buyer_ref": _read_pkg_buyer_ref(pkg, i),
                    "status": "active",
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
        """Auto-approve. Programmatic remnant doesn't gate on creative
        review — the SSP-side standards-and-practices is upstream."""
        creatives = _read_creatives(req)
        media_buy_id = _read_media_buy_id(req)
        with self._lock:
            buy = self._buys.get(media_buy_id) if media_buy_id else None
            if buy is not None and creatives:
                buy.creatives_attached += len(creatives)

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
        """Synthesize delivery scaling with elapsed flight time and
        budget. Variable CPM is modeled as floor × clearing-multiplier
        — high enough to deliver under budget, low enough to spend.
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
            spend = buy.total_budget_usd * ratio
            # Average clearing CPM across the catalog floor — adopters
            # in production read per-package clearing from their
            # auction store.
            avg_floor = sum(p.floor_cpm_usd for p in self._catalog.values()) / max(
                1, len(self._catalog)
            )
            avg_clearing = avg_floor * self._clearing_multiplier
            served = int((spend / max(avg_clearing, 0.01)) * 1000)

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
# Wire projection helpers (kept local — duplicated minimally for clarity)
# ---------------------------------------------------------------------------


def _project_product_to_wire(product: _Product) -> dict[str, Any]:
    return {
        "product_id": product.product_id,
        "name": product.name,
        "description": product.description,
        "delivery_type": "non_guaranteed",
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
                "pricing_option_id": "po-cpm-floor",
                "pricing_model": "cpm",
                "floor_price": product.floor_cpm_usd,
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
    }


def _attr(obj: Any, name: str, default: Any = None) -> Any:
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
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def _read_pkg_buyer_ref(pkg: Any, idx: int) -> str:
    return str(_attr(pkg, "buyer_ref", f"pkg-{idx}"))


def _read_target_state(patch: Any) -> str | None:
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
    ids = _attr(req, "media_buy_ids", None)
    if isinstance(ids, list) and ids:
        return str(ids[0])
    return None


def _creative_id(creative: Any, idx: int) -> str:
    raw = _attr(creative, "creative_id", None)
    if raw:
        return str(raw)
    return f"cr_{idx}"


__all__ = ["MockNonGuaranteedPlatform"]
