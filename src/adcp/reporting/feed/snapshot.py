"""Immutable wire membership and versioned private inputs for one complete walk.

Version 1 is permanently the legacy ownership representation. A future projector
can create version 2 for new snapshots, but must retain this decoder and serve
these stored bytes, ordering, counts and checkpoints without re-projection.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import aware_utc
from adcp.reporting.feed.errors import ReportingFeedError
from adcp.reporting.feed.request import TOKEN_LIMIT, FeedRequest
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal

FeedKind = Literal[
    "obligation",
    "revision",
    "adjustment",
    "consumer_status",
    "materialization",
    "revision_receipt",
    "adjustment_receipt",
]
_ARRAYS: dict[FeedKind, str] = {
    "obligation": "periods",
    "revision": "revisions",
    "adjustment": "adjustments",
    "consumer_status": "consumer_statuses",
    "materialization": "materializations",
    "revision_receipt": "receipts",
    "adjustment_receipt": "adjustment_receipts",
}
GlobalKey = tuple[int, int, str, str]


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(value)).hexdigest()


@dataclass(frozen=True)
class ReportingFeedRecord:
    kind: FeedKind
    record_id: str
    key: GlobalKey
    wire_json: bytes = field(repr=False)

    def to_storage(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.record_id,
            "key": list(self.key),
            "wire": json.loads(self.wire_json),
        }


@dataclass(frozen=True)
class ReportingFeedSnapshot:
    caller: ReportingDeliveryPrincipal
    snapshot_id: str
    as_of: datetime
    after: tuple[int, int]
    through: tuple[int, int]
    filters_json: bytes = field(repr=False)
    common_json: bytes = field(repr=False)
    records: tuple[ReportingFeedRecord, ...] = field(repr=False)
    inputs_json: bytes = field(repr=False)
    representation_version: int = 1
    ownership_mode: Literal["absent", "bindings"] = "absent"

    @property
    def total_count(self) -> int:
        return len(self.records)

    @property
    def inputs(self) -> dict[str, Any]:
        """A detached PRIVATE copy. Never serialize this into a response/ext."""
        return dict(json.loads(self.inputs_json))

    def to_storage(self) -> dict[str, Any]:
        return {
            "version": 1,
            "representation_version": self.representation_version,
            "ownership_mode": self.ownership_mode,
            "account_id": self.caller.account_id,
            "consumer_id": self.caller.consumer_id,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "after": list(self.after),
            "through": list(self.through),
            # Filters use ordinary JSON; keep their exact versioned bytes inside
            # the restricted canonical financial-history document as a string.
            "filters": self.filters_json.decode("ascii"),
            "common": json.loads(self.common_json),
            "total_count": self.total_count,
            "records": [r.to_storage() for r in self.records],
            "inputs": self.inputs,
        }

    @property
    def binding(self) -> str:
        # Includes the complete frozen projection and every private dependency,
        # not merely maxima followed by mutable reads. Principal lengths never
        # increase token size because the binding is a fixed SHA-256 digest.
        return _digest(self.to_storage())


def decode_snapshot(value: Any) -> ReportingFeedSnapshot:
    result = None
    try:
        if (
            type(value) is not dict
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["representation_version"]) is not int
            or (value["representation_version"], value["ownership_mode"])
            not in {(1, "absent"), (2, "absent"), (2, "bindings")}
            or type(value["filters"]) is not str
            or type(json.loads(value["filters"])) is not dict
        ):
            raise ValueError
        records = tuple(
            ReportingFeedRecord(
                r["kind"], r["id"], tuple(r["key"]), canonical_json_utf8_v1(r["wire"])
            )
            for r in value["records"]
        )
        if any(
            r.kind not in _ARRAYS
            or type(r.record_id) is not str
            or len(r.key) != 4
            or type(r.key[0]) is not int
            or r.key[0] not in {0, 1}
            or type(r.key[1]) is not int
            or r.key[1] < 1
            or r.key[2:] != (r.kind, r.record_id)
            for r in records
        ) or [r.key for r in records] != sorted({r.key for r in records}):
            raise ValueError
        if len({(r.kind, r.record_id) for r in records}) != len(records):
            raise ValueError
        positions = value["after"], value["through"]
        if any(
            type(p) is not list or len(p) != 2 or any(type(n) is not int or n < 0 for n in p)
            for p in positions
        ):
            raise ValueError
        if any(a > t for a, t in zip(*positions)):
            raise ValueError
        result = ReportingFeedSnapshot(
            ReportingDeliveryPrincipal(value["account_id"], value["consumer_id"]),
            value["snapshot_id"],
            aware_utc(datetime.fromisoformat(value["as_of"])),
            tuple(positions[0]),
            tuple(positions[1]),
            value["filters"].encode("ascii"),
            canonical_json_utf8_v1(value["common"]),
            records,
            canonical_json_utf8_v1(value["inputs"]),
            value["representation_version"],
            value["ownership_mode"],
        )
        if result.to_storage() != value:
            raise ValueError
        if result.representation_version == 2:
            inputs = value["inputs"]
            if (
                inputs["projection_version"] != 2
                or inputs["ownership_mode"] != result.ownership_mode
            ):
                raise ValueError
            core = inputs["core"]
            owners = {o["reporting_obligation_id"] for o in core["obligations"]}
            revisions = core["revisions"]
            expected = {r["reporting_revision_id"]: r["reporting_obligation_id"] for r in revisions}
            if len(expected) != len(revisions) or not set(expected.values()).issubset(owners):
                raise ValueError
            bindings = inputs["revision_ownership"]
            if (
                type(bindings) is not list
                or len(bindings) != len(expected)
                or any(
                    type(b) is not dict
                    or set(b) != {"reporting_revision_id", "reporting_obligation_id"}
                    for b in bindings
                )
                or {b["reporting_revision_id"]: b["reporting_obligation_id"] for b in bindings}
                != expected
            ):
                raise ValueError
            if any(r.record_id not in expected for r in records if r.kind == "revision"):
                raise ValueError
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        result = None
    if result is None:
        raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT")
    return result


@dataclass(frozen=True)
class StoredFeedSnapshot:
    snapshot: ReportingFeedSnapshot
    signing_key: bytes = field(repr=False)

    def token(self, kind: Literal["cursor", "checkpoint"], position: int) -> str:
        snapshot = self.snapshot
        key = snapshot.records[position - 1].key if position else None
        body = canonical_json_utf8_v1(
            [
                1,
                kind,
                snapshot.snapshot_id,
                position,
                snapshot.binding,
                _digest(key),
            ]
        )
        signature = hmac.digest(self.signing_key, body, "sha256")
        return "rpf1." + base64.urlsafe_b64encode(body + signature).decode().rstrip("=")

    def check(
        self,
        token: str,
        kind: Literal["cursor", "checkpoint"],
        request: FeedRequest,
        caller: ReportingDeliveryPrincipal,
    ) -> int:
        decoded = token_position(token)
        snapshot = self.snapshot
        position = decoded[3]
        if (
            snapshot.caller != caller
            or decoded[1] != kind
            or decoded[2] != snapshot.snapshot_id
            or type(position) is not int
            or not 0 <= position <= snapshot.total_count
            or (kind == "checkpoint" and position != snapshot.total_count)
            or (kind == "cursor" and not 0 < position < snapshot.total_count)
            or not hmac.compare_digest(token, self.token(kind, position))
        ):
            raise ReportingFeedError("INVALID_CHECKPOINT")
        if snapshot.filters_json != request.filters_json:
            # Only an authenticated, correctly signed position can disclose
            # this actionable version boundary. Other callers/tokens retain
            # the indistinguishable INVALID_CHECKPOINT error above.
            if json.loads(snapshot.filters_json).get("adcp_version") != request.filters.get(
                "adcp_version"
            ):
                raise ReportingFeedError("REPORTING_FEED_VERSION_MISMATCH")
            raise ReportingFeedError("INVALID_CHECKPOINT")
        return position

    def page(self, offset: int, limit: int) -> dict[str, Any]:
        snapshot = self.snapshot
        result: dict[str, Any] = json.loads(snapshot.common_json)
        for array in _ARRAYS.values():
            result[array] = []
        for record in snapshot.records[offset : offset + limit]:
            result[_ARRAYS[record.kind]].append(json.loads(record.wire_json))
        end = min(offset + limit, snapshot.total_count)
        more = end < snapshot.total_count
        result.update(
            ledger_snapshot_id=snapshot.snapshot_id,
            ledger_as_of=snapshot.as_of.isoformat().replace("+00:00", "Z"),
            changes_checkpoint=self.token("checkpoint", snapshot.total_count),
            pagination={
                "total_count": snapshot.total_count,
                "has_more": more,
                **({"cursor": self.token("cursor", end)} if more else {}),
            },
        )
        if snapshot.ownership_mode == "bindings":
            from adcp.reporting.ownership import ReportingOwnershipError, with_revision_ownership

            try:
                ownership: dict[str, str] = {}
                for item in snapshot.inputs["revision_ownership"]:
                    revision, owner = item["reporting_revision_id"], item["reporting_obligation_id"]
                    if revision in ownership:
                        raise ReportingOwnershipError()
                    ownership[revision] = owner
                result = with_revision_ownership(result, ownership)
            except (KeyError, TypeError, ReportingOwnershipError):
                raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT") from None
        return result


def token_position(token: str) -> list[Any]:
    value = None
    try:
        if type(token) is not str or not token.startswith("rpf1.") or len(token) > TOKEN_LIMIT:
            raise ValueError
        text = token[5:]
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw[:-32])
        if (
            type(value) is not list
            or len(value) != 6
            or type(value[0]) is not int
            or value[0] != 1
            or value[1] not in {"cursor", "checkpoint"}
            or type(value[2]) is not str
            or len(value[2]) != 37
            or not value[2].startswith("rpfs_")
            or any(c not in "0123456789abcdef" for c in value[2][5:])
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        value = None
    if value is None:
        # Unbound Core and old reconciliation tokens are explicitly rejected.
        raise ReportingFeedError("INVALID_CHECKPOINT")
    return value
