"""The Python reference seller for reporting subscriptions.

It passes the same storyboard the TypeScript reference seller passes
(adcp-client ``examples/storyboards/reporting-subscriptions.yaml``, vendored
below), run in process over MCP; a seller whose readback drops
``prior_versions`` fails it. Its ``comply_test_controller`` serves
``reporting_subscription_probe``, so a remote runner can drive grants,
timezones and the clock, and owed periods match what a buyer computes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from adcp.testing.reporting import create_reference_reporting_seller
from adcp.validation.schema_loader import get_named_validator
from tests.storyboard_runner import load_storyboard, mcp_call, run_storyboard

pytestmark = pytest.mark.asyncio

STORYBOARD_PATH = (
    Path(__file__).parent / "fixtures" / "storyboards" / "reporting-subscriptions.yaml"
)
#: adcontextprotocol/adcp-client commit the vendored storyboard comes from (PR #3534 and #3560).
STORYBOARD_COMMIT = "eeeb370dec63002ddf599823f63b9c1115df3436"
STORYBOARD_SHA256 = "5eac5cb2468d408d5fa08a4fb7735eab3b508b68e970f6c985aad9fdf191db7a"
VERSION = "3.3-beta.3"


def test_the_vendored_storyboard_is_the_pinned_typescript_file() -> None:
    assert hashlib.sha256(STORYBOARD_PATH.read_bytes()).hexdigest() == STORYBOARD_SHA256


async def _run(seller):
    await seller.service.run_worker()
    return await run_storyboard(
        mcp_call(seller.mcp_server()),
        load_storyboard(STORYBOARD_PATH),
        schema=get_named_validator,
        adcp_version=VERSION,
    )


async def test_the_reference_seller_passes_every_storyboard_step() -> None:
    result = await _run(create_reference_reporting_seller())
    failures = {
        step.step_id: [(v.check, v.path, v.message) for v in step.validations if not v.passed]
        for step in result.steps
        if not step.passed
    }
    assert result.passed, failures
    assert len(result.steps) >= 15


async def test_a_seller_whose_readback_drops_prior_versions_fails_it() -> None:
    seller = create_reference_reporting_seller()
    platform = seller.platform

    def dropping(original):
        async def call(params, context=None):
            response = await original(params, context)
            configuration = getattr(getattr(response, "result", None), "configuration", None)
            for state in getattr(configuration, "reporting_subscriptions", None) or ():
                state.prior_versions = None
            return response

        return call

    platform.sync_principal = dropping(platform.sync_principal)
    platform.get_principal = dropping(platform.get_principal)
    result = await _run(seller)
    assert not result.passed
    # Every step that reads prior_versions fails, and nothing else does.
    assert result.failed_steps == ["apply_version_2", "read_back_version_2", "omit_subscription"]
    failures = [v for step in result.steps for v in step.validations if not v.passed]
    assert failures
    assert all(".prior_versions[0]." in (v.path or "") for v in failures), failures


async def test_the_controller_drives_grants_and_the_clock_over_mcp() -> None:
    from adcp.testing.reporting import KitBuyer

    seller = create_reference_reporting_seller(start="2026-11-01T00:00:00Z")
    await seller.service.run_worker()
    call = mcp_call(seller.mcp_server(principal="agency-x"))

    account = {"account_id": "acct-reference", "sandbox": True}

    async def control(scenario, **params):
        structured, error = await call(
            "comply_test_controller",
            {"scenario": scenario, "params": params, "account": account, "adcp_version": VERSION},
        )
        assert error is None and structured.get("success"), structured
        return structured

    listed, _ = await call(
        "comply_test_controller", {"scenario": "list_scenarios", "account": account}
    )
    assert {"reporting_subscription_probe", "seed_media_buy"} <= set(listed["scenarios"])
    await control(
        "seed_media_buy",
        media_buy_id="mb_1",
        fixture={
            "account_id": "acct-reference",
            "confirmed_at": "2026-11-01T00:00:00Z",
            "start_time": "2026-11-01T00:00:00Z",
            "end_time": "2026-11-20T00:00:00Z",
        },
    )
    buyer = KitBuyer(seller.platform, "agency-x", seller.service)
    await buyer.reporting.subscribe(
        {
            "subscription_id": "analytics-daily",
            "scope": {"all_authorized_accounts": True},
            "selector": {
                "feed_purpose": "analytics",
                "period_duration": "P1D",
                "required_finality": "snapshot",
            },
            "reconciliation_mode": "delivery_only",
        }
    )
    moved = await control(
        "reporting_subscription_probe", operation="advance_time", target_time="2026-11-04T07:00:00Z"
    )
    assert moved["simulated_now"] == "2026-11-04T07:00:00Z"
    status, _ = await call(
        "get_reporting_status",
        {"account": {"account_id": "acct-reference"}, "view": "periods", "adcp_version": VERSION},
    )
    served = sorted(period["period"]["start"] for period in status["periods"])
    report = await buyer.reporting.reconcile(now=seller.clock())
    # owed_periods_match: what the seller planned is what the buyer computes.
    assert served == sorted(line.period["start"] for line in report.periods)
    assert served == ["2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z"]
    await control(
        "reporting_subscription_probe", operation="revoke_grant", account_id="acct-reference"
    )
    principal, _ = await call("get_principal", {"adcp_version": VERSION})
    state = principal["result"]["configuration"]["reporting_subscriptions"][0]
    assert [entry["account_id"] for entry in state["accounts_unauthorized"]] == ["acct-reference"]
    await control(
        "reporting_subscription_probe", operation="advance_time", target_time="2026-11-04T09:00:00Z"
    )
    regrant = await control(
        "reporting_subscription_probe", operation="regrant", account_id="acct-reference"
    )
    assert regrant["chain"] == "continued" and regrant["regranted_at"] == "2026-11-04T09:00:00Z"
    zone = await control(
        "reporting_subscription_probe",
        operation="set_account_timezone",
        account_id="acct-reference",
        timezone="Europe/Paris",
    )
    assert zone["previous_timezone"] == "UTC"
    reduced = await control(
        "reporting_subscription_probe",
        operation="reduce_grant",
        account_id="acct-reference",
        allowed_tasks=["get_reporting_status"],
    )
    assert reduced["success"]


def test_the_standalone_server_maps_bearer_tokens_to_principals(monkeypatch) -> None:
    from types import SimpleNamespace

    from adcp.testing import reporting

    meta = SimpleNamespace(
        request_context=SimpleNamespace(headers={"authorization": "Bearer agency-x"})
    )
    assert reporting._bearer_principal(meta).caller_identity == "agency-x"
    assert reporting._bearer_principal(SimpleNamespace()).caller_identity == "storyboard-buyer"
    served: dict = {}
    import sys

    monkeypatch.setattr(
        sys.modules["adcp.server.serve"], "serve", lambda platform, **kwargs: served.update(kwargs)
    )
    assert reporting.main(["reporting", "--port", "3999", "--start", "2026-11-01T00:00:00Z"]) == 0
    assert served["port"] == 3999 and served["test_controller"] is not None
    assert served["context_factory"] is reporting._bearer_principal
