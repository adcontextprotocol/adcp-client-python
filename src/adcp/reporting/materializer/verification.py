"""SDK-owned verification of frozen source content and actual destination reads.

The registry contains immutable, explicitly installed bytes. It never fetches a
URI or trusts a writer's digest as proof. B1 returns evidence only; B2 must
reselect and fence under its final account transaction before publishing it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator

from adcp.reporting.evidence import ReportingCanonicalDigest, ReportingControlTotalRecord
from adcp.reporting.ledger.delivery_models import (
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingObligationDeliveryRecord,
    ReportingPhysicalChecksum,
    ReportingResourceRecord,
    ReportingVerificationRecord,
    _ClosedValue,
    _freeze_fields,
)
from adcp.reporting.ledger.models import ReportingObligationRecord, ReportingRevisionRecord
from adcp.reporting.ledger.producer import revision_content_sha256
from adcp.reporting.ledger.store import LedgerConflictError, ReportingRowPage, revision_row_offset
from adcp.reporting.materializer._json import (
    ReportingVerificationLimits,
    parse_reporting_json,
    strict_reporting_json,
)
from adcp.reporting.materializer.contracts import (
    ReportingDestinationLocator,
    ReportingDestinationPage,
    ReportingDestinationRequest,
    ReportingDestinationResolver,
    ReportingDestinationSession,
    ReportingIOContext,
    ReportingIOPhase,
    ReportingNativeObservation,
    ReportingPreparedRevision,
    ReportingVerificationKey,
    ReportingWriterError,
    ReportingWriterFailure,
    _close_owned,
    binding_fingerprint,
    failure,
    object_path,
)
from adcp.reporting.revision_selection import select_reporting_revision


class ReportingRevisionRowReader(Protocol):
    async def read_revision_rows(
        self,
        *,
        account_id: str,
        reporting_revision_id: str,
        cursor: str | None = None,
        limit: int = 500,
    ) -> ReportingRowPage: ...


@dataclass(frozen=True, slots=True)
class ReportingVerifiedDestination(_ClosedValue):
    """Immutable SDK observations. B2 alone owns atomic publication/readiness."""

    request: ReportingDestinationRequest
    resource: ReportingResourceRecord
    verification: ReportingVerificationRecord

    def __post_init__(self) -> None:
        _freeze_fields(self)


def validate_materialization_target(
    prepared: ReportingPreparedRevision,
    *,
    binding: ReportingDestinationBinding,
    revisions: Sequence[ReportingRevisionRecord],
) -> None:
    """Pure B2 finish seam. Caller must supply its locked, complete current history."""
    selection = select_reporting_revision(
        revisions,
        account_id=prepared.request.principal.account_id,
        reporting_obligation_id=prepared.request.reporting_obligation_id,
        required_finality=prepared.obligation.required_finality,
    )
    if selection.kind == "corrupt":
        raise failure("HISTORY_CORRUPT")
    if (
        selection.kind != "selected"
        or not selection.revision.readable
        or selection.revision != prepared.revision
        or binding_fingerprint(binding) != prepared.request.binding_fingerprint
    ):
        raise failure("CURRENT_REVISION_CHANGED")


@dataclass(frozen=True, slots=True)
class ReportingRevisionVerifier(_ClosedValue):
    """One exact installed contract using the SDK's safe-integer JCS subset.

    Sum totals are declared on schema properties with ``x-adcp-control-total``
    (value_type, optional unit, decimal scale). This explicit pinned subset has
    no expression evaluator, custom callbacks, mutable registration or network.
    Unsupported formats and definition semantics fail during construction.
    """

    key: ReportingVerificationKey
    definition_bytes: bytes = field(repr=False)
    schema_bytes: bytes = field(repr=False)
    canonicalization_bytes: bytes = field(repr=False)
    limits: ReportingVerificationLimits = field(default_factory=ReportingVerificationLimits)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        error = False
        try:
            self._runtime()
        except Exception:
            error = True
        if error:
            raise failure("UNSUPPORTED_VERIFICATION")

    def _runtime(self) -> _Canonicalizer:
        key, definition = self.key, self.key.definition
        if key.capability.format not in {"jsonl", None}:
            raise failure("UNSUPPORTED_VERIFICATION")
        if (
            key.capability.method == "file_transfer"
            and key.capability.immutability != "immutable_location"
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        if (
            definition.schema_dialect != "https://json-schema.org/draft/2020-12/schema"
            or definition.schema_ref_policy != "local_fragment_only"
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        for raw, expected in (
            (self.definition_bytes, definition.report_definition_sha256),
            (self.schema_bytes, definition.schema_sha256),
            (self.canonicalization_bytes, key.canonicalization.canonicalization_sha256),
        ):
            if hashlib.sha256(raw).hexdigest() != expected.lower():
                raise failure("UNSUPPORTED_VERIFICATION")
        report = _mapping(parse_reporting_json(self.definition_bytes, self.limits))
        schema = _mapping(parse_reporting_json(self.schema_bytes, self.limits))
        contract = _mapping(parse_reporting_json(self.canonicalization_bytes, self.limits))
        if (
            report.get("report_definition_id") != key.report_definition_id
            or report.get("reporting_profile") != key.reporting_profile
            or schema.get("$schema") != definition.schema_dialect
            or set(contract)
            != {
                "contract_version",
                "media_type",
                "algorithm",
                "schema_sha256",
                "primary_keys",
                "golden_vectors",
            }
            or contract["contract_version"] != "1.0"
            or contract["media_type"] != "application/vnd.adcp.reporting-canonicalization+json"
            or contract["algorithm"] != "adcp_jcs_rows_v1"
            or contract["schema_sha256"].lower() != definition.schema_sha256.lower()
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        _local_schema(schema)
        Draft202012Validator.check_schema(schema)
        keys = contract["primary_keys"]
        if (
            type(keys) is not list
            or not keys
            or any(type(k) is not str for k in keys)
            or len(set(keys)) != len(keys)
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        totals: list[_Total] = []
        properties = _mapping(schema.get("properties"))
        metrics = report.get("metrics")
        if type(metrics) is not list or not metrics:
            raise failure("UNSUPPORTED_VERIFICATION")
        for metric in metrics:
            metric = _mapping(metric)
            name = metric["name"]
            prop = _mapping(properties[name])
            total = _mapping(prop["x-adcp-control-total"])
            if (
                metric.get("source_expression") != name
                or metric.get("aggregation") != "sum"
                or set(total) - {"value_type", "unit", "scale"}
                or total.get("unit") != metric.get("unit")
                or (total["value_type"], prop.get("type"))
                not in {("integer", "integer"), ("decimal", "string")}
            ):
                raise failure("UNSUPPORTED_VERIFICATION")
            scale = total.get("scale", 0)
            if (
                type(scale) is not int
                or not 0 <= scale <= 18
                or (total["value_type"] == "integer" and scale != 0)
            ):
                raise failure("UNSUPPORTED_VERIFICATION")
            totals.append(_Total(name, total["value_type"], total.get("unit"), scale))
        if len({t.name for t in totals}) != len(totals):
            raise failure("UNSUPPORTED_VERIFICATION")
        if {t.name for t in totals} != {
            name
            for name, prop in properties.items()
            if type(prop) is dict and "x-adcp-control-total" in prop
        }:
            raise failure("UNSUPPORTED_VERIFICATION")
        if not set(keys).union(t.name for t in totals) <= set(schema.get("required", [])):
            raise failure("UNSUPPORTED_VERIFICATION")
        units = {t.name: t.unit for t in totals}
        if any(
            units.get(name) != unit
            for name, unit in (
                *definition.monetary_metric_units,
                *definition.monetary_control_total_units,
            )
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        runtime = _Canonicalizer(
            tuple(keys), tuple(totals), Draft202012Validator(schema), self.limits
        )
        vectors = _mapping(contract["golden_vectors"])
        if not {"empty_report", "ordering_encoding"} <= set(vectors) or set(vectors) - {
            "empty_report",
            "ordering_encoding",
            "additional",
        }:
            raise failure("UNSUPPORTED_VERIFICATION")
        seen: set[str] = set()
        for vector in [
            vectors["empty_report"],
            vectors["ordering_encoding"],
            *vectors.get("additional", []),
        ]:
            vector = _mapping(vector)
            if (
                set(vector) != {"name", "purpose", "input_rows", "canonical_utf8_base64", "sha256"}
                or vector["name"] in seen
            ):
                raise failure("UNSUPPORTED_VERIFICATION")
            seen.add(vector["name"])
            rows, _ = runtime.canonicalize(vector["input_rows"])
            golden_bytes = base64.b64decode(vector["canonical_utf8_base64"], validate=True)
            if (
                b"[" + b",".join(rows) + b"]" != golden_bytes
                or hashlib.sha256(golden_bytes).hexdigest() != vector["sha256"].lower()
            ):
                raise failure("UNSUPPORTED_VERIFICATION")
        if (
            vectors["empty_report"]["input_rows"] != []
            or vectors["empty_report"]["purpose"] != "empty_report"
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        ordering = vectors["ordering_encoding"]
        order_rows = ordering["input_rows"]
        if (
            ordering["purpose"] != "ordering_encoding"
            or len(order_rows) < 2
            or [runtime.order_key(r) for r in order_rows]
            == sorted(runtime.order_key(r) for r in order_rows)
            or not _unordered_members(order_rows)
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        return runtime

    def canonicalize(
        self, rows: Sequence[object]
    ) -> tuple[tuple[bytes, ...], tuple[ReportingControlTotalRecord, ...]]:
        """Useful to trusted publishers preparing expected evidence before storage."""
        return self._runtime().canonicalize(rows)


@dataclass(frozen=True, slots=True)
class ReportingRevisionVerifierRegistry(_ClosedValue):
    verifiers: tuple[ReportingRevisionVerifier, ...]

    def __post_init__(self) -> None:
        _freeze_fields(self)
        if len({v.key for v in self.verifiers}) != len(self.verifiers):
            raise failure("UNSUPPORTED_VERIFICATION")

    def require(self, key: ReportingVerificationKey) -> ReportingRevisionVerifier:
        for verifier in self.verifiers:
            if verifier.key == key:
                return verifier
        raise failure("UNSUPPORTED_VERIFICATION")

    async def prepare(
        self,
        *,
        key: ReportingVerificationKey,
        binding: ReportingDestinationBinding,
        delivery: ReportingObligationDeliveryRecord,
        obligation: ReportingObligationRecord,
        revisions: Sequence[ReportingRevisionRecord],
        attempt: ReportingMaterializationAttempt,
        reader: ReportingRevisionRowReader,
        context: ReportingIOContext,
    ) -> ReportingPreparedRevision:
        verifier = self.require(key)  # Unsupported tuples fail before any source/resolver I/O.
        selection = select_reporting_revision(
            revisions,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            required_finality=obligation.required_finality,
        )
        if selection.kind == "corrupt":
            raise failure("HISTORY_CORRUPT")
        if selection.kind != "selected":
            raise failure("REVISION_NOT_READY")
        revision = selection.revision
        _revision_metadata(revision)
        if not revision.readable:
            raise failure("REVISION_NOT_READY")
        request = ReportingDestinationRequest.from_binding(binding, attempt, key)
        if (
            obligation.definition is None
            or replace(key, definition=obligation.definition) != key
            or (obligation.report_definition_id, obligation.reporting_profile)
            != (key.report_definition_id, key.reporting_profile)
            or request.generation != obligation.generation_key
            or request.reporting_obligation_id != obligation.reporting_obligation_id
            or request.reporting_revision_id != revision.reporting_revision_id
            or delivery.scope != attempt.scope
            or delivery.currency != obligation.currency
        ):
            raise failure("BINDING_MISMATCH")
        _expected_digest(revision, key)
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        budget = _Budget(verifier.limits)
        while True:
            budget.page()
            page = await context.run(
                lambda: reader.read_revision_rows(
                    account_id=obligation.account_id,
                    reporting_revision_id=revision.reporting_revision_id,
                    cursor=cursor,
                    limit=500,
                )
            )
            if (
                type(page) is not ReportingRowPage
                or type(page.rows) is not tuple
                or page.reporting_revision_id != revision.reporting_revision_id
            ):
                raise failure("SOURCE_INVALID")
            _page(
                page.total_count,
                page.has_more,
                page.cursor,
                len(page.rows),
                len(rows),
                revision.row_count,
                seen,
            )
            if page.cursor is not None:
                cursor_invalid = False
                try:
                    cursor_invalid = revision_row_offset(
                        page.cursor, revision.reporting_revision_id, 500
                    ) != len(rows) + len(page.rows)
                except (ValueError, LedgerConflictError):
                    cursor_invalid = True
                if cursor_invalid:
                    raise failure("SOURCE_INVALID")
            for row in page.rows:
                encoded = strict_reporting_json(row, verifier.limits)
                budget.add(encoded)
                rows.append(_mapping(parse_reporting_json(encoded, verifier.limits)))
            if not page.has_more:
                break
            cursor = page.cursor
        if (
            revision_content_sha256(
                reporting_revision_id=revision.reporting_revision_id,
                row_count=revision.row_count,
                control_totals=revision.control_totals,
                reporting_rows=rows,
                control_total_evidence=revision.managed_control_totals,
            )
            != revision.revision_content_sha256.lower()
        ):
            raise failure("SOURCE_INVALID")
        canonical_rows, totals = verifier.canonicalize(rows)
        _verify_content(revision, key, canonical_rows, totals)
        await context.run(_checkpoint)
        return ReportingPreparedRevision(
            request, obligation, revision, delivery, binding, canonical_rows
        )


@dataclass(frozen=True, slots=True)
class ReportingDestinationIO:
    """Explicit one-shot write/readback operations; no coordinator or lease loop."""

    registry: ReportingRevisionVerifierRegistry
    resolver: ReportingDestinationResolver = field(repr=False)

    @asynccontextmanager
    async def _session(
        self,
        prepared: ReportingPreparedRevision,
        phase: ReportingIOPhase,
        context: ReportingIOContext,
    ) -> AsyncIterator[ReportingDestinationSession]:
        self.registry.require(prepared.request.verification_key)
        problem = False
        canceled = False
        try:
            session = self.resolver.resolve(prepared.request, phase=phase, context=context)
        except asyncio.CancelledError:
            canceled = True
        except Exception:
            problem = True
        if canceled:
            raise asyncio.CancelledError
        if problem:
            raise ReportingWriterError(
                ReportingWriterFailure("RESOURCE_UNAVAILABLE", "same_identity")
            )
        if not isinstance(session, ReportingDestinationSession):
            raise failure("BINDING_MISMATCH")
        try:
            _session_binding(session, prepared.request, phase, context)
        except ReportingWriterError:
            problem = True
        if problem:
            await session.aclose()
            raise failure("BINDING_MISMATCH")
        failure_record: ReportingWriterFailure | None = None
        canceled = False
        try:
            async with session:
                _session_binding(session, prepared.request, phase, context)
                yield session
        except asyncio.CancelledError:
            canceled = True
        except ReportingWriterError as exc:
            failure_record = exc.failure
        except Exception:
            failure_record = ReportingWriterFailure(
                "RESOURCE_UNAVAILABLE", "same_identity", "unknown"
            )
        if canceled:
            raise asyncio.CancelledError
        if failure_record is not None:
            raise ReportingWriterError(failure_record)

    async def write(
        self, prepared: ReportingPreparedRevision, *, context: ReportingIOContext
    ) -> ReportingDestinationLocator:
        self._validate_prepared(prepared)
        problem: ReportingWriterFailure | None = None
        canceled = False
        try:
            async with self._session(prepared, "write", context) as session:
                _session_binding(session, prepared.request, "write", context)
                locator = await context.run(lambda: session.write(prepared), effect="unknown")
                invalid_locator = False
                try:
                    _locator_binding(locator, prepared.request)
                except ReportingWriterError:
                    invalid_locator = True
                if invalid_locator:
                    raise ReportingWriterError(
                        ReportingWriterFailure("BINDING_MISMATCH", "same_identity", "unknown")
                    )
                return locator
        except asyncio.CancelledError:
            canceled = True
        except ReportingWriterError as exc:
            problem = exc.failure
        if canceled:
            raise asyncio.CancelledError
        assert problem is not None
        raise ReportingWriterError(problem)

    def _validate_prepared(self, prepared: ReportingPreparedRevision) -> ReportingRevisionVerifier:
        verifier = self.registry.require(prepared.request.verification_key)
        request, obligation, binding = prepared.request, prepared.obligation, prepared.binding
        cap = request.verification_key.capability
        if (
            binding_fingerprint(binding) != request.binding_fingerprint
            or request.principal != binding.principal
            or request.generation != binding.generation_key
            or request.destination_ref != binding.destination_ref
            or request.trusted_binding_ref != binding.trusted_binding_ref
            or request.generation != obligation.generation_key
            or request.reporting_obligation_id != obligation.reporting_obligation_id
            or request.reporting_revision_id != prepared.revision.reporting_revision_id
            or prepared.revision.account_id != obligation.account_id
            or prepared.revision.reporting_obligation_id != obligation.reporting_obligation_id
            or not prepared.revision.readable
            or prepared.delivery.scope.principal != request.principal
            or prepared.delivery.scope.generation_key != request.generation
            or prepared.delivery.scope.reporting_obligation_id != request.reporting_obligation_id
            or prepared.delivery.currency != obligation.currency
            or obligation.definition is None
            or replace(verifier.key, definition=obligation.definition) != verifier.key
            or (obligation.report_definition_id, obligation.reporting_profile)
            != (verifier.key.report_definition_id, verifier.key.reporting_profile)
            or (binding.method, binding.transport, binding.format, binding.verification_profile)
            != (cap.method, cap.transport, cap.format, cap.verification_profile)
            or (binding.success_status == "delivered" and cap.verification_path != "destination")
        ):
            raise failure("BINDING_MISMATCH")
        rows, totals = verifier.canonicalize(
            [parse_reporting_json(r, verifier.limits) for r in prepared.rows]
        )
        if rows != prepared.rows:
            raise failure("SOURCE_INVALID")
        _verify_content(prepared.revision, verifier.key, rows, totals)
        return verifier

    async def verify(
        self,
        prepared: ReportingPreparedRevision,
        locator: ReportingDestinationLocator,
        *,
        context: ReportingIOContext,
    ) -> ReportingVerifiedDestination:
        verifier = self._validate_prepared(prepared)
        _locator_binding(locator, prepared.request)
        problem: ReportingWriterFailure | None = None
        canceled = False
        try:
            async with self._session(prepared, "readback", context) as session:
                _session_binding(session, prepared.request, "readback", context)
                return await _verify_destination(verifier, prepared, locator, session, context)
        except asyncio.CancelledError:
            canceled = True
        except ReportingWriterError as exc:
            problem = exc.failure
        if canceled:
            raise asyncio.CancelledError
        assert problem is not None
        if problem.code == "SOURCE_INVALID":
            problem = ReportingWriterFailure("DESTINATION_CORRUPT", "new_attempt", "applied")
        raise ReportingWriterError(problem)


def _session_binding(
    session: ReportingDestinationSession,
    request: ReportingDestinationRequest,
    phase: ReportingIOPhase,
    context: ReportingIOContext,
) -> None:
    if session.request != request or session.phase != phase or session.context is not context:
        raise failure("BINDING_MISMATCH")


def _locator_binding(
    locator: ReportingDestinationLocator, request: ReportingDestinationRequest
) -> None:
    if (
        type(locator) is not ReportingDestinationLocator
        or locator.external_id != request.external_id
        or locator.binding_fingerprint != request.binding_fingerprint
    ):
        raise failure("BINDING_MISMATCH")
    # Logical row readers may use these paths before reading the manifest.
    # Reject traversal before authorization or any readback method can see it.
    invalid = False
    try:
        for ref in locator.resource.object_refs:
            object_path(ref)
    except ValueError:
        invalid = True
    if invalid:
        raise failure("BINDING_MISMATCH")


def _revision_metadata(revision: ReportingRevisionRecord) -> None:
    # Foundation records are source compatible dataclasses, not the strict
    # destination boundary. Never let Python's bool/int equality bless counts
    # or readability flags from a custom/legacy reader.
    if (
        type(revision) is not ReportingRevisionRecord
        or type(revision.row_count) is not int
        or revision.row_count < 0
        or type(revision.readable) is not bool
        or type(revision.readable_at_commit) is not bool
        or type(revision.revision_content_sha256) is not str
        or re.fullmatch(r"[0-9a-fA-F]{64}", revision.revision_content_sha256) is None
    ):
        raise failure("SOURCE_INVALID")


def _expected_digest(
    revision: ReportingRevisionRecord, key: ReportingVerificationKey
) -> ReportingCanonicalDigest:
    _revision_metadata(revision)
    digest, canonical = revision.canonical_content_digest, key.canonicalization
    if digest is None or (
        digest.canonicalization_id,
        digest.canonicalization_uri,
        digest.canonicalization_sha256.lower(),
    ) != (
        canonical.canonicalization_id,
        canonical.canonicalization_uri,
        canonical.canonicalization_sha256,
    ):
        raise failure("UNSUPPORTED_VERIFICATION")
    return digest


def _verify_content(
    revision: ReportingRevisionRecord,
    key: ReportingVerificationKey,
    rows: tuple[bytes, ...],
    totals: tuple[ReportingControlTotalRecord, ...],
) -> None:
    expected = _expected_digest(revision, key)
    if (
        len(rows) != revision.row_count
        or _digest_rows(rows) != expected.value.lower()
        or totals != revision.managed_control_totals
    ):
        raise failure("SOURCE_INVALID")


def _digest_rows(rows: Sequence[bytes]) -> str:
    digest = hashlib.sha256(b"[")
    for index, row in enumerate(rows):
        if index:
            digest.update(b",")
        digest.update(row)
    digest.update(b"]")
    return digest.hexdigest()


def _mapping(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise failure("SOURCE_INVALID")
    return cast(dict[str, Any], value)


def _local_schema(schema: dict[str, Any]) -> None:
    stack: list[object] = [schema]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if any(k in value for k in ("$id", "$dynamicRef", "$recursiveRef", "$vocabulary")) or (
                "$ref" in value
                and (type(value["$ref"]) is not str or not value["$ref"].startswith("#"))
            ):
                raise failure("UNSUPPORTED_VERIFICATION")
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


@dataclass(frozen=True)
class _Total:
    name: str
    value_type: Any
    unit: str | None
    scale: int


@dataclass
class _Canonicalizer:
    keys: tuple[str, ...]
    totals: tuple[_Total, ...]
    validator: Any
    limits: ReportingVerificationLimits

    def order_key(self, row: object) -> bytes:
        row = _mapping(row)
        values = [row[k] for k in self.keys if k in row]
        if len(values) != len(self.keys) or any(
            type(v) not in {str, int, bool, type(None)} for v in values
        ):
            raise failure("SOURCE_INVALID")
        return strict_reporting_json(values, self.limits)

    def canonicalize(
        self, rows: Sequence[object]
    ) -> tuple[tuple[bytes, ...], tuple[ReportingControlTotalRecord, ...]]:
        budget = _Budget(self.limits)
        ordered: dict[bytes, bytes] = {}
        sums = [Decimal(0) for _ in self.totals]
        for row in rows:
            encoded = strict_reporting_json(row, self.limits)
            budget.add(encoded)
            value = _mapping(row)
            valid = False
            try:
                valid = self.validator.is_valid(value)
            except Exception:
                valid = False  # No schema resolver/validation diagnostics leave this boundary.
            if not valid:
                raise failure("SOURCE_INVALID")
            key = self.order_key(value)
            if key in ordered:
                raise failure("SOURCE_INVALID")
            ordered[key] = encoded
            for index, total in enumerate(self.totals):
                item = value.get(total.name)
                if total.value_type == "integer":
                    if type(item) is not int:
                        raise failure("SOURCE_INVALID")
                elif type(item) is not str or re_decimal(item, total.scale) is False:
                    raise failure("SOURCE_INVALID")
                with localcontext() as ctx:
                    ctx.prec = 128
                    sums[index] += Decimal(item)
        totals = tuple(
            ReportingControlTotalRecord(
                rule.name,
                format(value, f".{rule.scale}f"),
                rule.value_type,
                rule.unit,
            )
            for rule, value in zip(self.totals, sums)
        )
        return tuple(ordered[k] for k in sorted(ordered)), totals


def re_decimal(value: str, scale: int) -> bool:
    return bool(
        re.fullmatch(r"-?(?:0|[1-9][0-9]{0,37})" + (rf"\.[0-9]{{{scale}}}" if scale else ""), value)
    )


@dataclass
class _Budget:
    limits: ReportingVerificationLimits
    total_bytes: int = 0
    items: int = 0
    rows: int = 0
    pages: int = 0

    def page(self) -> None:
        self.pages += 1
        if self.pages > self.limits.max_pages:
            raise failure("LIMIT_EXCEEDED")

    def add(self, encoded: bytes) -> None:
        self.total_bytes += len(encoded)
        self.rows += 1
        stack: list[object] = [parse_reporting_json(encoded, self.limits)]
        while stack:
            item = stack.pop()
            self.items += 1
            if isinstance(item, dict):
                self.items += len(item)
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        if (
            self.total_bytes > self.limits.max_total_bytes
            or self.items > self.limits.max_items
            or self.rows > self.limits.max_rows
        ):
            raise failure("LIMIT_EXCEEDED")


def _page(
    total: int,
    has_more: bool,
    cursor: str | None,
    count: int,
    before: int,
    expected: int,
    seen: set[str],
) -> None:
    if (
        type(total) is not int
        or total != expected
        or type(has_more) is not bool
        or has_more != (cursor is not None)
        or count > 500
        or before + count > expected
        or (has_more and (count == 0 or before + count >= expected))
        or (not has_more and before + count != expected)
    ):
        raise failure("SOURCE_INVALID")
    if cursor is not None:
        if type(cursor) is not str or not cursor or len(cursor) > 2048 or cursor in seen:
            raise failure("SOURCE_INVALID")
        seen.add(cursor)


async def _verify_destination(
    verifier: ReportingRevisionVerifier,
    prepared: ReportingPreparedRevision,
    locator: ReportingDestinationLocator,
    session: ReportingDestinationSession,
    context: ReportingIOContext,
) -> ReportingVerifiedDestination:
    cap, resource = verifier.key.capability, locator.resource
    expected_kind = {
        "file_transfer": "manifest",
        "dataset_share": "dataset",
        "warehouse_materialization": "warehouse_relation",
    }[cap.method]
    if (
        resource.kind != expected_kind
        or resource.immutability != cap.immutability
        or resource.expires_at
        < max(
            prepared.delivery.resource_retained_until,
            datetime.now(timezone.utc) + timedelta(days=prepared.binding.resource_retention_days),
        )
        or resource.reader_compatibility != prepared.binding.reader_compatibility
    ):
        raise failure("SOURCE_INVALID")
    native: ReportingNativeObservation | None = None
    if cap.immutability == "native_version":
        native = await context.run(
            lambda: session.observe_native_version(locator), effect="unknown"
        )
        _native(native, resource, cap.verification_path)
    cursor: str | None = None
    seen: set[str] = set()
    budget = _Budget(verifier.limits)
    destination: list[object] = []
    while True:
        budget.page()
        page = await context.run(
            lambda: session.read_rows(locator, cursor=cursor, limit=500), effect="unknown"
        )
        if (
            type(page) is not ReportingDestinationPage
            or page.reporting_revision_id != prepared.request.reporting_revision_id
            or page.format != cap.format
            or page.verification_path != cap.verification_path
            or page.native_version_ref != (native.native_version_ref if native else None)
        ):
            raise failure("SOURCE_INVALID")
        _page(
            page.total_count,
            page.has_more,
            page.cursor,
            len(page.rows),
            len(destination),
            len(prepared.rows),
            seen,
        )
        for raw in page.rows:
            value = parse_reporting_json(raw, verifier.limits)
            row_bytes = strict_reporting_json(value, verifier.limits)
            budget.add(raw)
            if row_bytes != prepared.rows[len(destination)]:
                raise failure("SOURCE_INVALID")
            destination.append(value)
        if not page.has_more:
            break
        cursor = page.cursor
    encoded, totals = verifier.canonicalize(destination)
    _verify_content(prepared.revision, verifier.key, encoded, totals)
    checksums: tuple[ReportingPhysicalChecksum, ...] = ()
    manifest_digest: str | None = None
    if cap.method == "file_transfer":
        checksums, manifest_digest = await _verify_files(
            verifier, prepared, locator, session, context
        )
    if native is not None:
        observed = await context.run(
            lambda: session.observe_native_version(locator), effect="unknown"
        )
        _native(observed, resource, cap.verification_path)
        if observed != native:
            raise failure("SOURCE_INVALID")
    at = datetime.now(timezone.utc)
    canonical = verifier.key.canonicalization
    verification = ReportingVerificationRecord(
        verified_at=at,
        verification_path=cap.verification_path,
        verification_profile=cap.verification_profile,
        row_count=len(encoded),
        control_totals=totals,
        canonical_content_digest=(
            ReportingCanonicalDigest(
                _digest_rows(encoded),
                canonical.canonicalization_id,
                canonical.canonicalization_uri,
                canonical.canonicalization_sha256,
            )
            if cap.verification_profile == "canonical_digest"
            else None
        ),
        physical_checksums=checksums,
        native_version_ref=native.native_version_ref if native else None,
        native_observed_through=native.verification_path if native else None,
        verified_format=cap.format,
    )
    return ReportingVerifiedDestination(
        prepared.request, replace(resource, manifest_sha256=manifest_digest), verification
    )


def _native(
    observed: ReportingNativeObservation, resource: ReportingResourceRecord, path: str
) -> None:
    if type(observed) is not ReportingNativeObservation or (
        observed.location,
        observed.native_version_ref,
        observed.verification_path,
    ) != (resource.location, resource.native_version_ref, path):
        raise failure("SOURCE_INVALID")


async def _verify_files(
    verifier: ReportingRevisionVerifier,
    prepared: ReportingPreparedRevision,
    locator: ReportingDestinationLocator,
    session: ReportingDestinationSession,
    context: ReportingIOContext,
) -> tuple[tuple[ReportingPhysicalChecksum, ...], str]:
    raw = await context.run(lambda: session.read_manifest(locator), effect="unknown")
    if type(raw) is not bytes or len(raw) > verifier.limits.max_value_bytes:
        raise failure("SOURCE_INVALID")
    digest = hashlib.sha256(raw).hexdigest()
    if (
        locator.resource.manifest_sha256 is None
        or digest != locator.resource.manifest_sha256.lower()
    ):
        raise failure("SOURCE_INVALID")
    manifest = _mapping(parse_reporting_json(raw, verifier.limits))
    required = {
        "manifest_version",
        "complete",
        "reporting_revision_id",
        "reporting_obligation_id",
        "reporting_materialization_id",
        "period",
        "format",
        "compression",
        "files",
        "total_size_bytes",
        "row_count",
        "control_totals",
        "created_at",
    }
    period = prepared.obligation.period
    if (
        set(manifest) != required
        or manifest["manifest_version"] != "1.0"
        or manifest["complete"] is not True
        or manifest["reporting_revision_id"] != prepared.request.reporting_revision_id
        or manifest["reporting_obligation_id"] != prepared.request.reporting_obligation_id
        or manifest["reporting_materialization_id"] != prepared.request.reporting_materialization_id
        or manifest["format"] != verifier.key.capability.format
        or manifest["compression"] != "none"
        or manifest["period"]
        != {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "source_timezone": period.source_timezone,
        }
        or type(manifest["row_count"]) is not int
        or manifest["row_count"] != len(prepared.rows)
        or manifest["control_totals"]
        != [t.to_wire() for t in prepared.revision.managed_control_totals or ()]
        or type(manifest["total_size_bytes"]) is not int
        or manifest["total_size_bytes"] < 0
    ):
        raise failure("SOURCE_INVALID")
    created = None
    try:
        if type(manifest["created_at"]) is str and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
            manifest["created_at"],
        ):
            created = datetime.fromisoformat(
                manifest["created_at"].replace("Z", "+00:00").replace("z", "+00:00")
            )
    except ValueError:
        pass
    if (
        created is None
        or created.tzinfo is None
        or created < prepared.revision.created_at
        or created > datetime.now(timezone.utc)
    ):
        raise failure("SOURCE_INVALID")
    entries = manifest["files"]
    if type(entries) is not list or not 1 <= len(entries) <= verifier.limits.max_objects:
        raise failure("SOURCE_INVALID")
    refs = tuple(_checked_object_path(_mapping(entry).get("object_ref")) for entry in entries)
    inventory = await context.run(lambda: session.list_objects(locator), effect="unknown")
    if (
        len(set(refs)) != len(refs)
        or refs != locator.resource.object_refs
        or type(inventory) is not tuple
        or inventory != refs
    ):
        raise failure("SOURCE_INVALID")
    size, ordinal, chunks = 0, 0, 0
    checksums: list[ReportingPhysicalChecksum] = []
    for entry, ref in zip(entries, refs):
        if (
            set(entry) != {"object_ref", "size_bytes", "row_count", "sha256"}
            or type(entry["size_bytes"]) is not int
            or type(entry["row_count"]) is not int
            or entry["size_bytes"] < 0
            or entry["row_count"] < 0
        ):
            raise failure("SOURCE_INVALID")
        hashed, actual, count = hashlib.sha256(), 0, 0
        pending = b""
        stream = session.read_object(locator, object_ref=ref)
        try:
            while True:
                # StopAsyncIteration is a stream boundary, not a provider failure.
                chunk = await context.run(lambda: _next_chunk(stream), effect="unknown")
                if chunk is None:
                    break
                if type(chunk) is not bytes or not chunk:
                    raise failure("SOURCE_INVALID")
                chunks += 1
                size += len(chunk)
                actual += len(chunk)
                if (
                    chunks > verifier.limits.max_chunks
                    or size > verifier.limits.max_total_bytes
                    or actual > entry["size_bytes"]
                ):
                    raise failure("LIMIT_EXCEEDED")
                hashed.update(chunk)
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if (
                        ordinal >= len(prepared.rows)
                        or strict_reporting_json(
                            parse_reporting_json(line, verifier.limits), verifier.limits
                        )
                        != prepared.rows[ordinal]
                    ):
                        raise failure("SOURCE_INVALID")
                    ordinal += 1
                    count += 1
                if len(pending) > verifier.limits.max_value_bytes:
                    raise failure("LIMIT_EXCEEDED")
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await _close_owned(close, context.close_timeout_seconds)
        if (
            pending
            or actual != entry["size_bytes"]
            or count != entry["row_count"]
            or type(entry["sha256"]) is not str
            or hashed.hexdigest() != entry["sha256"].lower()
        ):
            raise failure("SOURCE_INVALID")
        checksums.append(ReportingPhysicalChecksum(ref, "sha256", hashed.hexdigest()))
    if size != manifest["total_size_bytes"] or ordinal != len(prepared.rows):
        raise failure("SOURCE_INVALID")
    return tuple(checksums), digest


async def _next_chunk(stream: Any) -> bytes | None:
    try:
        return cast(bytes, await stream.__anext__())
    except StopAsyncIteration:
        return None


async def _checkpoint() -> None:
    """One service-owned cancellation/deadline/heartbeat boundary; never a loop."""


def _checked_object_path(value: object) -> str:
    valid = False
    if type(value) is str:
        try:
            object_path(value)
            valid = True
        except ValueError:
            pass
    if not valid:
        raise failure("SOURCE_INVALID")
    return cast(str, value)


def _unordered_members(value: object) -> bool:
    """The mandatory golden vector must exercise JCS member ordering as well."""
    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        if list(mapping) != sorted(mapping, key=lambda name: name.encode("utf-16-be")):
            return True
        return any(_unordered_members(item) for item in mapping.values())
    if type(value) is list:
        return any(_unordered_members(item) for item in cast(list[object], value))
    return False
