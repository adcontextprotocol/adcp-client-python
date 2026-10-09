"""New revisions bind protocol-shape control totals; retained pair hashes never change.

Core used to hash ``{name, value}`` pairs and then serve typed totals, so a
buyer recomputing the digest from the wire failed whenever totals were
non-empty. New revisions now hash ``{name, value, value_type[, unit]}``.
Retained revisions keep their original digests and are recognised as
``legacy_py_core_pairs_v0``, an import-only binding algorithm that is never
written.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
import rfc8785

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    PgReportingLedgerStore,
    ReportingControlTotalRecord,
    revision_content_sha256,
)
from adcp.reporting.ledger import producer as producer_module
from adcp.reporting.ledger.producer import revision_binding_algorithm
from adcp.reporting.ledger.status import _revision_to_wire
from tests.conformance.reporting._generation_support import isolated_reporting_pool
from tests.test_reporting_settling import ACCOUNT, _capabilities, _harness, _revisions

# Frozen: the pair-hashed binding of these inputs must never change, because
# retained legacy evidence is verified against it.
LEGACY_PAIR_VECTOR = {
    "reporting_revision_id": "rpr_legacy_vector",
    "row_count": 1,
    "control_totals": (("impressions", "10"), ("spend", "1.25")),
    "reporting_rows": [{"impressions": 10, "spend": "1.25"}],
}
LEGACY_PAIR_DIGEST = "2f2ad577249dba8d237b995cb52a56d00d6318eb2292bb3224120ccc01726322"


@pytest.fixture(params=["memory", "postgres"])
async def make_harness(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":

        async def build(capabilities: Any) -> Any:
            return await _harness(
                capabilities,
                store_factory=lambda clock: InMemoryReportingLedgerStore(clock=clock),
            )

        yield build
    else:
        async with isolated_reporting_pool() as pool:

            async def build(capabilities: Any) -> Any:
                return await _harness(
                    capabilities,
                    store_factory=lambda clock: PgReportingLedgerStore(pool=pool, clock=clock),
                )

            yield build


def test_the_legacy_pair_binding_is_frozen() -> None:
    assert revision_content_sha256(**LEGACY_PAIR_VECTOR) == LEGACY_PAIR_DIGEST
    typed = revision_content_sha256(
        **LEGACY_PAIR_VECTOR,
        control_total_evidence=(
            ReportingControlTotalRecord("impressions", "10", "integer"),
            ReportingControlTotalRecord("spend", "1.25", "decimal", "USD"),
        ),
    )
    assert typed != LEGACY_PAIR_DIGEST
    # Without totals the two encodings are byte-identical.
    assert revision_content_sha256(
        reporting_revision_id="rpr_empty",
        row_count=0,
        control_totals=(),
        reporting_rows=[],
    ) == revision_content_sha256(
        reporting_revision_id="rpr_empty",
        row_count=0,
        control_totals=(),
        reporting_rows=[],
        control_total_evidence=(),
    )


async def test_a_new_revision_binds_totals_a_buyer_can_recompute_from_the_wire(
    make_harness: Any,
) -> None:
    producer, store, _, _ = await make_harness(_capabilities(restatement_window="P3D"))
    await producer.run_worker()
    (revision,) = await _revisions(store)
    obligation = await store.get_obligation(
        account_id=ACCOUNT, reporting_obligation_id=revision.reporting_obligation_id
    )
    wire = _revision_to_wire(revision, obligation)
    assert wire["control_totals"] == [
        {"name": "impressions", "value": "10", "value_type": "integer"},
        {"name": "spend", "value": "1.25", "value_type": "decimal", "unit": "USD"},
    ]
    page = await store.read_revision_rows(
        account_id=ACCOUNT, reporting_revision_id=revision.reporting_revision_id
    )
    recomputed = hashlib.sha256(
        rfc8785.dumps(
            {
                "reporting_revision_id": wire["reporting_revision_id"],
                "row_count": wire["row_count"],
                "control_totals": wire["control_totals"],
                "reporting_rows": list(page.rows),
            }
        )
    ).hexdigest()
    assert recomputed == wire["revision_content_sha256"]
    assert revision_binding_algorithm(revision) == "rfc8785_jcs_v1"


async def test_replaying_a_retained_pair_hashed_revision_keeps_its_original_binding(
    make_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, _, _ = await make_harness(_capabilities(restatement_window="P3D"))
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    original = producer.commit_revision_from_manifest

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return await original(*args, **kwargs)

    # Seed the history an earlier SDK would have written: pair-hashed totals.
    with monkeypatch.context() as legacy:
        legacy.setattr(producer, "commit_revision_from_manifest", spy)
        legacy.setattr(producer_module, "_typed_control_totals", lambda *_: None)
        await producer.run_worker()
    (retained,) = await _revisions(store)
    assert retained.managed_control_totals is None
    assert revision_binding_algorithm(retained) == "legacy_py_core_pairs_v0"

    # The same publication replayed by the new code reproduces the stored
    # evidence instead of conflicting with it or rehashing it.
    args, kwargs = calls[0]
    replayed = await original(*args, **kwargs)
    assert replayed == retained
    assert await _revisions(store) == (retained,)
