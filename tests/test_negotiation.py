from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, ConfigDict

from adcp.client import ADCPClient
from adcp.negotiation import (
    WIRE_RESPONSE_METADATA_KEY,
    NegotiationVerificationError,
    VerifiedRefinementResult,
    compute_terms_digest,
    preflight_refine_proposals,
    refine_proposals_verified,
    unsupported_refinement_recovery,
    verify_refine_proposals_response,
    verify_terms_digest,
)
from adcp.types import GeneratedTaskStatus, RefineProposalsRequest
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus


class _ParsedTerms(BaseModel):
    start_time: datetime


class _ParsedResponse(BaseModel):
    status: str
    results: list[dict]
    products: list[dict]


class _ParsedCommercialTerms(BaseModel):
    model_config = ConfigDict(extra="allow")

    start_time: datetime


class _ParsedSourceProposal(BaseModel):
    proposal_id: str
    proposal_status: str
    commercial_terms: _ParsedCommercialTerms
    terms_digest: str


class _TypedRequest(BaseModel):
    value: str


class _PollResponse(BaseModel):
    status: str
    result: dict | None = None


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
        "proposal_kind": "new_media_buy",
        "proposal_status": "draft",
        "name": "September plan",
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


@pytest.mark.parametrize(
    "request_case",
    [
        {"idempotency_key": "too-short", "refinements": []},
        {
            "idempotency_key": "buyer-preflight-0001",
            "refinements": [
                {"proposal_id": str(index), "action": "revise", "ask": "revise"}
                for index in range(26)
            ],
        },
        {
            "idempotency_key": "buyer-preflight-0002",
            "refinements": [{"proposal_id": "source-1", "action": "revise"}],
        },
        {
            "idempotency_key": "buyer-preflight-0003",
            "refinements": [{"proposal_id": "source-1", "ask": "Please revise."}],
        },
    ],
)
def test_buyer_preflight_enforces_request_schema(request_case: dict) -> None:
    result = preflight_refine_proposals(request_case, None)

    assert not result.valid
    assert {issue.code for issue in result.issues} == {"invalid_request_schema"}


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
    second = {"proposal_id": request["refinements"][0]["proposal_id"], "action": "finalize"}
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


def test_verified_submitted_result_is_pending_not_valid() -> None:
    result = VerifiedRefinementResult(
        TaskResult(status=TaskStatus.SUBMITTED, data={"task_id": "task-1"})
    )

    assert result.pending
    assert not result.valid


def test_finalize_requires_unchanged_live_source_terms() -> None:
    request = _request()
    request["refinements"][0] = {"proposal_id": "source-1", "action": "finalize"}
    finalized = _proposal()
    finalized["proposal_status"] = "committed"
    finalized["expires_at"] = "2099-01-01T00:00:00Z"
    response = {
        "status": "completed",
        "results": [
            {
                "source_proposal_id": "source-1",
                "outcome": "finalized",
                "proposal": finalized,
            }
        ],
        "products": [],
    }
    source = {"source-1": _proposal(proposal_id="source-1")}

    assert verify_refine_proposals_response(
        request,
        response,
        source_proposals=source,
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
    ).valid

    changed = deepcopy(response)
    changed_terms = deepcopy(_terms())
    changed_terms["total_budget"]["amount"] = 120
    changed["results"][0]["proposal"]["commercial_terms"] = changed_terms
    changed["results"][0]["proposal"]["terms_digest"] = compute_terms_digest(changed_terms)
    codes = {
        issue.code
        for issue in verify_refine_proposals_response(
            request,
            changed,
            source_proposals=source,
            now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        ).issues
    }
    assert "finalize_terms_changed" in codes

    expired = deepcopy(response)
    expired["results"][0]["proposal"]["expires_at"] = "2020-01-01T00:00:00Z"
    codes = {
        issue.code
        for issue in verify_refine_proposals_response(
            request,
            expired,
            source_proposals=source,
            now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        ).issues
    }
    assert "proposal_expired" in codes


def test_finalize_recomputes_source_digest_and_requires_draft() -> None:
    request = _request()
    request["refinements"][0] = {"proposal_id": "source-1", "action": "finalize"}
    finalized = _proposal(terms={**_terms(), "total_budget": {"amount": 200, "currency": "USD"}})
    finalized["proposal_status"] = "committed"
    finalized["expires_at"] = "2099-01-01T00:00:00Z"
    response = {
        "status": "completed",
        "results": [
            {"source_proposal_id": "source-1", "outcome": "finalized", "proposal": finalized}
        ],
        "products": [],
    }
    source = _proposal(proposal_id="source-1")
    source["terms_digest"] = finalized["terms_digest"]
    source["proposal_status"] = "committed"

    codes = {
        issue.code
        for issue in verify_refine_proposals_response(
            request,
            response,
            source_proposals={"source-1": source},
            now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        ).issues
    }

    assert {"source_terms_digest", "finalize_terms_changed", "source_proposal_state"} <= codes

    wrong_identity = deepcopy(source)
    wrong_identity["proposal_id"] = "wrong-source"
    identity_codes = {
        issue.code
        for issue in verify_refine_proposals_response(
            request,
            response,
            source_proposals={"source-1": wrong_identity},
            now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        ).issues
    }
    assert "source_proposal_identity" in identity_codes


def test_finalize_response_requires_committed_successor() -> None:
    request = _request()
    request["refinements"][0] = {"proposal_id": "source-1", "action": "finalize"}
    invalid = _proposal()
    invalid["expires_at"] = "2099-01-01T00:00:00Z"
    response = {
        "status": "completed",
        "results": [
            {"source_proposal_id": "source-1", "outcome": "finalized", "proposal": invalid}
        ],
        "products": [],
    }

    result = verify_refine_proposals_response(
        request,
        response,
        source_proposals={"source-1": _proposal(proposal_id="source-1")},
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    assert "invalid_response_schema" in {issue.code for issue in result.issues}


def test_finalize_accepts_parsed_source_with_retained_wire_digest() -> None:
    request = _request()
    request["refinements"][0] = {"proposal_id": "source-1", "action": "finalize"}
    wire_terms = deepcopy(_terms())
    wire_terms["start_time"] = "2026-09-01T00:00:00+00:00"
    source = _ParsedSourceProposal.model_validate(
        {
            "proposal_id": "source-1",
            "proposal_status": "draft",
            "commercial_terms": wire_terms,
            "terms_digest": compute_terms_digest(wire_terms),
        }
    )
    finalized = _proposal(terms=wire_terms)
    finalized["proposal_status"] = "committed"
    finalized["expires_at"] = "2099-01-01T00:00:00Z"
    response = {
        "status": "completed",
        "results": [
            {"source_proposal_id": "source-1", "outcome": "finalized", "proposal": finalized}
        ],
        "products": [],
    }

    result = verify_refine_proposals_response(
        request,
        response,
        source_proposals={"source-1": source},
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    assert result.valid, result.issues


def test_refinement_requires_fresh_globally_unique_successor_ids() -> None:
    request = _request()
    request["refinements"].append(
        {"proposal_id": "source-2", "action": "revise", "ask": "Please revise."}
    )
    first = _proposal(proposal_id="source-1")
    second = _proposal(proposal_id="source-1")
    second["parent_proposal_id"] = "source-2"
    response = {
        "status": "completed",
        "results": [
            {"source_proposal_id": "source-1", "outcome": "revised", "proposals": [first]},
            {"source_proposal_id": "source-2", "outcome": "revised", "proposals": [second]},
        ],
        "products": [],
    }

    codes = {issue.code for issue in verify_refine_proposals_response(request, response).issues}

    assert "successor_proposal_id" in codes
    assert "duplicate_successor_proposal_id" in codes


@pytest.mark.parametrize(
    ("reason_code", "request_update", "capability"),
    [
        ("commercially_declined", {}, None),
        ("uninterpreted", {}, None),
        (
            "unsupported_dimension",
            {"constraints": {"cpm": {"max": 5}}},
            {"supported_dimensions": ["cpm"]},
        ),
        ("alternatives_unavailable", {}, None),
        ("hold_unavailable", {}, None),
        ("batch_aborted", {"action": "finalize"}, None),
    ],
)
def test_reason_codes_are_bound_to_request_semantics(
    reason_code: str,
    request_update: dict,
    capability: dict | None,
) -> None:
    request = {
        "idempotency_key": "reason-test-0001",
        "refinements": [{"proposal_id": "source-1", "action": "revise", **request_update}],
    }
    response = {
        "status": "completed",
        "results": [
            {
                "source_proposal_id": "source-1",
                "outcome": "unable",
                "reason_code": reason_code,
                "reason": "unable",
            }
        ],
        "products": [],
    }

    result = verify_refine_proposals_response(request, response, proposal_refinement=capability)

    assert "reason_mismatch" in {issue.code for issue in result.issues}


@pytest.mark.parametrize(
    ("reason_code", "refinement", "result_update", "capability"),
    [
        ("commercially_declined", {"ask": "lower the price"}, {}, None),
        ("uninterpreted", {"ask": "make it pop"}, {}, None),
        (
            "constraint_unsatisfiable",
            {"constraints": {"total_budget": {"max": 50, "currency": "USD"}}},
            {"unsatisfied_constraints": ["total_budget"]},
            None,
        ),
        (
            "unsupported_dimension",
            {"constraints": {"cpm": {"max": 5, "currency": "USD"}}},
            {},
            {"supported_dimensions": []},
        ),
        ("source_unavailable", {}, {}, None),
        ("hold_unavailable", {"action": "finalize"}, {}, None),
    ],
)
def test_unable_reason_codes_accept_their_normative_context(
    reason_code: str,
    refinement: dict,
    result_update: dict,
    capability: dict | None,
) -> None:
    request_entry = {"proposal_id": "source-1", "action": "revise", **refinement}
    response_result = {
        "source_proposal_id": "source-1",
        "outcome": "unable",
        "reason_code": reason_code,
        "reason": "unable",
        **result_update,
    }

    verification = verify_refine_proposals_response(
        {"idempotency_key": "reason-valid-001", "refinements": [request_entry]},
        {"status": "completed", "results": [response_result], "products": []},
        proposal_refinement=capability,
    )

    assert verification.valid, verification.issues


def test_alternatives_unavailable_and_batch_aborted_have_valid_contexts() -> None:
    alternatives_request = {
        "idempotency_key": "reason-valid-002",
        "refinements": [
            {
                "proposal_id": "source-1",
                "action": "revise",
                "alternatives": {"count": 2},
            }
        ],
    }
    alternatives_response = _response()
    alternatives_response["results"][0].update(
        outcome="partial",
        reason_code="alternatives_unavailable",
        reason="Only one distinct alternative was available.",
    )
    assert verify_refine_proposals_response(alternatives_request, alternatives_response).valid

    finalize_request = {
        "idempotency_key": "reason-valid-003",
        "refinements": [
            {"proposal_id": "source-1", "action": "finalize"},
            {"proposal_id": "source-2", "action": "finalize"},
        ],
    }
    finalize_response = {
        "status": "completed",
        "results": [
            {
                "source_proposal_id": "source-1",
                "outcome": "unable",
                "reason_code": "hold_unavailable",
                "reason": "Inventory sold through.",
            },
            {
                "source_proposal_id": "source-2",
                "outcome": "unable",
                "reason_code": "batch_aborted",
                "reason": "A sibling failed.",
            },
        ],
        "products": [],
    }
    assert verify_refine_proposals_response(finalize_request, finalize_response).valid


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


@pytest.mark.parametrize(
    "extra",
    [{}, {"unsatisfied_constraints": []}, {"unsatisfied_product_changes": {}}],
)
def test_constraint_reason_requires_named_unsatisfied_requirement(extra: dict) -> None:
    response = _response()
    response["results"][0] = {
        "source_proposal_id": "source-1",
        "outcome": "unable",
        "reason_code": "constraint_unsatisfiable",
        "reason": "No satisfying proposal",
        **extra,
    }

    result = verify_refine_proposals_response(_request(), response)

    assert "constraint_precedence" in {issue.code for issue in result.issues}


def test_raise_for_errors_preserves_structured_issues() -> None:
    response = deepcopy(_response())
    response["results"] = []
    result = verify_refine_proposals_response(_request(), response)

    with pytest.raises(NegotiationVerificationError) as exc_info:
        result.raise_for_errors()

    assert "cardinality" in {issue.code for issue in exc_info.value.issues}


@pytest.mark.asyncio
async def test_verified_refinement_preflights_calls_and_verifies_wire_response() -> None:
    class Client:
        called = False

        async def refine_proposals(self, _request):
            self.called = True
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=_response(),
                metadata={WIRE_RESPONSE_METADATA_KEY: _response()},
            )

    client = Client()
    result = await refine_proposals_verified(
        client,
        _request(),
        {
            "supported_dimensions": [
                "total_budget",
                "cpm",
                "impressions",
                "flight",
                "product_changes",
            ]
        },
    )

    assert client.called
    assert result.valid
    assert result.verification is not None and result.verification.valid


@pytest.mark.asyncio
async def test_verified_refinement_stops_unsupported_request_before_transport() -> None:
    class Client:
        called = False

        async def refine_proposals(self, _request):
            self.called = True
            raise AssertionError("transport must not run")

    client = Client()
    with pytest.raises(NegotiationVerificationError, match="product_changes"):
        await refine_proposals_verified(
            client,
            _request(),
            {"supported_dimensions": ["total_budget"]},
        )
    assert not client.called


@pytest.mark.asyncio
async def test_verified_refinement_preserves_typed_task_error_recovery() -> None:
    class Client:
        async def refine_proposals(self, _request):
            return TaskResult(
                status=TaskStatus.FAILED,
                success=False,
                adcp_error={
                    "code": "UNSUPPORTED_FEATURE",
                    "message": "unsupported",
                    "details": {
                        "unsupported_dimension": "flight",
                        "supported_dimensions": ["total_budget", "cpm"],
                    },
                },
            )

    result = await refine_proposals_verified(Client(), _request())

    assert not result.valid
    assert result.verification is None
    assert result.unsupported_dimension is not None
    assert result.unsupported_dimension.unsupported_dimension == "flight"
    assert result.unsupported_dimension.supported_dimensions == ("total_budget", "cpm")


@pytest.mark.asyncio
async def test_verified_refinement_rejects_schema_invalid_generic_client_response() -> None:
    class Client:
        async def refine_proposals(self, _request):
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data={
                    "status": "completed",
                    "results": [{"source_proposal_id": "source-1", "outcome": "unable"}],
                    "products": [],
                },
            )

    with pytest.raises(NegotiationVerificationError) as exc_info:
        await refine_proposals_verified(Client(), _request())

    assert {issue.code for issue in exc_info.value.issues} == {"invalid_response_schema"}


def test_unsupported_refinement_recovery_rejects_noncanonical_details() -> None:
    assert (
        unsupported_refinement_recovery(
            {
                "code": "UNSUPPORTED_FEATURE",
                "details": {"unsupported_dimension": "flight"},
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_adcp_client_exposes_verified_refinement_workflow(monkeypatch) -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example", protocol=Protocol.A2A)
    )
    raw = TaskResult(status=TaskStatus.COMPLETED, data=_response())
    parsed = TaskResult(
        status=TaskStatus.COMPLETED, data=_ParsedResponse.model_validate(_response())
    )

    async def transport(_params):
        return raw

    monkeypatch.setattr(client.adapter, "refine_proposals", transport)
    monkeypatch.setattr(client.adapter, "_parse_response", lambda *_args: parsed)

    result = await client.refine_proposals_verified(
        RefineProposalsRequest.model_validate(_request())
    )

    assert result.valid
    assert result.verification is not None and result.verification.valid


@pytest.mark.asyncio
async def test_client_polls_and_verifies_submitted_refinement(monkeypatch) -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example", protocol=Protocol.A2A)
    )
    initial = VerifiedRefinementResult(
        TaskResult(
            status=TaskStatus.SUBMITTED,
            data={"task_id": "task-1"},
            metadata={WIRE_RESPONSE_METADATA_KEY: {"status": "submitted", "task_id": "task-1"}},
        )
    )

    async def get_task_status(_request):
        return TaskResult(
            status=TaskStatus.COMPLETED,
            data=_PollResponse(status="completed", result=_response()),
        )

    monkeypatch.setattr(client, "get_task_status", get_task_status)

    result = await client.wait_for_refinement_verified(
        RefineProposalsRequest.model_validate(_request()),
        initial,
        poll_interval=0.001,
    )

    assert result.valid
    assert result.task_result.metadata[WIRE_RESPONSE_METADATA_KEY] == _response()


def test_webhook_parser_retains_refinement_wire_payload() -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example", protocol=Protocol.A2A)
    )
    wire = _response()

    result = client._parse_webhook_result(
        task_id="task-1",
        task_type="refine_proposals",
        operation_id="op-1",
        status=GeneratedTaskStatus.completed,
        result=wire,
        timestamp="2026-08-24T00:00:00Z",
        message=None,
        context_id=None,
    )

    assert result.metadata[WIRE_RESPONSE_METADATA_KEY] is wire
