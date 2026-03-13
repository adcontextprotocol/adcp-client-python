"""Campaign governance types.

Hand-written from the AdCP campaign governance specification until JSON schemas
are published to the adcontextprotocol.org schema index.

Covers four tasks: sync_plans, check_governance, report_plan_outcome,
and get_plan_audit_logs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
# Shared enums as string literals (kept as plain strings for forward compat)
# ============================================================================

# authority_level: "agent_full" | "agent_limited" | "human_required"
# delegation_authority: "full" | "execute_only" | "propose_only"
# governance_binding: "proposed" | "committed"
# governance_phase: "purchase" | "modification" | "delivery"
# check_status: "approved" | "denied" | "conditions" | "escalated"
# governance_mode: "audit" | "advisory" | "enforce"
# finding_severity: "critical" | "warning" | "info"
# plan_status: "active" | "suspended" | "completed"
# outcome_type: "completed" | "failed" | "delivery"
# pacing: "ahead" | "on_track" | "behind"


# ============================================================================
# Campaign Plan (sync_plans)
# ============================================================================


class BudgetConfig(BaseModel):
    """Budget configuration for a campaign plan."""

    model_config = ConfigDict(extra="allow")

    total: float
    currency: str
    authority_level: str
    per_seller_max_pct: float | None = None
    reallocation_threshold: float | None = None


class ChannelMixTarget(BaseModel):
    """Min/max percentage targets for a channel."""

    model_config = ConfigDict(extra="allow")

    min_pct: float
    max_pct: float


class ChannelsConfig(BaseModel):
    """Channel configuration for a campaign plan."""

    model_config = ConfigDict(extra="allow")

    required: list[str] | None = None
    allowed: list[str] | None = None
    mix_targets: dict[str, ChannelMixTarget] | None = None


class FlightConfig(BaseModel):
    """Flight dates for a campaign plan."""

    model_config = ConfigDict(extra="allow")

    start: str
    end: str


class Delegation(BaseModel):
    """Agent delegation within a campaign plan."""

    model_config = ConfigDict(extra="allow")

    agent_url: str
    authority: str
    budget_limit: dict[str, Any] | None = None
    markets: list[str] | None = None
    expires_at: str | None = None


class PortfolioConfig(BaseModel):
    """Cross-brand portfolio governance configuration."""

    model_config = ConfigDict(extra="allow")

    member_plan_ids: list[str]
    total_budget_cap: dict[str, Any] | None = None
    shared_policy_ids: list[str] | None = None
    shared_exclusions: list[str] | None = None


class CampaignPlan(BaseModel):
    """A campaign governance plan pushed via sync_plans."""

    model_config = ConfigDict(extra="allow")

    plan_id: str
    brand: dict[str, Any]
    objectives: str
    budget: BudgetConfig
    channels: ChannelsConfig | None = None
    flight: FlightConfig
    countries: list[str] | None = None
    regions: list[str] | None = None
    policy_ids: list[str] | None = None
    custom_policies: list[str] | None = None
    approved_sellers: list[str] | None = None
    delegations: list[Delegation] | None = None
    portfolio: PortfolioConfig | None = None
    plan_version: int | None = None
    ext: dict[str, Any] | None = None


# ============================================================================
# sync_plans request / response
# ============================================================================


class SyncPlansRequest(BaseModel):
    """Request to push campaign plans to a governance agent."""

    model_config = ConfigDict(extra="allow")

    plans: list[CampaignPlan]


class ResolvedPolicyEntry(BaseModel):
    """A policy resolved during plan sync."""

    model_config = ConfigDict(extra="allow")

    policy_id: str
    source: str
    enforcement: str
    reason: str


class GovernanceCategory(BaseModel):
    """A governance category and its status for a plan."""

    model_config = ConfigDict(extra="allow")

    category_id: str
    status: str


class SyncPlanResult(BaseModel):
    """Result for a single plan in a sync_plans response."""

    model_config = ConfigDict(extra="allow")

    plan_id: str
    status: str
    version: int
    categories: list[GovernanceCategory] = Field(default_factory=list)
    resolved_policies: list[ResolvedPolicyEntry] = Field(default_factory=list)


class SyncPlansResponse(BaseModel):
    """Response from sync_plans."""

    model_config = ConfigDict(extra="allow")

    plans: list[SyncPlanResult]


# ============================================================================
# check_governance request / response
# ============================================================================


class PlannedDelivery(BaseModel):
    """Delivery parameters confirmed by seller."""

    model_config = ConfigDict(extra="allow")

    geo: dict[str, Any]
    channels: list[str]
    start_time: str
    end_time: str
    total_budget: float
    currency: str
    frequency_cap: dict[str, Any] | None = None
    audience_summary: str | None = None
    enforced_policies: list[str] | None = None


class GovernanceDeliveryMetrics(BaseModel):
    """Delivery performance metrics for governance reporting phase."""

    model_config = ConfigDict(extra="allow")

    reporting_period: dict[str, str] | None = None
    spend: float | None = None
    cumulative_spend: float | None = None
    impressions: int | None = None
    cumulative_impressions: int | None = None
    geo_distribution: dict[str, Any] | None = None
    channel_distribution: dict[str, Any] | None = None
    pacing: str | None = None


class CheckGovernanceRequest(BaseModel):
    """Request to validate a campaign action against governance policies.

    Two binding modes:
    - proposed: orchestrator checks before sending to seller (includes tool + payload)
    - committed: seller checks after confirming (includes planned_delivery)
    """

    model_config = ConfigDict(extra="allow")

    plan_id: str
    buyer_campaign_ref: str
    binding: str
    caller: str

    # Proposed binding fields
    tool: str | None = None
    payload: dict[str, Any] | None = None

    # Committed binding fields
    media_buy_id: str | None = None
    buyer_ref: str | None = None
    phase: str | None = None
    planned_delivery: PlannedDelivery | None = None
    modification_summary: str | None = None
    delivery_metrics: GovernanceDeliveryMetrics | None = None


class GovernanceFinding(BaseModel):
    """A finding from a governance check."""

    model_config = ConfigDict(extra="allow")

    category_id: str
    severity: str
    explanation: str
    policy_id: str | None = None
    details: dict[str, Any] | None = None
    confidence: float | None = None
    uncertainty_reason: str | None = None


class GovernanceCondition(BaseModel):
    """A required adjustment before an action can proceed."""

    model_config = ConfigDict(extra="allow")

    field: str
    required_value: Any | None = None
    reason: str


class GovernanceEscalation(BaseModel):
    """Escalation details when human review is required."""

    model_config = ConfigDict(extra="allow")

    reason: str
    severity: str
    requires_human: bool = True
    approval_tier: str | None = None


class CheckGovernanceResponse(BaseModel):
    """Response from a governance check."""

    model_config = ConfigDict(extra="allow")

    check_id: str
    status: str
    binding: str
    plan_id: str
    buyer_campaign_ref: str
    explanation: str
    mode: str

    findings: list[GovernanceFinding] | None = None
    conditions: list[GovernanceCondition] | None = None
    escalation: GovernanceEscalation | None = None
    expires_at: str | None = None
    next_check: str | None = None


# ============================================================================
# report_plan_outcome request / response
# ============================================================================


class OutcomeSellerResponse(BaseModel):
    """Seller's response data reported back to governance."""

    model_config = ConfigDict(extra="allow")

    media_buy_id: str | None = None
    buyer_ref: str | None = None
    packages: list[dict[str, Any]] | None = None
    planned_delivery: PlannedDelivery | None = None
    creative_deadline: str | None = None


class OutcomeError(BaseModel):
    """Error details for a failed outcome."""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str


class ReportPlanOutcomeRequest(BaseModel):
    """Request to close the governance loop after seller response."""

    model_config = ConfigDict(extra="allow")

    plan_id: str
    check_id: str | None = None
    buyer_campaign_ref: str
    outcome: str

    seller_response: OutcomeSellerResponse | None = None
    delivery: GovernanceDeliveryMetrics | None = None
    error: OutcomeError | None = None


class PlanSummary(BaseModel):
    """Budget summary for a plan after outcome reporting."""

    model_config = ConfigDict(extra="allow")

    total_committed: float
    budget_remaining: float


class ReportPlanOutcomeResponse(BaseModel):
    """Response from report_plan_outcome."""

    model_config = ConfigDict(extra="allow")

    outcome_id: str
    status: str
    committed_budget: float | None = None
    findings: list[GovernanceFinding] | None = None
    plan_summary: PlanSummary | None = None


# ============================================================================
# get_plan_audit_logs request / response
# ============================================================================


class GetPlanAuditLogsRequest(BaseModel):
    """Request audit trail for campaign plans."""

    model_config = ConfigDict(extra="allow")

    plan_ids: list[str] | None = None
    portfolio_plan_ids: list[str] | None = None
    buyer_campaign_ref: str | None = None
    include_entries: bool = False


class DriftMetrics(BaseModel):
    """Aggregate metrics tracking governance drift over time."""

    model_config = ConfigDict(extra="allow")

    escalation_rate: float | None = None
    escalation_rate_trend: str | None = None
    auto_approval_rate: float | None = None
    human_override_rate: float | None = None
    mean_confidence: float | None = None
    thresholds: dict[str, Any] | None = None


class AuditLogCampaign(BaseModel):
    """Campaign-level audit data within a plan."""

    model_config = ConfigDict(extra="allow")

    buyer_campaign_ref: str
    status: str
    committed: float
    active_media_buys: list[str] = Field(default_factory=list)


class AuditLogSummary(BaseModel):
    """Aggregate audit summary for a plan."""

    model_config = ConfigDict(extra="allow")

    checks_performed: int = 0
    outcomes_reported: int = 0
    statuses: dict[str, int] = Field(default_factory=dict)
    findings_count: int = 0
    escalations: list[dict[str, Any]] = Field(default_factory=list)
    drift_metrics: DriftMetrics | None = None


class PlanAuditLog(BaseModel):
    """Audit data for a single campaign plan."""

    model_config = ConfigDict(extra="allow")

    plan_id: str
    plan_version: int | None = None
    status: str
    budget: dict[str, Any] | None = None
    channel_allocation: dict[str, Any] | None = None
    campaigns: list[AuditLogCampaign] = Field(default_factory=list)
    summary: AuditLogSummary | None = None
    entries: list[dict[str, Any]] | None = None


class GetPlanAuditLogsResponse(BaseModel):
    """Response from get_plan_audit_logs."""

    model_config = ConfigDict(extra="allow")

    plans: list[PlanAuditLog]
