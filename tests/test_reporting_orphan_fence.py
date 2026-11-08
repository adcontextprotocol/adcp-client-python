"""The orphan collector's fence decision table, formats and scope, without a database.

Ported from the JavaScript SDK (adcp-client #3593, ``reporting-orphan-fence``).
"""

from __future__ import annotations

import json
import uuid

import pytest
from adcp_reporting_storage.ledger.orphan_fence import (
    ORPHAN_FENCE_MAX_BYTES,
    OrphanFenceRecord,
    OrphanFenceRow,
    decide_orphan_fence,
    encode_orphan_fence_record,
    is_orphan_fence_key,
    orphan_fence_directory,
    orphan_fence_epoch_of_key,
    orphan_fence_key,
    orphan_fence_scope,
    parse_orphan_fence_record,
)

NAMESPACE = "a" * 64
OWNED = f"orph/{NAMESPACE}/"
T1, T2, T3 = (str(uuid.uuid4()) for _ in range(3))


def row(epoch: int, token: str = T1, previous: str | None = None) -> OrphanFenceRow:
    return OrphanFenceRow(epoch, token, previous)


def test_it_claims_epoch_one_when_neither_side_has_a_claim_and_the_next_when_they_match() -> None:
    first = decide_orphan_fence(None, None)
    assert (first.action, first.epoch, first.previous_token) == ("claim", 1, None)
    nxt = decide_orphan_fence(row(4, T1), (4, T1))
    assert (nxt.action, nxt.epoch, nxt.previous_token) == ("claim", 5, T1)


def test_it_refuses_a_namespace_claimed_by_a_database_it_has_no_record_of() -> None:
    decision = decide_orphan_fence(None, (3, T1))
    assert decision.action == "fenced" and decision.refusal is not None
    assert decision.refusal.cause == "claimed_elsewhere"
    assert (decision.refusal.fence_epoch, decision.refusal.recorded_epoch) == (3, None)
    assert "take_over_orphan_collector_fence" in decision.refusal.detail


def test_it_finishes_its_own_unfinished_claim() -> None:
    # The object is missing: the first claim was recorded and never written.
    assert decide_orphan_fence(row(1, T1, None), None).action == "complete"
    # One epoch behind with the recorded previous token: the intent committed, the write did not.
    assert decide_orphan_fence(row(5, T2, T1), (4, T1)).action == "complete"


@pytest.mark.parametrize(
    ("recorded", "latest", "cause"),
    [
        (row(3, T1, T2), None, "fence_missing"),
        (row(1, T1, T2), None, "fence_missing"),
        (row(4, T1), (4, T2), "token_mismatch"),
        (row(4, T1), (6, T1), "fence_advanced"),
        (row(4, T1), (2, T1), "fence_regressed"),
        (row(5, T2, T1), (4, T3), "fence_regressed"),
        (row(5, T2, None), (4, T1), "fence_regressed"),
    ],
)
def test_it_refuses_every_other_state_with_the_epochs_in_the_refusal(
    recorded: OrphanFenceRow, latest: tuple[int, str] | None, cause: str
) -> None:
    decision = decide_orphan_fence(recorded, latest)
    assert decision.action == "fenced" and decision.refusal is not None
    assert decision.refusal.cause == cause
    assert decision.refusal.recorded_epoch == recorded.epoch
    assert decision.refusal.fence_epoch == (latest[0] if latest else None)


def test_no_input_makes_it_delete_when_the_bucket_is_ahead_of_the_record() -> None:
    for recorded in (None, row(1), row(7, T1, T2)):
        for epoch in range(1, 12):
            floor = recorded.epoch if recorded else 0
            if epoch <= floor:
                continue
            for token in (T1, T2, T3):
                assert decide_orphan_fence(recorded, (epoch, token)).action == "fenced"


def test_a_record_round_trips_and_a_first_claim_carries_a_null_previous_token() -> None:
    record = OrphanFenceRecord(1, T1, None, "2026-10-01T00:00:00.000Z")
    data = encode_orphan_fence_record(NAMESPACE, record)
    assert data.endswith(b"\n") and json.loads(data)["previous_token"] is None
    assert list(json.loads(data)) == [
        "v",
        "namespace_key",
        "epoch",
        "token",
        "previous_token",
        "claimed_at",
    ], "the key order the JavaScript SDK writes"
    assert parse_orphan_fence_record(data, namespace_key=NAMESPACE, epoch=1) == record
    chained = OrphanFenceRecord(2, T2, T1, "2026-10-02T00:00:00.000Z")
    assert (
        parse_orphan_fence_record(
            encode_orphan_fence_record(NAMESPACE, chained), namespace_key=NAMESPACE, epoch=2
        )
        == chained
    )


def test_it_parses_strictly_and_rejects_anything_that_is_not_exactly_a_fence_of_this_epoch() -> (
    None
):
    good = {
        "v": 1,
        "namespace_key": NAMESPACE,
        "epoch": 3,
        "token": T1,
        "previous_token": T2,
        "claimed_at": "2026-10-01T00:00:00.000Z",
    }

    def parse(value: object, epoch: int = 3, namespace: str = NAMESPACE) -> object:
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        return parse_orphan_fence_record(data, namespace_key=namespace, epoch=epoch)

    assert parse(good) is not None
    assert parse(good, epoch=4) is None, "another epoch"
    assert parse(good, namespace="b" * 64) is None, "another namespace"
    for change in (
        {"v": 2},
        {"v": True},
        {"epoch": 4},
        {"epoch": "3"},
        {"epoch": True},
        {"token": "nope"},
        {"token": T1.upper()},
        {"previous_token": "nope"},
        {"previous_token": 5},
        {"claimed_at": "yesterday"},
        {"claimed_at": "2026-10-01"},
        {"claimed_at": 5},
        {"extra": 1},
    ):
        assert parse({**good, **change}) is None, change
    missing = dict(good)
    del missing["previous_token"]
    assert parse(missing) is None
    for bad in (b"", b"not json", b"[]", b"null", b"3", b"\xff\xfe"):
        assert parse(bad) is None
    assert parse(b" " * (ORPHAN_FENCE_MAX_BYTES + 1)) is None


def test_keys_sort_by_epoch_and_only_exact_epoch_names_under_the_fence_directory_count() -> None:
    assert orphan_fence_directory(OWNED) == f"{OWNED}_collector-fence/"
    keys = [orphan_fence_key(OWNED, epoch) for epoch in (2, 10, 1, 9)]
    assert sorted(keys) == [orphan_fence_key(OWNED, epoch) for epoch in (1, 2, 9, 10)]
    assert orphan_fence_key(OWNED, 7).endswith("/0000000000000007.json")
    assert orphan_fence_epoch_of_key(OWNED, orphan_fence_key(OWNED, 12)) == 12
    directory = orphan_fence_directory(OWNED)
    for other in (
        f"{directory}7.json",
        f"{directory}00000000000000007.json",
        f"{directory}0000000000000000.json",
        f"{directory}0000000000000007.json.tmp",
        f"{directory}0000000000000007.JSON",
        f"{directory}٣000000000000007.json",
        f"{directory}sub/0000000000000007.json",
        f"{OWNED}0000000000000007.json",
    ):
        assert orphan_fence_epoch_of_key(OWNED, other) is None, other
    assert is_orphan_fence_key(OWNED, f"{directory}anything")
    assert not is_orphan_fence_key(OWNED, f"{OWNED}acct/row.jsonl")


def test_a_fence_is_one_physical_place_provider_location_prefix_and_namespace() -> None:
    base = {
        "provider": "s3",
        "location": {"bucket": "rows", "region": "us-east-1"},
        "prefix": "adcp-rows",
        "namespace_key": NAMESPACE,
    }
    scope = orphan_fence_scope(**base)  # type: ignore[arg-type]
    assert len(scope) == 64 and int(scope, 16) >= 0
    assert orphan_fence_scope(**{**base, "location": {"region": "us-east-1", "bucket": "rows"}}) == scope  # type: ignore[arg-type]
    for change in (
        {"provider": "gcs"},
        {"location": {"bucket": "other", "region": "us-east-1"}},
        {"prefix": "other"},
        {"namespace_key": "b" * 64},
    ):
        assert orphan_fence_scope(**{**base, **change}) != scope, change  # type: ignore[arg-type]


def test_the_scope_hash_is_the_one_the_javascript_sdk_computes() -> None:
    """sha256 of the RFC 8785 form of ["adcp.orphan_fence.v1", provider, location, prefix, ns]."""
    import hashlib

    canonical = (
        '["adcp.orphan_fence.v1","filesystem",{"root":"rows"},"orph","' + NAMESPACE + '"]'
    ).encode()
    assert (
        orphan_fence_scope(
            provider="filesystem", location={"root": "rows"}, prefix="orph", namespace_key=NAMESPACE
        )
        == hashlib.sha256(canonical).hexdigest()
    )
