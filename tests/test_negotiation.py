from __future__ import annotations

from copy import deepcopy

import pytest

from adcp.negotiation import (
    NegotiationVerificationError,
    compute_terms_digest,
    verify_refine_proposals_response,
    verify_terms_digest,
)


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


def test_raise_for_errors_preserves_structured_issues() -> None:
    response = deepcopy(_response())
    response["results"] = []
    result = verify_refine_proposals_response(_request(), response)

    with pytest.raises(NegotiationVerificationError) as exc_info:
        result.raise_for_errors()

    assert exc_info.value.issues[0].code == "cardinality"
