from __future__ import annotations

from copy import deepcopy
from datetime import datetime

import pytest
from pydantic import BaseModel

from adcp.client import ADCPClient
from adcp.negotiation import (
    WIRE_RESPONSE_METADATA_KEY,
    NegotiationVerificationError,
    compute_terms_digest,
    preflight_refine_proposals,
    verify_refine_proposals_response,
    verify_terms_digest,
)
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus


class _ParsedTerms(BaseModel):
    start_time: datetime


class _ParsedResponse(BaseModel):
    status: str
    results: list[dict]
    products: list[dict]


class _TypedRequest(BaseModel):
    value: str


def _terms() -> dict:
    return {
        "brand": {"domain": "buyer.example"},
        "purchases": [
            {
                "product_id": "p1",
                "pricing_option_id": "price1",
                "impressions": 1000,
                "pricing": {
                    "pricing_option_id": "price1",
                    "pricing_model": "cpm",
                    "currency": "USD",
                    "fixed_price": 4.5,
                },
            }
        ],
        "start_time": "2026-09-01T00:00:00Z",
        "end_time": "2026-09-30T00:00:00Z",
        "total_budget": {"amount": 100.0, "currency": "USD"},
    }


def _request() -> dict:
    return {
        "idempotency_key": "refine-request-0001",
        "refinements": [
            {
                "proposal_id": "source-1",
                "action": "revise",
                "constraints": {
                    "total_budget": {"min": 50, "max": 150, "currency": "USD"},
                    "cpm": {"max": 5, "currency": "USD"},
                    "impressions": {"min": 1000},
                    "flight": {
                        "start_no_later_than": "2026-09-01T00:00:00Z",
                        "end_no_earlier_than": "2026-09-30T00:00:00Z",
                    },
                },
                "product_changes": {"p1": "include", "p2": "omit"},
            }
        ],
    }


def _proposal(*, proposal_id: str = "draft-1", terms: dict | None = None) -> dict:
    commercial_terms = terms or _terms()
    return {
        "proposal_id": proposal_id,
        "parent_proposal_id": "source-1",
        "commercial_terms": commercial_terms,
        "terms_digest": compute_terms_digest(commercial_terms),
    }


def _response(*, proposal: dict | None = None) -> dict:
    return {
        "status": "completed",
        "results": [
            {
                "source_proposal_id": "source-1",
                "outcome": "revised",
                "proposals": [proposal or _proposal()],
            }
        ],
        "products": [],
    }


def test_compute_terms_digest_matches_pinned_jcs_vector() -> None:
    terms = _terms()

    assert compute_terms_digest(terms) == ("sha256:UUCtve7iK5_ipfee66eVUICUjfEW01lykFwf9GJw-jY")
    assert compute_terms_digest(dict(reversed(list(terms.items())))) == compute_terms_digest(terms)


def test_compute_terms_digest_rejects_normalized_models() -> None:
    parsed = _ParsedTerms(start_time="2026-09-01T00:00:00.000Z")

    with pytest.raises(TypeError, match="original wire mapping"):
        compute_terms_digest(parsed)  # type: ignore[arg-type]


def test_verify_terms_digest() -> None:
    proposal = _proposal()

    assert verify_terms_digest(proposal)
    proposal["terms_digest"] = "sha256:" + "A" * 43
    assert not verify_terms_digest(proposal)


def test_verify_completed_response_accepts_portable_constraints() -> None:
    result = verify_refine_proposals_response(_request(), _response())

    assert result.valid
    assert result.issues == ()
    result.raise_for_errors()


def test_preflight_missing_capability_is_unknown_not_unsupported() -> None:
    assert preflight_refine_proposals(_request(), None).valid


def test_preflight_explicit_dimensions_are_authoritative() -> None:
    result = preflight_refine_proposals(
        _request(), {"supported_dimensions": ["total_budget", "product_changes"]}
    )

    unsupported_pointers = {
        issue.pointer for issue in result.issues if issue.code == "unsupported_dimension"
    }
    assert unsupported_pointers == {
        "/refinements/0/constraints/cpm",
        "/refinements/0/constraints/impressions",
        "/refinements/0/constraints/flight",
    }


def test_preflight_enforces_alternative_ceiling_and_batch_invariants() -> None:
    request = _request()
    request["refinements"][0]["alternatives"] = {"count": 3}
    second = deepcopy(request["refinements"][0])
    second["action"] = "finalize"
    request["refinements"].append(second)
    capability = {
        "supported_dimensions": [
            "total_budget",
            "cpm",
            "impressions",
            "flight",
            "product_changes",
            "alternatives",
        ],
        "max_alternatives": 2,
    }

    result = preflight_refine_proposals(request, capability)

    codes = {issue.code for issue in result.issues}
    assert codes == {"duplicate_proposal_id", "max_alternatives", "mixed_finalize_batch"}


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda response: response["results"][0].update(source_proposal_id="other"),
            "result_order",
        ),
        (
            lambda response: response["results"][0]["proposals"][0].update(
                parent_proposal_id="other"
            ),
            "lineage",
        ),
        (
            lambda response: response["results"][0]["proposals"][0].update(
                terms_digest="sha256:" + "A" * 43
            ),
            "terms_digest",
        ),
    ],
)
def test_verify_response_detects_correlation_failures(mutation, code: str) -> None:
    response = _response()
    mutation(response)

    result = verify_refine_proposals_response(_request(), response)

    assert not result.valid
    assert code in {issue.code for issue in result.issues}


def test_partial_result_reports_failed_constraint_with_normative_precedence() -> None:
    terms = _terms()
    terms["total_budget"]["amount"] = 200
    response = _response(proposal=_proposal(terms=terms))
    response["results"][0].update(
        outcome="partial",
        reason_code="constraint_unsatisfiable",
        reason="Budget ceiling was not satisfied",
        unsatisfied_constraints=["total_budget"],
    )

    assert verify_refine_proposals_response(_request(), response).valid

    response["results"][0]["reason_code"] = "commercially_declined"
    result = verify_refine_proposals_response(_request(), response)
    assert "constraint_precedence" in {issue.code for issue in result.issues}


def test_revised_result_cannot_hide_failed_constraint() -> None:
    terms = _terms()
    terms["purchases"][0]["pricing"]["fixed_price"] = 6

    result = verify_refine_proposals_response(
        _request(), _response(proposal=_proposal(terms=terms))
    )

    assert "constraint_mismatch" in {issue.code for issue in result.issues}


def test_alternatives_require_requested_count_and_distinct_terms() -> None:
    request = _request()
    request["refinements"][0]["alternatives"] = {"count": 2}
    duplicate = _proposal(proposal_id="draft-2")
    response = _response()
    response["results"][0]["proposals"].append(duplicate)

    result = verify_refine_proposals_response(request, response)

    assert "duplicate_alternative" in {issue.code for issue in result.issues}

    response["results"][0]["proposals"].pop()
    result = verify_refine_proposals_response(request, response)
    assert "alternative_count" in {issue.code for issue in result.issues}


def test_submitted_response_without_results_has_nothing_to_verify() -> None:
    result = verify_refine_proposals_response(
        _request(), {"status": "submitted", "task_id": "task-1"}
    )

    assert result.valid


def test_completed_task_result_unwraps_wire_response() -> None:
    wire_response = _response()
    parsed = _ParsedResponse.model_validate(wire_response)
    task_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=parsed,
        metadata={WIRE_RESPONSE_METADATA_KEY: wire_response},
    )

    assert verify_refine_proposals_response(_request(), task_result).valid


@pytest.mark.asyncio
async def test_client_preserves_refinement_wire_response_for_verification() -> None:
    wire_response = _response()

    class _Adapter:
        async def refine_proposals(self, _params):
            return TaskResult(status=TaskStatus.COMPLETED, data=wire_response)

        def _parse_response(self, raw_result, _response_type):
            return raw_result

    client = ADCPClient(
        AgentConfig(
            id="wire-test",
            agent_uri="https://seller.example/mcp",
            protocol=Protocol.MCP,
        )
    )
    client.adapter = _Adapter()  # type: ignore[assignment]

    result = await client._execute_typed_task(
        "refine_proposals", _TypedRequest(value="x"), _ParsedResponse
    )

    assert result.metadata is not None
    assert result.metadata[WIRE_RESPONSE_METADATA_KEY] is wire_response


def test_completed_response_without_results_fails_closed() -> None:
    result = verify_refine_proposals_response(
        _request(), TaskResult(status=TaskStatus.COMPLETED, data=None)
    )

    assert {issue.code for issue in result.issues} == {"missing_results"}


def test_parsed_response_without_wire_data_cannot_verify_digest() -> None:
    parsed = _ParsedResponse.model_validate(_response())

    result = verify_refine_proposals_response(_request(), parsed)

    assert "wire_data_required" in {issue.code for issue in result.issues}


@pytest.mark.parametrize(
    ("action", "outcome"),
    [("revise", "finalized"), ("finalize", "revised"), ("finalize", "partial")],
)
def test_action_must_match_result_outcome(action: str, outcome: str) -> None:
    request = _request()
    request["refinements"][0]["action"] = action
    response = _response()
    response["results"][0]["outcome"] = outcome

    result = verify_refine_proposals_response(request, response)

    assert "action_outcome_mismatch" in {issue.code for issue in result.issues}


def test_revision_without_alternatives_returns_exactly_one_proposal() -> None:
    response = _response()
    second_terms = deepcopy(_terms())
    second_terms["total_budget"]["amount"] = 120
    response["results"][0]["proposals"].append(_proposal(proposal_id="draft-2", terms=second_terms))

    result = verify_refine_proposals_response(_request(), response)

    assert "alternative_count" in {issue.code for issue in result.issues}


def test_unsatisfied_product_changes_must_preserve_requested_values() -> None:
    response = _response()
    response["results"][0].update(
        outcome="partial",
        reason_code="constraint_unsatisfiable",
        unsatisfied_product_changes={"p1": "omit"},
    )

    result = verify_refine_proposals_response(_request(), response)

    assert "product_change_mismatch" in {issue.code for issue in result.issues}


def test_product_change_failure_has_constraint_reason_precedence() -> None:
    terms = _terms()
    terms["purchases"] = []
    response = _response(proposal=_proposal(terms=terms))
    response["results"][0].update(
        outcome="partial",
        reason_code="commercially_declined",
        unsatisfied_product_changes={"p1": "include"},
    )

    result = verify_refine_proposals_response(_request(), response)

    assert "constraint_precedence" in {issue.code for issue in result.issues}


def test_raise_for_errors_preserves_structured_issues() -> None:
    response = deepcopy(_response())
    response["results"] = []
    result = verify_refine_proposals_response(_request(), response)

    with pytest.raises(NegotiationVerificationError) as exc_info:
        result.raise_for_errors()

    assert exc_info.value.issues[0].code == "cardinality"
