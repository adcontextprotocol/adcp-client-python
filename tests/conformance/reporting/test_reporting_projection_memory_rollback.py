"""Shared frozen inputs never share mutable transaction or public response state."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, tzinfo

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.projection.capture import ReportingProjectionInput

from ._feed_support import feed_request, walk
from ._projection_support import projection_harness
from ._receipt_support import receipt_case, request_for


class InjectedMutationError(Exception):
    pass


async def test_shared_capture_rejects_a_mutable_timezone():
    class MutableZone(tzinfo):
        offset = timedelta(0)

        def utcoffset(self, dt):
            return self.offset

        def dst(self, dt):
            return timedelta(0)

    async with projection_harness("memory", notifications=False) as h:
        case = await receipt_case(h)
        await h.projection.activate(account_id=case.obligation.account_id)
        captured = h.store._projection_accounts[case.obligation.account_id].inputs[0]
        zone = MutableZone()
        core = replace(captured.core, as_of=captured.core.as_of.replace(tzinfo=zone))
        with pytest.raises(ReportingNotificationError, match="status_projection_history_corrupt"):
            ReportingProjectionInput(core, captured.reconciliation, captured.document)


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("point", ["receipt_ordinal", "projection_capture", "feed_save"])
async def test_memory_rollback_preserves_frozen_history_and_nested_public_values(
    notifications, point, monkeypatch
):
    async with projection_harness("memory", notifications=notifications) as h:
        case = await receipt_case(h)
        caller, account = case.binding.principal, case.obligation.account_id
        await h.projection.activate(account_id=account)
        request = feed_request(case)
        first = await h.store.read_reporting_feed(request, caller=caller)
        pages, records, checkpoint = await walk(h.store, request, caller, first=first)
        public_checkpoints = await h.projection.checkpoints(account_id=account)
        captured = h.store._projection_accounts[account].inputs[0]
        captured_bytes = captured.document
        assert deepcopy(captured) is captured
        before = await h.image()
        published = deepcopy((first, pages, records, checkpoint, public_checkpoints))

        # Returned JSON is mutable by design; changing it must not lend a way
        # to mutate retained bytes, a checkpoint, or a subsequent page.
        first["ext"]["adcp"]["reporting_revision_ownership"]["bindings"].append(
            {"reporting_revision_id": "caller-only", "reporting_obligation_id": "caller-only"}
        )
        public_checkpoints[0].snapshot["issues"].append({"code": "CALLER_ONLY"})
        frozen = await h.store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=caller
        )
        frozen_bytes = canonical_json_utf8_v1(frozen.to_storage())
        frozen.inputs["core"]["revisions"][0]["readable"] = False
        assert await h.image() == before
        first, pages, records, checkpoint, public_checkpoints = published
        retained = deepcopy(published)

        injected = False

        def fail_with_nested_changes():
            nonlocal injected
            injected = True
            # A failure after mutating actual transaction-owned nested JSON
            # must restore it, as well as ordinary immutable domain records.
            state = h.store._status_notification_state
            next(iter(state.checkpoints.values())).snapshot["issues"].append(
                {"code": "TRANSACTION_ONLY", "details": {"partial": [1, 2]}}
            )
            h.store._new_projection_collection = {"sequence_head": 1, "partial": ["new"]}
            raise InjectedMutationError(point)

        name = {
            "receipt_ordinal": "_append_receipt_result",
            "projection_capture": "_capture_projection",
            "feed_save": "_save_feed_snapshot",
        }[point]
        original = getattr(type(h.store), name)

        def fail_after(self, *args, **kwargs):
            original(self, *args, **kwargs)
            fail_with_nested_changes()

        with monkeypatch.context() as patch:
            patch.setattr(type(h.store), name, fail_after)
            with pytest.raises(Exception):
                async with h.store.transaction():
                    await h.store.set_revision_readable(
                        account_id=account,
                        reporting_revision_id=case.revision.reporting_revision_id,
                        readable=False,
                    )
                    if point == "feed_save":
                        await h.store.read_reporting_feed(request, caller=caller)
                    else:
                        await h.store.ingest_receipt_batch(request_for(case), caller=caller)
        assert injected
        assert not hasattr(h.store, "_new_projection_collection")
        assert await h.image() == before
        assert (first, pages, records, checkpoint, public_checkpoints) == retained
        assert await h.projection.checkpoints(account_id=account) == public_checkpoints
        assert h.store._projection_accounts[account].inputs[0] is captured
        assert captured.document == captured_bytes
        resumed = await walk(h.store, request, caller, first=first)
        assert resumed == (pages, records, checkpoint)
        reread = await h.store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=caller
        )
        assert canonical_json_utf8_v1(reread.to_storage()) == frozen_bytes
        with pytest.raises(FrozenInstanceError):
            captured.core.revisions[0].readable = False
        with pytest.raises(ReportingNotificationError):
            ReportingProjectionInput(
                replace(captured.core, configurations=list(captured.core.configurations)),
                captured.reconciliation,
                captured.document,
            )
        # Restore the fault and prove the real transaction can still commit.
        result = await h.store.ingest_receipt_batch(request_for(case), caller=caller)
        assert result["results"][0]["result"] == "recorded"
        assert captured.document == captured_bytes


@pytest.mark.parametrize("notifications", [False, True])
async def test_failed_first_feed_capture_removes_new_collection(notifications, monkeypatch):
    async with projection_harness("memory", notifications=notifications) as h:
        case = await receipt_case(h)
        await h.projection.activate(account_id=case.obligation.account_id)
        assert not hasattr(h.store, "_reporting_feed_snapshots")
        before = await h.image()
        original = type(h.store)._save_feed_snapshot

        def fail(self, stored):
            original(self, stored)
            assert self._reporting_feed_snapshots
            raise InjectedMutationError("first feed")

        with monkeypatch.context() as patch:
            patch.setattr(type(h.store), "_save_feed_snapshot", fail)
            with pytest.raises(Exception):
                await h.store.read_reporting_feed(feed_request(case), caller=case.binding.principal)
        assert await h.image() == before
        assert not hasattr(h.store, "_reporting_feed_snapshots")
