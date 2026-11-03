"""The orphan collector's ownership fence (adcp-client#3593, adcp-client-python#1829).

The collector deletes objects under ``{prefix}/{namespace_key}/`` that no ledger record names.
A clone or point-in-time restore of the ledger database carries the same configuration,
installation identity and namespace key, but an older set of references, so its collector would
delete the live database's newer objects. Nothing inside the database can tell the two apart; the
object store they share can.

The fence is a chain of create-only objects, one per epoch, under the binding's owned prefix::

    {prefix}/{namespace_key}/_collector-fence/0000000000000007.json

Each holds ``{v, namespace_key, epoch, token, previous_token, claimed_at}``. The highest epoch is
the current claim. The database records the ``(epoch, token)`` it last claimed in
``adcp_reporting_orphan_collector_fences``. A real pass deletes only when the highest epoch in the
store is exactly the claim this database recorded, then claims the next epoch (create-only, so
exactly one claimant wins) before it deletes anything. Every other state refuses with a cause. A
database that was copied before the original's last claim sees a higher epoch and refuses; so does
a restore, until an operator calls ``take_over_orphan_collector_fence``.

This module is the pure part: the decision table, the key and record formats and the scope. The
I/O and the transaction live in :mod:`adcp_reporting_storage.ledger.orphan_collector`. The bytes
and the scope hash are the JavaScript SDK's, so either SDK reads the other's fence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import rfc8785

from adcp_reporting_storage.ledger.row_storage_object import is_row_instant

__all__ = [
    "ORPHAN_FENCE_DIRECTORY",
    "ORPHAN_FENCE_MAX_BYTES",
    "OrphanFenceDecision",
    "OrphanFenceEntry",
    "OrphanFenceRecord",
    "OrphanFenceRow",
    "ReportingOrphanFenceCauseV1",
    "ReportingOrphanFenceRefusalV1",
    "decide_orphan_fence",
    "encode_orphan_fence_record",
    "is_orphan_fence_key",
    "orphan_fence_directory",
    "orphan_fence_epoch_of_key",
    "orphan_fence_key",
    "orphan_fence_scope",
    "parse_orphan_fence_record",
]

#: Object-store directory (under the owned prefix) that holds the fence chain.
ORPHAN_FENCE_DIRECTORY = "_collector-fence"
#: Fence objects are tiny; anything larger is not one.
ORPHAN_FENCE_MAX_BYTES = 4096

_EPOCH_DIGITS = 16
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_KEY_NAME = re.compile(rf"([0-9]{{{_EPOCH_DIGITS}}})\.json")
_FIELDS = frozenset({"claimed_at", "epoch", "namespace_key", "previous_token", "token", "v"})

#: Why a binding's collector refused to delete.
#:
#: * ``claimed_elsewhere``: this database never claimed the namespace, but the object store holds
#:   a claim. A clone that has not collected before, a database whose fence table was lost, or
#:   another installation.
#: * ``fence_advanced``: the object store holds a newer claim than this database recorded. Another
#:   database (usually the live one this was cloned from) has collected since this database's
#:   state was taken.
#: * ``token_mismatch``: the object store holds this database's epoch under a different token. Two
#:   databases claimed the same epoch; the other one won.
#: * ``fence_missing``: this database recorded a claim and the object store holds none: the bucket
#:   or prefix was reset or is a different one.
#: * ``fence_regressed``: the object store holds an older claim than this database recorded: a
#:   database restored from a newer state, or a bucket restored from an older one.
#: * ``fence_unreadable``: the fence objects are malformed, oversized, duplicated or name another
#:   namespace.
#: * ``claim_lost``: this pass tried to claim the next epoch and another database got there first.
#: * ``migration_required``: the fence table does not exist; apply ``reporting_row_storage.sql``.
ReportingOrphanFenceCauseV1 = Literal[
    "claimed_elsewhere",
    "fence_advanced",
    "token_mismatch",
    "fence_missing",
    "fence_regressed",
    "fence_unreadable",
    "claim_lost",
    "migration_required",
]


@dataclass(frozen=True, slots=True)
class ReportingOrphanFenceRefusalV1:
    """Why a binding was fenced, reported on its collection outcome."""

    cause: ReportingOrphanFenceCauseV1
    #: What to check, and how to take the namespace over if this database really is its only owner.
    detail: str
    #: Highest epoch found in the object store, when readable.
    fence_epoch: int | None = None
    #: Epoch this database last recorded, when it has one.
    recorded_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class OrphanFenceEntry:
    """One fence object found by a listing."""

    epoch: int
    object_ref: str
    native_version_ref: str


@dataclass(frozen=True, slots=True)
class OrphanFenceRecord:
    """The contents of one fence object."""

    epoch: int
    token: str
    previous_token: str | None
    claimed_at: str


@dataclass(frozen=True, slots=True)
class OrphanFenceRow:
    """What this database recorded for a fence scope."""

    epoch: int
    token: str
    previous_token: str | None


@dataclass(frozen=True, slots=True)
class OrphanFenceDecision:
    """``claim``: this database may collect. A real pass claims ``epoch`` first (the next epoch
    after its own claim, or 1 when the namespace was never claimed); a read-only check just
    proceeds. ``complete``: this database recorded a claim and crashed or failed before its object
    was written: write it now. ``fenced``: refuse, with ``refusal``."""

    action: Literal["claim", "complete", "fenced"]
    epoch: int | None = None
    previous_token: str | None = None
    refusal: ReportingOrphanFenceRefusalV1 | None = None


_TAKEOVER_HINT = (
    "If this database is the only one that owns this namespace (for example after restoring it "
    "from a backup, and the original is gone), call "
    "take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True). Otherwise leave "
    "orphan collection off on this copy."
)


def decide_orphan_fence(
    row: OrphanFenceRow | None, latest: tuple[int, str] | None
) -> OrphanFenceDecision:
    """Decide, from what this database recorded and what the object store holds, whether it may
    collect. Pure: the caller does the I/O and the transaction.

    ``latest`` is the highest ``(epoch, token)`` in the object store, or ``None`` when it holds no
    fence.
    """

    def refuse(cause: ReportingOrphanFenceCauseV1, detail: str) -> OrphanFenceDecision:
        return OrphanFenceDecision(
            "fenced",
            refusal=ReportingOrphanFenceRefusalV1(
                cause,
                f"{detail}. {_TAKEOVER_HINT}",
                latest[0] if latest else None,
                row.epoch if row else None,
            ),
        )

    if row is None:
        if latest is None:
            return OrphanFenceDecision("claim", epoch=1)
        return refuse(
            "claimed_elsewhere",
            f"the namespace is already claimed (epoch {latest[0]}) by a database this one has no "
            "record of: it is a clone or restore of that database, or another installation that "
            "shares the namespace",
        )
    if latest is None:
        # An unconfirmed first claim: this database recorded epoch 1 and did not write the object.
        if row.epoch == 1 and row.previous_token is None:
            return OrphanFenceDecision("complete")
        return refuse(
            "fence_missing",
            f"this database recorded epoch {row.epoch} and the object store holds no fence: the "
            "bucket or prefix was reset or is a different one",
        )
    latest_epoch, latest_token = latest
    if latest_epoch == row.epoch:
        if latest_token == row.token:
            return OrphanFenceDecision("claim", epoch=row.epoch + 1, previous_token=row.token)
        return refuse(
            "token_mismatch",
            f"epoch {row.epoch} in the object store belongs to another claim: two databases "
            "claimed the same epoch and the other one won",
        )
    if latest_epoch > row.epoch:
        return refuse(
            "fence_advanced",
            f"the object store holds epoch {latest_epoch} and this database recorded epoch "
            f"{row.epoch}: another database that shares this namespace has collected since this "
            "database was copied or restored",
        )
    # latest_epoch < row.epoch. One behind with the recorded previous token is an unconfirmed
    # claim of this database (it wrote the intent and not the object); anything else is a
    # regression.
    if (
        latest_epoch == row.epoch - 1
        and row.previous_token is not None
        and latest_token == row.previous_token
    ):
        return OrphanFenceDecision("complete")
    return refuse(
        "fence_regressed",
        f"the object store holds epoch {latest_epoch}, older than the epoch {row.epoch} this "
        "database recorded: the database was restored from a newer state than the bucket, or "
        "the bucket from an older one",
    )


def orphan_fence_directory(owned_prefix: str) -> str:
    """The listing prefix of the fence chain under a binding's owned prefix."""
    return f"{owned_prefix}{ORPHAN_FENCE_DIRECTORY}/"


def orphan_fence_key(owned_prefix: str, epoch: int) -> str:
    """Object key of one epoch's fence object under ``{prefix}/{namespace_key}/``."""
    return f"{orphan_fence_directory(owned_prefix)}{epoch:0{_EPOCH_DIGITS}d}.json"


def is_orphan_fence_key(owned_prefix: str, key: str) -> bool:
    """Whether a listed key is part of the fence chain."""
    return key.startswith(orphan_fence_directory(owned_prefix))


def orphan_fence_epoch_of_key(owned_prefix: str, key: str) -> int | None:
    """The epoch a fence key names, or ``None`` for any other key under the fence directory."""
    directory = orphan_fence_directory(owned_prefix)
    if not key.startswith(directory):
        return None
    match = _KEY_NAME.fullmatch(key[len(directory) :])
    if match is None:
        return None
    epoch = int(match.group(1))
    return epoch if epoch >= 1 else None


def encode_orphan_fence_record(namespace_key: str, record: OrphanFenceRecord) -> bytes:
    """The fence object's bytes: the JSON the JavaScript SDK writes, one line."""
    return (
        json.dumps(
            {
                "v": 1,
                "namespace_key": namespace_key,
                "epoch": record.epoch,
                "token": record.token,
                "previous_token": record.previous_token,
                "claimed_at": record.claimed_at,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def parse_orphan_fence_record(
    data: bytes, *, namespace_key: str, epoch: int
) -> OrphanFenceRecord | None:
    """Parse a fence object strictly.

    ``None`` for anything that is not exactly a fence of this namespace and epoch.
    """
    if len(data) > ORPHAN_FENCE_MAX_BYTES:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or set(value) != _FIELDS:
        return None
    token = value["token"]
    previous = value["previous_token"]
    if (
        value["v"] != 1
        or type(value["v"]) is not int
        or value["namespace_key"] != namespace_key
        or type(value["epoch"]) is not int
        or value["epoch"] != epoch
        or not isinstance(token, str)
        or _UUID.fullmatch(token) is None
        or not (
            previous is None
            or (isinstance(previous, str) and _UUID.fullmatch(previous) is not None)
        )
        or not is_row_instant(value["claimed_at"])
    ):
        return None
    return OrphanFenceRecord(epoch, token, previous, value["claimed_at"])


def orphan_fence_scope(
    *, provider: str, location: Mapping[str, str], prefix: str, namespace_key: str
) -> str:
    """Identity of one physical fence: the provider, its location, the prefix and the namespace.

    Bindings that share all four share a fence, because a collector listing through either one
    reaches the same objects.
    """
    return hashlib.sha256(
        rfc8785.dumps(["adcp.orphan_fence.v1", provider, dict(location), prefix, namespace_key])
    ).hexdigest()
