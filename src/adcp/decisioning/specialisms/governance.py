"""CampaignGovernancePlatform Protocol — covers ``governance-spend-authority``
and ``governance-delivery-monitor``.

A governance agent making runtime decisions for advertiser campaigns
implements the methods on this Protocol. Today's spec splits the
governance-AGENT role across two specialism slugs differing only by
capability (gating spend vs monitoring delivery); both share the
same Protocol surface. When ``adcontextprotocol/adcp#3329`` lands and
the spec consolidates to a single ``campaign-governance`` slug, the
underlying type stays unchanged — only the slug map updates.

Mirrors the JS-side ``CampaignGovernancePlatform`` interface at
``src/lib/server/decisioning/specialisms/campaign-governance.ts``.

**Distinct from ``governance-aware-seller``.** That third
``governance-*`` slug names a SELLER claim — a sales-* archetype
that composes with a buyer's governance agent (calls
``check_governance``, accepts ``sync_governance``, propagates
approvals/conditions/denials). It does NOT implement
``CampaignGovernancePlatform`` itself; it integrates WITH a platform
that does. The framework's required-method coverage for
``governance-aware-seller`` is therefore unenforced — the slug
remains a "spec-recognized but unenforced" claim until/unless
sync_governance handler shim wiring lands for sales adopters.

**Security gate (foundation).** Adopters claiming any of the three
``governance-*`` slugs MUST set
``DecisioningCapabilities.governance_aware=True`` AND wire a custom
:class:`adcp.decisioning.StateReader` that returns real
:data:`adcp.decisioning.GovernanceContextJWS` values. The
foundation's :func:`adcp.decisioning.dispatch.validate_platform`
fails-fast at server boot if any governance-* slug is claimed
without ``governance_aware=True``. Required-method enforcement
(this PR) AND governance-aware enforcement (foundation) are
INDEPENDENT gates; both fire independently. A platform passing the
required-method gate but with ``governance_aware=False`` still
fails server boot — silent governance-gate skipping is a security
regression the framework refuses to ship.

Required methods (every governance-AGENT specialism):

* :meth:`check_governance` — runtime decision (approved / denied /
  conditions). Sync.
* :meth:`sync_plans` — plan CRUD; buyers push their plans into the
    agent so it can maintain spend authority + delivery context.
* :meth:`report_plan_outcome` — buyer/orchestrator outcome reporting after a
  seller response. Sellers report delivery through execution checks instead.
* :meth:`get_plan_audit_logs` — chronological audit log read.

Async story: every method is sync at the wire level — none of the
governance response schemas declare a ``Submitted`` arm. Slow
approval pipelines (operator review) return current state (e.g.,
``status: 'pending'``) and emit ``ctx.publish_status_change(
resource_type='plan', ...)`` when the human decision lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import MaybeAsync
    from adcp.types import (
        CheckGovernanceRequest,
        CheckGovernanceResponse,
        GetPlanAuditLogsRequest,
        GetPlanAuditLogsResponse,
        ReportPlanOutcomeRequest,
        ReportPlanOutcomeResponse,
        SyncPlansRequest,
        SyncPlansResponse,
    )


#: Per-platform metadata generic.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class CampaignGovernancePlatform(Protocol, Generic[TMeta]):
    """Runtime governance decisioning for advertiser campaigns.

    A decision API: the agent inspects a proposed action (or running
    delivery) and returns ``approved``, ``denied``, or ``conditions``
    (approved-if). Status changes (plan moving from
    ``pending_approval`` → ``active`` → ``closed``) flow via
    ``ctx.publish_status_change(resource_type='plan', ...)``.

    Methods may be sync (return ``T`` directly) or async (return
    ``Awaitable[T]``); the dispatch adapter detects via
    :func:`asyncio.iscoroutinefunction` and runs sync methods on a
    thread pool.

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    rejection (``PLAN_NOT_FOUND``, ``INVALID_REQUEST``, etc.). Use
    the :meth:`check_governance` response ``status: 'denied'`` for
    governance decisions that ARE the answer (the plan exists and
    the agent is rejecting the action) — that's a legitimate
    business outcome, not an error.
    """

    def check_governance(
        self,
        req: CheckGovernanceRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[CheckGovernanceResponse]:
        """Runtime governance decision.

        A buyer sends an intent-shaped request (``target_agent`` plus the
        exact downstream ``tool`` and ``payload``); an executing service sends
        an execution-shaped request (opaque ``governance_context`` plus
        seller-authoritative ``planned_delivery``). Modern responses identify
        the shape with ``check_type``.

        Conditions are valid only for ``check_type='intent'``. Execution is a
        binary approved/denied boundary. Its ``phase`` is ``purchase``,
        ``modification``, or ``delivery`` and never exposes the buyer's plan or
        original payload to the service.

        :raises adcp.decisioning.AdcpError: for buyer-fixable
            rejection (``PLAN_NOT_FOUND``, ``INVALID_REQUEST``).
            ``status: 'denied'`` on the response is the
            governance-decision-as-answer path — not an error.
        """
        ...

    def sync_plans(
        self,
        req: SyncPlansRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[SyncPlansResponse]:
        """Plan CRUD with delta upsert semantics.

        Buyers sync their campaign plans into the governance agent so
        the agent can maintain spend authority + delivery context.
        The agent tracks plan state across the campaign lifecycle
        (pending_approval → active → closed); transitions are emitted
        via ``ctx.publish_status_change(resource_type='plan', ...)``.
        """
        ...

    def report_plan_outcome(
        self,
        req: ReportPlanOutcomeRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ReportPlanOutcomeResponse]:
        """Buyer/orchestrator outcome reporting after a seller response.

        Completed and failed reports settle the exact approved action;
        buyer-attributed delivery observations reconcile a prior seller
        delivery check. Sellers themselves report canonical delivery through
        ``check_governance(phase='delivery')``.
        """
        ...

    def get_plan_audit_logs(
        self,
        req: GetPlanAuditLogsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetPlanAuditLogsResponse]:
        """Audit log read.

        Returns the chronological history of governance decisions +
        outcome reports for a plan. Buyers and operators use this to
        reconstruct who approved what + when, what conditions were
        attached, and what the seller reported.
        """
        ...


__all__ = ["CampaignGovernancePlatform"]
