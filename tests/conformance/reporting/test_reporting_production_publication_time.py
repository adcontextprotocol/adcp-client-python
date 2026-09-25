"""Actual MCP/PG publication after real source observation, including restart."""

import json
from datetime import datetime

import pytest

from adcp.reporting import load_reporting_ledger
from adcp.types import GetReportingStatusRequest, SyncAccountsRequest

from .test_reporting_materializer_progress import progress_pool
from .test_reporting_production_late_accounts import (
    client_for,
    data,
    delivered_and_reconciled,
    running_server,
)


@pytest.mark.parametrize("account", ["usd", "eur"])
async def test_real_source_observation_before_publication_reconciles_and_replays(account, tmp_path):
    async with progress_pool(autocommit=True) as (pool, schema):
        runs = []
        for index in range(2):
            async with running_server(
                tmp_path,
                schema,
                index,
                notifications=False,
                live_source_observation=True,
            ) as (uri, ready):
                assert ready["pool_size"] == 1 and ready["live_source_observation"]
                assert ready["initial_configurations"] == (0 if index == 0 else 1)
                async with client_for(uri, account) as client:
                    if index == 0:
                        admitted = data(
                            await client.sync_accounts(
                                SyncAccountsRequest.model_validate(
                                    {
                                        "idempotency_key": "publication-time-" + account,
                                        "accounts": [
                                            {
                                                "account": {"account_id": account},
                                                "reporting_delivery_configs": [
                                                    ready["templates"][account]
                                                ],
                                            }
                                        ],
                                    }
                                )
                            )
                        )
                        assert (
                            admitted["accounts"][0]["reporting_delivery_configs"][0]["state"]
                            == "ready"
                        )
                    result = await delivered_and_reconciled(
                        client,
                        tmp_path,
                        ready,
                        account,
                        replay=bool(index),
                        period=runs[0]["period"] if index else None,
                    )
                    ledger = await load_reporting_ledger(
                        client,
                        GetReportingStatusRequest.model_validate(
                            {
                                "account": {"account_id": account},
                                "view": "periods",
                                "period": result["period"],
                            }
                        ),
                    )
                    assert len(ledger.revisions) == 1
                    revision = ledger.revisions[0]
                    observations = [
                        json.loads(line)
                        for line in (tmp_path / "source-observations.jsonl")
                        .read_text()
                        .splitlines()
                    ]
                    matches = [
                        item
                        for item in observations
                        if item["account"] == account and item["period"] == result["period"]
                    ]
                    assert len(matches) == 1
                    source = matches[0]
                    # These values come from the actual source's public request
                    # and result, and the revision comes from a typed HTTP read.
                    assert datetime.fromisoformat(source["source_read_cutoff_at"]) < (
                        revision.observed_at
                    )
                    assert revision.observed_at == datetime.fromisoformat(source["observed_at"])
                    assert revision.finalized_at == datetime.fromisoformat(source["finalized_at"])
                    assert revision.data_through == datetime.fromisoformat(source["data_through"])
                    assert datetime.fromisoformat(source["acquired_at"]) <= revision.created_at
                    assert revision.finalized_at <= revision.created_at
                    result["source"] = source
                    result["revision_evidence"] = revision.model_dump(
                        mode="json", exclude_none=True
                    )
                    runs.append(result)
            async with pool.connection() as connection:
                correction_condition_1 = await (
                    await connection.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name=%s", (schema,)
                    )
                ).fetchone() == (0,)
                assert correction_condition_1
        for field in (
            "revision",
            "digest",
            "receipt_ids",
            "materialization_id",
            "source",
            "revision_evidence",
        ):
            assert runs[0][field] == runs[1][field]
        assert "-test-token" not in (tmp_path / "wire.jsonl").read_text()
        print(json.dumps({"public_real_clock_publication": {"account": account, "runs": runs}}))
