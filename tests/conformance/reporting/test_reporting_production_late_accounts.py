"""Typed public enrollment, autonomous late-account delivery and durable restart."""

import asyncio
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import rfc8785
from pydantic import TypeAdapter

from adcp import ADCPClient, AgentConfig
from adcp.reporting import (
    ExpectedReportingPeriod,
    ReportingObservation,
    load_reporting_ledger,
    reconcile_reporting,
)
from adcp.reporting.materializer import ReportingWriterCapability, reference_digest
from adcp.types import (
    GetAdcpCapabilitiesRequest,
    GetMediaBuyDeliveryRequest,
    GetReportingStatusRequest,
    ReportingCanonicalContentDigest,
    ReportingControlTotal,
    SyncAccountsRequest,
)

from ._late_account_support import ACCOUNTS, rows_for, verifier_for
from .test_reporting_materializer_progress import progress_pool, served
from .test_reporting_production_scope import assert_access_denied


@asynccontextmanager
async def running_server(root, schema, index, *, notifications, live_source_observation=False):
    fixture_root = Path(__file__).resolve().parents[3]
    launcher = (
        "import sys; sys.path.insert(0,sys.argv.pop(1)); "
        "from tests.conformance.reporting._late_account_server import main; main()"
    )
    (root / "ready.json").unlink(missing_ok=True)
    (root / "stopped.json").unlink(missing_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [
        sys.executable,
        "-I",
        "-c",
        launcher,
        str(fixture_root),
        "--port",
        str(port),
        "--root",
        str(root),
        "--schema",
        schema,
    ]
    if notifications:
        command.append("--notifications")
    if live_source_observation:
        command.append("--live-source-observation")
    with (
        (root / f"server-{index}.stdout").open("xb") as output,
        (root / f"server-{index}.stderr").open("xb") as errors,
    ):
        child = subprocess.Popen(
            command,
            cwd=root,
            stdout=output,
            stderr=errors,
            start_new_session=True,
        )
        try:
            for _ in range(1800):
                assert child.poll() is None, (root / f"server-{index}.stderr").read_text()
                if (root / "ready.json").exists():
                    try:
                        _, writer = await asyncio.open_connection("127.0.0.1", port)
                    except OSError:
                        pass
                    else:
                        writer.close()
                        await writer.wait_closed()
                        break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("public production startup exceeded 90 seconds")
            ready = json.loads((root / "ready.json").read_text())
            (root / f"ready-{index}.json").write_text(json.dumps(ready))
            yield f"http://127.0.0.1:{port}/mcp/", ready
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    await asyncio.to_thread(child.wait, 25)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    await asyncio.to_thread(child.wait, 5)
            cleanup = {
                "pid": child.pid,
                "exit": child.returncode,
                "reaped": child.poll() is not None,
            }
            if (root / "stopped.json").exists():
                cleanup.update(json.loads((root / "stopped.json").read_text()))
            (root / f"cleanup-{index}.json").write_text(json.dumps(cleanup))
            assert cleanup.get("stopped") and cleanup["destination_sessions_closed"], cleanup


def client_for(uri, account):
    return ADCPClient(
        AgentConfig(
            id="late-" + account,
            agent_uri=uri,
            protocol="mcp",
            auth_token=account + "-test-token",
            auth_header="Authorization",
            auth_type="bearer",
        ),
        adcp_version="3.2.0-rc.6",
    )


def data(result):
    assert result.success and result.data is not None, result
    return result.data.model_dump(mode="json", exclude_none=True)


async def delivered_and_reconciled(client, root, ready, account, *, replay=False, period=None):
    currency, count = ACCOUNTS[account]
    capability = ReportingWriterCapability(
        "warehouse_materialization",
        "fixture-sql",
        "jsonl",
        "canonical_digest",
        "destination",
        "immutable_location",
        "sha256",
        "conditional_create",
    )
    verifier = verifier_for(currency, capability)
    selectors = {"account": {"account_id": account}, "view": "periods"}
    if period is not None:
        selectors["period"] = period
    request = GetReportingStatusRequest.model_validate(selectors)
    started = time.monotonic()
    ledger = None
    calls = 0
    # Every readiness observation is an actual typed RPC. The scheduler and
    # producer use real clocks; no worker is stepped, stopped or manually invoked.
    while time.monotonic() - started < 45 and calls < 150:
        ledger = await load_reporting_ledger(client, request)
        calls += 1
        if any(
            str(getattr(m.status, "value", m.status)) == "delivered"
            for m in ledger.materializations
        ):
            break
    else:
        raise AssertionError({"account": account, "materialization_missing_after_calls": calls})
    readiness_seconds = time.monotonic() - started
    if period is None:
        # Catch-up production is deliberately active throughout this test. Pin
        # a publicly delivered period, rather than assuming which candidate the
        # materializer chooses first among that account's outstanding periods.
        delivered = next(
            m
            for m in ledger.materializations
            if str(getattr(m.status, "value", m.status)) == "delivered"
        )
        obligation = next(
            o
            for o in ledger.obligations
            if o.reporting_obligation_id == delivered.reporting_obligation_id
        )
        period = {name: getattr(obligation.period, name).isoformat() for name in ("start", "end")}
        selectors["period"] = period
        request = GetReportingStatusRequest.model_validate(selectors)
        ledger = await load_reporting_ledger(client, request)
    assert len(ledger.obligations) == len(ledger.revisions) == len(ledger.materializations) == 1
    revision = ledger.revisions[0]
    materialization = ledger.materializations[0]
    assert revision.account_id == ledger.obligations[0].account_id == account
    assert revision.row_count == count
    params = {
        "account": {"account_id": account},
        "reporting_revision_id": revision.reporting_revision_id,
        "pagination": {"max_results": 100},
    }
    exact, binding, pages = [], None, 0
    while True:
        page = data(
            await client.get_media_buy_delivery(GetMediaBuyDeliveryRequest.model_validate(params))
        )
        if binding is None:
            binding = page["reporting_revision_binding"]
        else:
            assert binding == page["reporting_revision_binding"]
        exact.extend(page["reporting_rows"])
        pages += 1
        if not page["pagination"]["has_more"]:
            break
        params["pagination"]["cursor"] = page["pagination"]["cursor"]
        assert pages < 10
    assert exact == rows_for(account)
    assert pages == (6 if count == 503 else 1)
    digest_input = {k: binding[k] for k in ("reporting_revision_id", "row_count", "control_totals")}
    digest_input["reporting_rows"] = exact
    digest = hashlib.sha256(rfc8785.dumps(digest_input)).hexdigest()
    assert digest == binding["content_sha256"] == revision.revision_content_sha256

    async def inspect(context):
        with sqlite3.connect(root / "destination.sqlite") as connection:
            records = [
                json.loads(row[0]) for row in connection.execute("SELECT content FROM artifacts")
            ]
        matched = [
            record
            for record in records
            if record["revision"] == context.revision.reporting_revision_id
            and record["resource"]["location"] == context.materialization.resource.location
        ]
        assert len(matched) == 1
        rows = [json.loads(row) for row in matched[0]["rows"]]
        assert rows == exact
        _, totals = verifier.canonicalize(rows)
        return ReportingObservation(
            len(rows),
            [TypeAdapter(ReportingControlTotal).validate_python(t.to_wire()) for t in totals],
            ReportingCanonicalContentDigest.model_validate(
                reference_digest(verifier, rows).to_wire()
            ),
        )

    template = ready["templates"][account]
    expected = [
        ExpectedReportingPeriod(
            "shared-config",
            1,
            template["report_definition_id"],
            "billing",
            template["reporting_profile"],
            ("shared-media-buy",),
            period["start"],
            period["end"],
        )
    ]
    outcome = await reconcile_reporting(
        client, request, inspect, expected_periods=expected, inspection_retry_backoff_seconds=0
    )
    assert outcome.definitive, [(o.definitive, o.reasons) for o in outcome.obligations]
    assert len(outcome.submitted_receipts) == (0 if replay else 1)
    repeat = await reconcile_reporting(
        client, request, inspect, expected_periods=expected, inspection_retry_backoff_seconds=0
    )
    assert repeat.definitive and not repeat.submitted_receipts
    other = "eur" if account == "usd" else "usd"
    assert_access_denied(
        await client.get_reporting_status(
            GetReportingStatusRequest.model_validate(
                {
                    "account": {"account_id": other},
                    "view": "periods",
                }
            )
        )
    )
    assert_access_denied(
        await client.get_media_buy_delivery(
            GetMediaBuyDeliveryRequest.model_validate(
                {
                    "account": {"account_id": other},
                    "reporting_revision_id": revision.reporting_revision_id,
                }
            )
        )
    )
    return {
        "account": account,
        "currency": currency,
        "rows": count,
        "pages": pages,
        "revision": revision.reporting_revision_id,
        "digest": digest,
        "receipt_ids": [receipt.reporting_receipt_id for receipt in repeat.ledger.receipts],
        "materialization_id": materialization.reporting_materialization_id,
        "period": period,
        "readiness_calls": calls,
        "readiness_seconds": round(readiness_seconds, 3),
        "completed_seconds": round(time.monotonic() - started, 3),
    }


async def ongoing_first_turns(pool, account):
    """Observe real committed turns after first-account RPCs have finished.

    A one-period fixture can settle or encounter a busy-lock wrap during those
    RPCs, accidentally passing with the old cursor. Catch-up publications keep
    pending work due. These plain MVCC reads neither take account locks nor
    wake, advance or execute either SDK worker.
    """
    observed = []
    deadline = time.monotonic() + 30
    previous = (await served(pool))[account]
    while time.monotonic() < deadline:
        async with pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT served_at::text,due_at<=clock_timestamp(),"
                    " (SELECT count(*) FROM reporting_materializer_candidates c"
                    " WHERE c.account_id=a.account_id AND c.due_at<=clock_timestamp())"
                    " FROM reporting_materializer_accounts a WHERE account_id=%s",
                    (account,),
                )
            ).fetchone()
        if row[0] != previous:
            observed.append({"served_at": row[0], "due": row[1], "due_candidates": row[2]})
            previous = row[0]
            if len(observed) >= 3 and all(
                r["due"] and r["due_candidates"] >= 2 for r in observed[-3:]
            ):
                return observed[-3:]
        await asyncio.sleep(0.02)
    raise AssertionError({"continuous_first_work_not_established": observed})


@pytest.mark.parametrize("first", ["usd", "eur"], ids=["late-sorts-first", "late-sorts-last"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_late_account_progresses_via_typed_public_support_and_survives_restart(
    first, notifications, tmp_path
):
    order = (first, "eur" if first == "usd" else "usd")
    async with progress_pool(autocommit=True) as (pool, schema):
        results = []
        continuing_work = None
        for index in range(2):
            async with running_server(tmp_path, schema, index, notifications=notifications) as (
                uri,
                ready,
            ):
                assert ready["pool_size"] == 1
                assert ready["initial_configurations"] == (0 if index == 0 else 2)
                current = {}
                for account in order:
                    if index == 0 and account != first:
                        continuing_work = await ongoing_first_turns(pool, first)
                    async with client_for(uri, account) as client:
                        caps = data(
                            await client.get_adcp_capabilities(GetAdcpCapabilitiesRequest())
                        )
                        assert caps["media_buy"]["reporting_delivery"]["managed_delivery"]
                        if index == 0:
                            # The first remains active throughout late admission;
                            # no worker restart, due-gap workaround or manual turn.
                            first_before = (await served(pool)).get(first)
                            onboarding = SyncAccountsRequest.model_validate(
                                {
                                    "idempotency_key": "late-account-" + account,
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
                            admitted = data(await client.sync_accounts(onboarding))
                            assert admitted["accounts"][0]["account_id"] == account
                            assert (
                                admitted["accounts"][0]["reporting_delivery_configs"][0]["state"]
                                == "ready"
                            )
                        current[account] = await delivered_and_reconciled(
                            client,
                            tmp_path,
                            ready,
                            account,
                            replay=bool(index),
                            period=results[0][account]["period"] if index else None,
                        )
                        if index == 0 and account != first:
                            correction_condition_1 = (await served(pool))[first] != first_before
                            assert correction_condition_1
                results.append(current)
            async with pool.connection() as connection:
                correction_condition_2 = await (
                    await connection.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name=%s", (schema,)
                    )
                ).fetchone() == (0,)
                assert correction_condition_2
        for account in order:
            for field in ("revision", "digest", "receipt_ids", "materialization_id"):
                assert results[0][account][field] == results[1][account][field]
        assert results[0]["usd"]["revision"] != results[0]["eur"]["revision"]
        assert results[0]["usd"]["materialization_id"] != results[0]["eur"]["materialization_id"]
        wire = (tmp_path / "wire.jsonl").read_text()
        assert "-test-token" not in wire
        for line in wire.splitlines():
            request = json.loads(json.loads(line)["request_utf8"])
            if request.get("method") != "tools/call":
                continue
            task, arguments = request["params"]["name"], request["params"]["arguments"]
            if task == "sync_accounts":
                assert arguments["accounts"][0]["reporting_delivery_configs"][0]["scope"] == {
                    "media_buy_ids": ["shared-media-buy"],
                }
            if task == "get_media_buy_delivery":
                assert "include_package_daily_breakdown" not in arguments
                assert "include_window_breakdown" not in arguments
        print(
            json.dumps(
                {
                    "public_late_account_progress": {
                        "order": order,
                        "notifications": notifications,
                        "runs": results,
                        "typed_onboarding_and_exact_reads": True,
                        "autonomous_worker": True,
                        "observed_due_first_turns_before_late_admission": continuing_work,
                    }
                }
            ),
            flush=True,
        )
