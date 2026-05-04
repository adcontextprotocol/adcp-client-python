"""Proposal-mode :class:`ProposalManager` — declares ``finalize=True``.

Implements the three-phase proposal lifecycle the storyboard exercises:

* :meth:`get_products` — initial brief returns proposals + recipes.
  Framework auto-persists drafts to :class:`InMemoryProposalStore`.
* :meth:`refine_products` — multi-turn refine. Framework overwrites
  the draft on each iteration (D3 single-ledger).
* :meth:`finalize_proposal` — locks pricing + sets ``expires_at``.
  Framework commits via ``proposal_store.commit`` post-projection.

The mock catalog mirrors the ``proposal_finalize.yaml`` fixture: one
proposal id (``balanced_reach_q2``) with two products. Storyboard
sends a generic-shape brief; the mock returns a fixed-shape response.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from adcp.decisioning import (
    CapabilityOverlap,
    FinalizeProposalRequest,
    FinalizeProposalSuccess,
    ProposalCapabilities,
    RequestContext,
)
from examples.sales_proposal_mode_seller.src.recipe import ProposalModeRecipe

# ---------------------------------------------------------------------------
# Catalog — one proposal, two products
# ---------------------------------------------------------------------------

PROPOSAL_ID = "balanced_reach_q2"

_VIDEO_RECIPE = ProposalModeRecipe(
    line_item_template_id="lit_ctv_premium",
    floor_cpm=15.0,
)
_DISPLAY_RECIPE = ProposalModeRecipe(
    line_item_template_id="lit_display_run",
    floor_cpm=2.5,
    # Display product overlay matches the buyer's expected dimensions.
    capability_overlap=CapabilityOverlap(
        pricing_models=frozenset({"cpm"}),
        delivery_types=frozenset({"non_guaranteed"}),
    ),
)

_PRODUCTS: list[dict[str, Any]] = [
    {
        "product_id": "ctv-premium-q2",
        "name": "CTV Premium Q2",
        "description": "Connected-TV premium inventory, US/CA, A25-54.",
        "delivery_type": "non_guaranteed",
        "publisher_properties": [
            {"publisher_domain": "example.com", "selection_type": "all"},
        ],
        "format_ids": [
            {"agent_url": "https://creative.adcontextprotocol.org/", "id": "video_15s"},
        ],
        "pricing_options": [
            {
                "pricing_option_id": "po-ctv-cpm",
                "pricing_model": "cpm",
                "floor_price": 15.0,
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
        "implementation_config": _VIDEO_RECIPE,
    },
    {
        "product_id": "display-run-q2",
        "name": "Display Run-of-Network Q2",
        "description": "Run-of-network 300x250, US/CA, A25-54.",
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
                "pricing_option_id": "po-display-cpm",
                "pricing_model": "cpm",
                "floor_price": 2.5,
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
        "implementation_config": _DISPLAY_RECIPE,
    },
]


def _draft_proposal_payload(allocations: dict[str, float] | None = None) -> dict[str, Any]:
    """Wire ``Proposal`` shape per ``schemas/3.0.5/core/proposal.json``.

    Required fields: ``proposal_id``, ``name``, ``allocations``. Each
    allocation entry carries ``product_id`` + ``allocation_percentage``
    (0-100). Percentages must sum to 100.
    """
    alloc = allocations or {"ctv-premium-q2": 60.0, "display-run-q2": 40.0}
    return {
        "proposal_id": PROPOSAL_ID,
        "name": "Balanced Reach Q2 — CTV-led",
        "description": "60/40 CTV/display split, Q2 flight, A25-54.",
        "proposal_status": "draft",
        "allocations": [
            {
                "product_id": pid,
                "allocation_percentage": pct,
                "rationale": f"Indicative {pct:.0f}% allocation to {pid}.",
            }
            for pid, pct in alloc.items()
        ],
    }


class ProposalModeProposalManager:
    """Proposal-mode :class:`ProposalManager` declaring ``finalize=True``.

    Three methods, ~60 LOC of substantive logic. The framework handles
    draft persistence, finalize routing, expiry enforcement, and recipe
    hydration on subsequent calls.
    """

    capabilities = ProposalCapabilities(
        sales_specialism="sales-non-guaranteed",
        refine=True,
        finalize=True,
        # 60s grace absorbs clock skew between the seller's
        # expires_at and the buyer's create_media_buy call.
        expires_at_grace_seconds=60,
    )

    async def get_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Brief / catalog discovery — return products + draft proposal."""
        del req, ctx
        return {
            "products": _PRODUCTS,
            "proposals": [_draft_proposal_payload()],
        }

    async def refine_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Refine iteration. Adopter inspects ``req.refine`` and adjusts
        the proposal allocations / pricing per the buyer's ``ask``.

        For the storyboard this is a fixed shift — production adopters
        would parse the natural-language ``ask`` and re-run their
        allocator. The framework auto-persists each iteration as a
        draft so finalize hydrates from the latest state.

        Return ``refinement_applied[]`` matching the request's
        ``refine[]`` length + order per the wire spec.
        """
        del ctx
        refines = list(getattr(req, "refine", None) or [])
        # Storyboard ask: shift 60% to CTV, drop display, frequency cap.
        # Mock: bump CTV allocation to 0.8, leave display.
        proposal = _draft_proposal_payload(
            {"ctv-premium-q2": 80.0, "display-run-q2": 20.0},
        )
        applied = []
        for entry in refines:
            inner = getattr(entry, "root", entry)
            scope = getattr(inner, "scope", None)
            scope_str = str(getattr(scope, "value", scope)) if scope is not None else None
            if scope_str == "proposal":
                applied.append(
                    {
                        "scope": "proposal",
                        "proposal_id": str(getattr(inner, "proposal_id", PROPOSAL_ID)),
                        "status": "applied",
                        "notes": "Adjusted CTV/display split per ask.",
                    }
                )
            elif scope_str == "product":
                applied.append(
                    {
                        "scope": "product",
                        "product_id": str(getattr(inner, "product_id", "")),
                        "status": "applied",
                    }
                )
            else:
                applied.append(
                    {
                        "scope": "request",
                        "status": "applied",
                        "notes": "Frequency cap noted on all products.",
                    }
                )
        return {
            "products": _PRODUCTS,
            "proposals": [proposal],
            "refinement_applied": applied,
        }

    async def finalize_proposal(
        self,
        req: FinalizeProposalRequest,
        ctx: RequestContext[Any],
    ) -> FinalizeProposalSuccess:
        """Inline-commit finalize. Locks pricing on the draft proposal
        and emits a 24h ``expires_at`` hold window.

        The framework hydrated ``req.recipes`` and ``req.proposal_payload``
        from the draft; this method lock-prices and returns. Framework
        commits via ``proposal_store.commit`` after projection.

        For HITL flows, return ``ctx.handoff_to_task(...)`` instead.
        The framework projects ``Submitted`` immediately, runs the
        handoff fn in the background, and commits the proposal to the
        store on completion (single-ledger guarantee per § D3).
        """
        del ctx
        committed_payload = dict(req.proposal_payload)
        committed_payload["proposal_status"] = "committed"
        # Replace indicative pricing with firm pricing per allocation.
        firm_cpm = {"ctv-premium-q2": 15.0, "display-run-q2": 2.5}
        for entry in committed_payload.get("allocations", []) or []:
            pid = entry.get("product_id")
            if pid in firm_cpm:
                entry["firm_cpm"] = firm_cpm[pid]
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        committed_payload["expires_at"] = expires_at.isoformat()
        return FinalizeProposalSuccess(
            proposal=committed_payload,
            expires_at=expires_at,
        )
