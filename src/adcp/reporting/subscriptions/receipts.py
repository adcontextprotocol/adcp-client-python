"""The buyer's receipts policy for ``client.reporting.reconcile`` (AdCP 3.3, experimental).

A receipt says the buyer verified and committed the data, so it is never sent
on read. With ``ReceiptPolicy(mode="verified", commit=...)``, for each
``consumer_receipt`` obligation whose current revision is materialized and not
yet receipted:

1. the SDK verifies the materialization itself: the manifest or native commit
   through ``resource_reader`` (the built-in manifest inspector) or your
   ``inspect`` adapter, giving the observed row count, control totals and
   digest;
2. a materialization the verifier cannot read, or whose content the
   built-in inspector refuses (a digest, row-count or object mismatch), is
   listed in ``report.receipts.failed`` with the code and receipted nothing;
   an ``inspect`` adapter that returns a mismatched observation gets a
   ``rejected`` receipt with the SDK's codes (``ROW_COUNT_MISMATCH`` and
   friends), and ``commit`` is not called;
3. otherwise your ``commit(revision)`` hook runs: load the data, then return
   :class:`CommitResult` (``accepted``, with ``consumer_commit_ref``),
   ``"defer"`` (try again next run) or :class:`Reject` (``rejected``, with
   your codes).

Receipt ids are stable per (obligation, revision, materialization), and a
materialization already receipted, accepted or rejected, is not receipted
again.

``mode="off"`` (the default) submits nothing; billing periods then show
``receipt: {"status": "owed_by_buyer"}`` and ``responsible_party: "buyer"``,
and the client logs a warning once. Adjustments are offered to
``adjustments(adjustment)``; the default defers, and billing never
auto-accepts. Accepting an adjustment needs its raw evidence, so it stays on
:meth:`ReportingClient.submit_receipts`.
"""

from __future__ import annotations

import hashlib
import inspect as _inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommitResult:
    """Your commit loaded the data: submit an ``accepted`` receipt with this reference."""

    commit_ref: str


@dataclass(frozen=True)
class Reject:
    """Your commit refused the data: submit a ``rejected`` receipt with these codes."""

    codes: tuple[str, ...]

    def __init__(self, codes: Sequence[str]) -> None:
        if not codes:
            raise ValueError("Reject needs at least one rejection code")
        object.__setattr__(self, "codes", tuple(codes))


CommitOutcome = CommitResult | Reject | Literal["defer"]


@dataclass(frozen=True)
class VerifiedRevision:
    """What the SDK verified, handed to your ``commit`` hook."""

    account_id: str
    obligation: Any
    revision: Any
    materialization: Any
    observation: Any

    @property
    def resource(self) -> Any:
        """The materialized resource (``materialization.resource``)."""
        return self.materialization.resource


@dataclass(frozen=True)
class ReceiptPolicy:
    """How ``reconcile`` handles ``consumer_receipt`` revisions.

    ``mode="verified"`` needs ``commit`` and one of ``resource_reader`` or
    ``inspect``. ``adjustments`` sees each pending adjustment of a delivered
    revision; return ``"defer"``.
    """

    mode: Literal["off", "verified"] = "off"
    commit: Callable[[VerifiedRevision], Awaitable[CommitOutcome] | CommitOutcome] | None = None
    adjustments: Callable[[Any], Awaitable[str] | str] | None = None
    resource_reader: Any | None = None
    inspect: Callable[[Any], Awaitable[Any]] | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("off", "verified"):
            raise ValueError("ReceiptPolicy mode is 'off' or 'verified'")
        if self.mode == "verified":
            if self.commit is None:
                raise ValueError(
                    "ReceiptPolicy(mode='verified') needs commit: the hook that loads the data "
                    "and returns CommitResult, 'defer' or Reject."
                )
            if (self.resource_reader is None) == (self.inspect is None):
                raise ValueError(
                    "ReceiptPolicy(mode='verified') needs exactly one of resource_reader (the "
                    "built-in manifest inspector) or inspect (your verifier)."
                )


@dataclass
class ReceiptReport:
    """What the receipts policy did in one ``reconcile``."""

    submitted: list[Any] = field(default_factory=list)
    rejected: list[Any] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    #: ``(reporting_obligation_id, code)`` for revisions the SDK could not verify:
    #: the built-in inspector refused the materialization (a digest, row-count or
    #: object mismatch) or a read failed. Nothing is receipted for them.
    failed: list[tuple[str, str]] = field(default_factory=list)
    adjustments_deferred: list[str] = field(default_factory=list)
    owed_by_buyer: int = 0


def _receipt_id(obligation: Any, revision: Any, materialization: Any) -> str:
    """A stable receipt id per (obligation, revision, materialization), so a resend replays."""
    digest = hashlib.sha256(
        "\x1f".join(
            (
                obligation.reporting_obligation_id,
                revision.reporting_revision_id,
                materialization.reporting_materialization_id,
            )
        ).encode()
    ).hexdigest()
    return f"reporting-receipt:{digest[:40]}"


async def _resolve(value: Any) -> Any:
    return await value if _inspect.isawaitable(value) else value


async def apply_receipt_policy(
    client: Any,
    account_id: str,
    ledger: Any,
    policy: ReceiptPolicy,
    report: ReceiptReport,
    *,
    now: datetime,
) -> bool:
    """Verify, commit and receipt one account's unreceipted revisions; ``True`` when it wrote."""
    from adcp.reporting._reconcile import (
        ReportingInspectionContext,
        ReportingObservation,
        _enum,
        _receipt_matches,
        _validate_read_ledger,
        build_reporting_receipt,
    )
    from adcp.types import SyncReportingReceiptsRequest

    if policy.mode != "verified":
        return False
    inspect = policy.inspect
    if inspect is None:
        from adcp.reporting_inspection import ManifestReportingInspector

        assert policy.resource_reader is not None
        inspect = ManifestReportingInspector(policy.resource_reader)
    commit = policy.commit
    assert commit is not None
    _leaves, _complete, index = _validate_read_ledger(ledger)
    outgoing: list[Any] = []
    for obligation in ledger.obligations:
        if _enum(obligation.reconciliation_mode) != "consumer_receipt":
            continue
        revision, materialization, reasons = index.selections[obligation.reporting_obligation_id]
        if revision is None or materialization is None or reasons:
            continue
        if any(_receipt_matches(item, revision, materialization) for item in ledger.receipts):
            continue
        if any(
            item.reporting_obligation_id == obligation.reporting_obligation_id
            and item.reporting_revision_id == revision.reporting_revision_id
            and item.reporting_materialization_id == materialization.reporting_materialization_id
            for item in ledger.receipts
        ):
            # Already receipted, rejected included: a refusal is stated once per
            # materialization, never re-sent on every run.
            continue
        for adjustment in index.adjustments_by_revision.get(revision.reporting_revision_id, []):
            decision = "defer"
            if policy.adjustments is not None:
                decision = str(await _resolve(policy.adjustments(adjustment)))
            if decision != "defer":
                logger.warning(
                    "adjustment %s: only 'defer' is applied by the receipts policy; accept or "
                    "reject it with client.reporting.submit_receipts",
                    adjustment.reporting_adjustment_id,
                )
            report.adjustments_deferred.append(adjustment.reporting_adjustment_id)
        context = ReportingInspectionContext(obligation, revision, materialization)
        try:
            observation = await inspect(context)
        except Exception as error:  # noqa: BLE001 - one revision never fails the account
            raw = getattr(error, "code", None)
            code = "INSPECTION_FAILED" if raw is None else _enum(raw)
            report.failed.append((obligation.reporting_obligation_id, code))
            logger.warning(
                "receipts: %s could not be verified (%s); nothing was receipted for it",
                obligation.reporting_obligation_id,
                code,
            )
            continue
        receipt = build_reporting_receipt(
            context,
            observation,
            reporting_receipt_id=_receipt_id(obligation, revision, materialization),
            observed_at=now,
        )
        if _enum(receipt.status) == "rejected":
            outgoing.append(receipt)
            report.rejected.append(receipt)
            continue
        decision_value = await _resolve(
            commit(VerifiedRevision(account_id, obligation, revision, materialization, observation))
        )
        if isinstance(decision_value, CommitResult):
            committed = ReportingObservation(
                row_count=observation.row_count,
                control_totals=observation.control_totals,
                canonical_content_digest=observation.canonical_content_digest,
                manifest_sha256=observation.manifest_sha256,
                native_version_ref=observation.native_version_ref,
                consumer_commit_ref=decision_value.commit_ref,
            )
            receipt = build_reporting_receipt(
                context,
                committed,
                reporting_receipt_id=receipt.reporting_receipt_id,
                observed_at=now,
            )
            outgoing.append(receipt)
            report.submitted.append(receipt)
        elif isinstance(decision_value, Reject):
            payload = receipt.model_dump(mode="json", exclude_none=True)
            payload.update(status="rejected", rejection_codes=list(decision_value.codes))
            rejected = type(receipt).model_validate(payload)
            outgoing.append(rejected)
            report.rejected.append(rejected)
        elif decision_value == "defer":
            report.deferred.append(obligation.reporting_obligation_id)
        else:
            raise TypeError("commit returns CommitResult, Reject or 'defer'")
    if not outgoing:
        return False
    write = await client.sync_reporting_receipts(
        SyncReportingReceiptsRequest.model_validate(
            {
                "account": {"account_id": account_id},
                "idempotency_key": f"rrcpt-{uuid4()}",
                "receipts": [item.model_dump(mode="json", exclude_none=True) for item in outgoing],
            }
        )
    )
    data = getattr(write, "data", None)
    if not getattr(write, "success", False) or data is None:
        from adcp.reporting._reconcile import ReportingReconciliationError

        raise ReportingReconciliationError(
            "RECEIPT_WRITE_FAILED", "the seller did not record the reporting receipts; retry"
        )
    return True


__all__ = [
    "CommitOutcome",
    "CommitResult",
    "ReceiptPolicy",
    "ReceiptReport",
    "Reject",
    "VerifiedRevision",
    "apply_receipt_policy",
]
