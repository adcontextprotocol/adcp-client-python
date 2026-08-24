"""Runnable compact proposal-negotiation workflow.

Run with ``uv run python examples/negotiation_workflow.py``. Production buyers
use ``ADCPClient.refine_proposals_verified``; the in-process client below keeps
the example deterministic while exercising the same helper.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from adcp.decisioning import execute_refinement_batch
from adcp.negotiation import WIRE_RESPONSE_METADATA_KEY, refine_proposals_verified
from adcp.types.core import TaskResult, TaskStatus

CAPABILITY = {
    "supported_dimensions": [
        "total_budget",
        "cpm",
        "impressions",
        "flight",
        "product_changes",
        "alternatives",
        "criteria",
    ],
    "max_alternatives": 3,
}


def commercial_terms(amount: float = 100) -> dict[str, Any]:
    return {
        "brand": {"domain": "buyer.example"},
        "purchases": [
            {
                "product_id": "display-1",
                "pricing": {
                    "pricing_option_id": "po-display-1",
                    "pricing_model": "cpm",
                    "currency": "USD",
                    "fixed_price": 5,
                },
                "pricing_option_id": "po-display-1",
                "impressions": 20_000,
            }
        ],
        "total_budget": {"amount": amount, "currency": "USD"},
        "start_time": "2026-09-01T00:00:00Z",
        "end_time": "2026-09-30T00:00:00Z",
    }


def proposal(source_id: str, *, committed: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "proposal_id": f"next-{source_id}",
        "proposal_kind": "new_media_buy",
        "proposal_status": "committed" if committed else "draft",
        "name": "September display plan",
        "commercial_terms": commercial_terms(),
    }
    if committed:
        value["expires_at"] = "2099-08-25T00:00:00Z"
    return value


def source_proposal(proposal_id: str) -> dict[str, Any]:
    value = proposal(proposal_id)
    value["proposal_id"] = proposal_id
    return value


async def commercial_policy(refinement: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Example seller-owned commercial decision callback."""
    source_id = refinement["proposal_id"]
    if refinement["action"] == "finalize":
        return {"outcome": "finalized", "proposal": proposal(source_id, committed=True)}
    if source_id.startswith("missing-"):
        return {
            "outcome": "unable",
            "reason_code": "source_unavailable",
            "reason": "The source proposal is no longer available.",
        }
    budget = refinement.get("constraints", {}).get("total_budget", {})
    if budget.get("max", 100) < 100:
        return {
            "outcome": "unable",
            "reason_code": "constraint_unsatisfiable",
            "reason": "The requested budget cannot buy the minimum flight.",
            "unsatisfied_constraints": ["total_budget"],
        }
    alternatives = refinement.get("alternatives", {})
    if alternatives.get("count", 1) > 1:
        return {
            "outcome": "partial",
            "reason_code": "alternatives_unavailable",
            "reason": "Only one commercially distinct alternative is available.",
            "proposals": [proposal(source_id)],
        }
    return {"outcome": "revised", "proposals": [proposal(source_id)]}


@asynccontextmanager
async def inventory_transaction(_request: Any, _context: Any):
    """Replace with a database transaction that stages all inventory holds."""
    print("begin inventory transaction")
    try:
        yield
    except Exception:
        print("rollback inventory transaction")
        raise
    else:
        print("commit inventory transaction")


class InProcessClient:
    """Small transport stand-in; ADCPClient retains the same wire metadata."""

    def __init__(self, response: dict[str, Any], *, replayed: bool = False) -> None:
        self.response = response
        self.replayed = replayed

    async def refine_proposals(self, _request: Any) -> TaskResult[Any]:
        return TaskResult(
            status=TaskStatus.COMPLETED,
            data=self.response,
            metadata={WIRE_RESPONSE_METADATA_KEY: self.response},
            replayed=self.replayed,
        )


async def round_trip(
    request: dict[str, Any],
    *,
    cache: dict[str, tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    key = request["idempotency_key"]
    fingerprint = json.dumps(request, sort_keys=True, separators=(",", ":"))
    cached = cache.get(key)
    if cached is not None and cached[0] != fingerprint:
        raise ValueError("same idempotency_key was reused for a changed request")
    replayed = cached is not None
    source_proposals = {
        item["proposal_id"]: source_proposal(item["proposal_id"]) for item in request["refinements"]
    }
    response = (
        cached[1]
        if cached is not None
        else await execute_refinement_batch(
            request,
            CAPABILITY,
            commercial_policy,
            finalize_transaction=(
                inventory_transaction if request["refinements"][0]["action"] == "finalize" else None
            ),
            source_proposals=source_proposals,
        )
    )
    cache[key] = (fingerprint, response)
    verified = await refine_proposals_verified(
        InProcessClient(response, replayed=replayed),
        request,
        CAPABILITY,
        source_proposals=source_proposals,
    )
    outcomes = [item["outcome"] for item in response["results"]]
    print(f"outcomes={outcomes} valid={verified.valid} replayed={replayed}")
    return response


async def main() -> None:
    cache: dict[str, tuple[str, dict[str, Any]]] = {}
    revise = {
        "idempotency_key": "revise-example-0001",
        "refinements": [
            {
                "proposal_id": "draft-1",
                "action": "revise",
                "constraints": {"total_budget": {"min": 90, "max": 110, "currency": "USD"}},
            }
        ],
    }
    await round_trip(revise, cache=cache)

    partial = {
        "idempotency_key": "partial-example-001",
        "refinements": [
            {
                "proposal_id": "draft-partial",
                "action": "revise",
                "alternatives": {"count": 2},
            }
        ],
    }
    await round_trip(partial, cache=cache)

    unable = {
        "idempotency_key": "unable-example-001",
        "refinements": [
            {
                "proposal_id": "draft-2",
                "action": "revise",
                "constraints": {"total_budget": {"max": 50, "currency": "USD"}},
            }
        ],
    }
    await round_trip(unable, cache=cache)

    unavailable = {
        "idempotency_key": "missing-example-001",
        "refinements": [
            {"proposal_id": "missing-draft", "action": "revise", "ask": "Please revise."}
        ],
    }
    await round_trip(unavailable, cache=cache)

    finalize = {
        "idempotency_key": "finalize-example-01",
        "refinements": [{"proposal_id": "draft-3", "action": "finalize"}],
    }
    await round_trip(finalize, cache=cache)
    # Exact retry reuses the cached response and does not run seller policy or
    # inventory code a second time.
    await round_trip(finalize, cache=cache)

    changed = {**finalize, "refinements": [{"proposal_id": "draft-4", "action": "finalize"}]}
    try:
        await round_trip(changed, cache=cache)
    except ValueError as exc:
        print(f"changed-request conflict: {exc}")
    changed["idempotency_key"] = "finalize-example-02"
    await round_trip(changed, cache=cache)


if __name__ == "__main__":
    asyncio.run(main())
