"""sync_principal / get_principal ``reporting_subscriptions`` (AdCP 3.3, experimental)."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from adcp.server import (
    PrincipalChange,
    PrincipalIdentity,
    PrincipalService,
    ReportingSubscriptionOptions,
)

IDENTITY = PrincipalIdentity("https://buyer.example/agent", "buyer_agent")
BETA_2 = SimpleNamespace(resolved_adcp_version="3.3-beta.2")
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
DESTINATION = {
    "pattern": "file_transfer",
    "destination_id": "archive",
    "active": True,
    "provider": {"domain": "object-store.example"},
    "transport": "s3",
    "location": "s3://buyer-reporting/adcp/",
    "accepted_formats": ["parquet"],
    "accepted_verification_profiles": ["manifest_checksums"],
}
WAREHOUSE = {**DESTINATION, "destination_id": "warehouse", "location": "s3://buyer-wh/adcp/"}


def subscription(subscription_id: str = "analytics-daily", **overrides) -> dict:
    payload = {
        "subscription_id": subscription_id,
        "subscription_version": 1,
        "active": True,
        "scope": {"all_authorized_accounts": True},
        "selector": {
            "feed_purpose": "analytics",
            "period_duration": "P1D",
            "required_finality": "official",
            "dimensions": ["package"],
        },
        "reconciliation_mode": "delivery_only",
    }
    payload.update(overrides)
    return payload


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def service(*, delivery_config_ids: tuple[str, ...] = (), emitted: list | None = None):
    clock = Clock()

    async def ids(_identity: PrincipalIdentity) -> tuple[str, ...]:
        return delivery_config_ids

    async def emit(change: PrincipalChange) -> None:
        if emitted is not None:
            emitted.append(change)

    principals = PrincipalService(
        emit_change=emit,
        reporting_subscriptions=ReportingSubscriptionOptions(delivery_config_ids=ids, clock=clock),
    )
    return principals, clock


_keys = iter(range(10_000))


async def sync(principals: PrincipalService, configuration: dict, *, context=None, **extra):
    return await principals.sync_principal(
        IDENTITY,
        {
            "idempotency_key": f"subscriptions-key-{next(_keys):06d}",
            "configuration": configuration,
            **extra,
        },
        context=context,
    )


def states(response) -> dict:
    configuration = response.result.configuration
    return {
        item.subscription_id: item.model_dump(mode="json", exclude_none=True)
        for item in configuration.reporting_subscriptions or []
    }


def error_code(response) -> str:
    assert response.result.kind == "failed", response
    return response.result.errors[0].code


# -- option off --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_the_option_the_section_is_rejected() -> None:
    # Unsupported sections are rejected, not ignored (sync_principal.mdx): a
    # buyer told "applied" would believe it registered and receive nothing.
    principals = PrincipalService()
    response = await sync(principals, {"reporting_subscriptions": [subscription()]})
    assert error_code(response) == "UNSUPPORTED_FEATURE"
    assert (await principals.get_principal(IDENTITY)).result.kind == "unconfigured"
    # The other sections behave exactly as before.
    other = await sync(principals, {"reporting_destinations": [DESTINATION]})
    assert other.result.kind == "applied"


# -- acceptance and readback -----------------------------------------------------------


@pytest.mark.asyncio
async def test_accepts_and_reads_back_state() -> None:
    principals, _clock = service()
    response = await sync(
        principals,
        {
            "reporting_destinations": [DESTINATION],
            "reporting_subscriptions": [
                subscription(
                    destination={
                        "destination_id": "archive",
                        "path_template": "analytics/{account_id}/{period_start}",
                    }
                )
            ],
        },
    )
    assert response.result.kind == "applied"
    state = states(response)["analytics-daily"]
    destination_ref = response.result.configuration.reporting_destinations[0].destination_ref
    assert state == {
        "subscription_id": "analytics-daily",
        "subscription_version": 1,
        "state": "active",
        "accepted_at": "2026-10-01T12:00:00Z",
        "destination_ref": destination_ref,
        "configuration": subscription(
            destination={
                "destination_id": "archive",
                "path_template": "analytics/{account_id}/{period_start}",
            }
        ),
    }
    current = await principals.get_principal(IDENTITY)
    assert current.result.configuration.reporting_subscriptions[0].accepted_at == T0


@pytest.mark.asyncio
async def test_identical_resend_is_unchanged_and_keeps_accepted_at() -> None:
    principals, clock = service()
    first = await sync(principals, {"reporting_subscriptions": [subscription()]})
    clock.advance(hours=3)
    again = await sync(principals, {"reporting_subscriptions": [subscription()]})
    assert again.result.action == "unchanged"
    assert again.result.configuration_version == first.result.configuration_version
    assert states(again)["analytics-daily"]["accepted_at"] == "2026-10-01T12:00:00Z"


# -- versioning --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_version_records_prior_versions() -> None:
    principals, clock = service()
    await sync(principals, {"reporting_subscriptions": [subscription()]})
    clock.advance(days=2)
    v2 = subscription(subscription_version=2, exclude_account_ids=["acct-9"])
    response = await sync(principals, {"reporting_subscriptions": [v2]})
    state = states(response)["analytics-daily"]
    assert state["subscription_version"] == 2
    assert state["accepted_at"] == "2026-10-03T12:00:00Z"
    assert state["prior_versions"] == [
        {
            "subscription_version": 1,
            "accepted_at": "2026-10-01T12:00:00Z",
            "superseded_at": "2026-10-03T12:00:00Z",
        }
    ]
    clock.advance(days=1)
    v3 = subscription(subscription_version=3)
    state = states(await sync(principals, {"reporting_subscriptions": [v3]}))["analytics-daily"]
    assert [item["subscription_version"] for item in state["prior_versions"]] == [2, 1]


@pytest.mark.asyncio
async def test_reused_version_with_new_semantics_is_rejected() -> None:
    principals, _clock = service()
    await sync(principals, {"reporting_subscriptions": [subscription()]})
    changed = subscription(exclude_account_ids=["acct-9"])
    assert error_code(await sync(principals, {"reporting_subscriptions": [changed]})) == (
        "INVALID_REQUEST"
    )
    older = subscription(subscription_version=1)
    await sync(principals, {"reporting_subscriptions": [subscription(subscription_version=2)]})
    assert error_code(await sync(principals, {"reporting_subscriptions": [older]})) == (
        "INVALID_REQUEST"
    )


@pytest.mark.asyncio
async def test_active_changes_in_place() -> None:
    principals, clock = service()
    await sync(principals, {"reporting_subscriptions": [subscription()]})
    clock.advance(hours=5)
    response = await sync(principals, {"reporting_subscriptions": [subscription(active=False)]})
    state = states(response)["analytics-daily"]
    assert state["subscription_version"] == 1
    assert state["state"] == "inactive"
    assert state["deactivated_at"] == "2026-10-01T17:00:00Z"
    assert state["accepted_at"] == "2026-10-01T12:00:00Z"
    clock.advance(hours=1)
    again = await sync(principals, {"reporting_subscriptions": [subscription(active=False)]})
    assert states(again)["analytics-daily"]["deactivated_at"] == "2026-10-01T17:00:00Z"


@pytest.mark.asyncio
async def test_replace_account_configs_is_refused_until_implemented() -> None:
    principals, _clock = service()
    response = await sync(
        principals, {"reporting_subscriptions": [subscription(replace_account_configs=True)]}
    )
    assert error_code(response) == "UNSUPPORTED_FEATURE"
    allowed = await sync(
        principals, {"reporting_subscriptions": [subscription(replace_account_configs=False)]}
    )
    assert allowed.result.kind == "applied"


@pytest.mark.asyncio
async def test_readback_lists_inactive_intervals() -> None:
    principals, clock = service()
    await sync(principals, {"reporting_subscriptions": [subscription()]})
    clock.advance(hours=5)
    await sync(principals, {"reporting_subscriptions": [subscription(active=False)]})
    clock.advance(hours=24)
    reactivated = await sync(
        principals, {"reporting_subscriptions": [subscription()]}, context=BETA_2
    )
    state = states(reactivated)["analytics-daily"]
    assert state["state"] == "active" and "deactivated_at" not in state
    assert state["inactive_intervals"] == [
        {"deactivated_at": "2026-10-01T17:00:00Z", "reactivated_at": "2026-10-02T17:00:00Z"}
    ]
    clock.advance(hours=1)
    omitted = await sync(principals, {"reporting_subscriptions": []}, context=BETA_2)
    state = states(omitted)["analytics-daily"]
    assert state["inactive_intervals"][-1] == {"deactivated_at": "2026-10-02T18:00:00Z"}
    assert state["deactivated_at"] == "2026-10-02T18:00:00Z"
    current = await principals.get_principal(IDENTITY, context=BETA_2)
    read = current.result.configuration.reporting_subscriptions[0]
    assert len(read.inactive_intervals) == 2
    # A caller served 3.3.0-beta.1 (or with no known release) never sees the
    # field, so the response validates against the pinned schema.
    for context in (None, SimpleNamespace(resolved_adcp_version="3.3-beta.1")):
        older = await principals.get_principal(IDENTITY, context=context)
        assert older.result.configuration.reporting_subscriptions[0].inactive_intervals is None


@pytest.mark.asyncio
async def test_omitted_subscription_is_deactivated_and_retained() -> None:
    principals, clock = service()
    await sync(
        principals,
        {
            "reporting_subscriptions": [
                subscription(),
                subscription(
                    "pacing",
                    selector={
                        "feed_purpose": "pacing",
                        "period_duration": "PT1H",
                        "required_finality": "snapshot",
                    },
                ),
            ]
        },
    )
    clock.advance(hours=2)
    response = await sync(principals, {"reporting_subscriptions": [subscription()]})
    pacing = states(response)["pacing"]
    assert pacing["state"] == "inactive"
    assert pacing["deactivated_at"] == "2026-10-01T14:00:00Z"
    assert pacing["configuration"]["active"] is False
    cleared = await sync(principals, {"reporting_subscriptions": []})
    assert cleared.result.action == "cleared"
    assert {item["state"] for item in states(cleared).values()} == {"inactive"}


# -- validation ------------------------------------------------------------------------------


INVALID = {
    "both scopes": subscription(scope={"all_authorized_accounts": True, "account_ids": ["a"]}),
    "empty scope": subscription(scope={}),
    "exclude with account_ids": subscription(
        scope={"account_ids": ["a"]}, exclude_account_ids=["b"]
    ),
    "period_timezone with all accounts": subscription(
        selector={
            "feed_purpose": "analytics",
            "period_duration": "P1D",
            "required_finality": "official",
            "period_timezone": "America/New_York",
        }
    ),
    "period_timezone with two accounts": subscription(
        scope={"account_ids": ["a", "b"]},
        selector={
            "feed_purpose": "analytics",
            "period_duration": "P1D",
            "required_finality": "official",
            "period_timezone": "America/New_York",
        },
    ),
    "unknown period_timezone": subscription(
        scope={"account_ids": ["a"]},
        selector={
            "feed_purpose": "analytics",
            "period_duration": "P1D",
            "required_finality": "official",
            "period_timezone": "Mars/Olympus_Mons",
        },
    ),
    "billing with snapshot": subscription(
        selector={
            "feed_purpose": "billing",
            "period_duration": "P1D",
            "required_finality": "snapshot",
        },
        reconciliation_mode="consumer_receipt",
        destination={"destination_id": "archive", "path_template": "b/{account_id}"},
    ),
    "billing without receipts": subscription(
        selector={
            "feed_purpose": "billing",
            "period_duration": "P1D",
            "required_finality": "official",
        },
        destination={"destination_id": "archive", "path_template": "b/{account_id}"},
    ),
    "receipts without destination": subscription(reconciliation_mode="consumer_receipt"),
    "missing destination": subscription(
        destination={"destination_id": "nowhere", "path_template": "x/{account_id}"}
    ),
    "multi-account file_transfer without template": subscription(
        destination={"destination_id": "archive"}
    ),
    "multi-account template without account_id": subscription(
        destination={"destination_id": "archive", "path_template": "analytics/{period_start}"}
    ),
    "unknown placeholder": subscription(
        destination={"destination_id": "archive", "path_template": "a/{account_id}/{brand}"}
    ),
    "subscription_id equals a delivery_config_id": subscription("acme-pacing"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(INVALID))
async def test_invalid_subscriptions_fail_the_whole_request(case: str) -> None:
    principals, _clock = service(delivery_config_ids=("acme-pacing",))
    response = await sync(
        principals,
        {"reporting_destinations": [DESTINATION], "reporting_subscriptions": [INVALID[case]]},
    )
    assert error_code(response) == "INVALID_REQUEST"
    assert (await principals.get_principal(IDENTITY)).result.kind == "unconfigured"


@pytest.mark.asyncio
async def test_single_account_subscription_needs_no_account_placeholder() -> None:
    principals, _clock = service()
    single = subscription(
        scope={"account_ids": ["acct-1"]},
        destination={"destination_id": "archive", "path_template": "acme/{period_start}"},
        selector={
            "feed_purpose": "analytics",
            "period_duration": "P1D",
            "required_finality": "official",
            "period_timezone": "America/Chicago",
        },
    )
    response = await sync(
        principals, {"reporting_destinations": [DESTINATION], "reporting_subscriptions": [single]}
    )
    assert response.result.kind == "applied"


@pytest.mark.asyncio
async def test_set_level_rules() -> None:
    principals, _clock = service()
    duplicate = await sync(
        principals, {"reporting_subscriptions": [subscription(), subscription()]}
    )
    assert error_code(duplicate) == "INVALID_REQUEST"
    two_all = await sync(
        principals,
        {"reporting_subscriptions": [subscription("a"), subscription("b")]},
    )
    assert error_code(two_all) == "INVALID_REQUEST"
    disjoint_operators = await sync(
        principals,
        {
            "reporting_subscriptions": [
                subscription("a", operator_domains=["one.example"]),
                subscription("b", operator_domains=["two.example"]),
            ]
        },
    )
    assert disjoint_operators.result.kind == "applied"
    shared_accounts = await sync(
        principals,
        {
            "reporting_subscriptions": [
                subscription("a", scope={"account_ids": ["x", "y"]}),
                subscription("b", scope={"account_ids": ["y"]}),
            ]
        },
    )
    assert error_code(shared_accounts) == "INVALID_REQUEST"
    # Different levels are precedence, not a conflict.
    levels = await sync(
        principals,
        {
            "reporting_subscriptions": [
                subscription("all"),
                subscription("named", scope={"account_ids": ["y"]}),
            ]
        },
    )
    assert levels.result.kind == "applied"


@pytest.mark.asyncio
async def test_shared_destination_templates_must_differ() -> None:
    principals, _clock = service()
    first = subscription(
        "a", destination={"destination_id": "archive", "path_template": "x/{account_id}"}
    )
    second = subscription(
        "b",
        selector={
            "feed_purpose": "pacing",
            "period_duration": "PT1H",
            "required_finality": "snapshot",
        },
        destination={"destination_id": "archive", "path_template": "x/{account_id}"},
    )
    response = await sync(
        principals,
        {"reporting_destinations": [DESTINATION], "reporting_subscriptions": [first, second]},
    )
    assert error_code(response) == "INVALID_REQUEST"
    second["destination"]["path_template"] = "y/{account_id}"
    response = await sync(
        principals,
        {"reporting_destinations": [DESTINATION], "reporting_subscriptions": [first, second]},
    )
    assert response.result.kind == "applied"


# -- interaction with the other sections ---------------------------------------------------


@pytest.mark.asyncio
async def test_destination_removed_by_a_later_request_fails_it() -> None:
    principals, _clock = service()
    named = subscription(
        destination={"destination_id": "warehouse", "path_template": "a/{account_id}"}
    )
    await sync(
        principals,
        {"reporting_destinations": [DESTINATION, WAREHOUSE], "reporting_subscriptions": [named]},
    )
    response = await sync(principals, {"reporting_destinations": [DESTINATION]})
    assert error_code(response) == "INVALID_REQUEST"
    # Removing both in one request is fine: the subscription is deactivated.
    both = await sync(
        principals, {"reporting_destinations": [DESTINATION], "reporting_subscriptions": []}
    )
    assert both.result.kind == "applied"


@pytest.mark.asyncio
async def test_dry_run_and_stale_configuration_version() -> None:
    principals, _clock = service()
    first = await sync(principals, {"reporting_subscriptions": [subscription()]})
    dry = await sync(
        principals,
        {"reporting_subscriptions": [subscription(subscription_version=2)]},
        dry_run=True,
    )
    assert dry.result.kind == "validated"
    assert dry.result.action == "would_update"
    current = await principals.get_principal(IDENTITY)
    assert current.result.configuration.reporting_subscriptions[0].subscription_version == 1
    invalid_dry = await sync(
        principals, {"reporting_subscriptions": [subscription(scope={})]}, dry_run=True
    )
    assert error_code(invalid_dry) == "INVALID_REQUEST"
    stale = await sync(
        principals,
        {"reporting_subscriptions": [subscription(subscription_version=2)]},
        expected_configuration_version="cfg:stale",
    )
    assert error_code(stale) == "CONFLICT"
    fresh = await sync(
        principals,
        {"reporting_subscriptions": [subscription(subscription_version=2)]},
        expected_configuration_version=first.result.configuration_version,
    )
    assert fresh.result.kind == "applied"


@pytest.mark.asyncio
async def test_destination_needing_action_moves_the_subscription_to_action_required() -> None:
    emitted: list[PrincipalChange] = []
    principals, _clock = service(emitted=emitted)
    named = subscription(
        destination={"destination_id": "archive", "path_template": "a/{account_id}"}
    )
    await sync(
        principals, {"reporting_destinations": [DESTINATION], "reporting_subscriptions": [named]}
    )
    await principals.transition_destination(
        IDENTITY,
        "archive",
        "action_required",
        issues=[{"code": "ACCESS_DENIED", "message": "grant write access"}],
    )
    state = (await principals.get_principal(IDENTITY)).result.configuration
    subscription_state = state.reporting_subscriptions[0]
    assert subscription_state.state == "action_required"
    assert subscription_state.issues[0].code == "DESTINATION_NOT_READY"
    reasons = [(item.reason, item.subscription_id) for item in emitted]
    assert ("reporting_subscription_changed", "analytics-daily") in reasons
    emitted.clear()
    await principals.transition_destination(IDENTITY, "archive", "ready")
    state = (await principals.get_principal(IDENTITY)).result.configuration
    assert state.reporting_subscriptions[0].state == "active"
    assert state.reporting_subscriptions[0].issues is None
    # The recovery is announced too.
    assert ("reporting_subscription_changed", "analytics-daily") in [
        (item.reason, item.subscription_id) for item in emitted
    ]


@pytest.mark.asyncio
async def test_destination_transitions_work_without_a_change_emitter() -> None:
    principals = PrincipalService(reporting_subscriptions=ReportingSubscriptionOptions())
    named = subscription(
        destination={"destination_id": "archive", "path_template": "a/{account_id}"}
    )
    await sync(
        principals, {"reporting_destinations": [DESTINATION], "reporting_subscriptions": [named]}
    )
    await principals.transition_destination(IDENTITY, "archive", "action_required")
    await principals.transition_destination(IDENTITY, "archive", "ready")
    state = (await principals.get_principal(IDENTITY)).result.configuration
    assert state.reporting_subscriptions[0].state == "active"


@pytest.mark.asyncio
async def test_seller_status_updates_do_not_bump_the_configuration_version() -> None:
    emitted: list[PrincipalChange] = []
    principals, _clock = service(emitted=emitted)
    first = await sync(principals, {"reporting_subscriptions": [subscription()]})
    changed = await principals.update_subscription_status(
        IDENTITY,
        "analytics-daily",
        accounts_with_exceptions=[{"account_id": "acct-1", "reasons": ["capacity"]}],
        accounts_unauthorized=[],
    )
    assert changed is True
    unchanged = await principals.update_subscription_status(
        IDENTITY,
        "analytics-daily",
        accounts_with_exceptions=[{"account_id": "acct-1", "reasons": ["capacity"]}],
    )
    assert unchanged is False
    current = await principals.get_principal(IDENTITY)
    assert current.result.configuration_version == first.result.configuration_version
    state = current.result.configuration.reporting_subscriptions[0]
    assert state.accounts_with_exceptions[0].reasons[0] == "capacity"
    assert [item.reason for item in emitted] == ["reporting_subscription_changed"]
    # A resend keeps seller-derived status.
    resend = await sync(principals, {"reporting_subscriptions": [deepcopy(subscription())]})
    assert states(resend)["analytics-daily"]["accounts_with_exceptions"] == [
        {"account_id": "acct-1", "reasons": ["capacity"]}
    ]


@pytest.mark.asyncio
async def test_resends_that_reorder_or_spell_out_defaults_are_the_same_version() -> None:
    principals, _clock = service()
    first = subscription(scope={"account_ids": ["b", "a"]})
    await sync(principals, {"reporting_subscriptions": [first]})
    reordered = subscription(
        scope={"account_ids": ["a", "b"]},
        selector={**first["selector"], "match": "exact"},
    )
    response = await sync(principals, {"reporting_subscriptions": [reordered]})
    assert response.result.kind == "applied"


@pytest.mark.asyncio
async def test_braces_outside_placeholders_are_refused() -> None:
    principals, _clock = service()
    for template in ("a/{{account_id}}", "a/}{account_id}{", "a/{account_id}/x}"):
        response = await sync(
            principals,
            {
                "reporting_destinations": [DESTINATION],
                "reporting_subscriptions": [
                    subscription(
                        destination={"destination_id": "archive", "path_template": template}
                    )
                ],
            },
        )
        assert error_code(response) == "INVALID_REQUEST", template


def test_render_path_template() -> None:
    from adcp.reporting.subscriptions import render_path_template

    rendered = render_path_template(
        "analytics/{account_id}/{offering_id}/{period_start}",
        account_id="acme/eu",
        subscription_id="analytics-daily",
        offering_id="..",
        period_start=datetime(2026, 10, 1, 4, tzinfo=timezone.utc),
    )
    # A value with "/" or equal to "." / ".." fills exactly one segment.
    assert rendered == "analytics/acme%2Feu/%2E%2E/2026-10-01T04:00:00Z"
    backslash = render_path_template(
        "a/{account_id}/{subscription_id}",
        account_id="..\\..\\secret",
        subscription_id="analytics-daily",
        offering_id="o",
        period_start=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert backslash == "a/..%5C..%5Csecret/analytics-daily"


@pytest.mark.asyncio
async def test_inactive_intervals_are_never_dropped() -> None:
    principals, clock = service()
    await sync(principals, {"reporting_subscriptions": [subscription()]})
    for _ in range(64):
        clock.advance(hours=1)
        await sync(principals, {"reporting_subscriptions": [subscription(active=False)]})
        clock.advance(hours=1)
        await sync(principals, {"reporting_subscriptions": [subscription()]})
    clock.advance(hours=1)
    response = await sync(principals, {"reporting_subscriptions": [subscription(active=False)]})
    assert error_code(response) == "INVALID_REQUEST"
    current = await principals.get_principal(IDENTITY, context=BETA_2)
    assert len(current.result.configuration.reporting_subscriptions[0].inactive_intervals) == 64


@pytest.mark.asyncio
async def test_prior_versions_carry_their_configuration_for_beta_2() -> None:
    principals, clock = service()
    await sync(principals, {"reporting_subscriptions": [subscription()]})
    clock.advance(days=1)
    await sync(
        principals,
        {
            "reporting_subscriptions": [
                subscription(subscription_version=2, exclude_account_ids=["x"])
            ]
        },
    )
    served = await principals.get_principal(IDENTITY, context=BETA_2)
    (prior,) = served.result.configuration.reporting_subscriptions[0].prior_versions
    assert prior.configuration.subscription_version == 1
    older = await principals.get_principal(IDENTITY)
    assert (
        older.result.configuration.reporting_subscriptions[0].prior_versions[0].configuration
        is None
    )


@pytest.mark.asyncio
async def test_subscription_ids_for_the_reverse_namespace_check() -> None:
    principals, _clock = service()
    await sync(
        principals,
        {
            "reporting_subscriptions": [
                subscription(),
                subscription(
                    "pacing",
                    selector={
                        "feed_purpose": "pacing",
                        "period_duration": "PT1H",
                        "required_finality": "snapshot",
                    },
                ),
            ]
        },
    )
    await sync(principals, {"reporting_subscriptions": [subscription()]})
    assert await principals.reporting_subscription_ids(IDENTITY) == {"analytics-daily", "pacing"}
