"""Buyer-side verification helpers for AdCP proposal negotiation.

The generated models validate individual request and response shapes.  This
module verifies the cross-object invariants that JSON Schema cannot express:
ordered result correlation, immutable lineage, RFC 8785 terms digests,
alternative uniqueness, and the portable typed refinement constraints.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import rfc8785
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from adcp.types.core import TaskResult, TaskStatus

WIRE_RESPONSE_METADATA_KEY = "adcp_negotiation_wire_response"
"""TaskResult metadata key containing the unnormalized refinement response."""


@dataclass(frozen=True, slots=True)
class NegotiationVerificationIssue:
    """One failed proposal-negotiation invariant."""

    code: str
    message: str
    pointer: str


@dataclass(frozen=True, slots=True)
class NegotiationVerificationResult:
    """Aggregate result returned by :func:`verify_refine_proposals_response`."""

    issues: tuple[NegotiationVerificationIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues

    def raise_for_errors(self) -> None:
        """Raise :class:`NegotiationVerificationError` when verification failed."""

        if self.issues:
            raise NegotiationVerificationError(self.issues)


class NegotiationVerificationError(ValueError):
    """Raised when a refine-proposals response violates negotiation invariants."""

    def __init__(self, issues: Sequence[NegotiationVerificationIssue]) -> None:
        self.issues = tuple(issues)
        summary = "; ".join(f"{issue.pointer}: {issue.message}" for issue in self.issues)
        super().__init__(summary)


def _wire_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return to_jsonable_python(value, by_alias=True, exclude_none=True)


def compute_terms_digest(commercial_terms: Mapping[str, Any]) -> str:
    """Return the normative digest for a ``commercial_terms`` object.

    The digest is ``sha256:`` followed by unpadded base64url SHA-256 of the
    RFC 8785 JSON Canonicalization Scheme bytes. The input must be the original
    JSON object, not a parsed Pydantic model: model parsing can normalize date
    strings or inject defaults and therefore change the signed wire value.
    """

    if not isinstance(commercial_terms, Mapping):
        raise TypeError("commercial_terms must be the original wire mapping")
    wire_terms = dict(commercial_terms)
    canonical = rfc8785.dumps(wire_terms)
    encoded = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=")
    return "sha256:" + encoded.decode("ascii")


def verify_terms_digest(proposal: Mapping[str, Any]) -> bool:
    """Return whether a proposal's ``terms_digest`` matches its terms."""

    if not isinstance(proposal, Mapping):
        raise TypeError("proposal must be the original wire mapping")
    wire_proposal = proposal
    terms = wire_proposal.get("commercial_terms")
    claimed = wire_proposal.get("terms_digest")
    if not isinstance(terms, dict) or not isinstance(claimed, str):
        return False
    return hmac.compare_digest(claimed, compute_terms_digest(terms))


def preflight_refine_proposals(
    request: BaseModel | Mapping[str, Any],
    proposal_refinement: BaseModel | Mapping[str, Any] | None,
) -> NegotiationVerificationResult:
    """Check a refine request against an advertised capability declaration.

    A missing declaration means support is unknown and does not fail
    preflight.  An explicit ``supported_dimensions`` list is authoritative;
    omitted typed dimensions are reported before transport.  Free-text
    ``ask`` is intentionally not capability-gated.
    """

    request_data = _as_dict(request)
    if request_data is None:
        raise TypeError("request must serialize to a JSON object")
    refinements = _as_list(request_data.get("refinements"))
    if refinements is None:
        return NegotiationVerificationResult(
            (
                NegotiationVerificationIssue(
                    "invalid_request_shape",
                    "request refinements are not an ordered array",
                    "/refinements",
                ),
            )
        )

    issues: list[NegotiationVerificationIssue] = []
    proposal_ids: set[str] = set()
    actions: set[str] = set()
    capability = _as_dict(proposal_refinement) if proposal_refinement is not None else None
    supported = capability.get("supported_dimensions") if capability is not None else None
    supported_dimensions = set(supported) if isinstance(supported, list) else None
    max_alternatives = capability.get("max_alternatives") if capability is not None else None

    for index, refinement in enumerate(refinements):
        pointer = f"/refinements/{index}"
        if not isinstance(refinement, dict):
            issues.append(
                NegotiationVerificationIssue(
                    "invalid_request_shape", "refinement must be an object", pointer
                )
            )
            continue

        proposal_id = refinement.get("proposal_id")
        if isinstance(proposal_id, str):
            if proposal_id in proposal_ids:
                issues.append(
                    NegotiationVerificationIssue(
                        "duplicate_proposal_id",
                        "proposal_id values must be unique within the batch",
                        f"{pointer}/proposal_id",
                    )
                )
            proposal_ids.add(proposal_id)

        action = refinement.get("action")
        if isinstance(action, str):
            actions.add(action)

        requested_dimensions: list[tuple[str, str]] = []
        constraints = refinement.get("constraints")
        if isinstance(constraints, dict):
            requested_dimensions.extend(
                (dimension, f"{pointer}/constraints/{dimension}")
                for dimension, value in constraints.items()
                if value is not None
            )
        for dimension in ("product_changes", "alternatives", "criteria"):
            if refinement.get(dimension) is not None:
                requested_dimensions.append((dimension, f"{pointer}/{dimension}"))

        if supported_dimensions is not None:
            for dimension, dimension_pointer in requested_dimensions:
                if dimension not in supported_dimensions:
                    issues.append(
                        NegotiationVerificationIssue(
                            "unsupported_dimension",
                            f"seller does not advertise refinement dimension {dimension!r}",
                            dimension_pointer,
                        )
                    )

        alternatives = refinement.get("alternatives")
        count = alternatives.get("count") if isinstance(alternatives, dict) else None
        if (
            isinstance(count, int)
            and isinstance(max_alternatives, int)
            and count > max_alternatives
        ):
            issues.append(
                NegotiationVerificationIssue(
                    "max_alternatives",
                    f"requested {count} alternatives; seller maximum is {max_alternatives}",
                    f"{pointer}/alternatives/count",
                )
            )

    if "finalize" in actions and actions != {"finalize"}:
        issues.append(
            NegotiationVerificationIssue(
                "mixed_finalize_batch",
                "a batch containing finalize must contain only finalize entries",
                "/refinements",
            )
        )

    return NegotiationVerificationResult(tuple(issues))


def _as_dict(value: Any) -> dict[str, Any] | None:
    wire = _wire_value(value)
    return wire if isinstance(wire, dict) else None


def _as_list(value: Any) -> list[Any] | None:
    wire = _wire_value(value)
    return wire if isinstance(wire, list) else None


def _unwrap_protocol_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return task data from an optional protocol ``data``/``payload`` wrapper."""

    if "results" in value:
        return value
    for key in ("data", "payload"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return nested
    return value


def _response_data(
    response: TaskResult[Any] | BaseModel | Mapping[str, Any],
) -> tuple[dict[str, Any] | None, TaskStatus | str | None, bool]:
    """Resolve response data, status, and whether values retain wire lexemes."""

    if isinstance(response, TaskResult):
        status: TaskStatus | str | None = response.status
        raw = (response.metadata or {}).get(WIRE_RESPONSE_METADATA_KEY)
        if isinstance(raw, Mapping):
            return dict(_unwrap_protocol_payload(raw)), status, True
        if isinstance(response.data, Mapping):
            return dict(_unwrap_protocol_payload(response.data)), status, True
        if isinstance(response.data, BaseModel):
            return _as_dict(response.data), status, False
        return None, status, False

    if isinstance(response, Mapping):
        data = _unwrap_protocol_payload(response)
        return dict(data), data.get("status"), True
    if isinstance(response, BaseModel):
        model_data = _as_dict(response)
        model_status = model_data.get("status") if model_data is not None else None
        return model_data, model_status, False


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or value == "asap":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _failed_constraints(constraints: Mapping[str, Any], terms: Mapping[str, Any]) -> set[str]:
    failed: set[str] = set()
    purchases = terms.get("purchases")
    purchase_rows = purchases if isinstance(purchases, list) else []

    total_budget = constraints.get("total_budget")
    if isinstance(total_budget, dict):
        actual = terms.get("total_budget")
        if not isinstance(actual, dict) or actual.get("currency") != total_budget.get("currency"):
            failed.add("total_budget")
        else:
            amount = actual.get("amount")
            minimum = total_budget.get("min")
            maximum = total_budget.get("max")
            if not isinstance(amount, int | float):
                failed.add("total_budget")
            elif isinstance(minimum, int | float) and amount < minimum:
                failed.add("total_budget")
            elif isinstance(maximum, int | float) and amount > maximum:
                failed.add("total_budget")

    cpm = constraints.get("cpm")
    if isinstance(cpm, dict):
        ceiling = cpm.get("max")
        currency = cpm.get("currency")
        if not purchase_rows:
            failed.add("cpm")
        for purchase in purchase_rows:
            pricing = purchase.get("pricing") if isinstance(purchase, dict) else None
            if (
                not isinstance(pricing, dict)
                or pricing.get("pricing_model") not in {"cpm", "vcpm"}
                or pricing.get("currency") != currency
                or not isinstance(pricing.get("fixed_price"), int | float)
                or not isinstance(ceiling, int | float)
                or pricing["fixed_price"] > ceiling
            ):
                failed.add("cpm")
                break

    impressions = constraints.get("impressions")
    if isinstance(impressions, dict):
        values = [
            purchase.get("impressions") if isinstance(purchase, dict) else None
            for purchase in purchase_rows
        ]
        minimum = impressions.get("min")
        if (
            not values
            or not isinstance(minimum, int | float)
            or any(not isinstance(value, int | float) for value in values)
            or sum(value for value in values if isinstance(value, int | float)) < minimum
        ):
            failed.add("impressions")

    flight = constraints.get("flight")
    if isinstance(flight, dict):
        start_bound = _parse_datetime(flight.get("start_no_later_than"))
        end_bound = _parse_datetime(flight.get("end_no_earlier_than"))
        start = _parse_datetime(terms.get("start_time"))
        end = _parse_datetime(terms.get("end_time"))
        if start_bound is not None and (start is None or start > start_bound):
            failed.add("flight")
        if end_bound is not None and (end is None or end < end_bound):
            failed.add("flight")

    return failed


def _failed_product_changes(changes: Mapping[str, Any], terms: Mapping[str, Any]) -> set[str]:
    purchases = terms.get("purchases")
    purchase_rows = purchases if isinstance(purchases, list) else []
    product_ids = {
        purchase.get("product_id") for purchase in purchase_rows if isinstance(purchase, dict)
    }
    return {
        product_id
        for product_id, action in changes.items()
        if (action == "include" and product_id not in product_ids)
        or (action == "omit" and product_id in product_ids)
    }


def verify_refine_proposals_response(
    request: BaseModel | Mapping[str, Any],
    response: TaskResult[Any] | BaseModel | Mapping[str, Any],
) -> NegotiationVerificationResult:
    """Verify cross-object invariants for a completed refine response.

    Submitted/working responses without ``results`` are valid but have no
    proposal invariants to inspect.  The helper never mutates either model.
    """

    request_data = _as_dict(request)
    response_data, response_status, wire_faithful = _response_data(response)
    if request_data is None:
        raise TypeError("request must serialize to a JSON object")
    if response_data is None:
        if response_status in {TaskStatus.SUBMITTED, TaskStatus.WORKING, "submitted", "working"}:
            return NegotiationVerificationResult()
        if isinstance(response, TaskResult):
            return NegotiationVerificationResult(
                (
                    NegotiationVerificationIssue(
                        "missing_results",
                        "a completed refinement response must contain ordered results",
                        "/results",
                    ),
                )
            )
        raise TypeError("response must serialize to a JSON object")

    refinements = _as_list(request_data.get("refinements"))
    results = _as_list(response_data.get("results"))
    if results is None:
        if response_status in {TaskStatus.SUBMITTED, TaskStatus.WORKING, "submitted", "working"}:
            return NegotiationVerificationResult()
        return NegotiationVerificationResult(
            (
                NegotiationVerificationIssue(
                    "missing_results",
                    "a completed refinement response must contain ordered results",
                    "/results",
                ),
            )
        )
    if refinements is None:
        return NegotiationVerificationResult(
            (
                NegotiationVerificationIssue(
                    "invalid_request_shape",
                    "request refinements are not an ordered array",
                    "/refinements",
                ),
            )
        )

    issues: list[NegotiationVerificationIssue] = []
    if not wire_faithful:
        issues.append(
            NegotiationVerificationIssue(
                "wire_data_required",
                "digest verification requires the original response mapping; pass the "
                "TaskResult returned by ADCPClient or an unparsed wire response",
                "/results",
            )
        )
    if len(results) != len(refinements):
        issues.append(
            NegotiationVerificationIssue(
                "cardinality",
                f"expected {len(refinements)} ordered results, got {len(results)}",
                "/results",
            )
        )

    outcomes = {result.get("outcome") for result in results if isinstance(result, dict)}
    if "finalized" in outcomes and outcomes != {"finalized"}:
        issues.append(
            NegotiationVerificationIssue(
                "mixed_finalize_batch",
                "a finalize batch must contain only finalized results",
                "/results",
            )
        )

    for index, (refinement, result) in enumerate(zip(refinements, results)):
        pointer = f"/results/{index}"
        if not isinstance(refinement, dict) or not isinstance(result, dict):
            issues.append(
                NegotiationVerificationIssue(
                    "invalid_result_shape", "refinement and result must be objects", pointer
                )
            )
            continue

        source_id = refinement.get("proposal_id")
        if result.get("source_proposal_id") != source_id:
            issues.append(
                NegotiationVerificationIssue(
                    "result_order",
                    "source_proposal_id does not match the request entry at this position",
                    f"{pointer}/source_proposal_id",
                )
            )

        action = refinement.get("action")
        outcome = result.get("outcome")
        allowed_outcomes = {
            "revise": {"revised", "partial", "unable"},
            "finalize": {"finalized", "unable"},
        }
        if action in allowed_outcomes and outcome not in allowed_outcomes[action]:
            issues.append(
                NegotiationVerificationIssue(
                    "action_outcome_mismatch",
                    f"action {action!r} cannot produce outcome {outcome!r}",
                    f"{pointer}/outcome",
                )
            )

        proposals: list[dict[str, Any]] = []
        plural = result.get("proposals")
        if isinstance(plural, list):
            proposals.extend(item for item in plural if isinstance(item, dict))
        else:
            singular = result.get("proposal")
            if isinstance(singular, dict):
                proposals.append(singular)

        computed_digests: list[str] = []
        failed_constraint_union: set[str] = set()
        failed_change_union: set[str] = set()
        for proposal_index, proposal in enumerate(proposals):
            proposal_pointer = f"{pointer}/proposals/{proposal_index}"
            if proposal.get("parent_proposal_id") != result.get("source_proposal_id"):
                issues.append(
                    NegotiationVerificationIssue(
                        "lineage",
                        "parent_proposal_id must equal source_proposal_id",
                        f"{proposal_pointer}/parent_proposal_id",
                    )
                )
            terms = proposal.get("commercial_terms")
            claimed_digest = proposal.get("terms_digest")
            if not isinstance(terms, dict) or not isinstance(claimed_digest, str):
                issues.append(
                    NegotiationVerificationIssue(
                        "terms_digest",
                        "proposal must carry commercial_terms and terms_digest",
                        proposal_pointer,
                    )
                )
                continue
            if wire_faithful:
                computed = compute_terms_digest(terms)
                computed_digests.append(computed)
                if not hmac.compare_digest(claimed_digest, computed):
                    issues.append(
                        NegotiationVerificationIssue(
                            "terms_digest",
                            "terms_digest does not match RFC 8785 commercial_terms digest",
                            f"{proposal_pointer}/terms_digest",
                        )
                    )

            constraints = refinement.get("constraints")
            failed_constraints = _failed_constraints(
                constraints if isinstance(constraints, dict) else {}, terms
            )
            failed_constraint_union.update(failed_constraints)
            declared_unsatisfied = {
                str(value) for value in result.get("unsatisfied_constraints", [])
            }
            undeclared = failed_constraints - declared_unsatisfied
            if undeclared:
                issues.append(
                    NegotiationVerificationIssue(
                        "constraint_mismatch",
                        f"proposal fails undeclared constraints: {sorted(undeclared)!r}",
                        proposal_pointer,
                    )
                )

            changes = refinement.get("product_changes", {})
            if isinstance(changes, dict):
                failed_changes = _failed_product_changes(changes, terms)
                failed_change_union.update(failed_changes)
                declared_changes = result.get("unsatisfied_product_changes", {})
                declared_ids = (
                    set(declared_changes) if isinstance(declared_changes, dict) else set()
                )
                undeclared_changes = failed_changes - declared_ids
                if undeclared_changes:
                    issues.append(
                        NegotiationVerificationIssue(
                            "product_change_mismatch",
                            "proposal fails undeclared product changes: "
                            f"{sorted(undeclared_changes)!r}",
                            proposal_pointer,
                        )
                    )

        declared_unsatisfied = {str(value) for value in result.get("unsatisfied_constraints", [])}
        requested_constraints = refinement.get("constraints", {})
        invalid_declared_constraints = {
            key
            for key in declared_unsatisfied
            if not isinstance(requested_constraints, dict) or key not in requested_constraints
        }
        if invalid_declared_constraints:
            issues.append(
                NegotiationVerificationIssue(
                    "constraint_mismatch",
                    "unsatisfied_constraints must be a subset of requested constraint keys: "
                    f"{sorted(invalid_declared_constraints)!r}",
                    f"{pointer}/unsatisfied_constraints",
                )
            )
        if proposals and declared_unsatisfied - failed_constraint_union:
            issues.append(
                NegotiationVerificationIssue(
                    "constraint_mismatch",
                    "unsatisfied_constraints includes constraints satisfied by every proposal: "
                    f"{sorted(declared_unsatisfied - failed_constraint_union)!r}",
                    f"{pointer}/unsatisfied_constraints",
                )
            )
        declared_changes = result.get("unsatisfied_product_changes", {})
        requested_changes = refinement.get("product_changes", {})
        declared_change_ids = set(declared_changes) if isinstance(declared_changes, dict) else set()
        if isinstance(declared_changes, dict):
            invalid_declared_changes = {
                product_id: action
                for product_id, action in declared_changes.items()
                if not isinstance(requested_changes, dict)
                or requested_changes.get(product_id) != action
            }
            if invalid_declared_changes:
                issues.append(
                    NegotiationVerificationIssue(
                        "product_change_mismatch",
                        "unsatisfied_product_changes must be an exact subset of the "
                        "requested product_changes map",
                        f"{pointer}/unsatisfied_product_changes",
                    )
                )
        if proposals and declared_change_ids - failed_change_union:
            issues.append(
                NegotiationVerificationIssue(
                    "product_change_mismatch",
                    "unsatisfied_product_changes includes changes satisfied by every proposal: "
                    f"{sorted(declared_change_ids - failed_change_union)!r}",
                    f"{pointer}/unsatisfied_product_changes",
                )
            )

        if len(computed_digests) != len(set(computed_digests)):
            issues.append(
                NegotiationVerificationIssue(
                    "duplicate_alternative",
                    "returned alternatives must have distinct commercial_terms",
                    f"{pointer}/proposals",
                )
            )

        alternatives = refinement.get("alternatives")
        requested_count = alternatives.get("count") if isinstance(alternatives, dict) else None
        if isinstance(requested_count, int) and result.get("outcome") == "revised":
            if len(proposals) != requested_count:
                issues.append(
                    NegotiationVerificationIssue(
                        "alternative_count",
                        f"revised result must contain {requested_count} alternatives",
                        f"{pointer}/proposals",
                    )
                )
        elif isinstance(requested_count, int) and len(proposals) > requested_count:
            issues.append(
                NegotiationVerificationIssue(
                    "alternative_count",
                    f"result exceeds requested alternative count {requested_count}",
                    f"{pointer}/proposals",
                )
            )
        elif requested_count is None and result.get("outcome") in {"revised", "partial"}:
            if len(proposals) != 1:
                issues.append(
                    NegotiationVerificationIssue(
                        "alternative_count",
                        "a revision without alternatives must return exactly one proposal",
                        f"{pointer}/proposals",
                    )
                )

        unsatisfied = result.get("unsatisfied_constraints")
        unsatisfied_changes = result.get("unsatisfied_product_changes")
        if (unsatisfied or unsatisfied_changes) and result.get("outcome") not in {
            "partial",
            "unable",
        }:
            issues.append(
                NegotiationVerificationIssue(
                    "unsatisfied_outcome",
                    "unsatisfied constraints or product changes require partial or unable",
                    f"{pointer}/outcome",
                )
            )
        if (unsatisfied or unsatisfied_changes) and result.get(
            "reason_code"
        ) != "constraint_unsatisfiable":
            issues.append(
                NegotiationVerificationIssue(
                    "constraint_precedence",
                    "unsatisfied typed constraints require reason_code constraint_unsatisfiable",
                    f"{pointer}/reason_code",
                )
            )

    return NegotiationVerificationResult(tuple(issues))


__all__ = [
    "NegotiationVerificationError",
    "NegotiationVerificationIssue",
    "NegotiationVerificationResult",
    "WIRE_RESPONSE_METADATA_KEY",
    "compute_terms_digest",
    "preflight_refine_proposals",
    "verify_refine_proposals_response",
    "verify_terms_digest",
]
