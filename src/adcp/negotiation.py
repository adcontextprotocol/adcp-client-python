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
from datetime import datetime, timezone
from typing import Any, Protocol

import rfc8785
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from adcp.types import (
    RefineProposalsRequest,
    RefineProposalsResponse,
    UnsupportedRefinementDimensionDetails,
)
from adcp.types.core import TaskResult, TaskStatus

WIRE_RESPONSE_METADATA_KEY = "adcp_negotiation_wire_response"
"""TaskResult metadata key containing the unnormalized refinement response."""

_REFINE_REQUEST_ADAPTER: TypeAdapter[Any] = TypeAdapter(RefineProposalsRequest)
_REFINE_RESPONSE_ADAPTER: TypeAdapter[Any] = TypeAdapter(RefineProposalsResponse)


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


@dataclass(frozen=True, slots=True)
class UnsupportedRefinementRecovery:
    """Typed recovery data from a task-level unsupported-dimension error."""

    unsupported_dimension: str
    supported_dimensions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedRefinementResult:
    """A transport result paired with completed-response verification."""

    task_result: TaskResult[Any]
    verification: NegotiationVerificationResult | None = None
    unsupported_dimension: UnsupportedRefinementRecovery | None = None

    @property
    def valid(self) -> bool:
        """Return whether a completed transport result passed verification."""

        return bool(
            self.task_result.success
            and self.task_result.status == TaskStatus.COMPLETED
            and self.verification is not None
            and self.verification.valid
        )

    @property
    def pending(self) -> bool:
        """Return whether the seller accepted the request for async completion."""

        return self.task_result.status in {TaskStatus.SUBMITTED, TaskStatus.WORKING}


class RefineProposalsClient(Protocol):
    """Minimal client surface consumed by :func:`refine_proposals_verified`."""

    async def refine_proposals(self, request: Any) -> TaskResult[Any]: ...


def unsupported_refinement_recovery(
    result: TaskResult[Any] | Mapping[str, Any],
) -> UnsupportedRefinementRecovery | None:
    """Parse the canonical task-level unsupported-dimension error details.

    Unknown or malformed error-detail shapes return ``None`` so callers still
    handle the task failure through the normal ``TaskResult.adcp_error`` path.
    """

    if isinstance(result, TaskResult):
        error = result.adcp_error
    else:
        candidate = result.get("adcp_error", result)
        error = candidate if isinstance(candidate, dict) else None
    if not isinstance(error, dict) or error.get("code") != "UNSUPPORTED_FEATURE":
        return None
    details = error.get("details")
    if not isinstance(details, dict):
        return None
    try:
        parsed = UnsupportedRefinementDimensionDetails.model_validate(details)
    except ValueError:
        return None
    return UnsupportedRefinementRecovery(
        unsupported_dimension=parsed.unsupported_dimension,
        supported_dimensions=tuple(item.root for item in parsed.supported_dimensions),
    )


async def refine_proposals_verified(
    client: RefineProposalsClient,
    request: BaseModel | Mapping[str, Any],
    proposal_refinement: BaseModel | Mapping[str, Any] | None = None,
    *,
    source_proposals: Mapping[str, BaseModel | Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> VerifiedRefinementResult:
    """Preflight, execute, and verify one proposal-refinement request.

    Preflight failures raise :class:`NegotiationVerificationError` before
    transport. A successful completed response is verified against the exact
    wire payload retained by :class:`~adcp.client.ADCPClient`; invalid seller
    output also raises. Task-level failures remain ordinary ``TaskResult``
    values, with canonical unsupported-dimension recovery parsed alongside.
    Submitted/working results are returned without premature verification.
    """

    preflight = preflight_refine_proposals(request, proposal_refinement)
    preflight.raise_for_errors()
    task_result = await client.refine_proposals(request)
    return verify_refinement_result(
        request,
        task_result,
        proposal_refinement=proposal_refinement,
        source_proposals=source_proposals,
        now=now,
    )


def verify_refinement_result(
    request: BaseModel | Mapping[str, Any],
    task_result: TaskResult[Any],
    *,
    proposal_refinement: BaseModel | Mapping[str, Any] | None = None,
    source_proposals: Mapping[str, BaseModel | Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> VerifiedRefinementResult:
    """Verify a terminal poll/webhook result without repeating transport."""

    recovery = unsupported_refinement_recovery(task_result)
    verification: NegotiationVerificationResult | None = None
    if (
        task_result.status == TaskStatus.COMPLETED
        and task_result.success
        and task_result.adcp_error is None
    ):
        response_data, _, _ = _response_data(task_result)
        try:
            _REFINE_RESPONSE_ADAPTER.validate_python(response_data)
        except ValidationError as exc:
            issues = tuple(
                NegotiationVerificationIssue(
                    "invalid_response_schema",
                    error["msg"],
                    "/" + "/".join(str(part) for part in error["loc"]),
                )
                for error in exc.errors(include_url=False)
            )
            raise NegotiationVerificationError(issues) from exc
        verification = verify_refine_proposals_response(
            request,
            task_result,
            proposal_refinement=proposal_refinement,
            source_proposals=source_proposals,
            now=now,
        )
        verification.raise_for_errors()
    return VerifiedRefinementResult(task_result, verification, recovery)


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

    request_data = (
        request.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
        if isinstance(request, BaseModel)
        else _as_dict(request)
    )
    if request_data is None:
        raise TypeError("request must serialize to a JSON object")
    try:
        _REFINE_REQUEST_ADAPTER.validate_python(request_data)
    except ValidationError as exc:
        return NegotiationVerificationResult(
            tuple(
                NegotiationVerificationIssue(
                    "invalid_request_schema",
                    error["msg"],
                    "/" + "/".join(str(part) for part in error["loc"]),
                )
                for error in exc.errors(include_url=False)
            )
        )
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
        if action not in {"revise", "finalize"}:
            issues.append(
                NegotiationVerificationIssue(
                    "invalid_request_schema",
                    "action must be explicitly set to revise or finalize",
                    f"{pointer}/action",
                )
            )

        change_fields = {
            key
            for key in ("constraints", "product_changes", "alternatives", "ask", "criteria")
            if key in refinement
        }
        cancellation = refinement.get("change_kind") == "cancellation"
        if action == "revise" and not change_fields and not cancellation:
            issues.append(
                NegotiationVerificationIssue(
                    "invalid_request_schema",
                    "revise requires a typed change, ask, criteria, or cancellation",
                    pointer,
                )
            )
        if action == "finalize":
            forbidden = change_fields | ({"change_kind"} if "change_kind" in refinement else set())
            for key in sorted(forbidden):
                issues.append(
                    NegotiationVerificationIssue(
                        "invalid_request_schema",
                        "finalize cannot include revision fields",
                        f"{pointer}/{key}",
                    )
                )

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
    *,
    proposal_refinement: BaseModel | Mapping[str, Any] | None = None,
    source_proposals: Mapping[str, BaseModel | Mapping[str, Any]] | None = None,
    now: datetime | None = None,
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
    try:
        _REFINE_RESPONSE_ADAPTER.validate_python(response_data)
    except ValidationError as exc:
        issues.extend(
            NegotiationVerificationIssue(
                "invalid_response_schema",
                error["msg"],
                "/" + "/".join(str(part) for part in error["loc"]),
            )
            for error in exc.errors(include_url=False)
        )
    capability = _as_dict(proposal_refinement) if proposal_refinement is not None else None
    supported = capability.get("supported_dimensions") if capability is not None else None
    supported_dimensions = set(supported) if isinstance(supported, list) else None
    verification_time = now or datetime.now(timezone.utc)
    if verification_time.tzinfo is None or verification_time.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
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

    source_ids = {
        str(refinement.get("proposal_id"))
        for refinement in refinements
        if isinstance(refinement, dict) and isinstance(refinement.get("proposal_id"), str)
    }
    returned_proposal_ids: set[str] = set()

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
        reason_code = result.get("reason_code")
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

        ask = refinement.get("ask")
        if reason_code in {"commercially_declined", "uninterpreted"} and not (
            isinstance(ask, str) and ask.strip()
        ):
            issues.append(
                NegotiationVerificationIssue(
                    "reason_mismatch",
                    f"reason_code {reason_code!r} requires a non-empty free-text ask",
                    f"{pointer}/reason_code",
                )
            )
        if reason_code in {"hold_unavailable", "batch_aborted"} and action != "finalize":
            issues.append(
                NegotiationVerificationIssue(
                    "reason_mismatch",
                    f"reason_code {reason_code!r} is valid only for finalize",
                    f"{pointer}/reason_code",
                )
            )
        if reason_code == "unsupported_dimension":
            requested_dimensions: set[str] = set()
            constraints = refinement.get("constraints")
            if isinstance(constraints, dict):
                requested_dimensions.update(
                    key for key, value in constraints.items() if value is not None
                )
            requested_dimensions.update(
                key
                for key in ("product_changes", "alternatives", "criteria")
                if refinement.get(key) is not None
            )
            invalid_unsupported_reason = action != "revise" or not requested_dimensions
            if supported_dimensions is not None:
                invalid_unsupported_reason = invalid_unsupported_reason or (
                    requested_dimensions <= supported_dimensions
                )
            if invalid_unsupported_reason:
                issues.append(
                    NegotiationVerificationIssue(
                        "reason_mismatch",
                        "unsupported_dimension requires revise with a requested typed "
                        "dimension outside the seller's advertised supported_dimensions",
                        f"{pointer}/reason_code",
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
            proposal_id = proposal.get("proposal_id")
            if isinstance(proposal_id, str):
                if proposal_id in source_ids:
                    issues.append(
                        NegotiationVerificationIssue(
                            "successor_proposal_id",
                            "a refinement result must use a new proposal_id",
                            f"{proposal_pointer}/proposal_id",
                        )
                    )
                if proposal_id in returned_proposal_ids:
                    issues.append(
                        NegotiationVerificationIssue(
                            "duplicate_successor_proposal_id",
                            "returned proposal_id values must be unique across the batch",
                            f"{proposal_pointer}/proposal_id",
                        )
                    )
                returned_proposal_ids.add(proposal_id)
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

            if outcome == "finalized":
                expires_at = _parse_datetime(proposal.get("expires_at"))
                if expires_at is None or expires_at <= verification_time:
                    issues.append(
                        NegotiationVerificationIssue(
                            "proposal_expired",
                            "a finalized hold must have expires_at later than verification time",
                            f"{proposal_pointer}/expires_at",
                        )
                    )
                source = source_proposals.get(str(source_id)) if source_proposals else None
                source_data = _as_dict(source) if source is not None else None
                source_terms = source_data.get("commercial_terms") if source_data else None
                source_digest = source_data.get("terms_digest") if source_data else None
                source_status = source_data.get("proposal_status") if source_data else None
                if source_data is not None and source_data.get("proposal_id") != source_id:
                    issues.append(
                        NegotiationVerificationIssue(
                            "source_proposal_identity",
                            "source proposal payload does not match the requested proposal_id",
                            proposal_pointer,
                        )
                    )
                if not isinstance(source_terms, dict):
                    issues.append(
                        NegotiationVerificationIssue(
                            "source_proposal_required",
                            "finalize verification requires the original source proposal",
                            proposal_pointer,
                        )
                    )
                else:
                    if source_status != "draft":
                        issues.append(
                            NegotiationVerificationIssue(
                                "source_proposal_state",
                                "finalize requires a draft source proposal",
                                proposal_pointer,
                            )
                        )
                    finalized_digest = compute_terms_digest(terms)
                    if isinstance(source, BaseModel):
                        # Parsed models normalize wire lexemes (notably RFC3339
                        # offsets), so their commercial_terms cannot be rehashed
                        # faithfully. Compare the retained source digest to the
                        # independently verified finalized wire digest instead.
                        computed_source_digest = source_digest
                    else:
                        computed_source_digest = compute_terms_digest(source_terms)
                        if isinstance(source_digest, str) and not hmac.compare_digest(
                            source_digest, computed_source_digest
                        ):
                            issues.append(
                                NegotiationVerificationIssue(
                                    "source_terms_digest",
                                    "source proposal terms_digest does not match its "
                                    "commercial_terms",
                                    f"{proposal_pointer}/commercial_terms",
                                )
                            )
                    if not isinstance(computed_source_digest, str):
                        issues.append(
                            NegotiationVerificationIssue(
                                "source_terms_digest",
                                "source proposal must carry its original terms_digest",
                                f"{proposal_pointer}/commercial_terms",
                            )
                        )
                    elif not hmac.compare_digest(computed_source_digest, finalized_digest):
                        issues.append(
                            NegotiationVerificationIssue(
                                "finalize_terms_changed",
                                "finalize must preserve the source proposal's commercial_terms",
                                f"{proposal_pointer}/commercial_terms",
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
        if "unsatisfied_constraints" in result and not result.get("unsatisfied_constraints"):
            issues.append(
                NegotiationVerificationIssue(
                    "constraint_mismatch",
                    "unsatisfied_constraints must be non-empty when present",
                    f"{pointer}/unsatisfied_constraints",
                )
            )
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
        if "unsatisfied_product_changes" in result and not declared_changes:
            issues.append(
                NegotiationVerificationIssue(
                    "product_change_mismatch",
                    "unsatisfied_product_changes must be non-empty when present",
                    f"{pointer}/unsatisfied_product_changes",
                )
            )
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
        if reason_code == "alternatives_unavailable" and not (
            isinstance(requested_count, int) and len(proposals) < requested_count
        ):
            issues.append(
                NegotiationVerificationIssue(
                    "reason_mismatch",
                    "alternatives_unavailable requires fewer proposals than requested",
                    f"{pointer}/reason_code",
                )
            )
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
        if reason_code == "constraint_unsatisfiable" and not (unsatisfied or unsatisfied_changes):
            issues.append(
                NegotiationVerificationIssue(
                    "constraint_precedence",
                    "reason_code constraint_unsatisfiable requires at least one "
                    "unsatisfied constraint or product change",
                    f"{pointer}/reason_code",
                )
            )

    if any(
        isinstance(result, dict) and result.get("reason_code") == "batch_aborted"
        for result in results
    ) and not any(
        isinstance(result, dict)
        and result.get("outcome") == "unable"
        and result.get("reason_code") not in {None, "batch_aborted"}
        for result in results
    ):
        issues.append(
            NegotiationVerificationIssue(
                "reason_mismatch",
                "batch_aborted requires a sibling finalize failure",
                "/results",
            )
        )

    return NegotiationVerificationResult(tuple(issues))


__all__ = [
    "NegotiationVerificationError",
    "NegotiationVerificationIssue",
    "NegotiationVerificationResult",
    "RefineProposalsClient",
    "UnsupportedRefinementRecovery",
    "VerifiedRefinementResult",
    "WIRE_RESPONSE_METADATA_KEY",
    "compute_terms_digest",
    "preflight_refine_proposals",
    "refine_proposals_verified",
    "unsupported_refinement_recovery",
    "verify_refinement_result",
    "verify_refine_proposals_response",
    "verify_terms_digest",
]
