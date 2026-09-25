"""Buyer adjustment evidence and deterministic receipt construction.

These primitives do not authenticate a transport, select a revision/history leaf,
or persist submission intents. The caller supplies trusted scope and a selected
official revision from a complete authorized history. Capture the raw adjustment
before domain-model normalization; a model dump is never raw evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, NoReturn

import rfc8785

from adcp.reporting.evidence import (
    aware_utc,
    consumer_reference,
    principal_reference,
    reporting_identifier,
)
from adcp.types import (
    ReportingAdjustment,
    ReportingAdjustmentReceipt,
    ReportingObligation,
    ReportingRevision,
)
from adcp.validation.schema_loader import get_named_validator

__all__ = [
    "ReportingAdjustmentEvidence",
    "ReportingAdjustmentEvidenceError",
    "ReportingAdjustmentEvidenceLimits",
    "ReportingAdjustmentReceiptContext",
    "ReportingAdjustmentScope",
    "build_reporting_adjustment_receipt",
    "capture_reporting_adjustment_evidence",
]

ErrorCode = Literal[
    "INVALID_EVIDENCE",
    "EVIDENCE_LIMIT_EXCEEDED",
    "TYPED_EVIDENCE_MISMATCH",
    "INVALID_CONTEXT",
    "ADJUSTMENT_CONTEXT_MISMATCH",
    "INVALID_RECEIPT",
    "RECEIPT_TERMINAL",
    "SCHEMA_UNAVAILABLE",
]


class ReportingAdjustmentEvidenceError(ValueError):
    """Closed diagnostics; no input, decoder error or validation context."""

    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: ErrorCode) -> NoReturn:
    raise ReportingAdjustmentEvidenceError(code) from None


@dataclass(frozen=True, slots=True)
class ReportingAdjustmentScope:
    """Caller-asserted trusted identities, never derived from response fields.

    Constructing this value does not establish authentication or authorization.
    Resolve aliases before capture; these identities are preserved verbatim.
    """

    seller_identity: str = field(repr=False)
    account_id: str = field(repr=False)
    consumer_id: str = field(repr=False)
    reporting_obligation_id: str = field(repr=False)

    def __post_init__(self) -> None:
        valid = False
        try:
            consumer_reference(self.seller_identity)
            principal_reference(self.account_id)
            consumer_reference(self.consumer_id)
            reporting_identifier(self.reporting_obligation_id)
            valid = True
        except (ValueError, TypeError):
            pass
        if not valid:
            _fail("INVALID_CONTEXT")


@dataclass(frozen=True, slots=True)
class ReportingAdjustmentEvidenceLimits:
    """Caller-selected admission bounds, checked before schema/model work.

    Defaults are conservative; there is no separate SDK-wide ceiling on trusted
    caller configuration. Raising these limits expands the admitted workload.
    """

    max_bytes: int = 65_536
    max_depth: int = 12
    max_nodes: int = 4096

    def __post_init__(self) -> None:
        if any(
            type(v) is not int or v < 1 for v in (self.max_bytes, self.max_depth, self.max_nodes)
        ):
            _fail("INVALID_EVIDENCE")


def _bounded(value: object, limits: ReportingAdjustmentEvidenceLimits) -> None:
    pending = [(value, 0)]
    seen: set[int] = set()
    nodes = size = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > limits.max_nodes or depth > limits.max_depth:
            _fail("EVIDENCE_LIMIT_EXCEEDED")
        if type(item) in (dict, list):
            if id(item) in seen:
                _fail("INVALID_EVIDENCE")
            seen.add(id(item))
            size += 2
            if type(item) is dict:
                if len(item) > limits.max_nodes:
                    _fail("EVIDENCE_LIMIT_EXCEEDED")
                for key, child in item.items():
                    if type(key) is not str:
                        _fail("INVALID_EVIDENCE")
                    pending.extend(((key, depth + 1), (child, depth + 1)))
            elif type(item) is list:
                if len(item) > limits.max_nodes:
                    _fail("EVIDENCE_LIMIT_EXCEEDED")
                pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            if len(item) > limits.max_bytes:
                _fail("EVIDENCE_LIMIT_EXCEEDED")
            size += len(item.encode("utf-8"))
        elif item is None or type(item) is bool:
            size += 5
        elif type(item) is int:
            if abs(item) > 9_007_199_254_740_991:
                _fail("INVALID_EVIDENCE")
            size += 20
        elif type(item) is float:
            if not math.isfinite(item):
                _fail("INVALID_EVIDENCE")
            size += 24
        else:
            _fail("INVALID_EVIDENCE")
        if size > limits.max_bytes:
            _fail("EVIDENCE_LIMIT_EXCEEDED")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("INVALID_EVIDENCE")
        result[key] = value
    return result


def _nonfinite(_value: str) -> NoReturn:
    _fail("INVALID_EVIDENCE")


def _instant(parsed: datetime, raw: str | None = None) -> tuple[datetime, Decimal]:
    """Compare RFC3339 instants without losing sub-microsecond wire precision."""
    fraction = re.search(r"\.(\d+)", raw) if raw is not None else None
    digits = fraction.group(1) if fraction else f"{parsed.microsecond:06d}"
    return aware_utc(parsed).replace(microsecond=0), Decimal("0." + digits)


def _schema_valid(value: dict[str, Any], name: str) -> bool:
    valid: bool | None = None
    try:
        validator = get_named_validator("core/" + name + ".json")
        if validator is not None:
            valid = bool(validator.is_valid(value))
    except Exception:
        # Resolver errors can include evidence values. Expose only a closed code.
        valid = None
    if valid is None:
        _fail("SCHEMA_UNAVAILABLE")
    return valid


@dataclass(frozen=True, slots=True)
class ReportingAdjustmentEvidence:
    """Immutable captured JSON; explicit access can expose private evidence.

    Prefer capture_reporting_adjustment_evidence. Construction revalidates the
    input, so serialized evidence may be restored with the same checks. ``bytes``
    records strict duplicate-key admission; ``mapping`` cannot prove what its
    upstream decoder discarded. Neither value proves transport authenticity.
    """

    scope: ReportingAdjustmentScope = field(repr=False)
    raw_json: bytes = field(repr=False)
    input_kind: Literal["bytes", "mapping"]
    limits: ReportingAdjustmentEvidenceLimits = field(
        default_factory=ReportingAdjustmentEvidenceLimits, repr=False
    )
    canonical_json: bytes = field(init=False, repr=False)
    observed_adjustment_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.scope) is not ReportingAdjustmentScope
            or type(self.limits) is not ReportingAdjustmentEvidenceLimits
            or type(self.raw_json) is not bytes
            or self.input_kind not in ("bytes", "mapping")
        ):
            _fail("INVALID_EVIDENCE")
        if len(self.raw_json) > self.limits.max_bytes:
            _fail("EVIDENCE_LIMIT_EXCEEDED")
        valid = False
        canonical = b""
        try:
            raw = json.loads(
                self.raw_json.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite
            )
            _bounded(raw, self.limits)
            if type(raw) is dict and _schema_valid(raw, "reporting-adjustment"):
                typed = ReportingAdjustment.model_validate(raw)
                names = [item.name for item in typed.control_total_deltas]
                valid = (
                    typed.canonical_adjustment_sha256 is not None
                    and len(names) == len(set(names))
                    and _instant(typed.accounting_period.start, raw["accounting_period"]["start"])
                    < _instant(typed.accounting_period.end, raw["accounting_period"]["end"])
                    and _instant(typed.correction_observed_at, raw["correction_observed_at"])
                    <= _instant(typed.created_at, raw["created_at"])
                )
                if valid:
                    canonical = rfc8785.dumps(
                        {
                            key: value
                            for key, value in raw.items()
                            if key != "canonical_adjustment_sha256"
                        }
                    )
        except ReportingAdjustmentEvidenceError:
            raise
        except (ValueError, TypeError, OverflowError, RecursionError):
            pass
        if not valid:
            _fail("INVALID_EVIDENCE")
        object.__setattr__(self, "canonical_json", canonical)
        object.__setattr__(
            self, "observed_adjustment_sha256", hashlib.sha256(canonical).hexdigest()
        )

    @property
    def adjustment(self) -> ReportingAdjustment:
        """Fresh mutable model; mutations cannot alter retained evidence."""
        return ReportingAdjustment.model_validate_json(self.raw_json)


def capture_reporting_adjustment_evidence(
    raw: bytes | Mapping[str, Any],
    *,
    typed_adjustment: ReportingAdjustment,
    scope: ReportingAdjustmentScope,
    limits: ReportingAdjustmentEvidenceLimits = ReportingAdjustmentEvidenceLimits(),
) -> ReportingAdjustmentEvidence:
    """Capture authenticated-ingress data supplied by the caller, then align views.

    Bytes retain the supplied spelling and reject duplicate keys. A mapping is
    only evidence of those supplied decoded values, not of original wire bytes,
    duplicate-key absence, or numeric information lost by its upstream decoder.
    Never supply a model dump or debug capture as the mapping.
    """
    if type(limits) is not ReportingAdjustmentEvidenceLimits:
        _fail("INVALID_EVIDENCE")
    encoded: bytes | None = None
    kind: Literal["bytes", "mapping"] = "bytes"
    try:
        if type(raw) is bytes:
            encoded = raw
        elif isinstance(raw, Mapping):
            kind = "mapping"
            if len(raw) > limits.max_nodes:
                _fail("EVIDENCE_LIMIT_EXCEEDED")
            value = dict(raw)
            _bounded(value, limits)
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except ReportingAdjustmentEvidenceError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError):
        pass
    if encoded is None:
        _fail("INVALID_EVIDENCE")
    evidence = ReportingAdjustmentEvidence(scope, encoded, kind, limits)
    aligned = False
    try:
        if type(typed_adjustment) is ReportingAdjustment:
            supplied = ReportingAdjustment.model_validate(
                typed_adjustment.model_dump(mode="json", warnings="error")
            )
            aligned = supplied == evidence.adjustment
    except (ValueError, TypeError, OverflowError, RecursionError):
        pass
    if not aligned:
        _fail("TYPED_EVIDENCE_MISMATCH")
    return evidence


@dataclass(frozen=True, slots=True)
class ReportingAdjustmentReceiptContext:
    """Caller-asserted selected official and exact ownership, not a selector.

    The caller must establish complete authorized history and the current leaf
    before using this primitive. from_selection validates its supplied binding;
    it cannot establish completeness or authorization from those objects alone.
    """

    scope: ReportingAdjustmentScope = field(repr=False)
    reporting_revision_id: str = field(repr=False)
    finalized_at: datetime = field(repr=False)
    control_total_units: tuple[tuple[str, str | None], ...] = field(repr=False)

    def __post_init__(self) -> None:
        valid = False
        try:
            if type(self.scope) is ReportingAdjustmentScope:
                reporting_identifier(self.reporting_revision_id)
                final = aware_utc(self.finalized_at)
                if type(self.control_total_units) not in (tuple, list) or any(
                    type(item) not in (tuple, list) for item in self.control_total_units
                ):
                    _fail("INVALID_CONTEXT")
                units = tuple(tuple(item) for item in self.control_total_units)
                valid = bool(units) and all(
                    len(item) == 2
                    and type(item[0]) is str
                    and (item[1] is None or type(item[1]) is str)
                    for item in units
                )
                valid = valid and len({item[0] for item in units}) == len(units)
                if valid:
                    object.__setattr__(self, "finalized_at", final)
                    object.__setattr__(self, "control_total_units", units)
        except (ValueError, TypeError, AttributeError):
            pass
        if not valid:
            _fail("INVALID_CONTEXT")

    @classmethod
    def from_selection(
        cls,
        scope: ReportingAdjustmentScope,
        *,
        obligation: ReportingObligation,
        revision: ReportingRevision,
        revision_owner: str,
    ) -> ReportingAdjustmentReceiptContext:
        """Use an explicit ownership binding, never infer identity from scope."""
        valid = False
        try:
            obligation = ReportingObligation.model_validate(
                obligation.model_dump(mode="json", warnings="error")
            )
            revision = ReportingRevision.model_validate(
                revision.model_dump(mode="json", warnings="error")
            )
            valid = (
                scope.reporting_obligation_id
                == revision_owner
                == obligation.reporting_obligation_id
                and scope.account_id == obligation.account_id == revision.account_id
                and str(obligation.feed_purpose) == "billing"
                and str(obligation.reconciliation_mode) == "consumer_receipt"
                and str(obligation.required_finality) == str(revision.finality) == "official"
                and revision.finalized_at is not None
                and revision.report_definition_id == obligation.report_definition_id
                and revision.reporting_profile == obligation.reporting_profile
                and revision.period.start == obligation.period.start
                and revision.period.end == obligation.period.end
                and revision.period.source_timezone == obligation.period.source_timezone
            )
        except (ValueError, TypeError, AttributeError):
            pass
        if not valid or revision.finalized_at is None:
            _fail("INVALID_CONTEXT")
        return cls(
            scope,
            revision.reporting_revision_id,
            revision.finalized_at,
            tuple((item.name, item.unit) for item in revision.control_totals),
        )


def build_reporting_adjustment_receipt(
    evidence: ReportingAdjustmentEvidence,
    context: ReportingAdjustmentReceiptContext,
    *,
    reporting_receipt_id: str,
    observed_at: datetime,
    current_receipt: ReportingAdjustmentReceipt | None = None,
    rejection_codes: tuple[str, ...] = (),
) -> ReportingAdjustmentReceipt:
    """Build one receipt; caller owns history proof, stable identity and persistence.

    current_receipt must be the already-verified current leaf, including for the
    same trusted consumer. A lone receipt cannot prove a complete chain. Missing
    current_receipt asserts no prior leaf; this function never discovers one.
    The caller also owns pinned calendar/report-definition semantic checks;
    report disagreement with ADJUSTMENT_SEMANTIC_MISMATCH. These primitives
    carry accounting evidence, never invoice, settlement or booking authority.
    """
    if (
        type(evidence) is not ReportingAdjustmentEvidence
        or type(context) is not ReportingAdjustmentReceiptContext
    ):
        _fail("INVALID_CONTEXT")
    adjustment = evidence.adjustment
    raw = json.loads(evidence.raw_json)
    units = dict(context.control_total_units)
    if (
        evidence.scope != context.scope
        or adjustment.adjusts_reporting_revision_id != context.reporting_revision_id
        or _instant(adjustment.correction_observed_at, raw["correction_observed_at"])
        < _instant(context.finalized_at)
        or any(
            item.name not in units or item.unit != units[item.name]
            for item in adjustment.control_total_deltas
        )
    ):
        _fail("ADJUSTMENT_CONTEXT_MISMATCH")
    allowed = {"ADJUSTMENT_DIGEST_MISMATCH", "ADJUSTMENT_SEMANTIC_MISMATCH"}
    if type(rejection_codes) is not tuple or any(
        type(code) is not str or code not in allowed for code in rejection_codes
    ):
        _fail("INVALID_RECEIPT")
    failures = set(rejection_codes)
    if evidence.observed_adjustment_sha256 != str(adjustment.canonical_adjustment_sha256).lower():
        failures.add("ADJUSTMENT_DIGEST_MISMATCH")
    payload: dict[str, Any] = {
        "reporting_receipt_id": reporting_receipt_id,
        "reporting_adjustment_id": adjustment.reporting_adjustment_id,
        "adjusts_reporting_revision_id": adjustment.adjusts_reporting_revision_id,
        "status": "rejected" if failures else "accepted",
        "observed_adjustment_sha256": evidence.observed_adjustment_sha256,
    }
    receipt: ReportingAdjustmentReceipt | None = None
    try:
        reporting_identifier(reporting_receipt_id)
        moment = aware_utc(observed_at)
        if _instant(moment) < _instant(adjustment.created_at, raw["created_at"]):
            _fail("INVALID_RECEIPT")
        payload["observed_at"] = moment.isoformat()
        if failures:
            payload["rejection_codes"] = sorted(failures)
        if current_receipt is not None:
            leaf = ReportingAdjustmentReceipt.model_validate(
                current_receipt.model_dump(mode="json", exclude_none=True, warnings="error")
            )
            wire = leaf.model_dump(mode="json", exclude_none=True)
            if (
                not _schema_valid(wire, "reporting-adjustment-receipt")
                or leaf.reporting_adjustment_id != adjustment.reporting_adjustment_id
                or leaf.adjusts_reporting_revision_id != adjustment.adjusts_reporting_revision_id
                or leaf.reporting_receipt_id == reporting_receipt_id
                or leaf.observed_at > moment
            ):
                _fail("INVALID_RECEIPT")
            if str(leaf.status) == "accepted":
                _fail("RECEIPT_TERMINAL")
            payload["supersedes_reporting_receipt_id"] = leaf.reporting_receipt_id
        if _schema_valid(payload, "reporting-adjustment-receipt"):
            receipt = ReportingAdjustmentReceipt.model_validate(payload)
    except ReportingAdjustmentEvidenceError:
        raise
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    if receipt is None:
        _fail("INVALID_RECEIPT")
    return receipt
