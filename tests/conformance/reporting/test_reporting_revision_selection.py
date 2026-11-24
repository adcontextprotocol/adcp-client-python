"""Whole-history selection is identical for Core, C, ingest and B1 preparation."""

import asyncio
from dataclasses import replace
from itertools import permutations

import pytest

from adcp.reporting.fixtures import SNAPSHOT_OFFERING_ID
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ProducerOfferings,
    ReportingContractualCutoffPolicy,
    ReportingProducer,
    current_required_revision,
    project_obligation_health,
    select_reporting_revision,
)
from adcp.reporting.ledger.finality import manifest_finality_proof
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusProjectionInput,
    lifecycle_intents,
    project_status_scope,
)
from adcp.reporting.ledger.status_snapshot import validate_status_evidence
from adcp.reporting.outbox import ReportingStatusScope
from adcp.reporting.revision_selection import RevisionHistoryEntry

from ._generation_support import NOW, UncalledSource, configuration, obligation_for, revision_for
from ._reliable_support import complete_fetch, reliable_factory, verified
from ._reliable_support import configuration as reliable_configuration
from .test_reporting_notification_outbox import statement


def entry(name, predecessor=None, finality="snapshot", account="acct_a", obligation="rpo_acct_a"):
    return RevisionHistoryEntry(account, obligation, name, finality, predecessor)


VECTORS = (
    ((), "snapshot", "not_ready", "empty_history"),
    ((), "official", "not_ready", "empty_history"),
    ((entry("a"),), "official", "not_ready", "official_required"),
    ((entry("a"),), "snapshot", "selected", "a"),
    ((entry("a"), entry("b", "a")), "snapshot", "selected", "b"),
    (
        (entry("a"), entry("b", "a"), entry("official", finality="official")),
        "snapshot",
        "selected",
        "official",
    ),
    ((entry("official", finality="official"),), "official", "selected", "official"),
    ((entry("a"), entry("a")), "snapshot", "corrupt", "duplicate_revision_id"),
    ((entry("a", account="acct_b"),), "snapshot", "corrupt", "ownership_mismatch"),
    ((entry("a", obligation="rpo_other"),), "snapshot", "corrupt", "ownership_mismatch"),
    ((entry(""),), "snapshot", "corrupt", "invalid_revision_identity"),
    ((entry("a", finality="draft"),), "snapshot", "corrupt", "invalid_finality"),
    ((entry("a", "absent"),), "official", "corrupt", "missing_predecessor"),
    (
        (entry("a", "o"), entry("o", finality="official")),
        "official",
        "corrupt",
        "cross_finality_edge",
    ),
    ((entry("a"), entry("o", "a", "official")), "official", "corrupt", "cross_finality_edge"),
    (
        (entry("o", finality="official"), entry("p", "o", "official")),
        "official",
        "corrupt",
        "official_predecessor",
    ),
    (
        (entry("a"), entry("b", "a"), entry("c", "a")),
        "official",
        "corrupt",
        "forked_snapshot_history",
    ),
    ((entry("a"), entry("b")), "official", "corrupt", "disconnected_snapshot_history"),
    ((entry("a", "a"),), "snapshot", "corrupt", "revision_cycle"),
    ((entry("a", "b"), entry("b", "a")), "snapshot", "corrupt", "revision_cycle"),
    (
        (entry("unique-leaf"), entry("a", "b"), entry("b", "a")),
        "snapshot",
        "corrupt",
        "revision_cycle",
    ),
    (
        (entry("a", finality="official"), entry("b", finality="official")),
        "snapshot",
        "corrupt",
        "multiple_officials",
    ),
)


@pytest.mark.parametrize("history,required,kind,detail", VECTORS)
def test_complete_history_in_any_input_order(history, required, kind, detail):
    for rows in permutations(history):
        result = select_reporting_revision(
            rows,
            account_id="acct_a",
            reporting_obligation_id="rpo_acct_a",
            required_finality=required,
        )
        assert result.kind == kind
        assert (
            result.revision.reporting_revision_id if kind == "selected" else result.reason
        ) == detail
    if kind == "corrupt" and not any(r.finality == "official" for r in history):
        # A unique official never excuses a damaged retained snapshot history.
        result = select_reporting_revision(
            (*history, entry("official", finality="official")),
            account_id="acct_a",
            reporting_obligation_id="rpo_acct_a",
            required_finality="official",
        )
        assert result.kind == "corrupt"


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", []),
        ("reporting_obligation_id", {}),
        ("reporting_revision_id", []),
        ("supersedes_reporting_revision_id", []),
        ("finality", {}),
    ],
)
def test_damaged_identity_types_return_corrupt_without_hashing_them(field, value):
    result = select_reporting_revision(
        (replace(entry("a"), **{field: value}),),
        account_id="acct_a",
        reporting_obligation_id="rpo_acct_a",
        required_finality="official",
    )
    assert result.kind == "corrupt"


def records(history, obligation):
    revision, _ = revision_for(obligation)
    result = []
    for r in history:
        record = replace(
            revision,
            account_id=r.account_id,
            reporting_obligation_id=r.reporting_obligation_id,
            reporting_revision_id=r.reporting_revision_id,
            finality=r.finality,
            supersedes_reporting_revision_id=None,
            finality_basis="source_final" if r.finality == "official" else None,
            finality_policy_id="closed" if r.finality == "official" else None,
            finalized_at=NOW if r.finality == "official" else None,
        )
        # Model a damaged custom-store image, including edges normal dataclass
        # construction already rejects. The pure wire adapter covers these too.
        object.__setattr__(
            record, "supersedes_reporting_revision_id", r.supersedes_reporting_revision_id
        )
        result.append(record)
    return tuple(result)


@pytest.mark.parametrize("history,required,kind,detail", VECTORS)
def test_compatibility_health_handler_projection_and_ingest_agree(history, required, kind, detail):
    if any(not r.reporting_revision_id or r.finality == "draft" for r in history):
        return  # Persisted records have their own closed field validation.
    config = replace(configuration(), required_finality=required)
    obligation = obligation_for(config)
    revisions = records(history, obligation)
    current = current_required_revision(obligation, revisions)
    assert (current.reporting_revision_id if current else None) == (
        detail if kind == "selected" else None
    )
    if any(r.reporting_obligation_id != obligation.reporting_obligation_id for r in revisions):
        return  # Snapshot readers partition the complete account set by obligation.
    projection = project_obligation_health(
        obligation, revisions, ledger_as_of=NOW, scope_closed=True
    )
    snapshot = ReportingStatusSnapshot("acct_a", NOW, (config,), (obligation,), revisions)
    scoped = project_status_scope(
        StatusProjectionInput(snapshot, ReportingStatusScope.for_obligation(obligation))
    )
    response = ReportingStatusHandler(InMemoryReportingLedgerStore()).render_snapshot(
        {"view": "summary"}, caller=ReportingStatusCaller("acct_a", "buyer"), snapshot=snapshot
    )
    assert projection.health == scoped.health == response["health"]
    if kind == "corrupt":
        assert projection.current_revision is None
        assert [i.code for i in scoped.issues] == ["HISTORY_UNAVAILABLE"]
        disputed = replace(statement(obligation), consumer_status="missing")
        with pytest.raises(LedgerConflictError, match="history requires repair"):
            validate_status_evidence(disputed, snapshot)
        with pytest.raises(LedgerConflictError, match="history requires repair"):
            validate_status_evidence(replace(disputed, reporting_obligation_id=None), snapshot)
        assert lifecycle_intents(replace(snapshot, statuses=(disputed,))) == ()


@pytest.mark.parametrize(
    "history",
    [
        v[0]
        for v in VECTORS
        if v[2] == "corrupt"
        and all(r.reporting_revision_id and r.finality != "draft" for r in v[0])
    ],
)
async def test_producer_rejects_corruption_before_source_or_adapter_io(history):
    class DamagedStore(InMemoryReportingLedgerStore):
        async def list_revisions(self, **kwargs):
            return records(history, obligation)

    config = configuration()
    store = DamagedStore()
    await store.put_configuration(config)
    obligation = await store.commit_obligation(obligation_for(config))
    producer = ReportingProducer(
        source=UncalledSource(), offerings=ProducerOfferings(), store=store
    )
    with pytest.raises(LedgerConflictError, match="history requires repair"):
        await producer.acquire_obligation(config, obligation, restate=True)


def test_unreadable_official_never_falls_back_to_materialized_snapshot():
    obligation = obligation_for(configuration())
    snapshot, _ = revision_for(obligation)
    official = replace(
        snapshot,
        reporting_revision_id="official",
        finality="official",
        readable=False,
        finality_basis="source_final",
        finality_policy_id="closed",
        finalized_at=NOW,
    )
    assert current_required_revision(obligation, (snapshot, official)) is official
    result = project_obligation_health(
        obligation, (snapshot, official), ledger_as_of=NOW, scope_closed=True
    )
    assert result.health == "action_required" and result.current_revision is official


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_official_close_preserves_the_snapshot_chain_and_rejects_restatement(backend):
    async with reliable_factory(backend) as h:
        config = reliable_configuration("eur")
        config = replace(
            config,
            definition=replace(
                config.definition,
                finality_policy=ReportingContractualCutoffPolicy("period-close", "PT0S"),
            ),
        )
        await h.store.put_configuration(config)
        producer = h.producer(h.source(complete_fetch), managed=False)
        (obligation,) = await producer.close_elapsed_periods(config)

        async def commit(observation, finality):
            request = producer._build_slice(  # noqa: SLF001 - the suite's slice fixture
                config,
                obligation,
                SNAPSHOT_OFFERING_ID,
                finality=finality,
                now=h.clock(),
                observation=observation,
            )
            result = await producer._source.execute(request, cancel=asyncio.Event())  # noqa: SLF001
            return await h.commit_slice(
                producer,
                obligation,
                request,
                result,
                finality=finality,
                finality_proof=(
                    manifest_finality_proof(obligation, verified(result), h.clock())
                    if finality == "official"
                    else None
                ),
            )

        first = await commit(0, "snapshot")
        second = await commit(1, "snapshot")
        official = await commit(2, "official")
        assert first.supersedes_reporting_revision_id is None
        assert second.supersedes_reporting_revision_id == first.reporting_revision_id
        assert official.supersedes_reporting_revision_id is None

        with pytest.raises(LedgerConflictError) as error:
            await commit(3, "snapshot")
        assert error.value.code == "OFFICIAL_REVISION_TERMINAL"
        assert await commit(1, "snapshot") == second

        history = await h.store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        assert {revision.reporting_revision_id for revision in history} == {
            first.reporting_revision_id,
            second.reporting_revision_id,
            official.reporting_revision_id,
        }
        selection = select_reporting_revision(
            history,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            required_finality=obligation.required_finality,
        )
        assert selection.kind == "selected"
        assert selection.revision.reporting_revision_id == official.reporting_revision_id
        projection = project_obligation_health(
            obligation, history, ledger_as_of=h.clock(), scope_closed=True
        )
        assert [i.code for i in projection.issues] == []
        assert projection.current_revision is not None
