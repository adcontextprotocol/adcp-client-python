"""Agent-scoped reporting capability discovery and generation admission."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from adcp.reporting.fixtures import redacted_capabilities
from adcp.reporting.ledger import InMemoryReportingLedgerStore, ReportingStatusCaller
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.service import ReliableReportingConfigurationError, ReliableReportingService
from adcp.reporting.testing import ScriptedReportingAdapter
from adcp.server import AccountAwareToolContext, ADCPHandler, RequestMetadata
from adcp.server.auth import ROUTED_TENANT_METADATA_KEY, auth_context_factory
from tests.test_reliable_reporting_service import (
    _account_binding,
    _account_context,
    _capability_offering,
    _configuration,
    _rows,
)


def _unexpected_deactivation(*args):
    raise AssertionError("unexpected deactivation")


class _CatalogService(ReliableReportingService):
    def __init__(self, **kwargs):
        kwargs.setdefault("supports_frozen_offering", lambda *_: True)
        kwargs.setdefault("deactivate_unserved_generation", _unexpected_deactivation)
        super().__init__(**kwargs)


@pytest.mark.asyncio
async def test_agent_catalog_changes_capability_version_without_cross_agent_leakage() -> None:
    _, _, capabilities, first = _account_binding("a")
    _, _, _, second = _account_binding("b")
    catalogs = {"agent-a": [first], "agent-b": [second]}
    service = _CatalogService.memory(
        account_context=lambda _: None,
        agent_offerings=lambda agent: catalogs[agent],
        report_definition_versions={first["report_definition_sha256"]: "1.2"},
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    base = {"supported_protocols": ["media_buy"], "adcp": {"major_versions": [3]}}
    with pytest.raises(ReliableReportingConfigurationError, match="inject_agent_capabilities"):
        service.inject_capabilities(base)
    with pytest.raises(ReliableReportingConfigurationError, match="inject_agent_capabilities"):
        service.capability_block()
    a1 = await service.inject_agent_capabilities(base, agent_id="agent-a", adcp_version="3.3-rc.3")
    a2 = await service.inject_agent_capabilities(base, agent_id="agent-a", adcp_version="3.3-rc.3")
    b = await service.inject_agent_capabilities(base, agent_id="agent-b", adcp_version="3.3-rc.3")
    assert a1 == a2
    older = await service.inject_agent_capabilities(base, agent_id="agent-a", adcp_version="3.2.1")
    assert older["adcp"]["capability_changes"]["capabilities_version"] == (
        a1["adcp"]["capability_changes"]["capabilities_version"]
    )
    assert "reporting_delivery" not in older.get("media_buy", {})
    assert await service.agent_capabilities_version("agent-a") == (
        a1["adcp"]["capability_changes"]["capabilities_version"]
    )
    assert a1["media_buy"]["reporting_delivery"]["offerings"] == [first]
    assert b["media_buy"]["reporting_delivery"]["offerings"] == [second]

    catalogs["agent-a"] = [replace_offering(first)]
    updated = await service.inject_agent_capabilities(
        base, agent_id="agent-a", adcp_version="3.3-rc.3"
    )
    assert updated["adcp"]["capability_changes"]["capabilities_version"] != (
        a1["adcp"]["capability_changes"]["capabilities_version"]
    )
    assert (
        await service.inject_agent_capabilities(base, agent_id="agent-b", adcp_version="3.3-rc.3")
        == b
    )
    catalogs["agent-a"] = []
    empty = await service.inject_agent_capabilities(
        {
            **base,
            "media_buy": {"reporting_delivery": {"offerings": [second]}},
        },
        agent_id="agent-a",
        adcp_version="3.3-rc.3",
    )
    assert "reporting_delivery" not in empty.get("media_buy", {})
    assert empty["adcp"]["capability_changes"]["capabilities_version"] != (
        updated["adcp"]["capability_changes"]["capabilities_version"]
    )


def replace_offering(offering: dict) -> dict:
    return {**offering, "standard_dimensions": ["campaign_id"]}


@pytest.mark.asyncio
async def test_installed_handler_requires_trusted_serving_agent_resolver() -> None:
    _, _, capabilities, offering = _account_binding("a")
    service = _CatalogService.memory(
        account_context=lambda _: None, agent_offerings=lambda _: [offering]
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))

    class Seller(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"supported_protocols": ["media_buy"], "adcp": {"major_versions": [3]}}

    handler = service.install(Seller())
    with pytest.raises(ReliableReportingConfigurationError, match="serving_agent"):
        await handler.get_adcp_capabilities({}, AccountAwareToolContext(tenant_id="caller"))


@pytest.mark.asyncio
async def test_installed_handler_uses_serving_agent_not_caller_account() -> None:
    _, _, capabilities, first = _account_binding("a")
    _, _, _, second = _account_binding("b")
    catalogs = {"agent-a": [first], "agent-b": [second]}
    service = _CatalogService.memory(
        account_context=lambda _: None,
        agent_offerings=lambda agent: catalogs[agent],
        serving_agent=lambda context: context.metadata[ROUTED_TENANT_METADATA_KEY],
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))

    class Seller(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"supported_protocols": ["media_buy"], "adcp": {"major_versions": [3]}}

    handler = service.install(Seller())

    def context(agent, account):
        request = SimpleNamespace(
            state=SimpleNamespace(
                adcp_auth_principal=account,
                adcp_auth_tenant=account,
                adcp_auth_principal_metadata={},
                adcp_routed_tenant=agent,
            )
        )
        metadata = RequestMetadata(
            tool_name="get_adcp_capabilities", transport="mcp", request_context=request
        )
        ctx = auth_context_factory(metadata)
        ctx.resolved_adcp_version = "3.3-rc.3"
        return AccountAwareToolContext(**vars(ctx), account_id=account)

    a1 = await handler.get_adcp_capabilities({}, context("agent-a", "account-a"))
    a2 = await handler.get_adcp_capabilities({}, context("agent-a", "account-b"))
    anonymous = await handler.get_adcp_capabilities({}, context("agent-a", None))
    b = await handler.get_adcp_capabilities({}, context("agent-b", "account-a"))
    assert a1 == a2
    assert a1 == anonymous
    assert a1["media_buy"]["reporting_delivery"]["offerings"] == [first]
    assert b["media_buy"]["reporting_delivery"]["offerings"] == [second]


@pytest.mark.asyncio
async def test_live_definition_versions_are_negotiated() -> None:
    _, _, capabilities, first = _account_binding("a")
    _, _, _, second = _account_binding("b")
    offerings = [first]
    versions = {first["report_definition_sha256"]: "1.1"}
    service = _CatalogService.memory(
        account_context=lambda _: None,
        agent_offerings=lambda _: offerings,
        agent_definition_versions=lambda _: versions,
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    base = {"supported_protocols": ["media_buy"], "adcp": {"major_versions": [3]}}
    before = await service.inject_agent_capabilities(base, agent_id="agent-a", adcp_version="3.2.1")
    offerings.append(second)
    unknown = await service.inject_agent_capabilities(
        base, agent_id="agent-a", adcp_version="3.2.1"
    )
    assert unknown["media_buy"]["reporting_delivery"]["offerings"] == [first]
    versions[second["report_definition_sha256"]] = "1.2"
    older = await service.inject_agent_capabilities(base, agent_id="agent-a", adcp_version="3.2.1")
    current = await service.inject_agent_capabilities(
        base, agent_id="agent-a", adcp_version="3.3-rc.3"
    )
    assert older["media_buy"]["reporting_delivery"]["offerings"] == [first]
    assert current["media_buy"]["reporting_delivery"]["offerings"] == [first, second]
    assert before["adcp"]["capability_changes"]["capabilities_version"] != (
        current["adcp"]["capability_changes"]["capabilities_version"]
    )
    assert unknown["adcp"]["capability_changes"]["capabilities_version"] != (
        current["adcp"]["capability_changes"]["capabilities_version"]
    )


@pytest.mark.asyncio
async def test_registered_unknown_definition_preserves_version_policy() -> None:
    _, _, capabilities, offering = _account_binding("a")
    service = _CatalogService.memory(
        account_context=lambda _: None, agent_offerings=lambda _: [offering]
    )
    service.sources.register(
        "gam", ScriptedReportingAdapter(capabilities, []), capability_offerings=[offering]
    )
    response = await service.inject_agent_capabilities({}, agent_id="agent-a", adcp_version="3.2.1")
    assert response["media_buy"]["reporting_delivery"]["offerings"] == [offering]


@pytest.mark.asyncio
async def test_other_agents_definition_versions_do_not_change_the_catalog() -> None:
    _, _, capabilities, offering = _account_binding("a")
    service = _CatalogService.memory(
        account_context=lambda _: None,
        agent_offerings=lambda _: [offering],
        agent_definition_versions=lambda agent: (
            {offering["report_definition_sha256"]: "1.1"} if agent == "agent-b" else {}
        ),
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    first = await service.inject_agent_capabilities({}, agent_id="agent-a", adcp_version="3.2.1")
    await service.inject_agent_capabilities({}, agent_id="agent-b", adcp_version="3.2.1")
    again = await service.inject_agent_capabilities({}, agent_id="agent-a", adcp_version="3.2.1")
    assert first == again
    assert "reporting_delivery" not in first.get("media_buy", {})


@pytest.mark.asyncio
async def test_context_unknown_definition_preserves_version_policy() -> None:
    generation = _configuration()
    context = replace(_account_context(generation), serving_agent_id="agent-a")
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(generation)
    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [dict(context.capability_offering)],
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    await service.initialize()
    response = await service.inject_agent_capabilities({}, agent_id="agent-a", adcp_version="3.2.1")
    assert response["media_buy"]["reporting_delivery"]["offerings"] == [_capability_offering("gam")]


@pytest.mark.asyncio
async def test_context_declarations_do_not_change_another_agents_version_policy() -> None:
    generation = _configuration()
    context = replace(_account_context(generation), serving_agent_id="agent-a")
    store = InMemoryReportingLedgerStore()
    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [_capability_offering("gam")],
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    await service.initialize()
    before = await service.inject_agent_capabilities({}, agent_id="agent-b", adcp_version="3.2.1")
    await store.put_configuration(generation)
    await service._recover_configuration(generation)
    after = await service.inject_agent_capabilities({}, agent_id="agent-b", adcp_version="3.2.1")
    assert before == after
    assert "reporting_delivery" not in after.get("media_buy", {})


@pytest.mark.asyncio
async def test_capability_version_tracks_reporting_commitments() -> None:
    _, _, capabilities, offering = _account_binding("a")

    def service(enabled):
        result = _CatalogService.memory(
            account_context=lambda _: None,
            agent_offerings=lambda _: [offering],
            consumer_status_enabled=enabled,
        )
        result.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
        return result

    assert await service(False).agent_capabilities_version("agent-a") != (
        await service(True).agent_capabilities_version("agent-a")
    )


@pytest.mark.asyncio
async def test_capability_version_tracks_undeclared_definition_policy() -> None:
    _, _, capabilities, offering = _account_binding("a")

    def service(policy):
        result = _CatalogService.memory(
            account_context=lambda _: None,
            agent_offerings=lambda _: [offering],
            undeclared_report_definitions=policy,
        )
        result.sources.register(
            "gam", ScriptedReportingAdapter(capabilities, []), capability_offerings=[offering]
        )
        return result

    assert await service("assume_pre_1_2").agent_capabilities_version("agent-a") != (
        await service("withhold_from_pre_3_3").agent_capabilities_version("agent-a")
    )


@pytest.mark.asyncio
async def test_manual_projection_is_installed_once_and_matches_notification_version() -> None:
    _, _, capabilities, offering = _account_binding("a")
    service = _CatalogService.memory(
        account_context=lambda _: None,
        agent_offerings=lambda _: [offering],
        capability_projection="manual",
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    base = {"adcp": {"capability_changes": {"capabilities_version": "seller-1"}}}

    class Seller(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return await service.inject_agent_capabilities(base, agent_id="agent-a")

    handler = service.install(Seller())
    first = await handler.get_adcp_capabilities({})
    assert first == await service.inject_agent_capabilities(base, agent_id="agent-a")
    assert first["adcp"]["capability_changes"]["capabilities_version"] == (
        await service.agent_capabilities_version("agent-a", base_version="seller-1")
    )


@pytest.mark.asyncio
async def test_capability_version_distinguishes_all_opaque_base_versions() -> None:
    _, _, capabilities, offering = _account_binding("a")
    service = _CatalogService.memory(
        account_context=lambda _: None, agent_offerings=lambda _: [offering]
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    first = "rr1:" + "a" * 64 + ":" + "b" * 64
    second = "rr1:" + "a" * 64 + ":" + "c" * 64
    assert await service.agent_capabilities_version("agent-a", base_version=first) != (
        await service.agent_capabilities_version("agent-a", base_version=second)
    )


@pytest.mark.asyncio
async def test_capability_version_tracks_each_offerings_version_policy() -> None:
    _, _, capabilities, first = _account_binding("a")
    second = {**first, "offering_id": "SECOND"}

    def service(registered):
        result = _CatalogService.memory(
            account_context=lambda _: None, agent_offerings=lambda _: [first, second]
        )
        result.sources.register(
            "gam", ScriptedReportingAdapter(capabilities, []), capability_offerings=[registered]
        )
        return result

    first_service = service(first)
    second_service = service(second)
    first_response = await first_service.inject_agent_capabilities(
        {}, agent_id="agent-a", adcp_version="3.2.1"
    )
    second_response = await second_service.inject_agent_capabilities(
        {}, agent_id="agent-a", adcp_version="3.2.1"
    )
    assert first_response["media_buy"]["reporting_delivery"]["offerings"] == [first]
    assert second_response["media_buy"]["reporting_delivery"]["offerings"] == [second]
    assert first_response["adcp"]["capability_changes"]["capabilities_version"] != (
        second_response["adcp"]["capability_changes"]["capabilities_version"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, value",
    [
        ("reconciliation_mode", "consumer_receipt"),
        (
            "method",
            {
                "pattern": "file_transfer",
                "transport": "https",
                "orchestration": "producer_managed",
                "destination_modes": ["existing"],
            },
        ),
    ],
)
async def test_catalog_rejects_unavailable_delivery_components(field, value) -> None:
    _, _, capabilities, offering = _account_binding("a")
    invalid = {**offering, field: value}
    service = _CatalogService.memory(
        account_context=lambda _: None, agent_offerings=lambda _: [invalid]
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    with pytest.raises(ReliableReportingConfigurationError, match="requires"):
        await service.inject_agent_capabilities({}, agent_id="agent-a")


@pytest.mark.parametrize("mode", ["production", "subscriptions", "reporting_subscriptions"])
def test_agent_catalog_rejects_fixed_graphs(mode: str) -> None:
    with pytest.raises(ReliableReportingConfigurationError, match="agent offerings"):
        _CatalogService(
            store=InMemoryReportingLedgerStore(),
            account_context=lambda _: None,
            agent_offerings=lambda _: [],
            **{mode: object()},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_digest", [False, True])
async def test_admission_and_recovery_use_current_and_pinned_offerings(prior_digest: bool) -> None:
    configuration, context, capabilities, offering = _account_binding("a")
    digest = context.binding_sha256()
    context = replace(context, serving_agent_id="agent-a")
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    current = {"agent-a": [offering]}
    store = InMemoryReportingLedgerStore()

    def create(allow: bool) -> ReliableReportingService:
        service = _CatalogService(
            store=store,
            account_context=lambda _: context,
            agent_offerings=lambda agent: current[agent],
            supports_frozen_offering=lambda agent, config, bound: allow,
        )
        service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
        return service

    original = create(True)
    await original.configure(configuration, serving_agent_id="agent-a")
    await original.initialize()
    if prior_digest:
        store = InMemoryReportingLedgerStore()
        configuration = replace(configuration, account_binding_sha256=digest)
        await store.put_configuration(configuration)
    current["agent-a"] = []
    with pytest.raises(ReliableReportingConfigurationError, match="advertised"):
        await original.configure(
            replace(
                configuration,
                delivery_config_version=2,
                account_binding_sha256=context.binding_sha256(),
            ),
            serving_agent_id="agent-a",
        )
    recovered = create(True)
    await recovered.initialize()
    assert configuration.generation_key in recovered._bindings
    assert await store.list_all_configurations() == (configuration,)


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True])
async def test_snapshot_generations_with_and_without_digest_recover(pinned: bool) -> None:
    configuration = _configuration()
    context = replace(_account_context(configuration), serving_agent_id="agent-a")
    if pinned:
        configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(configuration)
    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [],
        supports_frozen_offering=lambda *_: True,
    )
    service.sources.register(
        "gam",
        ScriptedReportingAdapter(redacted_capabilities(), []),
        capability_offerings=[_capability_offering("gam")],
    )
    await service.initialize()
    assert configuration.generation_key in service._bindings
    assert await store.list_all_configurations() == (configuration,)


@pytest.mark.asyncio
async def test_recovery_refuses_modified_pinned_snapshot_offering() -> None:
    configuration = _configuration()
    context = replace(_account_context(configuration), serving_agent_id="agent-a")
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    context = replace(
        context, capability_offering=replace_offering(dict(context.capability_offering))
    )
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(configuration)
    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [],
        supports_frozen_offering=lambda *_: True,
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    await service.initialize()
    assert "frozen configuration" in str(
        service.initialization_errors[configuration.generation_key]
    )
    assert configuration.generation_key not in service._bindings
    assert await store.list_all_configurations() == (configuration,)


@pytest.mark.asyncio
async def test_recovery_requires_confirmation_before_deactivation() -> None:
    generation, context, capabilities, _ = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(generation)
    deactivated = []
    with pytest.raises(ReliableReportingConfigurationError, match="supports_frozen_offering"):
        ReliableReportingService(
            store=store,
            account_context=lambda _: context,
            agent_offerings=lambda _: [],
            deactivate_unserved_generation=lambda *args: deactivated.append(args),
        )
    assert not deactivated
    assert await store.list_all_configurations() == (generation,)


def test_agent_catalog_requires_a_deactivation_path() -> None:
    with pytest.raises(ReliableReportingConfigurationError, match="deactivate_unserved_generation"):
        ReliableReportingService.memory(
            account_context=lambda _: None,
            agent_offerings=lambda _: [],
            supports_frozen_offering=lambda *_: False,
        )


@pytest.mark.asyncio
async def test_drift_in_one_agent_does_not_block_healthy_generations() -> None:
    first, first_context, capabilities, offering = _account_binding("a")
    second, second_context, _, _ = _account_binding("b")
    first_context = replace(first_context, serving_agent_id="agent-a")
    second_context = replace(second_context, serving_agent_id="agent-b")
    first = replace(first, account_binding_sha256=first_context.binding_sha256())
    second = replace(second, account_binding_sha256=second_context.binding_sha256())
    first_context = replace(first_context, capability_offering=replace_offering(offering))
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(first)
    await store.put_configuration(second)
    contexts = {first.account_id: first_context, second.account_id: second_context}
    service = _CatalogService(
        store=store,
        account_context=lambda configuration: contexts[configuration.account_id],
        agent_offerings=lambda _: [],
        supports_frozen_offering=lambda *_: True,
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    await service.initialize()
    assert first.generation_key in service.initialization_errors
    assert first.generation_key not in service._bindings
    assert second.generation_key in service._bindings


@pytest.mark.asyncio
async def test_unavailable_confirmation_is_retried_without_deactivation() -> None:
    generation, context, capabilities, _ = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(generation)
    attempts = 0

    async def supports(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("unavailable")
        return True

    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [],
        supports_frozen_offering=supports,
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    await service.initialize()
    assert generation.generation_key in service.initialization_errors
    await service.run_worker(now=generation.activated_at)
    assert generation.generation_key in service._bindings
    assert generation.generation_key not in service.initialization_errors
    assert await store.list_all_configurations() == (generation,)


@pytest.mark.asyncio
async def test_nonboolean_support_response_does_not_deactivate() -> None:
    generation, context, capabilities, _ = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(generation)
    deactivated = []
    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [],
        supports_frozen_offering=lambda *_: None,
        deactivate_unserved_generation=lambda *args: deactivated.append(args),
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    await service.initialize()
    assert isinstance(service.initialization_errors[generation.generation_key], TypeError)
    assert not deactivated
    assert await store.list_all_configurations() == (generation,)


@pytest.mark.asyncio
async def test_snapshot_admission_pins_the_serving_agents_advertised_offering() -> None:
    configuration = _configuration()
    context = replace(_account_context(configuration), serving_agent_id="agent-a")
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    service = _CatalogService.memory(
        account_context=lambda _: context,
        agent_offerings=lambda agent: [_capability_offering("gam")] if agent == "agent-a" else [],
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    with pytest.raises(ReliableReportingConfigurationError, match="serving agent"):
        await service.configure(configuration, serving_agent_id="agent-b")
    await service.configure(configuration, serving_agent_id="agent-a")
    await service.initialize()
    assert await service.store.list_all_configurations() == (configuration,)


@pytest.mark.asyncio
async def test_admission_preserves_protocol_version_error() -> None:
    configuration, context, capabilities, offering = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    service = _CatalogService.memory(
        account_context=lambda _: context,
        agent_offerings=lambda _: [offering],
        report_definition_versions={offering["report_definition_sha256"]: "1.2"},
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    with pytest.raises(LedgerConflictError) as failure:
        await service.configure(configuration, serving_agent_id="agent-a", adcp_version="3.2.1")
    assert failure.value.code == "UNSUPPORTED_FEATURE"


@pytest.mark.asyncio
async def test_declined_historical_offering_deactivates_without_removing_generation() -> None:
    configuration, context, capabilities, offering = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(configuration)

    async def deactivate(agent, config, bound):
        from datetime import datetime, timezone

        async def persist():
            async with service._configuration_lock:
                await store.put_configuration(
                    replace(config, deactivated_at=datetime.now(timezone.utc))
                )

        await service._lifecycle.call(persist, before_start=True)

    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [],
        supports_frozen_offering=lambda agent, config, bound: False,
        deactivate_unserved_generation=deactivate,
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    await asyncio.wait_for(service.initialize(), timeout=0.5)
    (retained,) = await store.list_all_configurations()
    assert retained.deactivated_at is not None
    assert retained.account_binding_sha256 == configuration.account_binding_sha256
    assert retained.generation_key in service.initialization_errors
    assert retained.generation_key not in service._bindings


@pytest.mark.asyncio
async def test_failed_deactivation_is_visible_and_retried() -> None:
    from datetime import datetime, timezone

    configuration, context, capabilities, _ = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    configuration = replace(configuration, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(configuration)
    attempts = 0

    async def deactivate(agent, config, bound):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("unavailable")
        await store.put_configuration(replace(config, deactivated_at=datetime.now(timezone.utc)))

    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [],
        supports_frozen_offering=lambda *_: False,
        deactivate_unserved_generation=deactivate,
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
    await service.initialize()
    assert isinstance(service.initialization_errors[configuration.generation_key], RuntimeError)
    assert (await store.list_all_configurations())[0].deactivated_at is None
    await service.run_worker()
    assert attempts == 2
    assert (await store.list_all_configurations())[0].deactivated_at is not None
    assert configuration.generation_key in service.initialization_errors


@pytest.mark.asyncio
async def test_unserved_deactivated_generation_can_resume_owed_periods_without_restart() -> None:
    from datetime import datetime, timezone

    generation, context, capabilities, _ = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(generation)
    supported = False
    cutoff = datetime(2026, 11, 1, 3, 10, tzinfo=timezone.utc)

    async def deactivate(agent, config, bound):
        await store.put_configuration(replace(config, deactivated_at=cutoff))

    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [],
        supports_frozen_offering=lambda *_: supported,
        deactivate_unserved_generation=deactivate,
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, [_rows(10)]))
    await service.initialize()
    assert generation.generation_key in service.initialization_errors
    assert (await store.list_all_configurations())[0].deactivated_at == cutoff
    await service.run_worker(now=generation.activated_at)
    assert generation.generation_key in service.initialization_errors
    supported = True
    turn = await service.run_worker(now=datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc))
    assert service._bindings[generation.generation_key].configuration.deactivated_at == cutoff
    assert generation.generation_key not in service.initialization_errors
    assert turn.configurations[generation.generation_key].obligations_committed
    snapshot = await store.read_status_snapshot(
        caller=ReportingStatusCaller(generation.account_id, generation.consumer_id)
    )
    assert snapshot.obligations


@pytest.mark.asyncio
async def test_concurrent_admission_observes_recovery_deactivation_cutoff() -> None:
    from datetime import datetime, timezone

    generation, context, capabilities, offering = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(generation)
    catalog = []
    callback_started = asyncio.Event()
    finish_callback = asyncio.Event()
    cutoff = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)

    async def deactivate(agent, config, bound):
        callback_started.set()
        await finish_callback.wait()
        async with service._configuration_lock:
            await store.put_configuration(replace(config, deactivated_at=cutoff))

    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: catalog,
        supports_frozen_offering=lambda *_: False,
        deactivate_unserved_generation=deactivate,
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, [_rows(10)]))
    initializing = asyncio.create_task(service.initialize())
    await asyncio.wait_for(callback_started.wait(), timeout=2)
    catalog.append(offering)
    await service.configure(generation, serving_agent_id="agent-a")
    finish_callback.set()
    await asyncio.wait_for(initializing, timeout=2)
    assert service._bindings[generation.generation_key].configuration.deactivated_at == cutoff
    await service.run_worker(now=datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc))
    snapshot = await store.read_status_snapshot(
        caller=ReportingStatusCaller(generation.account_id, generation.consumer_id)
    )
    assert all(obligation.period.end <= cutoff for obligation in snapshot.obligations)


@pytest.mark.asyncio
async def test_other_process_deactivation_is_reflected_before_producing() -> None:
    from datetime import datetime, timezone

    generation, context, capabilities, offering = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    store = InMemoryReportingLedgerStore()
    catalog = [offering]
    cutoff = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)

    def service_for(supported, deactivate=None):
        service = _CatalogService(
            store=store,
            account_context=lambda _: context,
            agent_offerings=lambda _: catalog,
            supports_frozen_offering=lambda *_: supported,
            deactivate_unserved_generation=deactivate or _unexpected_deactivation,
        )
        service.sources.register("gam", ScriptedReportingAdapter(capabilities, [_rows(10)]))
        return service

    first = service_for(True)
    await first.configure(generation, serving_agent_id="agent-a")
    await first.initialize()
    catalog.clear()

    async def deactivate(agent, config, bound):
        await store.put_configuration(replace(config, deactivated_at=cutoff))

    second = service_for(False, deactivate)
    await second.initialize()
    await first.run_worker(now=datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc))
    snapshot = await store.read_status_snapshot(
        caller=ReportingStatusCaller(generation.account_id, generation.consumer_id)
    )
    assert snapshot.obligations
    assert all(obligation.period.end <= cutoff for obligation in snapshot.obligations)


@pytest.mark.asyncio
async def test_obligation_commit_fences_concurrent_deactivation() -> None:
    from datetime import datetime, timezone

    started = asyncio.Event()
    resume = asyncio.Event()

    class PausingStore(InMemoryReportingLedgerStore):
        async def commit_agent_obligation(self, configuration, obligation):
            started.set()
            await resume.wait()
            return await super().commit_agent_obligation(configuration, obligation)

    generation, context, capabilities, offering = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    store = PausingStore()
    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [offering],
        supports_frozen_offering=lambda *_: True,
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, [_rows(10)]))
    await service.configure(generation, serving_agent_id="agent-a")
    await service.initialize()
    now = datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)
    cutoff = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
    turn = asyncio.create_task(service.run_worker(now=now))
    await asyncio.wait_for(started.wait(), timeout=2)
    await store.put_configuration(replace(generation, deactivated_at=cutoff))
    resume.set()
    await asyncio.wait_for(turn, timeout=2)
    await service.run_worker(now=now)
    snapshot = await store.read_status_snapshot(
        caller=ReportingStatusCaller(generation.account_id, generation.consumer_id)
    )
    assert snapshot.obligations
    assert all(obligation.period.end <= cutoff for obligation in snapshot.obligations)


@pytest.mark.asyncio
async def test_worker_retries_after_configuration_refresh_failure() -> None:
    from datetime import datetime, timezone

    unavailable = False

    class FlakyStore(InMemoryReportingLedgerStore):
        async def list_all_configurations(self):
            if unavailable:
                raise ConnectionError("temporarily unavailable")
            return await super().list_all_configurations()

    generation, context, capabilities, offering = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    store = FlakyStore()
    service = _CatalogService(
        store=store,
        account_context=lambda _: context,
        agent_offerings=lambda _: [offering],
        supports_frozen_offering=lambda *_: True,
    )
    service.sources.register("gam", ScriptedReportingAdapter(capabilities, [_rows(10)]))
    await service.configure(generation, serving_agent_id="agent-a")
    await service.initialize()
    now = datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)
    unavailable = True
    first = await service.run_worker(now=now)
    assert isinstance(first.extension_errors["configuration_refresh"], ConnectionError)
    assert not first.configurations
    assert service.ready
    unavailable = False
    second = await service.run_worker(now=now)
    assert second.configurations[generation.generation_key].obligations_committed
