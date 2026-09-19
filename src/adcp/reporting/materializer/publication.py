"""SDK-owned canonical evidence before an immutable source revision is committed."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

from adcp.reporting.evidence import ReportingCanonicalDigest
from adcp.reporting.ledger.models import ReportingObligationRecord, ReportingRevisionRecord
from adcp.reporting.materializer.contracts import failure
from adcp.reporting.materializer.verification import ReportingRevisionVerifier, _same_definition


def verified_publication(
    verifier: ReportingRevisionVerifier,
    obligation: ReportingObligationRecord,
    revision: ReportingRevisionRecord,
    rows: Sequence[dict[str, Any]],
) -> ReportingRevisionRecord:
    """Validate the source rows and totals; never reinterpret an existing revision.

    Core publications retain their original representation when no verifier is
    installed. Production publications use the exact same installed contract as
    destination verification, before the first immutable commit.
    """
    from adcp.reporting.ledger.producer import revision_content_sha256

    key = verifier.key
    if (
        not _same_definition(key, obligation.definition)
        or (key.report_definition_id, key.reporting_profile)
        != (obligation.report_definition_id, obligation.reporting_profile)
        or revision.row_count != len(rows)
    ):
        raise failure("SOURCE_INVALID")
    encoded, totals = verifier.canonicalize(rows)
    expected = {t.name: Decimal(t.value) for t in totals}
    actual = {name: Decimal(value) for name, value in revision.control_totals}
    if len(actual) != len(revision.control_totals) or expected != actual:
        raise failure("SOURCE_INVALID")
    contract = key.canonicalization
    pairs = tuple((t.name, t.value) for t in totals)
    return replace(
        revision,
        control_totals=pairs,
        managed_control_totals=totals,
        canonical_content_digest=ReportingCanonicalDigest(
            hashlib.sha256(b"[" + b",".join(encoded) + b"]").hexdigest(),
            contract.canonicalization_id,
            contract.canonicalization_uri,
            contract.canonicalization_sha256,
        ),
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision.reporting_revision_id,
            row_count=revision.row_count,
            control_totals=pairs,
            reporting_rows=rows,
            control_total_evidence=totals,
        ),
    )
