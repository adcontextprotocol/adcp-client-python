"""HITL gate — compose_method before-hook for salesagent's approval flow.

Salesagent's HITL today (verified at file:line in PR #506):
  1. Adapter checks _requires_manual_approval(op) and not _already_approved
     (google_ad_manager.py:571)
  2. If gated: writes WorkflowStep row + ObjectWorkflowMapping linking
     step → media buy + MediaBuy(status='pending_approval', raw_request=...)
  3. Returns 200 with workflow_step_id; no GAM call yet
  4. Operator approves through admin UI
  5. admin/blueprints/workflows.py:155 directly calls
     execute_approved_media_buy (media_buy_create.py:458)
  6. execute_approved_media_buy reconstructs request, sets
     setattr(request, "_already_approved", True), re-calls adapter

The SDK runtime preserves this flow:
- The before-hook does step 2 (write WorkflowStep + MediaBuy rows via
  the existing workflow_manager) and short-circuits with
  CreateMediaBuySuccess(status='pending_approval', workflow_step_id=...)
- The admin UI route is unchanged
- execute_approved_media_buy gets a small body rewrite to call back
  into the SDK runtime's create_media_buy with the marker attached
- compose_method passes req through unchanged (verified in Step 0.5);
  setattr survives Python-level dispatch

Operation-name mapping: salesagent's manual_approval_operations config
keys on GAM-internal names (`create_media_buy`, `update_media_buy`,
`add_creative_assets`). The wire-level equivalents are the first two
verbatim, plus `sync_creatives` for `add_creative_assets`. The gate
maps wire → GAM-internal before checking the config set.
"""

from __future__ import annotations

import uuid
from typing import Any

from adcp.decisioning.compose import ShortCircuit
from adcp.decisioning.context import RequestContext

# Wire-name → GAM-internal-name mapping for HITL config lookup.
# salesagent's AdapterConfig.gam_manual_approval_required is a single
# bool (tenant-scoped); manual_approval_operations is the per-op set.
_WIRE_TO_GAM_OPERATION: dict[str, str] = {
    "create_media_buy": "create_media_buy",
    "update_media_buy": "update_media_buy",
    "sync_creatives": "add_creative_assets",
}


def make_hitl_before_hook(operation: str) -> Any:
    """Build a `before` hook for `compose_method` that checks the
    HITL flag and short-circuits with pending status.

    :param operation: Wire-level operation name (`create_media_buy`,
        `update_media_buy`, `sync_creatives`). The hook maps to the
        GAM-internal name internally.
    """
    gam_op = _WIRE_TO_GAM_OPERATION.get(operation, operation)

    async def hitl_gate(req: Any, ctx: RequestContext[Any]) -> ShortCircuit[Any] | None:
        # The marker — set by execute_approved_media_buy after admin approval.
        # See verification in Step 0.5: setattr survives compose_method dispatch.
        if getattr(req, "_already_approved", False):
            return None  # fall through to inner; gate already cleared

        from .account_store import fetch_gam_manual_approval_required

        if not await fetch_gam_manual_approval_required(ctx):
            return None  # tenant doesn't require manual approval

        # Check the per-op set on AdapterConfig (or fall back to default
        # set: {create_media_buy, update_media_buy, add_creative_assets}).
        # In practice, salesagent uses the default — check in production
        # data before relying on it.
        if not _operation_requires_approval(ctx, gam_op):
            return None

        # Write the WorkflowStep + MediaBuy rows via salesagent's existing
        # workflow_manager. The admin UI sees the pending step and the
        # operator approves via the unchanged Flask blueprint route.
        workflow_step_id = await _create_workflow_step(req, ctx, gam_op)

        # Return pending status to the buyer. The wire shape is the
        # operation's *_response with status='pending_approval'.
        return ShortCircuit(value=_build_pending_response(operation, req, workflow_step_id))

    return hitl_gate


# ---------------------------------------------------------------------------
# Private helpers — wrap salesagent's workflow_manager
# ---------------------------------------------------------------------------


def _operation_requires_approval(ctx: RequestContext[Any], gam_op: str) -> bool:
    """Check tenant's manual_approval_operations set, falling back to default."""
    # AdapterConfig.manual_approval_operations isn't a column today —
    # salesagent stores the default set hardcoded (base.py:228). Only
    # gam_manual_approval_required is the per-tenant gate.
    # If we needed per-op customization, we'd extend AdapterConfig in
    # the experiment fork. For v1, the gate fires on all three ops when
    # gam_manual_approval_required is true.
    default_ops = {"create_media_buy", "update_media_buy", "add_creative_assets"}
    return gam_op in default_ops


async def _create_workflow_step(req: Any, ctx: RequestContext[Any], gam_op: str) -> str:
    """Persist the pending WorkflowStep + MediaBuy(raw_request=...) rows.

    Wraps salesagent's existing workflow_manager.create_manual_order_workflow_step
    (and equivalents for update/creatives). The MediaBuy.raw_request column
    stores the JSON for reconstruction at approval time.
    """
    try:
        from src.adapters.gam.managers.workflow import (  # type: ignore[import-not-found]
            WorkflowManager,
        )
    except ImportError:
        raise RuntimeError("Requires salesagent imports.")

    tenant_id = ctx.account.metadata["tenant_id"]
    media_buy_id = f"gam_order_{uuid.uuid4().hex[:8]}"

    # Salesagent's WorkflowManager takes (tenant_id, principal_id, ...);
    # exact constructor depends on salesagent version. The wrap
    # delegates entirely.
    workflow_mgr = WorkflowManager(
        tenant_id=tenant_id,
        principal_id=ctx.account.metadata["principal_id"],
    )

    # Branch by op — different create_*_workflow_step calls in salesagent.
    if gam_op == "create_media_buy":
        step_id = workflow_mgr.create_manual_order_workflow_step(
            request=req,
            packages=getattr(req, "packages", []),
            start_time=getattr(req, "start_time", None),
            end_time=getattr(req, "end_time", None),
            media_buy_id=media_buy_id,
        )
    elif gam_op == "update_media_buy":
        step_id = workflow_mgr.create_approval_workflow_step(
            media_buy_id=getattr(req, "media_buy_id", media_buy_id),
            workflow_type=f"update_media_buy_{getattr(req, 'action', '')}",
        )
    elif gam_op == "add_creative_assets":
        step_id = workflow_mgr.create_approval_workflow_step(
            media_buy_id=getattr(req, "media_buy_id", media_buy_id),
            workflow_type="creative_assets_approval",
        )
    else:
        raise ValueError(f"Unknown gam_op: {gam_op}")

    return step_id


def _build_pending_response(operation: str, req: Any, workflow_step_id: str) -> Any:
    """Build the wire-level pending response for the gate's short-circuit.

    Each operation has its own response shape. For create_media_buy,
    salesagent's existing _build_create_success(...) helper produces the
    correct shape — but it's an adapter-side helper. The side-car needs
    a small projection that returns the wire response with status=
    'pending_approval' and workflow_step_id populated.

    For v1, we delegate to salesagent's existing helpers where they
    exist; otherwise build the response from CreateMediaBuySuccess etc.
    """
    from adcp.types import (  # noqa: PLC0415
        CreateMediaBuyResponse,
        SyncCreativesResponse,
        UpdateMediaBuyResponse,
    )

    if operation == "create_media_buy":
        return CreateMediaBuyResponse(
            status="pending_approval",  # type: ignore[arg-type]  # wire literal
            workflow_step_id=workflow_step_id,
            buyer_ref=getattr(req, "buyer_ref", None),
        )
    elif operation == "update_media_buy":
        return UpdateMediaBuyResponse(
            status="pending_approval",  # type: ignore[arg-type]
            workflow_step_id=workflow_step_id,
            media_buy_id=getattr(req, "media_buy_id", None),
        )
    elif operation == "sync_creatives":
        return SyncCreativesResponse(
            status="pending_approval",  # type: ignore[arg-type]
            workflow_step_id=workflow_step_id,
        )
    raise ValueError(f"Unknown operation: {operation}")
