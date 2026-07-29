"""Execution-side :class:`DecisioningPlatform` for the proposal-mode mock.

Reads :attr:`RequestContext.recipes` populated by the framework on
every post-acceptance dispatch path:

* ``create_media_buy(proposal_id=...)`` — framework hydrates from
  :class:`InMemoryProposalStore` via ``store.get(proposal_id)``.
* ``update_media_buy`` / ``get_media_buy_delivery`` — framework
  hydrates via the reverse-index ``store.get_by_media_buy_id`` (same
  recipes, different index).

Adapter never re-derives recipes; ``ctx.recipes`` is the typed contract.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SalesPlatform,
)
from adcp.decisioning.capabilities import Account as CapabilitiesAccount
from adcp.decisioning.capabilities import (
    Adcp,
    IdempotencyUnsupported,
    MediaBuy,
    SupportedProtocol,
)
from examples.sales_proposal_mode_seller.src.recipe import ProposalModeRecipe


@dataclass
class _MediaBuy:
    media_buy_id: str
    proposal_id: str | None
    total_budget: float
    start_time: datetime
    end_time: datetime
    confirmed_at: str
    revision: int = 1
    status: str = "active"
    recipes_seen: dict[str, str] | None = None  # product_id -> line_item_template_id


class ProposalModeDecisioningPlatform(DecisioningPlatform, SalesPlatform):
    """In-process proposal-mode platform. Reads ``ctx.recipes`` on
    ``create_media_buy`` to look up the per-product line-item template
    id the framework hydrated from the :class:`InMemoryProposalStore`.
    """

    upstream_url = None

    capabilities = DecisioningCapabilities(
        # Advertise both the underlying sales specialism AND the
        # ``sales-proposal-mode`` overlay so the storyboard runner
        # elects the ``proposal_finalize`` scenario.
        specialisms=["sales-non-guaranteed", "sales-proposal-mode"],
        adcp=Adcp(
            major_versions=[3],
            idempotency=IdempotencyUnsupported(supported=False),
        ),
        account=CapabilitiesAccount(supported_billing=["operator"]),
        media_buy=MediaBuy(
            supported_pricing_models=["cpm"],
            supports_proposals=True,
        ),
        supported_protocols=[SupportedProtocol.media_buy],
    )

    accounts: Any = None  # type: ignore[assignment]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buys: dict[str, _MediaBuy] = {}

    def get_products(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        # Fall-through path. Tenants with a wired ProposalManager hit
        # the manager's get_products instead — this is only for tenants
        # without proposal mode wiring (none in this example).
        del req, ctx
        return {"products": [], "cache_scope": "public"}

    def create_media_buy(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Accept a media buy.

        Three valid shapes:

        * ``proposal_id`` set, ``packages`` empty — the framework
          hydrated ``ctx.recipes`` from the committed proposal; the
          adapter reads recipes to wire upstream line items.
        * ``packages[]`` populated, ``proposal_id`` empty — buyer
          constructed packages explicitly. Storyboard runner v6.10.0
          accepts a committed proposal this way (LLM-derived packages
          from the proposal's allocations). The adapter processes
          packages directly; no recipes hydration.
        * Both empty — invalid request, neither path is wireable.
        """
        proposal_id = getattr(req, "proposal_id", None)
        packages = getattr(req, "packages", None) or []
        if proposal_id is None and not packages and not ctx.recipes:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "create_media_buy requires either packages[] or proposal_id; got neither."
                ),
                recovery="correctable",
                field="proposal_id",
            )

        # Recipes are keyed by product_id when the buyer accepted a
        # committed proposal via proposal_id; empty when the buyer
        # constructed packages explicitly. Both shapes flow through
        # this adapter.
        recipes_seen: dict[str, str] = {}
        for product_id, recipe in ctx.recipes.items():
            if not isinstance(recipe, ProposalModeRecipe):
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Expected ProposalModeRecipe for {product_id!r}; "
                        f"got {type(recipe).__name__}."
                    ),
                    recovery="terminal",
                )
            typed = cast(ProposalModeRecipe, recipe)
            recipes_seen[product_id] = typed.line_item_template_id

        media_buy_id = f"mb_{uuid.uuid4().hex[:12]}"
        total_budget = float(_dotted(req, "total_budget.amount", 0.0) or 0.0)
        start_time = _read_datetime(getattr(req, "start_time", None))
        end_time = _read_datetime(getattr(req, "end_time", None))
        confirmed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        with self._lock:
            self._buys[media_buy_id] = _MediaBuy(
                media_buy_id=media_buy_id,
                proposal_id=str(proposal_id) if proposal_id else None,
                total_budget=total_budget,
                start_time=start_time,
                end_time=end_time,
                confirmed_at=confirmed_at,
                recipes_seen=recipes_seen,
            )

        # Wire response: per the storyboard, echo proposal_id + return
        # active media_buy.
        return {
            "media_buy_id": media_buy_id,
            "buyer_ref": getattr(req, "buyer_ref", None),
            "status": "active",
            "confirmed_at": confirmed_at,
            "revision": 1,
            "proposal_id": str(proposal_id) if proposal_id else None,
            "packages": [
                {
                    "package_id": f"pkg_{product_id}",
                    "product_id": product_id,
                    "status": "active",
                    "buyer_ref": product_id,
                }
                for product_id in recipes_seen.keys()
            ],
        }

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Apply a media-buy patch. Framework re-hydrates ``ctx.recipes``
        from the consumed proposal via the reverse-index — adapter sees
        the same typed recipes it saw on ``create_media_buy``."""
        del patch
        with self._lock:
            buy = self._buys.get(media_buy_id)
            if buy is None:
                raise AdcpError(
                    "MEDIA_BUY_NOT_FOUND",
                    message=f"unknown media_buy_id={media_buy_id!r}",
                    recovery="correctable",
                    field="media_buy_id",
                )
            # Demonstrate ctx.recipes hydration on update path.
            # An empty ctx.recipes here means the dispatch wiring is broken
            # (the framework should have hydrated from the consumed proposal
            # via store.get_by_media_buy_id). NOT an assert — runtime invariants
            # need to fire even under ``python -O`` where asserts are stripped.
            if not ctx.recipes:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        "ctx.recipes is empty on update_media_buy — the "
                        "framework's reverse-index hydration did not run. "
                        "This is a framework bug, not adopter input."
                    ),
                    recovery="terminal",
                )
            buy.revision += 1
        return {
            "media_buy_id": media_buy_id,
            "buyer_ref": buy.proposal_id,
            "status": buy.status,
            "revision": buy.revision,
            "packages": [],
        }

    def sync_creatives(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        del ctx
        creatives = getattr(req, "creatives", None) or []
        return {
            "creatives": [
                {
                    "creative_id": (
                        c.creative_id if hasattr(c, "creative_id") else c.get("creative_id")
                    ),
                    "action": "created",
                    "status": "approved",
                }
                for c in creatives
            ],
        }

    def get_media_buy_delivery(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Synthetic delivery scaling with elapsed flight time."""
        media_buy_ids = list(getattr(req, "media_buy_ids", None) or [])
        media_buy_id = str(media_buy_ids[0]) if media_buy_ids else ""
        with self._lock:
            buy = self._buys.get(media_buy_id) if media_buy_id else None
        if buy is None:
            return {"media_buy_deliveries": []}
        # Demonstrate ctx.recipes hydration on delivery path.
        # Empty is acceptable on legacy buys, but proposal-driven buys
        # should always have recipes hydrated.
        now = datetime.now(timezone.utc)
        elapsed = max(0.0, (now - buy.start_time).total_seconds())
        window = max(1.0, (buy.end_time - buy.start_time).total_seconds())
        ratio = max(0.0, min(1.0, elapsed / window))
        spend = round(buy.total_budget * ratio, 2)
        impressions = int(spend * 100)
        return {
            "media_buy_deliveries": [
                {
                    "media_buy_id": media_buy_id,
                    "totals": {
                        "impressions": impressions,
                        "spend": spend,
                    },
                },
            ],
        }

    def get_media_buys(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        del req, ctx
        with self._lock:
            buys = list(self._buys.values())
        return {
            "media_buys": [
                {
                    "media_buy_id": b.media_buy_id,
                    "status": b.status,
                    "buyer_ref": b.proposal_id,
                    "confirmed_at": b.confirmed_at,
                    "revision": b.revision,
                    "packages": [],
                }
                for b in buys
            ],
        }


def _dotted(obj: Any, path: str, default: Any = None) -> Any:
    cur = obj
    for part in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return default if cur is None else cur


def _read_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        # Pydantic / aware ISO strings.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)
