"""Unversioned representation-one walks survive the protocol-filter addition.

These are legacy-format store controls. The separate rolling lane supplies
actual installed-parent evidence; this fixture does not claim that provenance.
"""

import json
from dataclasses import replace

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.feed import ReportingFeedError

from ._feed_support import feed_request, mixed_case, walk
from ._projection_support import projection_harness


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_legacy_unversioned_walk_survives_activation_and_current_protocol(
    backend, notifications
):
    async with projection_harness(backend, notifications=notifications) as h:
        scenario, _, _ = await mixed_case(h)
        caller = scenario.binding.principal
        # The historical direct pin produces the pre-marker filter format.
        # Before activation its representation is the parent's original v1.
        old_request = feed_request(scenario, adcp_version="3.2-rc.3")
        first = await h.store.read_reporting_feed(old_request, caller=caller)
        snapshot = await h.store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=caller
        )
        assert snapshot.representation_version == 1
        assert snapshot.ownership_mode == "absent"
        assert "adcp_version" not in json.loads(snapshot.filters_json)
        stored_bytes = canonical_json_utf8_v1(snapshot.to_storage())
        expected = await walk(h.store, old_request, caller, first=first)
        current_request = feed_request(scenario)

        for activated in (False, True):
            if activated:
                await h.projection.activate(account_id=caller.account_id)
            continued = await walk(h.store, current_request, caller, first=first)
            assert continued == expected
            retained = await h.store.read_reporting_feed_snapshot(
                snapshot.snapshot_id, caller=caller
            )
            assert canonical_json_utf8_v1(retained.to_storage()) == stored_bytes

            # Caller, filter and signature failures remain indistinguishable.
            continuation = feed_request(
                scenario,
                pagination={"cursor": first["pagination"]["cursor"], "max_results": 1},
            )
            for changed, principal in (
                ({**continuation, "media_buy_ids": ["different"]}, caller),
                (continuation, replace(caller, consumer_id="different")),
                (
                    {
                        **continuation,
                        "pagination": {"cursor": first["pagination"]["cursor"] + "x"},
                    },
                    caller,
                ),
            ):
                with pytest.raises(ReportingFeedError) as error:
                    await h.store.read_reporting_feed(changed, caller=principal)
                assert error.value.code == "INVALID_CHECKPOINT"

        # A legacy checkpoint can start a new current-version walk without
        # modifying its original boundary or any persisted old page.
        repaired = await h.store.read_reporting_feed(
            feed_request(scenario, changes_after=expected[2]), caller=caller
        )
        newer = await h.store.read_reporting_feed_snapshot(
            repaired["ledger_snapshot_id"], caller=caller
        )
        assert newer.snapshot_id != snapshot.snapshot_id
        assert newer.after == snapshot.through
        assert json.loads(newer.filters_json)["adcp_version"] == "3.2-rc.6"
        assert newer.representation_version == 2
