from __future__ import annotations

"""Tests for campaign governance types."""

import pytest

from adcp.types.campaign_governance import (
    CampaignPlan,
    CheckGovernanceRequest,
    CheckGovernanceResponse,
    GetPlanAuditLogsRequest,
    GetPlanAuditLogsResponse,
    GovernanceCondition,
    GovernanceDeliveryMetrics,
    GovernanceEscalation,
    GovernanceFinding,
    PlannedDelivery,
    PortfolioConfig,
    ReportPlanOutcomeRequest,
    ReportPlanOutcomeResponse,
    SyncPlanResult,
    SyncPlansRequest,
    SyncPlansResponse,
)

PLAN_DATA = {
    "plan_id": "plan_nike_q2_2026",
    "brand": {"domain": "nike.com"},
    "objectives": "Drive athletic wear awareness among fitness enthusiasts",
    "budget": {
        "total": 500000,
        "currency": "USD",
        "authority_level": "agent_limited",
        "per_seller_max_pct": 40,
        "reallocation_threshold": 0.15,
    },
    "channels": {
        "required": ["display", "video"],
        "allowed": ["display", "video", "ctv"],
        "mix_targets": {
            "display": {"min_pct": 30, "max_pct": 50},
            "video": {"min_pct": 40, "max_pct": 60},
        },
    },
    "flight": {
        "start": "2026-03-01T00:00:00Z",
        "end": "2026-05-31T23:59:59Z",
    },
    "countries": ["US", "CA"],
    "policy_ids": ["us_coppa", "scope3_brand_safety"],
    "approved_sellers": ["https://agent.publisher1.com", "https://agent.publisher2.com"],
    "delegations": [
        {
            "agent_url": "https://agency.example.com",
            "authority": "execute_only",
            "budget_limit": {"amount": 200000, "currency": "USD"},
            "markets": ["US"],
            "expires_at": "2026-06-01T00:00:00Z",
        }
    ],
}


class TestCampaignPlan:
    """Test CampaignPlan and sub-types."""

    def test_validates_full_plan(self):
        plan = CampaignPlan.model_validate(PLAN_DATA)
        assert plan.plan_id == "plan_nike_q2_2026"
        assert plan.brand == {"domain": "nike.com"}
        assert plan.budget.total == 500000
        assert plan.budget.authority_level == "agent_limited"
        assert plan.flight.start == "2026-03-01T00:00:00Z"
        assert plan.countries == ["US", "CA"]

    def test_channels_with_mix_targets(self):
        plan = CampaignPlan.model_validate(PLAN_DATA)
        assert plan.channels is not None
        assert plan.channels.required == ["display", "video"]
        assert "display" in plan.channels.mix_targets
        assert plan.channels.mix_targets["display"].min_pct == 30

    def test_delegations(self):
        plan = CampaignPlan.model_validate(PLAN_DATA)
        assert plan.delegations is not None
        assert len(plan.delegations) == 1
        assert plan.delegations[0].authority == "execute_only"
        assert plan.delegations[0].markets == ["US"]

    def test_minimal_plan(self):
        minimal = {
            "plan_id": "plan_1",
            "brand": {"domain": "example.com"},
            "objectives": "test",
            "budget": {"total": 1000, "currency": "USD", "authority_level": "agent_full"},
            "flight": {"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
        }
        plan = CampaignPlan.model_validate(minimal)
        assert plan.channels is None
        assert plan.countries is None
        assert plan.policy_ids is None
        assert plan.delegations is None
        assert plan.portfolio is None

    def test_portfolio_config(self):
        portfolio = PortfolioConfig.model_validate(
            {
                "member_plan_ids": ["plan_a", "plan_b"],
                "total_budget_cap": {"amount": 1000000, "currency": "USD"},
                "shared_policy_ids": ["scope3_brand_safety"],
            }
        )
        assert len(portfolio.member_plan_ids) == 2

    def test_extra_fields_preserved(self):
        data = {**PLAN_DATA, "custom_metadata": "value"}
        plan = CampaignPlan.model_validate(data)
        assert plan.custom_metadata == "value"  # type: ignore[attr-defined]


class TestSyncPlans:
    """Test sync_plans request/response types."""

    def test_request_validates(self):
        req = SyncPlansRequest.model_validate({"plans": [PLAN_DATA]})
        assert len(req.plans) == 1
        assert req.plans[0].plan_id == "plan_nike_q2_2026"

    def test_response_validates(self):
        resp = SyncPlansResponse.model_validate(
            {
                "plans": [
                    {
                        "plan_id": "plan_nike_q2_2026",
                        "status": "active",
                        "version": 1,
                        "categories": [
                            {"category_id": "budget_authority", "status": "active"},
                            {"category_id": "strategic_alignment", "status": "active"},
                        ],
                        "resolved_policies": [
                            {
                                "policy_id": "us_coppa",
                                "source": "explicit",
                                "enforcement": "must",
                                "reason": "Listed in plan policy_ids",
                            },
                            {
                                "policy_id": "scope3_brand_safety",
                                "source": "auto_applied",
                                "enforcement": "should",
                                "reason": "Default brand safety standard",
                            },
                        ],
                    }
                ]
            }
        )
        assert resp.plans[0].status == "active"
        assert len(resp.plans[0].categories) == 2
        assert resp.plans[0].resolved_policies[0].enforcement == "must"


class TestCheckGovernance:
    """Test check_governance request/response types."""

    def test_proposed_request(self):
        req = CheckGovernanceRequest.model_validate(
            {
                "plan_id": "plan_nike_q2_2026",
                "buyer_campaign_ref": "nike_spring_2026",
                "binding": "proposed",
                "caller": "https://orchestrator.example.com",
                "tool": "create_media_buy",
                "payload": {
                    "account": {"account_id": "acc_123"},
                    "brand": {"domain": "nike.com"},
                    "packages": [{"product_id": "prod_1", "budget": {"amount": 50000}}],
                },
            }
        )
        assert req.binding == "proposed"
        assert req.tool == "create_media_buy"
        assert req.payload is not None
        assert req.planned_delivery is None

    def test_committed_request(self):
        req = CheckGovernanceRequest.model_validate(
            {
                "plan_id": "plan_nike_q2_2026",
                "buyer_campaign_ref": "nike_spring_2026",
                "binding": "committed",
                "caller": "https://sales.publisher.com",
                "media_buy_id": "mb_456",
                "phase": "purchase",
                "planned_delivery": {
                    "geo": {"countries": ["US"]},
                    "channels": ["display"],
                    "start_time": "2026-03-01T00:00:00Z",
                    "end_time": "2026-05-31T23:59:59Z",
                    "total_budget": 50000,
                    "currency": "USD",
                    "enforced_policies": ["us_coppa"],
                },
            }
        )
        assert req.binding == "committed"
        assert req.media_buy_id == "mb_456"
        assert req.planned_delivery is not None
        assert req.planned_delivery.total_budget == 50000

    def test_delivery_phase_with_metrics(self):
        req = CheckGovernanceRequest.model_validate(
            {
                "plan_id": "plan_1",
                "buyer_campaign_ref": "camp_1",
                "binding": "committed",
                "caller": "https://seller.com",
                "media_buy_id": "mb_1",
                "phase": "delivery",
                "planned_delivery": {
                    "geo": {"countries": ["US"]},
                    "channels": ["display"],
                    "start_time": "2026-03-01T00:00:00Z",
                    "end_time": "2026-05-31T23:59:59Z",
                    "total_budget": 50000,
                    "currency": "USD",
                },
                "delivery_metrics": {
                    "reporting_period": {
                        "start": "2026-03-01T00:00:00Z",
                        "end": "2026-03-08T00:00:00Z",
                    },
                    "spend": 5000,
                    "cumulative_spend": 5000,
                    "impressions": 200000,
                    "cumulative_impressions": 200000,
                    "pacing": "on_track",
                },
            }
        )
        assert req.delivery_metrics is not None
        assert req.delivery_metrics.pacing == "on_track"

    def test_approved_response(self):
        resp = CheckGovernanceResponse.model_validate(
            {
                "check_id": "chk_001",
                "status": "approved",
                "binding": "proposed",
                "plan_id": "plan_1",
                "buyer_campaign_ref": "camp_1",
                "explanation": "All checks passed",
                "mode": "enforce",
                "expires_at": "2026-03-02T00:00:00Z",
            }
        )
        assert resp.status == "approved"
        assert resp.mode == "enforce"
        assert resp.findings is None

    def test_denied_response_with_findings(self):
        resp = CheckGovernanceResponse.model_validate(
            {
                "check_id": "chk_002",
                "status": "denied",
                "binding": "proposed",
                "plan_id": "plan_1",
                "buyer_campaign_ref": "camp_1",
                "explanation": "Budget exceeds per-seller limit",
                "mode": "enforce",
                "findings": [
                    {
                        "category_id": "budget_authority",
                        "severity": "critical",
                        "explanation": "Package budget is 60% of plan, exceeds 40% per-seller max",
                        "confidence": 0.99,
                    }
                ],
            }
        )
        assert resp.status == "denied"
        assert len(resp.findings) == 1
        assert resp.findings[0].category_id == "budget_authority"
        assert resp.findings[0].confidence == 0.99

    def test_conditions_response(self):
        resp = CheckGovernanceResponse.model_validate(
            {
                "check_id": "chk_003",
                "status": "conditions",
                "binding": "proposed",
                "plan_id": "plan_1",
                "buyer_campaign_ref": "camp_1",
                "explanation": "Budget adjustment required",
                "mode": "enforce",
                "conditions": [
                    {
                        "field": "packages[0].budget.amount",
                        "required_value": 200000,
                        "reason": "Exceeds per-seller maximum of 40%",
                    }
                ],
            }
        )
        assert resp.status == "conditions"
        assert len(resp.conditions) == 1
        assert resp.conditions[0].required_value == 200000

    def test_escalated_response(self):
        resp = CheckGovernanceResponse.model_validate(
            {
                "check_id": "chk_004",
                "status": "escalated",
                "binding": "proposed",
                "plan_id": "plan_1",
                "buyer_campaign_ref": "camp_1",
                "explanation": "Requires manager approval for budget reallocation",
                "mode": "enforce",
                "escalation": {
                    "reason": "Reallocation exceeds threshold",
                    "severity": "warning",
                    "requires_human": True,
                    "approval_tier": "manager",
                },
            }
        )
        assert resp.status == "escalated"
        assert resp.escalation is not None
        assert resp.escalation.approval_tier == "manager"

    def test_audit_mode_always_approves(self):
        resp = CheckGovernanceResponse.model_validate(
            {
                "check_id": "chk_005",
                "status": "approved",
                "binding": "proposed",
                "plan_id": "plan_1",
                "buyer_campaign_ref": "camp_1",
                "explanation": "Audit mode: logged findings but approved",
                "mode": "audit",
                "findings": [
                    {
                        "category_id": "budget_authority",
                        "severity": "warning",
                        "explanation": "Would exceed per-seller limit in enforce mode",
                    }
                ],
            }
        )
        assert resp.status == "approved"
        assert resp.mode == "audit"
        assert len(resp.findings) == 1


class TestReportPlanOutcome:
    """Test report_plan_outcome request/response types."""

    def test_completed_outcome(self):
        req = ReportPlanOutcomeRequest.model_validate(
            {
                "plan_id": "plan_1",
                "check_id": "chk_001",
                "buyer_campaign_ref": "camp_1",
                "outcome": "completed",
                "seller_response": {
                    "media_buy_id": "mb_456",
                    "packages": [
                        {"package_id": "pkg_1", "product_id": "prod_1", "budget": 50000}
                    ],
                    "planned_delivery": {
                        "geo": {"countries": ["US"]},
                        "channels": ["display"],
                        "start_time": "2026-03-01T00:00:00Z",
                        "end_time": "2026-05-31T23:59:59Z",
                        "total_budget": 50000,
                        "currency": "USD",
                    },
                },
            }
        )
        assert req.outcome == "completed"
        assert req.seller_response is not None
        assert req.seller_response.media_buy_id == "mb_456"

    def test_failed_outcome(self):
        req = ReportPlanOutcomeRequest.model_validate(
            {
                "plan_id": "plan_1",
                "check_id": "chk_001",
                "buyer_campaign_ref": "camp_1",
                "outcome": "failed",
                "error": {
                    "code": "SELLER_REJECTED",
                    "message": "Publisher rejected the media buy",
                },
            }
        )
        assert req.outcome == "failed"
        assert req.error is not None
        assert req.error.code == "SELLER_REJECTED"

    def test_delivery_outcome(self):
        req = ReportPlanOutcomeRequest.model_validate(
            {
                "plan_id": "plan_1",
                "buyer_campaign_ref": "camp_1",
                "outcome": "delivery",
                "delivery": {
                    "media_buy_id": "mb_456",
                    "reporting_period": {
                        "start": "2026-03-01T00:00:00Z",
                        "end": "2026-03-08T00:00:00Z",
                    },
                    "impressions": 200000,
                    "spend": 5000,
                },
            }
        )
        assert req.outcome == "delivery"
        assert req.delivery is not None
        assert req.delivery.impressions == 200000

    def test_response_accepted(self):
        resp = ReportPlanOutcomeResponse.model_validate(
            {
                "outcome_id": "out_001",
                "status": "accepted",
                "committed_budget": 50000,
                "plan_summary": {
                    "total_committed": 50000,
                    "budget_remaining": 450000,
                },
            }
        )
        assert resp.status == "accepted"
        assert resp.plan_summary.budget_remaining == 450000

    def test_response_with_findings(self):
        resp = ReportPlanOutcomeResponse.model_validate(
            {
                "outcome_id": "out_002",
                "status": "findings",
                "findings": [
                    {
                        "category_id": "seller_verification",
                        "severity": "warning",
                        "explanation": "Delivery geo differs from plan",
                    }
                ],
            }
        )
        assert resp.status == "findings"
        assert len(resp.findings) == 1


class TestGetPlanAuditLogs:
    """Test get_plan_audit_logs request/response types."""

    def test_request_by_plan_ids(self):
        req = GetPlanAuditLogsRequest.model_validate(
            {"plan_ids": ["plan_1", "plan_2"], "include_entries": True}
        )
        assert req.plan_ids == ["plan_1", "plan_2"]
        assert req.include_entries is True

    def test_request_by_portfolio(self):
        req = GetPlanAuditLogsRequest.model_validate(
            {"portfolio_plan_ids": ["portfolio_1"]}
        )
        assert req.portfolio_plan_ids == ["portfolio_1"]
        assert req.include_entries is False

    def test_response_validates(self):
        resp = GetPlanAuditLogsResponse.model_validate(
            {
                "plans": [
                    {
                        "plan_id": "plan_1",
                        "plan_version": 1,
                        "status": "active",
                        "budget": {
                            "authorized": 500000,
                            "committed": 50000,
                            "remaining": 450000,
                            "utilization_pct": 10,
                        },
                        "campaigns": [
                            {
                                "buyer_campaign_ref": "camp_1",
                                "status": "active",
                                "committed": 50000,
                                "active_media_buys": ["mb_456"],
                            }
                        ],
                        "summary": {
                            "checks_performed": 5,
                            "outcomes_reported": 2,
                            "statuses": {"approved": 4, "conditions": 1},
                            "findings_count": 3,
                            "drift_metrics": {
                                "escalation_rate": 0.05,
                                "escalation_rate_trend": "stable",
                                "auto_approval_rate": 0.8,
                                "human_override_rate": 0.02,
                                "mean_confidence": 0.92,
                            },
                        },
                    }
                ]
            }
        )
        plan = resp.plans[0]
        assert plan.status == "active"
        assert plan.budget["authorized"] == 500000
        assert len(plan.campaigns) == 1
        assert plan.summary.checks_performed == 5
        assert plan.summary.drift_metrics.escalation_rate == 0.05
        assert plan.summary.drift_metrics.escalation_rate_trend == "stable"

    def test_response_with_entries(self):
        resp = GetPlanAuditLogsResponse.model_validate(
            {
                "plans": [
                    {
                        "plan_id": "plan_1",
                        "status": "active",
                        "entries": [
                            {
                                "id": "entry_1",
                                "type": "check",
                                "timestamp": "2026-03-01T10:00:00Z",
                                "tool": "create_media_buy",
                                "status": "approved",
                                "binding": "proposed",
                            }
                        ],
                    }
                ]
            }
        )
        assert resp.plans[0].entries is not None
        assert len(resp.plans[0].entries) == 1
        assert resp.plans[0].entries[0]["type"] == "check"


class TestCampaignGovernanceExports:
    """Test that campaign governance types are exported from adcp.types."""

    def test_exported_from_types(self):
        import adcp.types

        assert adcp.types.CampaignPlan is CampaignPlan
        assert adcp.types.CheckGovernanceRequest is CheckGovernanceRequest
        assert adcp.types.CheckGovernanceResponse is CheckGovernanceResponse
        assert adcp.types.SyncPlansRequest is SyncPlansRequest
        assert adcp.types.SyncPlansResponse is SyncPlansResponse
        assert adcp.types.ReportPlanOutcomeRequest is ReportPlanOutcomeRequest
        assert adcp.types.ReportPlanOutcomeResponse is ReportPlanOutcomeResponse
        assert adcp.types.GetPlanAuditLogsRequest is GetPlanAuditLogsRequest
        assert adcp.types.GetPlanAuditLogsResponse is GetPlanAuditLogsResponse
        assert adcp.types.GovernanceFinding is GovernanceFinding
        assert adcp.types.GovernanceCondition is GovernanceCondition
        assert adcp.types.GovernanceEscalation is GovernanceEscalation
        assert adcp.types.PlannedDelivery is PlannedDelivery
        assert adcp.types.GovernanceDeliveryMetrics is GovernanceDeliveryMetrics


class TestValidationErrors:
    """Test that required fields are enforced."""

    def test_campaign_plan_missing_required_fields(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CampaignPlan.model_validate({})

        with pytest.raises(ValidationError):
            CampaignPlan.model_validate({"plan_id": "p1"})

        with pytest.raises(ValidationError):
            CampaignPlan.model_validate(
                {
                    "plan_id": "p1",
                    "brand": {"domain": "test.com"},
                    "objectives": "awareness",
                    "budget": {
                        "total": 10000,
                        "currency": "USD",
                        "authority_level": "agent_full",
                    },
                    # missing flight
                }
            )

    def test_check_governance_request_missing_required(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CheckGovernanceRequest.model_validate({})

        with pytest.raises(ValidationError):
            CheckGovernanceRequest.model_validate({"plan_id": "p1"})

        with pytest.raises(ValidationError):
            CheckGovernanceRequest.model_validate(
                {"plan_id": "p1", "buyer_campaign_ref": "c1", "binding": "proposed"}
            )

    def test_check_governance_response_missing_required(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CheckGovernanceResponse.model_validate({})

        with pytest.raises(ValidationError):
            CheckGovernanceResponse.model_validate({"check_id": "x", "status": "approved"})

    def test_sync_plan_result_requires_version(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SyncPlanResult.model_validate({"plan_id": "p1", "status": "active"})

    def test_report_plan_outcome_request_missing_required(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ReportPlanOutcomeRequest.model_validate({})

        with pytest.raises(ValidationError):
            ReportPlanOutcomeRequest.model_validate({"plan_id": "p1"})

    def test_delivery_metrics_impressions_accepts_float_from_json(self):
        """JSON has no int/float distinction; ensure float coerces to int."""
        metrics = GovernanceDeliveryMetrics.model_validate(
            {"impressions": 200000.0, "cumulative_impressions": 500000.0}
        )
        assert metrics.impressions == 200000
        assert metrics.cumulative_impressions == 500000

    def test_extra_fields_preserved_on_all_request_types(self):
        """Verify extra='allow' for forward compatibility."""
        req = CheckGovernanceRequest.model_validate(
            {
                "plan_id": "p",
                "buyer_campaign_ref": "c",
                "binding": "proposed",
                "caller": "agent",
                "future_field": "value",
            }
        )
        assert req.model_extra["future_field"] == "value"

        outcome_req = ReportPlanOutcomeRequest.model_validate(
            {
                "plan_id": "p",
                "buyer_campaign_ref": "c",
                "outcome": "completed",
                "future_field": True,
            }
        )
        assert outcome_req.model_extra["future_field"] is True

    def test_get_plan_audit_logs_with_buyer_campaign_ref(self):
        req = GetPlanAuditLogsRequest.model_validate(
            {"buyer_campaign_ref": "camp_1", "include_entries": True}
        )
        assert req.buyer_campaign_ref == "camp_1"
        assert req.include_entries is True
        assert req.plan_ids is None
