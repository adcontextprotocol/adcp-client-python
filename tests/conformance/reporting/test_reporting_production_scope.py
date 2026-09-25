"""Public typed onboarding over real MCP HTTP, including installed floor wheels."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

import pytest
import rfc8785
from pydantic import ValidationError

from adcp import ADCPClient, AgentConfig
from adcp.types import GetMediaBuyDeliveryRequest, GetReportingStatusRequest, SyncAccountsRequest
from adcp.validation.schema_loader import get_named_validator


@asynccontextmanager
async def onboarding_server(backend, root, *, scenario="scope", count=0, notifications=False):
    if backend == "postgres":
        if not os.environ.get("ADCP_PG_TEST_URL"):
            pytest.skip("requires real PostgreSQL")
        pytest.importorskip("psycopg")
    fixture_root = Path(__file__).resolve().parents[3]
    # Only test fixtures are put on sys.path. SDK imports still come from the
    # current interpreter's installation (a wheel in the installed gate).
    launcher = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from tests.conformance.reporting._scope_onboarding_server import main; main()"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with (root / "server.stdout").open("xb") as out, (root / "server.stderr").open("xb") as err:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    launcher,
                    str(fixture_root),
                    backend,
                    str(root),
                    str(listener.fileno()),
                    scenario,
                    str(count),
                    str(notifications).lower(),
                ],
                cwd=root,
                stdout=out,
                stderr=err,
                pass_fds=(listener.fileno(),),
                start_new_session=True,
            )
            try:
                for _ in range(1800):
                    if (root / "ready.json").exists():
                        break
                    assert child.poll() is None, (root / "server.stderr").read_text()
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("MCP startup exceeded 90 seconds")
                yield f"http://127.0.0.1:{port}/mcp/", json.loads((root / "ready.json").read_text())
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        await asyncio.to_thread(child.wait, 15)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        await asyncio.to_thread(child.wait)


def public_client(uri, account, route="mcp"):
    return ADCPClient(
        AgentConfig(
            id="scope-" + account,
            agent_uri=uri if route == "mcp" else uri.removesuffix("/mcp/"),
            protocol="mcp" if route == "mcp" else "a2a",
            auth_token=account,
            auth_header="Authorization",
            auth_type="bearer",
        ),
        adcp_version="3.2.0-rc.6",
        force_a2a_version=route.removeprefix("a2a-") if route != "mcp" else None,
    )


@pytest.fixture(autouse=True)
def _a2a_compat_send_and_aggregate():
    # Keep the real a2a-sdk stream; the repository's unit mock shim is not
    # appropriate for these separate-process HTTP integrations.
    pass


async def last_http_audit(root, previous):
    for _ in range(100):
        value = json.loads((root / "audit.json").read_text())
        if len(value.get("http", [])) > previous:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("HTTP request did not retain its before/after store comparison")


async def audited(root, call):
    previous = len(json.loads((root / "audit.json").read_text()).get("http", []))
    result = await call
    await last_http_audit(root, previous)
    return result


def assert_access_denied(result):
    # MCP may retain an ADCPTaskError as redacted text, whereas A2A returns
    # the structured classification. In either case no account data is sent.
    assert not result.success and result.data is None, result
    if result.adcp_error is not None:
        assert result.adcp_error["code"] == "UNAUTHORIZED", result
    else:
        assert result.error.endswith(
            "the reporting account or authenticated consumer is unavailable"
        ), result.error


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_typed_onboarding_reaches_real_mcp_production_admission(backend, tmp_path):
    async with onboarding_server(backend, tmp_path) as (uri, ready):
        raw = ready["requests"]["acct_a"]
        before = deepcopy(raw)
        validator = get_named_validator("account/sync-accounts-request.json")
        assert validator is not None and not list(validator.iter_errors(raw))
        request = SyncAccountsRequest.model_validate(raw)
        async with public_client(uri, "acct_a") as client:
            result = await client.sync_accounts(request)
        assert result.success, result
        assert raw == before
        audit = json.loads((tmp_path / "audit.json").read_text())
        assert audit["admissions"] == [
            {
                "account_id": "acct_a",
                "consumer_id": "urn:buyer:alpha",
                "scope": {"media_buy_ids": ["shared-media-buy"]},
            }
        ]


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_typed_scope_modes_accounts_and_invalid_requests_over_public_http(backend, tmp_path):
    async with onboarding_server(backend, tmp_path) as (uri, ready):
        for route in ("mcp", "a2a-0.3", "a2a-1.0"):
            for account in ("acct_a", "acct_b"):
                request = SyncAccountsRequest.model_validate(ready["requests"][account])
                async with public_client(uri, account, route) as client:
                    for _ in range(2):
                        response = await audited(tmp_path, client.sync_accounts(request))
                        assert response.success, response
                        wire = response.data.model_dump(mode="json", exclude_none=True)
                        assert [a["account_id"] for a in wire["accounts"]] == [account]
                        assert wire["accounts"][0]["reporting_delivery_configs"][0][
                            "configuration"
                        ]["scope"] == {"media_buy_ids": ["shared-media-buy"]}
                    own = await audited(
                        tmp_path,
                        client.get_reporting_status(
                            GetReportingStatusRequest.model_validate(
                                {
                                    "account": {"account_id": account},
                                    "view": "summary",
                                }
                            )
                        ),
                    )
                    assert own.success, own
                    other = "acct_b" if account == "acct_a" else "acct_a"
                    denied = await audited(
                        tmp_path,
                        client.get_reporting_status(
                            GetReportingStatusRequest.model_validate(
                                {
                                    "account": {"account_id": other},
                                    "view": "summary",
                                }
                            )
                        ),
                    )
                    assert_access_denied(denied)
                    denied = await audited(
                        tmp_path,
                        client.sync_accounts(
                            SyncAccountsRequest.model_validate(ready["requests"][other])
                        ),
                    )
                    assert_access_denied(denied)

                    for scope in ({}, {"all_media_buys": True}):
                        raw = deepcopy(ready["requests"][account])
                        raw["accounts"][0]["reporting_delivery_configs"][0]["scope"] = scope
                        before = json.loads((tmp_path / "audit.json").read_text())
                        response = await audited(
                            tmp_path, client.sync_accounts(SyncAccountsRequest.model_validate(raw))
                        )
                        assert not response.success and response.data is None, response
                        assert "VALIDATION_ERROR" not in str(response.error), response
                        after = await last_http_audit(tmp_path, len(before["http"]))
                        assert len(after["calls"]) == len(before["calls"]) + 1
                        assert after["calls"][-1]["accounts"][0]["reporting_delivery_configs"][0][
                            "scope"
                        ] == {"all_media_buys": True}
                        assert after["admissions"] == before["admissions"]
                        assert after["http"][-1]["store_unchanged"]

                    bad = deepcopy(ready["requests"][account])
                    bad["accounts"][0]["reporting_delivery_configs"][0]["scope"][
                        "all_media_buys"
                    ] = True
                    with pytest.raises(ValidationError):
                        SyncAccountsRequest.model_validate(bad)
                    before = json.loads((tmp_path / "audit.json").read_text())
                    # The raw negative control reaches the actual server
                    # validator; successful onboarding above is always typed.
                    rejected = await audited(tmp_path, client.adapter.sync_accounts(bad))
                    assert (
                        not rejected.success and rejected.adcp_error["code"] == "VALIDATION_ERROR"
                    ), rejected
                    after = await last_http_audit(tmp_path, len(before["http"]))
                    assert after["calls"] == before["calls"]
                    assert after["admissions"] == before["admissions"]
                    assert after["http"][-1]["store_unchanged"]


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("count", [0, 503])
async def test_typed_exact_revision_pages_after_actual_publication(backend, count, tmp_path):
    import hashlib

    from ._materializer_support import reference_rows

    async with onboarding_server(backend, tmp_path, scenario="exact", count=count) as (uri, ready):
        assert ready["source_requests"] == ready["destination_writes"] == 1
        for route in ("mcp", "a2a-0.3", "a2a-1.0"):
            raw = deepcopy(ready["request"])
            pages, rows, seen = [], [], set()
            async with public_client(uri, "acct_a", route) as client:
                for _ in range(7):
                    result = await client.get_media_buy_delivery(
                        GetMediaBuyDeliveryRequest.model_validate(raw)
                    )
                    assert result.success, result
                    page = result.data.model_dump(mode="json", exclude_none=True)
                    binding = page["reporting_revision_binding"]
                    assert binding["reporting_revision_id"] == raw["reporting_revision_id"]
                    assert binding["row_count"] == count
                    assert binding["content_sha256"] == ready["revision"]["content_sha256"]
                    assert page["reporting_revision"]["finality"] == "official"
                    pages.append(page)
                    rows.extend(page["reporting_rows"])
                    if not page["pagination"]["has_more"]:
                        break
                    cursor = page["pagination"]["cursor"]
                    assert cursor not in seen
                    seen.add(cursor)
                    raw["pagination"]["cursor"] = cursor
                else:
                    pytest.fail("exact revision traversal failed to terminate")
                assert len(pages) == (6 if count else 1)
                assert rows == reference_rows(count)
                # Protobuf responses spell ordinary integral JSON values as
                # doubles. JCS canonicalizes these fixture values identically;
                # this comparison does not relax financial ingress validation
                # or claim recovery of a value rounded before transmission.
                recomputed = hashlib.sha256(
                    rfc8785.dumps(
                        {
                            "reporting_revision_id": raw["reporting_revision_id"],
                            "row_count": count,
                            "control_totals": binding["control_totals"],
                            "reporting_rows": rows,
                        }
                    )
                ).hexdigest()
                assert recomputed == binding["content_sha256"]
                replay = await client.get_media_buy_delivery(
                    GetMediaBuyDeliveryRequest.model_validate(raw)
                )
                assert (
                    replay.success
                    and replay.data.model_dump(mode="json", exclude_none=True) == pages[-1]
                )
                for field in ("include_package_daily_breakdown", "include_window_breakdown"):
                    invalid = {**ready["request"], field: False}
                    with pytest.raises(ValidationError):
                        GetMediaBuyDeliveryRequest.model_validate(invalid)
                    rejected = await client.adapter.get_media_buy_delivery(invalid)
                    assert (
                        not rejected.success and rejected.adcp_error["code"] == "VALIDATION_ERROR"
                    ), rejected
            async with public_client(uri, "acct_b", route) as client:
                denied = await client.get_media_buy_delivery(
                    GetMediaBuyDeliveryRequest.model_validate(raw)
                )
                assert_access_denied(denied)
