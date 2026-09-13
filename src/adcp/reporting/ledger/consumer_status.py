"""PREVIEW: ``sync_reporting_status`` ingest and the mismatch projection.

This closes the operational loop between what a seller says it published and
what an authenticated buyer could actually consume.  Without it, a seller's
ledger is a monologue: every obligation can look ``complete`` while the buyer
has never successfully read a single revision.

**Preview, and off by default.** ``sync_reporting_status`` merged to
``adcontextprotocol/adcp`` main but is not in a cut release tag, so its wire
types are not in this SDK's pinned bundle.  The models come from
:mod:`adcp.reporting._preview`, which is generated from vendored schemas and
will be deleted when rc.2 regenerates.  Turn the ingest on with::

    import adcp.reporting.ledger.consumer_status as consumer_status
    consumer_status.CONSUMER_STATUS_PREVIEW_ENABLED = True

or per-instance via ``ConsumerStatusIngest(..., enabled=True)``.  A seller that
turns it on must advertise ``consumer_status_task``; a seller that does not
should leave it off, because a half-implemented status loop is worse than none
-- a buyer that can file statements nobody reads believes it has told you.

What a statement is, and is not
-------------------------------

It is **operational status**: could the required reporting for this expected
period actually be consumed?  It is *not* measurement data, delivery totals, a
billing receipt, or authority over the seller's ledger.  ``received`` proves the
buyer consumed the exact Core revision binding -- nothing about materialization
evidence or billing control totals.

Four statuses, four different problems:

============================ ====================================================
``received``                 The exact revision content was consumed.
``obligation_missing``       The period is required by the accepted configuration
                             generation but the seller ledger omitted it.
``revision_missing``         The obligation exists but no required revision was
                             available after ``expected_at``.
``unreadable``               A revision was advertised but its content could not
                             be consumed; ``failure_code`` says why.
============================ ====================================================

``obligation_missing`` is deliberately keyed *without* a seller-issued
obligation id.  Requiring one would make the first missing report invisible
again, which is the exact failure this loop exists to surface.

Attribution
-----------

Consumer status is separately attributed and never satisfies seller production
health.  A conflicting current statement makes **only the submitting caller's**
view ``action_required`` with a stable ``CONSUMER_STATUS_MISMATCH`` issue.  One
buyer's assertion never changes another caller's view, and never touches
seller-advertised reliability statistics without corroboration.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from adcp.reporting.ledger.health import issue_id_for
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    ReportingHealth,
    ReportingIssue,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.store import LedgerConflictError, ReportingLedgerStore

__all__ = [
    "CONSUMER_STATUS_PREVIEW_ENABLED",
    "ConsumerStatusIngest",
    "ConsumerStatusPreviewDisabledError",
    "project_consumer_mismatch",
]

ResponsibleParty = Literal["buyer", "seller", "provider"]

#: Module-level opt-in for the whole preview surface.  Off because the wire
#: shape is not in a cut release yet; see the module docstring.
CONSUMER_STATUS_PREVIEW_ENABLED = False


class ConsumerStatusPreviewDisabledError(RuntimeError):
    """The preview ingest was called without being enabled."""


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ConsumerStatusIngest:
    """Records authenticated consumer statements into the ledger.

    Framework-agnostic like :class:`~adcp.reporting.ledger.status.ReportingStatusHandler`:
    a request mapping plus an authenticated caller in, a response mapping out.
    """

    store: ReportingLedgerStore
    enabled: bool | None = None

    def _require_enabled(self) -> None:
        active = CONSUMER_STATUS_PREVIEW_ENABLED if self.enabled is None else self.enabled
        if not active:
            raise ConsumerStatusPreviewDisabledError(
                "sync_reporting_status is a preview surface built on vendored, unreleased "
                "schemas. Set adcp.reporting.ledger.consumer_status."
                "CONSUMER_STATUS_PREVIEW_ENABLED = True, or pass enabled=True, and advertise "
                "consumer_status_task before accepting live statements."
            )

    async def handle(
        self, request: dict[str, Any], *, account_id: str, consumer_id: str
    ) -> dict[str, Any]:
        """Serve one ``sync_reporting_status`` batch.

        Returns one ``recorded`` / ``unchanged`` / ``failed`` result per
        submitted statement.  A failure in one statement does not fail the
        batch: a buyer reporting five periods should not lose four good
        statements to one stale supersession pointer.
        """
        self._require_enabled()
        from adcp.reporting._preview import validate_consumer_status_wire

        statements = request.get("statuses") or []
        if not statements:
            raise LedgerConflictError(
                "EMPTY_BATCH", "a sync_reporting_status batch must carry at least one statement"
            )
        seen_chains: set[tuple[Any, ...]] = set()
        results: list[dict[str, Any]] = []
        for statement in statements:
            status_id = statement.get("reporting_status_id", "")
            problems = validate_consumer_status_wire(statement)
            if problems:
                results.append(_failed(status_id, "INVALID_CONSUMER_STATUS", problems[0]))
                continue
            try:
                record = self._to_record(statement, account_id=account_id, consumer_id=consumer_id)
            except ValueError as error:
                results.append(_failed(status_id, "INVALID_CONSUMER_STATUS", str(error)))
                continue
            if record.chain_key in seen_chains:
                # "A batch contains at most one update for each logical status
                # chain" -- two updates in one batch have no defined order, so
                # the second would silently win or silently lose.
                results.append(
                    _failed(
                        status_id,
                        "DUPLICATE_CHAIN_IN_BATCH",
                        "a batch may carry at most one statement per logical status chain",
                    )
                )
                continue
            seen_chains.add(record.chain_key)
            try:
                await self._validate_against_configuration(record)
                stored, recorded = await self.store.record_consumer_status(record)
            except LedgerConflictError as error:
                results.append(_failed(status_id, error.code, str(error)))
                continue
            results.append(
                {
                    "result": "recorded" if recorded else "unchanged",
                    "consumer_status": _to_wire(stored),
                }
            )
        return {"status": "completed", "results": results}

    async def _validate_against_configuration(self, record: ConsumerStatusRecord) -> None:
        """Check the period against the caller's accepted configuration generation.

        Deliberately does *not* require an obligation to exist: the whole point
        of ``obligation_missing`` is that it is filed when the seller's ledger
        omitted the period.  The configuration generation is what makes the
        period legitimate.
        """
        configurations = await self.store.list_configurations(
            account_id=record.account_id, delivery_config_ids=[record.delivery_config_id]
        )
        generation = next(
            (
                item
                for item in configurations
                if item.delivery_config_version == record.delivery_config_version
            ),
            None,
        )
        if generation is None:
            raise LedgerConflictError(
                "UNKNOWN_CONFIGURATION_GENERATION",
                f"{record.delivery_config_id}@{record.delivery_config_version} is not an "
                "accepted configuration generation for this account",
            )
        if generation.report_definition_id != record.report_definition_id:
            raise LedgerConflictError(
                "REPORT_DEFINITION_MISMATCH",
                "the statement names a different report definition than the configuration "
                "generation accepted; unlike reporting promises must not share a status chain",
            )
        if (record.seller_ledger_snapshot_id is None) != (record.seller_ledger_as_of is None):
            raise LedgerConflictError(
                "SELLER_SNAPSHOT_EVIDENCE_INCOMPLETE",
                "seller_ledger_snapshot_id and seller_ledger_as_of are present together or "
                "not at all",
            )

    @staticmethod
    def _to_record(
        statement: dict[str, Any], *, account_id: str, consumer_id: str
    ) -> ConsumerStatusRecord:
        from adcp.reporting._preview import ReportingConsumerStatus

        parsed = ReportingConsumerStatus.model_validate(statement)
        period = parsed.period
        return ConsumerStatusRecord(
            reporting_status_id=parsed.reporting_status_id,
            # Identity comes from authenticated transport. A body that asserts
            # a buyer or consumer principal is ignored, not trusted.
            account_id=account_id,
            consumer_id=consumer_id,
            delivery_config_id=parsed.delivery_config_id,
            delivery_config_version=parsed.delivery_config_version,
            report_definition_id=parsed.report_definition_id,
            period_start=_utc(period.start),
            period_end=_utc(period.end),
            period_source_timezone=period.source_timezone,
            consumer_status=parsed.consumer_status.value,
            status_as_of=_utc(parsed.status_as_of),
            recorded_at=datetime.now(timezone.utc),
            supersedes_reporting_status_id=parsed.supersedes_reporting_status_id,
            reporting_obligation_id=parsed.reporting_obligation_id,
            reporting_revision_id=parsed.reporting_revision_id,
            observed_revision_content_sha256=parsed.observed_revision_content_sha256,
            failure_code=parsed.failure_code.value if parsed.failure_code else None,
            consumer_commit_ref=parsed.consumer_commit_ref,
            seller_ledger_snapshot_id=parsed.seller_ledger_snapshot_id,
            seller_ledger_as_of=(
                _utc(parsed.seller_ledger_as_of) if parsed.seller_ledger_as_of else None
            ),
        )


def _failed(status_id: str, code: str, message: str) -> dict[str, Any]:
    return {
        "result": "failed",
        "reporting_status_id": status_id,
        "errors": [{"code": code, "message": message}],
    }


def _to_wire(record: ConsumerStatusRecord) -> dict[str, Any]:
    from adcp.reporting.ledger.status import _consumer_status_to_wire

    return _consumer_status_to_wire(record)


def project_consumer_mismatch(
    *,
    obligation: ReportingObligationRecord,
    current_revision: ReportingRevisionRecord | None,
    statuses: Sequence[ConsumerStatusRecord],
    seller_health: ReportingHealth,
) -> ReportingIssue | None:
    """Compare the seller's projection with this caller's current statement.

    Returns an issue only when they genuinely conflict:

    * any negative current status (``obligation_missing``, ``revision_missing``,
      ``unreadable``) against an otherwise ``healthy`` / ``complete`` seller
      projection; or
    * ``received`` naming a revision that is **no longer the current required
      revision** after a seller restatement -- the buyer consumed real content,
      but the seller has since superseded it, so the buyer is working from data
      it does not know is stale.

    Missing consumer status stays *unknown*.  It never excuses seller reporting
    and never by itself degrades seller health: a buyer that has not integrated
    the loop is not evidence of anything.
    """
    current = next((item for item in statuses if not item.superseded), None)
    if current is None:
        return None
    if seller_health not in {"healthy", "complete"}:
        # The seller already knows something is wrong and has said so. Adding a
        # second issue for the same condition would double-count it.
        return None

    if current.consumer_status != "received":
        return _mismatch_issue(
            obligation,
            current,
            responsible_party=_responsible_for(current),
            message=(
                f"the authenticated consumer reports {current.consumer_status} for this "
                "period while the seller projects healthy reporting"
            ),
        )

    if (
        current_revision is not None
        and current.reporting_revision_id is not None
        and current.reporting_revision_id != current_revision.reporting_revision_id
    ):
        return _mismatch_issue(
            obligation,
            current,
            responsible_party="buyer",
            message=(
                "the authenticated consumer received revision "
                f"{current.reporting_revision_id} but the current required revision is "
                f"{current_revision.reporting_revision_id}; the consumer is working from a "
                "superseded restatement"
            ),
        )
    return None


def _responsible_for(status: ConsumerStatusRecord) -> ResponsibleParty:
    """Diagnose who must act, from the typed failure rather than from prose.

    ``access_denied`` and ``reader_incompatible`` describe the consumer's own
    access or reader; everything else points at the seller's production or
    publication.  A seller with better diagnostics should override this.
    """
    if status.consumer_status == "unreadable" and status.failure_code in {
        "access_denied",
        "reader_incompatible",
    }:
        return "buyer"
    if status.consumer_status == "unreadable" and status.failure_code == "transport_failed":
        return "provider"
    return "seller"


def _mismatch_issue(
    obligation: ReportingObligationRecord,
    status: ConsumerStatusRecord,
    *,
    responsible_party: ResponsibleParty,
    message: str,
) -> ReportingIssue:
    return ReportingIssue(
        issue_id=issue_id_for(
            "core-consumer-status-mismatch-v1",
            obligation.reporting_obligation_id,
            status.reporting_status_id,
        ),
        code="CONSUMER_STATUS_MISMATCH",
        severity="action_required",
        responsible_party=responsible_party,
        recommended_action=("repair_access" if responsible_party == "buyer" else "contact_seller"),
        reporting_obligation_id=obligation.reporting_obligation_id,
        delivery_config_id=obligation.delivery_config_id,
        delivery_config_version=obligation.delivery_config_version,
        feed_purpose=obligation.feed_purpose,
        media_buy_ids=obligation.media_buy_ids,
        period_start=obligation.period.start,
        period_end=obligation.period.end,
        expected_at=obligation.period.expected_at,
        reporting_status_id=status.reporting_status_id,
        message=message,
    )


def consumer_status_chain_id(
    *,
    account_id: str,
    consumer_id: str,
    delivery_config_id: str,
    version: int,
    period_end: datetime,
) -> str:
    """A stable id for one logical status chain, for logs and metrics."""
    payload = f"{account_id}|{consumer_id}|{delivery_config_id}|{version}|{_utc(period_end)}"
    return "rpsc_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
