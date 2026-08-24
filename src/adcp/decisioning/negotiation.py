"""Seller-side helpers for the compact proposal-refinement lifecycle.

The framework validates a whole batch before adopter policy runs, prepares
immutable proposal lineage and terms digests, and validates the completed
response before a transaction commits. Finalize batches require an adopter-
supplied async transaction context so every requested hold commits or rolls
back together.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import deepcopy
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, TypeAdapter, ValidationError

from adcp.decisioning.types import AdcpError
from adcp.negotiation import (
    NegotiationVerificationIssue,
    compute_terms_digest,
    preflight_refine_proposals,
    verify_refine_proposals_response,
)
from adcp.types import RefineProposalsRequest, RefineProposalsResponse

_REFINE_REQUEST_ADAPTER: TypeAdapter[Any] = TypeAdapter(RefineProposalsRequest)
_REFINE_RESPONSE_ADAPTER: TypeAdapter[Any] = TypeAdapter(RefineProposalsResponse)


class RefinementProcessor(Protocol):
    """Adopter callback that evaluates one already-preflighted entry."""

    def __call__(
        self, refinement: Mapping[str, Any], context: Any
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...


class RefinementTransactionFactory(Protocol):
    """Create the atomic boundary used by a refinement batch."""

    def __call__(
        self, request: BaseModel | Mapping[str, Any], context: Any
    ) -> AbstractAsyncContextManager[None]: ...


def _wire_mapping(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    elif isinstance(value, Mapping):
        return dict(value)
    raise TypeError("value must serialize to a JSON object")


def _wire_request_mapping(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
        if isinstance(dumped, dict):
            return dumped
    return _wire_mapping(value)


def _wire_refinements(request: BaseModel | Mapping[str, Any]) -> list[dict[str, Any]]:
    refinements = _wire_request_mapping(request).get("refinements")
    if not isinstance(refinements, list) or any(not isinstance(item, dict) for item in refinements):
        raise AdcpError(
            "VALIDATION_ERROR",
            message="refinements must be an ordered non-empty array",
            recovery="correctable",
            field="refinements",
        )
    return refinements


def snapshot_refinement_sources(
    source_proposals: Mapping[str, BaseModel | Mapping[str, Any]] | None,
) -> dict[str, BaseModel | dict[str, Any]]:
    """Detach source wire values before adopter code can mutate them."""

    if source_proposals is None:
        return {}
    return {
        str(key): deepcopy(value) if isinstance(value, BaseModel) else deepcopy(dict(value))
        for key, value in source_proposals.items()
    }


def validate_finalize_source_states_or_raise(
    request: BaseModel | Mapping[str, Any],
    source_proposals: Mapping[str, BaseModel | Mapping[str, Any]],
) -> None:
    """Reject double-finalize before any adopter policy or inventory mutation."""

    for index, refinement in enumerate(_wire_refinements(request)):
        if refinement.get("action") != "finalize":
            continue
        source_id = str(refinement.get("proposal_id"))
        source = source_proposals.get(source_id)
        source_data = _wire_mapping(source) if source is not None else None
        if source_data is not None and source_data.get("proposal_id") != source_id:
            raise AdcpError(
                "INVALID_STATE",
                message="source proposal payload does not match the requested proposal_id",
                recovery="correctable",
                field=f"refinements.{index}.proposal_id",
                details={"proposal_id": source_id},
            )
        if source_data is not None and source_data.get("proposal_status") != "draft":
            raise AdcpError(
                "INVALID_STATE",
                message="finalize requires a draft source proposal",
                recovery="correctable",
                field=f"refinements.{index}.proposal_id",
                details={"proposal_id": source_id},
            )


def _issues_details(issues: Sequence[NegotiationVerificationIssue]) -> dict[str, Any]:
    return {
        "issues": [
            {"code": issue.code, "message": issue.message, "pointer": issue.pointer}
            for issue in issues
        ]
    }


def preflight_refinement_batch_or_raise(
    request: BaseModel | Mapping[str, Any],
    proposal_refinement: BaseModel | Mapping[str, Any] | None,
) -> None:
    """Run batch-wide seller preflight and raise one task-level error.

    Unsupported typed dimensions use the canonical
    ``UnsupportedRefinementDimensionDetails`` keys. Other malformed batch
    conditions use ``VALIDATION_ERROR``. No adopter callback has run when
    either error is raised.
    """

    wire_request = _wire_request_mapping(request)
    try:
        _REFINE_REQUEST_ADAPTER.validate_python(wire_request)
    except ValidationError as exc:
        raise AdcpError(
            "VALIDATION_ERROR",
            message="proposal refinement request is schema-invalid",
            recovery="correctable",
            field="refinements",
            details={"schema_errors": exc.errors(include_url=False)},
        ) from exc

    result = preflight_refine_proposals(wire_request, proposal_refinement)
    if result.valid:
        return
    unsupported = next(
        (issue for issue in result.issues if issue.code == "unsupported_dimension"), None
    )
    if unsupported is not None:
        dimension = unsupported.pointer.rsplit("/", 1)[-1]
        capability = _wire_mapping(proposal_refinement) if proposal_refinement is not None else {}
        supported = capability.get("supported_dimensions")
        raise AdcpError(
            "UNSUPPORTED_FEATURE",
            message=f"refinement dimension {dimension!r} is not supported",
            recovery="correctable",
            field=unsupported.pointer,
            details={
                "unsupported_dimension": dimension,
                "supported_dimensions": list(supported) if isinstance(supported, list) else [],
            },
        )
    first = result.issues[0]
    raise AdcpError(
        "VALIDATION_ERROR",
        message="proposal refinement batch failed preflight",
        recovery="correctable",
        field=first.pointer,
        details=_issues_details(result.issues),
    )


def prepare_refinement_result(
    refinement: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    """Fill SDK-owned source lineage and missing normative terms digests.

    Existing conflicting values are never overwritten; completed-response
    verification rejects them before the surrounding transaction commits.
    """

    prepared = dict(result)
    source_id = refinement.get("proposal_id")
    prepared.setdefault("source_proposal_id", source_id)
    proposals: list[dict[str, Any]] = []
    plural = prepared.get("proposals")
    if isinstance(plural, list):
        proposals.extend(item for item in plural if isinstance(item, dict))
    singular = prepared.get("proposal")
    if isinstance(singular, dict):
        proposals.append(singular)
    for proposal in proposals:
        proposal.setdefault("parent_proposal_id", source_id)
        terms = proposal.get("commercial_terms")
        if isinstance(terms, dict):
            proposal.setdefault("terms_digest", compute_terms_digest(terms))
    return prepared


def validate_refinement_response_or_raise(
    request: BaseModel | Mapping[str, Any],
    response: BaseModel | Mapping[str, Any],
    *,
    proposal_refinement: BaseModel | Mapping[str, Any] | None = None,
    source_proposals: Mapping[str, BaseModel | Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> None:
    """Reject schema- or protocol-invalid output before serialization."""

    wire_response = _wire_mapping(response)
    try:
        _REFINE_RESPONSE_ADAPTER.validate_python(wire_response)
    except ValidationError as exc:
        raise AdcpError(
            "INTERNAL_ERROR",
            message="refinement processor produced a schema-invalid response",
            recovery="terminal",
            details={"schema_errors": exc.errors(include_url=False)},
        ) from exc
    verification = verify_refine_proposals_response(
        request,
        wire_response,
        proposal_refinement=proposal_refinement,
        source_proposals=source_proposals,
        now=now,
    )
    if verification.valid:
        return
    first = verification.issues[0]
    raise AdcpError(
        "INTERNAL_ERROR",
        message="refinement processor produced a protocol-invalid response",
        recovery="terminal",
        field=first.pointer,
        details=_issues_details(verification.issues),
    )


@asynccontextmanager
async def _no_transaction() -> Any:
    yield


class _FinalizeBatchRollbackError(Exception):
    """Internal signal carrying a valid all-unable response through rollback."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        super().__init__("finalize batch rolled back")


async def execute_refinement_batch(
    request: BaseModel | Mapping[str, Any],
    proposal_refinement: BaseModel | Mapping[str, Any] | None,
    process: RefinementProcessor,
    *,
    context: Any = None,
    finalize_transaction: RefinementTransactionFactory | None = None,
    products: Sequence[Mapping[str, Any] | BaseModel] = (),
    source_proposals: Mapping[str, BaseModel | Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute an ordered seller refinement batch behind one validation seam.

    ``process`` owns commercial decisions but receives entries only after the
    entire request passes capability/cardinality preflight. A batch containing
    ``finalize`` requires ``finalize_transaction``; that async context manager
    must stage every inventory hold and commit only on clean exit. Callback,
    lineage, digest, or response-validation failures exit exceptionally and
    therefore roll the whole finalize batch back.
    """

    preflight_refinement_batch_or_raise(request, proposal_refinement)
    refinements = _wire_refinements(request)
    source_snapshot = snapshot_refinement_sources(source_proposals)
    is_finalize = any(item.get("action") == "finalize" for item in refinements)
    if is_finalize and finalize_transaction is None:
        raise AdcpError(
            "UNSUPPORTED_FEATURE",
            message="finalize requires an atomic refinement transaction",
            recovery="terminal",
            field="refinements",
            details={"missing": "atomic_finalize_transaction"},
        )
    if is_finalize and source_proposals is None:
        raise AdcpError(
            "UNSUPPORTED_FEATURE",
            message="finalize requires the original source proposals for terms verification",
            recovery="terminal",
            field="refinements",
            details={"missing": "source_proposals"},
        )
    if is_finalize:
        validate_finalize_source_states_or_raise(request, source_snapshot)

    transaction = (
        finalize_transaction(request, context)
        if finalize_transaction is not None
        else _no_transaction()
    )
    try:
        async with transaction:
            results: list[dict[str, Any]] = []
            for refinement in refinements:
                candidate = process(refinement, context)
                if inspect.isawaitable(candidate):
                    candidate = await candidate
                if not isinstance(candidate, Mapping):
                    raise AdcpError(
                        "INTERNAL_ERROR",
                        message="refinement processor must return a mapping",
                        recovery="terminal",
                    )
                results.append(prepare_refinement_result(refinement, candidate))

            finalize_failed = is_finalize and any(
                result.get("outcome") == "unable" for result in results
            )
            if finalize_failed:
                results = [
                    (
                        result
                        if result.get("outcome") == "unable"
                        else {
                            "source_proposal_id": result.get("source_proposal_id"),
                            "outcome": "unable",
                            "reason_code": "batch_aborted",
                            "reason": "A sibling entry prevented the all-or-none finalize batch.",
                        }
                    )
                    for result in results
                ]

            response = {
                "status": "completed",
                "results": results,
                "products": [
                    (
                        item.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if isinstance(item, BaseModel)
                        else dict(item)
                    )
                    for item in products
                ],
            }
            validate_refinement_response_or_raise(
                request,
                response,
                proposal_refinement=proposal_refinement,
                source_proposals=source_snapshot,
                now=now,
            )
            if finalize_failed:
                raise _FinalizeBatchRollbackError(response)
            return response
    except _FinalizeBatchRollbackError as exc:
        return exc.response


__all__ = [
    "RefinementProcessor",
    "RefinementTransactionFactory",
    "execute_refinement_batch",
    "preflight_refinement_batch_or_raise",
    "prepare_refinement_result",
    "snapshot_refinement_sources",
    "validate_finalize_source_states_or_raise",
    "validate_refinement_response_or_raise",
]
