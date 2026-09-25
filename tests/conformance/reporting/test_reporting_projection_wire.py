"""Authenticated tier reads use the same captured private inputs in every view."""

import json
from functools import partial

import pytest

from adcp.reporting.ownership import page_revision_ownership

from ._feed_support import MountedFeed, feed_request, second_consumer
from ._projection_support import projection_harness
from ._receipt_support import receipt_case
from ._receipt_transport import error_code


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("feedback", [False, True])
async def test_mounted_private_summary_revision_and_page_walk(backend, notifications, feedback):
    async with projection_harness(backend, notifications=notifications, feedback=feedback) as h:
        first = await receipt_case(h)
        second = await second_consumer(h, first, "https://buyer.example/second")
        await h.projection.activate(account_id=first.obligation.account_id)
        mounted = MountedFeed(h, feedback=feedback, hydrated=True, registry_kind="oauth")
        mounted.authorize(first)
        mounted.authorize(second, token="token-two")
        async with mounted.client() as client:
            for call in (mounted.mcp, mounted.a2a, partial(mounted.a2a, v1=True)):
                summaries = []
                for item, token, receipt_count in (
                    (first, "token-one", 0),
                    (second, "token-two", 1),
                ):
                    request = feed_request(item, view="summary")
                    del request["pagination"]
                    _, summary = await call(client, request, token=token)
                    assert summary.get("status") == "completed", summary
                    summaries.append(summary)
                    request.update(
                        view="revision",
                        reporting_revision_id=item.revision.reporting_revision_id,
                    )
                    _, exact = await call(client, request, token=token)
                    assert exact.get("status") == "completed", exact
                    assert len(exact["receipts"]) == receipt_count
                    assert len(exact["materializations"]) == 1
                    assert page_revision_ownership(exact) == {
                        item.revision.reporting_revision_id: item.obligation.reporting_obligation_id
                    }
                    assert exact["revision"]["revision_content_sha256"] == (
                        item.revision.revision_content_sha256
                    )
                    records = []
                    request = feed_request(item)
                    for _ in range(20):
                        _, page = await call(client, request, token=token)
                        assert page.get("status") == "completed", page
                        page_revision_ownership(page)
                        records.extend(page["receipts"])
                        if not page["pagination"]["has_more"]:
                            break
                        request["pagination"]["cursor"] = page["pagination"]["cursor"]
                    else:
                        pytest.fail("bounded mounted walk did not finish")
                    assert len(records) == receipt_count
                assert summaries[0]["ledger_snapshot_id"] != summaries[1]["ledger_snapshot_id"]
                assert summaries[0]["health"] != summaries[1]["health"]
                assert second.binding.consumer_id not in json.dumps(summaries[0])
            mounted.grants.remove((first.obligation.account_id, first.binding.consumer_id))
            for view in ("summary", "revision", "periods"):
                request = feed_request(first, view=view)
                if view != "periods":
                    del request["pagination"]
                if view == "revision":
                    request["reporting_revision_id"] = first.revision.reporting_revision_id
                _, denied = await mounted.mcp(client, request)
                assert error_code(denied) == "UNAUTHORIZED"
