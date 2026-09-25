"""Immutable buyer intent records and closed diagnostics.

These records describe submission, not evidence selection or reconciliation.
The caller must validate its receipt plan against the complete seller history.
Received adjustment evidence must be retained separately, before model parsing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Literal, TypeAlias

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import consumer_reference, principal_reference
from adcp.reporting.outbox.identity import canonical_consumer
from adcp.reporting.receipts.wire import ReceiptBatch, validate_receipt_response
from adcp.types import (
    ReportingAdjustmentReceipt,
    ReportingReceipt,
    SyncReportingReceiptsRequest,
    SyncReportingReceiptsResponse,
)

ReportingSubmissionReceipt: TypeAlias = ReportingReceipt | ReportingAdjustmentReceipt
ReceiptKind: TypeAlias = Literal["receipt", "adjustment_receipt"]
ReceiptResult: TypeAlias = Literal["recorded", "unchanged", "failed"]

MAX_SUBMISSION_RECEIPTS = 10_000
MAX_SUBMISSION_BYTES = 16 * 1024 * 1024
MAX_CONFIRMATION_BYTES = 16 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * MAX_CHUNK_BYTES
_CHUNK_SIZE = 100
_VERSION = "3.2-rc.6"


class ReportingSubmissionCode(str, Enum):
    INVALID_SCOPE = "INVALID_SUBMISSION_SCOPE"
    INVALID_PLAN = "INVALID_SUBMISSION_PLAN"
    UNAUTHORIZED = "SUBMISSION_NOT_AUTHORIZED"
    NOT_FOUND = "SUBMISSION_NOT_FOUND"
    HISTORY_CORRUPT = "SUBMISSION_HISTORY_CORRUPT"
    STORAGE_UNAVAILABLE = "SUBMISSION_STORAGE_UNAVAILABLE"
    PG_REQUIRED = "SUBMISSION_PG_REQUIRED"
    INVALID_RESPONSE = "SUBMISSION_RESPONSE_INVALID"
    TRANSPORT_UNCERTAIN = "SUBMISSION_TRANSPORT_UNCERTAIN"
    RESPONSE_UNCONFIRMED = "SUBMISSION_RESPONSE_UNCONFIRMED"


class ReportingSubmissionError(RuntimeError):
    """Closed code only: no provider, authentication, SQL or wire body details."""

    def __init__(self, code: ReportingSubmissionCode) -> None:
        self.code = (
            code
            if isinstance(code, ReportingSubmissionCode)
            else ReportingSubmissionCode.INVALID_PLAN
        )
        super().__init__(self.code.value)


class ReportingReceiptFailureCode(str, Enum):
    """Known item failures; unrecognized seller codes map to UNKNOWN.

    Seller messages/details are deliberately not persisted or exposed. Each
    failed item and each error position is retained, including unknown codes.
    """

    UNKNOWN = "UNKNOWN"
    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_REPORTING_RECORD = "INVALID_REPORTING_RECORD"
    REPORTING_RECORD_UNAVAILABLE = "REPORTING_RECORD_UNAVAILABLE"
    REPORTING_HISTORY_CORRUPT = "REPORTING_HISTORY_CORRUPT"
    REPORTING_IDENTITY_CONFLICT = "REPORTING_IDENTITY_CONFLICT"
    REPORTING_TIME_INVALID = "REPORTING_TIME_INVALID"
    RECEIPTS_NOT_ENABLED = "RECEIPTS_NOT_ENABLED"
    RECEIVED_AT_READ_ONLY = "RECEIVED_AT_READ_ONLY"
    RECEIPT_PROFILE_MISMATCH = "RECEIPT_PROFILE_MISMATCH"
    RECEIPT_TOTALS_MISMATCH = "RECEIPT_TOTALS_MISMATCH"
    RECEIPT_EVIDENCE_MISMATCH = "RECEIPT_EVIDENCE_MISMATCH"
    MATERIALIZATION_UNREADABLE = "MATERIALIZATION_UNREADABLE"
    ADJUSTMENT_REQUIRES_OFFICIAL = "ADJUSTMENT_REQUIRES_OFFICIAL"
    ADJUSTMENT_ORDER_INVALID = "ADJUSTMENT_ORDER_INVALID"
    ADJUSTMENT_DIGEST_MISMATCH = "ADJUSTMENT_DIGEST_MISMATCH"
    ACCEPTED_RECEIPT_TERMINAL = "ACCEPTED_RECEIPT_TERMINAL"


@dataclass(frozen=True)
class ReportingSubmissionScope:
    """Identity resolved by a trusted authorization adapter, never from a request.

    ``seller_id`` identifies the configured seller, ``account_id`` is the
    seller-resolved account and ``consumer_id`` is the canonical authenticated
    principal. Syntax checks cannot establish provenance: the required
    authorizer must bind all three to the exact client and its current access.
    No credential or natural-key account assertion belongs in these fields.
    """

    seller_id: str = field(repr=False)
    account_id: str = field(repr=False)
    consumer_id: str = field(repr=False)

    def __post_init__(self) -> None:
        valid = False
        try:
            consumer_reference(self.seller_id)
            principal_reference(self.account_id)
            canonical_consumer(self.consumer_id)
            valid = all(value == value.strip() for value in (self.seller_id, self.account_id))
        except (ValueError, TypeError, RuntimeError):
            pass
        if not valid:
            raise ReportingSubmissionError(ReportingSubmissionCode.INVALID_SCOPE)

    @property
    def canonical_identity(self) -> bytes:
        return canonical_json_utf8_v1([self.seller_id, self.account_id, self.consumer_id])

    @property
    def storage_key(self) -> str:
        # Index a fixed-size digest, not a possibly 2048-character principal.
        # Stores also compare the full identity to fail closed on collisions.
        return hashlib.sha256(self.canonical_identity).hexdigest()


def _receipt(kind: ReceiptKind, body: dict[str, Any]) -> ReportingSubmissionReceipt:
    if kind == "receipt":
        return ReportingReceipt.model_validate(body)
    return ReportingAdjustmentReceipt.model_validate(body)


@dataclass(frozen=True)
class ReportingReceiptOutcome:
    """One confirmed item, in original input order, with fresh model views."""

    ordinal: int
    kind: ReceiptKind
    result: ReceiptResult
    error_codes: tuple[ReportingReceiptFailureCode, ...] = ()
    _submitted: bytes = field(default=b"", repr=False)
    _stored: bytes | None = field(default=None, repr=False)

    @property
    def submitted_receipt(self) -> ReportingSubmissionReceipt:
        return _receipt(self.kind, json.loads(self._submitted))

    @property
    def receipt(self) -> ReportingSubmissionReceipt | None:
        """The seller's actual stored receipt on success, including received_at."""
        return _receipt(self.kind, json.loads(self._stored)) if self._stored is not None else None


@dataclass(frozen=True)
class ReportingReceiptSubmission:
    """Exact reserved plan and its immutable, fully confirmed chunk prefix.

    Construct with :func:`prepare_reporting_receipt_submission`. Stores validate
    persisted records before returning them. Private bytes keep mutable caller
    models, driver results, and representations out of the retained identity.
    """

    scope: ReportingSubmissionScope = field(repr=False)
    submission_id: str = field(repr=False)
    _plan: bytes = field(repr=False)
    _confirmed: tuple[bytes, ...] = field(default=(), repr=False)

    @property
    def chunk_count(self) -> int:
        return len(json.loads(self._plan)["requests"])

    @property
    def confirmed_chunks(self) -> int:
        return len(self._confirmed)

    @property
    def pending(self) -> bool:
        return self.confirmed_chunks < self.chunk_count

    def request(self, ordinal: int) -> SyncReportingReceiptsRequest:
        """Return a fresh typed view of one exact persisted request body."""
        body = json.loads(self._plan)["requests"][ordinal]
        return SyncReportingReceiptsRequest.model_validate(body)

    @property
    def outcomes(self) -> tuple[ReportingReceiptOutcome, ...]:
        plan = json.loads(self._plan)
        confirmed = {
            item["reporting_receipt_id"]: item
            for chunk in self._confirmed
            for item in json.loads(chunk)
        }
        outcomes = []
        for ordinal, item in enumerate(plan["items"]):
            result = confirmed.get(item["body"]["reporting_receipt_id"])
            if result is not None:
                stored = result.get("receipt")
                outcomes.append(
                    ReportingReceiptOutcome(
                        ordinal,
                        item["kind"],
                        result["result"],
                        tuple(ReportingReceiptFailureCode(code) for code in result["errors"]),
                        canonical_json_utf8_v1(item["body"]),
                        canonical_json_utf8_v1(stored) if stored is not None else None,
                    )
                )
        return tuple(outcomes)


@dataclass(frozen=True)
class ReportingSubmissionResult:
    """Submission outcomes only; completion does not establish reconciliation.

    ``proposal_deferred`` means an earlier pending scope reservation was resumed
    instead. Inspect its outcomes before planning any subsequent submission.
    """

    submission: ReportingReceiptSubmission
    proposal_deferred: bool = False
    diagnostic: ReportingSubmissionCode | None = None

    @property
    def pending(self) -> bool:
        return self.submission.pending

    @property
    def outcomes(self) -> tuple[ReportingReceiptOutcome, ...]:
        return self.submission.outcomes

    @property
    def submitted_receipts(self) -> tuple[ReportingSubmissionReceipt, ...]:
        return tuple(
            receipt for outcome in self.outcomes if (receipt := outcome.receipt) is not None
        )


def prepare_reporting_receipt_submission(
    scope: ReportingSubmissionScope,
    receipts: Sequence[ReportingSubmissionReceipt],
) -> ReportingReceiptSubmission:
    """Freeze an already validated receipt plan, with at most 100 items per call.

    Outbound typed values are normalized once, then retained byte-for-byte.
    This is not a capture or proof of raw inbound adjustment evidence. Requests
    contain the authorized resolved account only, with no caller-provided
    account, idempotency key, context, extensions or credentials.
    """
    result = None
    try:
        if (
            type(scope) is not ReportingSubmissionScope
            or not 1 <= len(receipts) <= MAX_SUBMISSION_RECEIPTS
        ):
            raise ValueError
        items: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        size = 0
        for receipt in receipts:
            if isinstance(receipt, ReportingReceipt):
                kind = "receipt"
            elif isinstance(receipt, ReportingAdjustmentReceipt):
                kind = "adjustment_receipt"
            else:
                raise ValueError
            body = receipt.model_dump(mode="json", exclude_none=True)
            identifier = body["reporting_receipt_id"]
            if identifier in identifiers or "received_at" in body:
                raise ValueError
            identifiers.add(identifier)
            item = {"kind": kind, "body": body}
            size += len(canonical_json_utf8_v1(item))
            if size > MAX_SUBMISSION_BYTES // 2:
                raise ValueError
            items.append(item)
        fingerprint = hashlib.sha256(
            scope.canonical_identity + b"\n" + canonical_json_utf8_v1(items)
        ).hexdigest()
        requests = []
        for offset in range(0, len(items), _CHUNK_SIZE):
            request: dict[str, Any] = {
                "adcp_version": _VERSION,
                "account": {"account_id": scope.account_id},
                "idempotency_key": f"reporting-buyer:{fingerprint}:{offset // _CHUNK_SIZE}",
            }
            for kind, name in (
                ("receipt", "receipts"),
                ("adjustment_receipt", "adjustment_receipts"),
            ):
                values = [
                    item["body"]
                    for item in items[offset : offset + _CHUNK_SIZE]
                    if item["kind"] == kind
                ]
                if values:
                    request[name] = values
            encoded = canonical_json_utf8_v1(request)
            if len(encoded) > MAX_CHUNK_BYTES:
                raise ValueError
            ReceiptBatch.parse(request)
            requests.append(request)
        plan = canonical_json_utf8_v1({"version": 1, "items": items, "requests": requests})
        if len(plan) > MAX_SUBMISSION_BYTES:
            raise ValueError
        result = ReportingReceiptSubmission(scope, f"reporting-submission:{fingerprint}", plan)
    except Exception:
        result = None
    if result is None:
        raise ReportingSubmissionError(ReportingSubmissionCode.INVALID_PLAN)
    return result


def validate_submission(submission: ReportingReceiptSubmission) -> None:
    """Check every supplied/persisted byte and confirmed result before use."""
    valid = False
    try:
        if type(submission._plan) is not bytes or len(submission._plan) > MAX_SUBMISSION_BYTES:
            raise ValueError
        plan = json.loads(submission._plan)
        if (
            type(plan) is not dict
            or set(plan) != {"version", "items", "requests"}
            or plan["version"] != 1
        ):
            raise ValueError
        if not 1 <= len(plan["items"]) <= MAX_SUBMISSION_RECEIPTS:
            raise ValueError
        rebuilt = prepare_reporting_receipt_submission(
            submission.scope,
            [_receipt(item["kind"], item["body"]) for item in plan["items"]],
        )
        if rebuilt._plan != submission._plan or rebuilt.submission_id != submission.submission_id:
            raise ValueError
        if (
            not isinstance(submission._confirmed, tuple)
            or len(submission._confirmed) > len(plan["requests"])
            or _confirmation_bytes(submission._confirmed) > MAX_CONFIRMATION_BYTES
        ):
            raise ValueError
        for ordinal, chunk in enumerate(submission._confirmed):
            if type(chunk) is not bytes or len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError
            if _restore_confirmation(submission, ordinal, chunk) != chunk:
                raise ValueError
        valid = True
    except Exception:
        valid = False
    if not valid:
        raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)


def _confirmation(
    submission: ReportingReceiptSubmission,
    ordinal: int,
    response: SyncReportingReceiptsResponse,
) -> bytes:
    """Validate complete unique coverage and exact immutable success bodies."""
    result = None
    try:
        body = response.model_dump(mode="json", exclude_none=True)
        if len(canonical_json_utf8_v1(body)) > MAX_RESPONSE_BYTES:
            raise ValueError
        batch = ReceiptBatch.parse(json.loads(submission._plan)["requests"][ordinal])
        results = body["results"]
        by_id = {}
        for entry in results:
            identifier = (
                entry["reporting_receipt_id"]
                if entry["result"] == "failed"
                else entry.get("receipt", entry.get("adjustment_receipt"))["reporting_receipt_id"]
            )
            if identifier in by_id:
                raise ValueError
            by_id[identifier] = entry
        expected = [item["reporting_receipt_id"] for _, item in batch.items]
        if set(by_id) != set(expected):
            raise ValueError
        # Sellers may reorder results. Validate their actual schema after matching
        # exact IDs, then restore the original *mixed* input order in outcomes.
        body["results"] = [by_id[identifier] for identifier in expected]
        validate_receipt_response(body, batch)
        normalized = []
        known = {code.value for code in ReportingReceiptFailureCode}
        for kind, submitted in batch.items:
            identifier = submitted["reporting_receipt_id"]
            entry = by_id[identifier]
            errors = []
            stored = None
            if entry["result"] == "failed":
                errors = [
                    code if (code := error["code"]) in known else "UNKNOWN"
                    for error in entry["errors"]
                ]
            else:
                key = "receipt" if kind == "revision_receipt" else "adjustment_receipt"
                stored = entry[key]
                immutable = {key: value for key, value in stored.items() if key != "received_at"}
                if canonical_json_utf8_v1(immutable) != canonical_json_utf8_v1(submitted):
                    raise ValueError
            normalized.append(
                {
                    "reporting_receipt_id": identifier,
                    "result": entry["result"],
                    "errors": errors,
                    "receipt": stored,
                }
            )
        result = canonical_json_utf8_v1(normalized)
        if len(result) > MAX_RESPONSE_BYTES:
            raise ValueError
    except Exception:
        result = None
    if result is None:
        raise ReportingSubmissionError(ReportingSubmissionCode.INVALID_RESPONSE)
    return result


def _restore_confirmation(
    submission: ReportingReceiptSubmission, ordinal: int, encoded: bytes
) -> bytes:
    """Revalidate retained sanitized evidence, including exact result coverage."""
    kinds = {
        item["body"]["reporting_receipt_id"]: item["kind"]
        for item in json.loads(submission._plan)["items"]
    }
    results = []
    for item in json.loads(encoded):
        if item["result"] == "failed":
            if item["receipt"] is not None:
                raise ValueError
            results.append(
                {
                    "result": "failed",
                    "reporting_receipt_id": item["reporting_receipt_id"],
                    "errors": [
                        {"code": code, "message": "receipt submission failed"}
                        for code in item["errors"]
                    ],
                }
            )
        else:
            if item["errors"]:
                raise ValueError
            results.append(
                {"result": item["result"], kinds[item["reporting_receipt_id"]]: item["receipt"]}
            )
    response = SyncReportingReceiptsResponse.model_validate({"results": results})
    return _confirmation(submission, ordinal, response)


def _confirmation_bytes(chunks: tuple[bytes, ...]) -> int:
    # Each chunk is canonical JSON. Account for the enclosing array/commas as
    # well, so memory and PostgreSQL enforce the same cumulative storage bound.
    return 2 + sum(map(len, chunks)) + max(0, len(chunks) - 1)


def confirm_submission(
    submission: ReportingReceiptSubmission,
    ordinal: int,
    response: SyncReportingReceiptsResponse,
) -> ReportingReceiptSubmission:
    if (
        type(ordinal) is not int
        or not 0 <= ordinal < submission.chunk_count
        or ordinal > submission.confirmed_chunks
    ):
        raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
    confirmed = _confirmation(submission, ordinal, response)
    if ordinal < submission.confirmed_chunks:
        # First confirmation is immutable. Concurrent exact replays may use
        # recorded/unchanged differently, but must agree on the actual evidence
        # and failures. A contradictory reply cannot overwrite retained results.
        previous = json.loads(submission._confirmed[ordinal])
        repeated = json.loads(confirmed)
        for results in (previous, repeated):
            for item in results:
                if item["result"] in {"recorded", "unchanged"}:
                    item["result"] = "recorded"
        if previous != repeated:
            raise ReportingSubmissionError(ReportingSubmissionCode.INVALID_RESPONSE)
        return submission
    chunks = (*submission._confirmed, confirmed)
    if _confirmation_bytes(chunks) > MAX_CONFIRMATION_BYTES:
        raise ReportingSubmissionError(ReportingSubmissionCode.INVALID_RESPONSE)
    return replace(submission, _confirmed=chunks)


def encode_submission(submission: ReportingReceiptSubmission) -> tuple[str, str, str, str]:
    """Exact text/hashes for PostgreSQL; JSONB is not the identity store."""
    confirmed = canonical_json_utf8_v1([json.loads(chunk) for chunk in submission._confirmed])
    return (
        submission._plan.decode(),
        hashlib.sha256(submission._plan).hexdigest(),
        confirmed.decode(),
        hashlib.sha256(confirmed).hexdigest(),
    )


def decode_submission(
    scope: ReportingSubmissionScope, row: Sequence[Any]
) -> ReportingReceiptSubmission:
    result = None
    try:
        identifier, plan, plan_digest, confirmed, confirmed_digest, pending = row
        if (
            len(plan.encode()) > MAX_SUBMISSION_BYTES
            or len(confirmed.encode()) > MAX_CONFIRMATION_BYTES
        ):
            raise ValueError
        if (
            hashlib.sha256(plan.encode()).hexdigest() != plan_digest
            or hashlib.sha256(confirmed.encode()).hexdigest() != confirmed_digest
        ):
            raise ValueError
        candidate = ReportingReceiptSubmission(
            scope,
            identifier,
            plan.encode(),
            tuple(canonical_json_utf8_v1(chunk) for chunk in json.loads(confirmed)),
        )
        validate_submission(candidate)
        if (
            type(pending) is not bool
            or pending != candidate.pending
            or encode_submission(candidate) != (plan, plan_digest, confirmed, confirmed_digest)
        ):
            raise ValueError
        result = candidate
    except Exception:
        result = None
    if result is None:
        raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
    return result
