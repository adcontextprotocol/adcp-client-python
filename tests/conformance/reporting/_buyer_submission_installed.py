"""Public Python 3.10 wheel checks, executed with -I outside the source tree."""

import asyncio
import hashlib
import importlib
import importlib.util
import json
import sys
from importlib.resources import files
from pathlib import Path


async def main():
    settings = json.loads(sys.stdin.read())
    assert sys.version_info[:2] == (3, 10)
    workspace = Path(settings["workspace"]).resolve()
    origins = {}
    for name, expected in settings["modules"].items():
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
        assert "site-packages" in str(origin) and not origin.is_relative_to(workspace)
        assert hashlib.sha256(origin.read_bytes()).hexdigest() == expected
        origins[name] = str(origin)
    assert not any(Path(entry).resolve().is_relative_to(workspace) for entry in sys.path)

    from adcp.reporting.submissions import (
        InMemoryReportingSubmissionIntentStore,
        PgReportingSubmissionIntentStore,
        ReportingSubmissionCode,
        ReportingSubmissionError,
        ReportingSubmissionScope,
        submit_reporting_receipts,
    )
    from adcp.types import (
        ReportingAdjustment,
        ReportingAdjustmentReceipt,
        ReportingReceipt,
        SyncReportingReceiptsResponse,
    )
    from adcp.types.core import TaskResult, TaskStatus

    assert ReportingAdjustment.__name__ == "ReportingAdjustment"
    sql = files("adcp.reporting.ledger").joinpath("reporting_buyer_submissions.sql").read_bytes()
    assert hashlib.sha256(sql).hexdigest() == settings["sql_sha256"]
    if not settings["drivers"]:
        assert importlib.util.find_spec("psycopg") is None
        assert importlib.util.find_spec("psycopg_pool") is None
        try:
            await PgReportingSubmissionIntentStore(pool=None).create_schema()
        except ReportingSubmissionError as error:
            assert error.code == ReportingSubmissionCode.PG_REQUIRED
            assert error.__context__ is None
        else:
            raise AssertionError("missing PG driver was accepted")

    inputs = []
    for index in range(201):
        common = {
            "reporting_receipt_id": f"installed-receipt-{index:06d}",
            "status": "accepted",
            "observed_at": "2026-09-01T01:00:00Z",
        }
        if index % 2 == 0:
            inputs.append(
                ReportingAdjustmentReceipt.model_validate(
                    {
                        **common,
                        "reporting_adjustment_id": f"adjustment-{index}",
                        "adjusts_reporting_revision_id": f"revision-{index}",
                        "observed_adjustment_sha256": "b" * 64,
                    }
                )
            )
        else:
            inputs.append(
                ReportingReceipt.model_validate(
                    {
                        **common,
                        "reporting_obligation_id": f"obligation-{index}",
                        "reporting_revision_id": f"revision-{index}",
                        "reporting_materialization_id": f"materialization-{index}",
                        "verification_profile": "manifest_checksums",
                        "observed_row_count": 0,
                        "observed_control_totals": [],
                        "observed_manifest_sha256": "a" * 64,
                    }
                )
            )
    requests = []

    class Client:
        async def sync_reporting_receipts(self, request):
            body = request.model_dump(mode="json", exclude_none=True)
            requests.append(body)
            results = []
            for kind, key in (
                ("receipt", "receipts"),
                ("adjustment_receipt", "adjustment_receipts"),
            ):
                for item in body.get(key, []):
                    if item["reporting_receipt_id"] == inputs[0].reporting_receipt_id:
                        results.append(
                            {
                                "result": "failed",
                                "reporting_receipt_id": item["reporting_receipt_id"],
                                "errors": [
                                    {
                                        "code": "UNKNOWN_PRIVATE_SENTINEL",
                                        "message": "password=PRIVATE_SENTINEL",
                                    }
                                ],
                            }
                        )
                    else:
                        results.append(
                            {
                                "result": "recorded",
                                kind: {**item, "received_at": "2026-09-01T02:00:00Z"},
                            }
                        )
            # Lose the second response after the exact body has been sent.
            if len(requests) == 2:
                raise TimeoutError("password=PRIVATE_SENTINEL")
            results.reverse()
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=SyncReportingReceiptsResponse.model_validate({"results": results}),
            )

    client = Client()
    scope = ReportingSubmissionScope("seller", "account", "https://buyer.example.test/agent")

    async def authorize(candidate):
        assert candidate is client
        return scope

    store = InMemoryReportingSubmissionIntentStore()
    first = await submit_reporting_receipts(
        client, authorizer=authorize, store=store, receipts=inputs
    )
    assert first.pending and len(first.outcomes) == 100
    result = await submit_reporting_receipts(client, authorizer=authorize, store=store)
    assert not result.pending and len(result.outcomes) == 201
    assert len(result.submitted_receipts) == 200 and result.outcomes[0].result == "failed"
    assert [outcome.submitted_receipt for outcome in result.outcomes] == inputs
    assert requests[1] == requests[2] and len(requests) == 4
    assert "PRIVATE_SENTINEL" not in repr(result)
    replay = await submit_reporting_receipts(client, authorizer=authorize, store=store)
    assert replay == result and len(requests) == 4
    for name, module in tuple(sys.modules.items()):
        if (name == "adcp" or name.startswith("adcp.")) and getattr(module, "__file__", None):
            assert "site-packages" in module.__file__
            assert not Path(module.__file__).resolve().is_relative_to(workspace)
    print(
        json.dumps(
            {
                "python": "3.10",
                "outcomes": 201,
                "submitted": 200,
                "origins": origins,
                "drivers": settings["drivers"],
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
