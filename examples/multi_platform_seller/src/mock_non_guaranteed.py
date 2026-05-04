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
class _Package:
    """One package within a media buy. Persists fields the storyboard
    expects to round-trip through ``get_media_buys`` (targeting_overlay,
    measurement_terms) and to drive lifecycle transitions
    (creative_assignments)."""

    package_id: str
    buyer_ref: str
    product_id: str
    budget_usd: float
    targeting_overlay: dict[str, Any] | None = None
    measurement_terms: dict[str, Any] | None = None
    creative_assignments: list[Any] | None = None


@dataclass
class _MediaBuy:
    media_buy_id: str
    buyer_ref: str
    packages: list[_Package]
    total_budget_usd: float
    start_time: datetime
    end_time: datetime
    status: str = "pending_creatives"
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
        # Creative library — populated by sync_creatives, read by
        # list_creatives. Wire-shape dicts keyed by creative_id so
        # list_creatives can return them without re-projecting.
        self._creatives: dict[str, dict[str, Any]] = {}

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
        """Always accept; auction at serving time decides clearing.

        Per AdCP wire contract, ``packages[].product_id`` (singular)
        references the buyer-known product id and ``budget`` is a flat
        number. Resolves products against the in-memory catalog and
        rejects ``PRODUCT_NOT_FOUND`` for unknown ids.

        Per the storyboard ``pending_creatives_to_start`` scenario the
        buy starts in ``pending_creatives`` until creatives sync — even
        for non-guaranteed inventory, where ``active`` is the steady
        state but creatives gate go-live.

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

        resolved: list[_Package] = []
        total_budget = 0.0
        for i, wire_pkg in enumerate(wire_packages):
            product_id = _attr(wire_pkg, "product_id")
            if product_id is None or not str(product_id):
                raise AdcpError(
                    "INVALID_REQUEST",
                    message="package.product_id is required",
                    recovery="correctable",
                    field="packages[].product_id",
                )
            product_id = str(product_id)
            if product_id not in self._catalog:
                raise AdcpError(
                    "PRODUCT_NOT_FOUND",
                    message=f"unknown product {product_id!r}",
                    recovery="correctable",
                    field="packages[].product_id",
                )
            _check_measurement_terms(_attr(wire_pkg, "measurement_terms"))
            budget = float(_attr(wire_pkg, "budget", 0.0) or 0.0)
            total_budget += budget
            resolved.append(
                _Package(
                    package_id=f"pkg_n_{uuid.uuid4().hex[:8]}",
                    buyer_ref=_read_pkg_buyer_ref(wire_pkg, i),
                    product_id=product_id,
                    budget_usd=budget,
                    targeting_overlay=_read_dict(_attr(wire_pkg, "targeting_overlay")),
                    measurement_terms=_read_dict(_attr(wire_pkg, "measurement_terms")),
                    creative_assignments=_read_list(_attr(wire_pkg, "creative_assignments")),
                )
            )

        media_buy_id = f"mb_n_{uuid.uuid4().hex[:12]}"
        buyer_ref = _read_buyer_ref(req)
        start_time, end_time = _read_window(req)

        with self._lock:
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
        """Apply a media-buy patch.

        Errors:

        * ``MEDIA_BUY_NOT_FOUND`` when ``media_buy_id`` is unknown.
        * ``PACKAGE_NOT_FOUND`` when a referenced ``packages[].package_id``
          isn't on the buy. Recovery ``correctable`` for both.
        * ``INVALID_STATE`` for illegal status transitions.
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
        """Auto-approve. Programmatic remnant doesn't gate on creative
        review — the SSP-side standards-and-practices is upstream.

        Advances any buys waiting on creatives from
        ``pending_creatives → active`` so the storyboard's
        ``pending_creatives_to_start`` lifecycle assertion passes.

        Per ``schemas/3.0.6/creative/sync-creatives-response.json`` the
        per-item shape is ``{creative_id, action, status?}``.
        """
        creatives = _read_creatives(req)
        with self._lock:
            for buy in self._buys.values():
                if buy.status == "pending_creatives" and creatives:
                    # Lifecycle is pending_creatives → pending_start →
                    # active. The state machine doesn't permit a direct
                    # pending_creatives → active jump even for
                    # non-guaranteed inventory; advance through
                    # pending_start in one synchronous step so the
                    # storyboard's pending_creatives_to_start scenario
                    # sees the buy go live as soon as creatives clear.
                    assert_media_buy_transition(
                        buy.status, "pending_start", media_buy_id=buy.media_buy_id
                    )
                    buy.status = "pending_start"
                    assert_media_buy_transition(buy.status, "active", media_buy_id=buy.media_buy_id)
                    buy.status = "active"
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

        Echoes back ``targeting_overlay`` persisted at create / update
        time so ``inventory_list_targeting`` can verify round-trip.
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


def _read_media_buy_ids(req: Any) -> set[str]:
    """Read the ``media_buy_ids`` filter from a get_media_buys request.

    Returns the empty set when no filter was supplied (caller treats
    that as "return all").
    """
    ids = _attr(req, "media_buy_ids", None)
    if isinstance(ids, list):
        return {str(x) for x in ids if x is not None}
    return set()


def _read_dict(value: Any) -> dict[str, Any] | None:
    """Project a Pydantic-or-dict value to a plain dict; ``None`` for
    null input. Calls ``model_dump`` for Pydantic so persisted state
    survives a round-trip through ``json.dumps``."""
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


def _check_measurement_terms(terms: Any) -> None:
    """Reject buyer-proposed terms the seller can't accept.

    Mirrors the v3 reference seller's policy — variance must be >=5%
    and measurement_window must be c3 or c7.
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
    ``schemas/3.0.6/media-buy/get-media-buys-response.json`` shape."""
    packages: list[dict[str, Any]] = []
    for pkg in buy.packages:
        wire_pkg: dict[str, Any] = {
            "package_id": pkg.package_id,
            "product_id": pkg.product_id,
            "budget": pkg.budget_usd,
            "currency": "USD",
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


def _creative_id(creative: Any, idx: int) -> str:
    raw = _attr(creative, "creative_id", None)
    if raw:
        return str(raw)
    return f"cr_{idx}"


__all__ = ["MockNonGuaranteedPlatform"]
