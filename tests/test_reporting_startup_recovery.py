"""Stored account-context failures remain local to their configuration."""

from __future__ import annotations

import asyncio
import operator
from collections.abc import AsyncIterator
from dataclasses import fields, replace
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from adcp_reporting_storage import postgres_reliable_reporting_service

from adcp.reporting.fixtures import redacted_capabilities
from adcp.reporting.ledger import ReportingProducer, WorkerTurn
from adcp.reporting.service import (
    ReliableReportingConfigurationError,
    ReliableReportingService,
    ReliableReportingServiceError,
    ReportingAccountContext,
)
from adcp.reporting.testing import ScriptedReportingAdapter
from tests.conformance.reporting._generation_support import isolated_reporting_pool
from tests.test_reliable_reporting_service import NOW, _account_context, _configuration


def _service(resolver: Any) -> ReliableReportingService:
    service = ReliableReportingService.memory(account_context=resolver, clock=lambda: NOW)
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    return service


@pytest.mark.parametrize("async_resolver", [False, True])
async def test_restart_binds_healthy_accounts_and_retries_a_failed_resolver(
    async_resolver: bool, caplog: Any
) -> None:
    configs = tuple(_configuration(account_id=account) for account in ("a", "b", "c"))
    failure = RuntimeError("account credentials: private-test-token")
    unavailable = True
    attempts: list[str] = []

    def resolve(configuration: Any) -> Any:
        attempts.append(configuration.account_id)
        if configuration.account_id == "b" and unavailable:
            raise failure
        return _account_context(configuration)

    async def resolve_async(configuration: Any) -> Any:
        await asyncio.sleep(0)
        return resolve(configuration)

    service = _service(resolve_async if async_resolver else resolve)
    for configuration in configs:
        await service.store.put_configuration(configuration)
    try:
        await service.start()
        assert attempts == ["a", "b", "c"]
        assert "private-test-token" not in caplog.text
        assert service.initialization_errors == {configs[1].generation_key: failure}
        with pytest.raises(TypeError):
            operator.setitem(service.initialization_errors, configs[0].generation_key, failure)

        first = await service.run_worker(now=NOW)
        assert set(first.configurations) == {configs[0].generation_key, configs[2].generation_key}
        assert first.configuration_errors == {configs[1].generation_key: failure}

        unavailable = False
        recovered = await service.run_worker(now=NOW)
        assert set(recovered.configurations) == {
            configuration.generation_key for configuration in configs
        }
        assert not recovered.configuration_errors
        assert not service.initialization_errors
        assert attempts == ["a", "b", "c", "b", "b"]
        assert await service.store.list_all_configurations() == configs
    finally:
        await service.close()


async def test_permanent_resolver_error_is_observable_without_retry_and_can_be_reconfigured() -> (
    None
):
    configuration = _configuration()
    failure = ReliableReportingConfigurationError("account has no execution currency")
    unavailable = True
    attempts = 0

    def resolve(config: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if unavailable:
            raise failure
        return _account_context(config)

    service = _service(resolve)
    await service.store.put_configuration(configuration)
    try:
        await service.start()
        for _ in range(2):
            turn = await service.run_worker(now=NOW)
            assert turn.configuration_errors == {configuration.generation_key: failure}
        assert attempts == 1

        unavailable = False
        await service.configure(configuration)
        assert not service.initialization_errors
        assert not (await service.run_worker(now=NOW)).configuration_errors
        assert attempts == 2
    finally:
        await service.close()


async def test_retry_that_becomes_permanent_stops_retrying() -> None:
    configuration = _configuration()
    permanent = ReliableReportingConfigurationError("account was removed")
    attempts = 0

    def resolve(config: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary lookup failure")
        raise permanent

    service = _service(resolve)
    await service.store.put_configuration(configuration)
    try:
        await service.start()
        for _ in range(2):
            turn = await service.run_worker(now=NOW)
            assert turn.configuration_errors == {configuration.generation_key: permanent}
        assert attempts == 2
    finally:
        await service.close()


async def test_schema_failure_still_prevents_startup(monkeypatch: Any) -> None:
    service = _service(_account_context)
    failure = RuntimeError("schema unavailable")
    monkeypatch.setattr(service.store, "create_schema", AsyncMock(side_effect=failure))
    try:
        with pytest.raises(ReliableReportingServiceError, match="startup"):
            await service.start()
    finally:
        await service.close()


async def test_static_adapter_error_still_prevents_startup() -> None:
    service = _service(lambda config: replace(_account_context(config), adapter="unknown"))
    await service.store.put_configuration(_configuration())
    try:
        with pytest.raises(ReliableReportingConfigurationError, match="unregistered"):
            await service.start()
    finally:
        await service.close()


async def test_resolver_cancellation_still_fails_startup() -> None:
    async def resolve(configuration: Any) -> Any:
        raise asyncio.CancelledError()

    service = _service(resolve)
    await service.store.put_configuration(_configuration())
    try:
        with pytest.raises(ReliableReportingServiceError, match="startup"):
            await service.start()
    finally:
        await service.close()


async def test_new_configuration_admission_still_raises_resolver_failures() -> None:
    failure = RuntimeError("account lookup unavailable")

    def resolve(configuration: Any) -> Any:
        raise failure

    service = _service(resolve)
    try:
        with pytest.raises(RuntimeError, match="account lookup unavailable"):
            await service.configure(_configuration())
    finally:
        await service.close()


@pytest.fixture(params=["memory", "pg", "pg-autocommit"])
async def recovery_service_factory(request: Any) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield _service
    else:
        async with isolated_reporting_pool(autocommit=request.param == "pg-autocommit") as pool:

            def create(resolver: Any) -> ReliableReportingService:
                service = postgres_reliable_reporting_service(
                    pool=pool, account_context=resolver, clock=lambda: NOW
                )
                service.sources.register(
                    "gam", ScriptedReportingAdapter(redacted_capabilities(), [])
                )
                return service

            yield create


def _invalid_context(configuration: Any, kind: str) -> Any:
    context = _account_context(configuration)
    if kind in {"projection", "projection_shape"}:

        class InvalidProjection(ReportingAccountContext):
            def producer_offerings(self) -> Any:
                if kind == "projection_shape":
                    return None
                raise ValueError("invalid account projection")

        return InvalidProjection(
            **{field.name: getattr(context, field.name) for field in fields(context)}
        )
    changes: dict[str, Any] = {
        "account": {"account_id": "wrong-account"},
        "timezone": {"account_timezone": "America/New_York"},
        "adapter": {"adapter": "unknown"},
        "scope": {"source_scope": {"network": "wrong-network"}},
        "offering": {"snapshot_offering_id": "UNKNOWN_OFFERING"},
        "capability": {
            "capability_offering": {**context.capability_offering, "feed_purpose": "billing"}
        },
        "capability_shape": {"capability_offering": "bad-shape"},
    }
    return {} if kind == "type" else replace(context, **changes[kind])


@pytest.mark.parametrize(
    "invalid",
    [
        "type",
        "account",
        "timezone",
        "adapter",
        "scope",
        "offering",
        "capability",
        "capability_shape",
        "projection",
        "projection_shape",
    ],
)
async def test_retry_resolving_invalid_context_stays_local_and_requires_explicit_repair(
    recovery_service_factory: Any, invalid: str, caplog: Any
) -> None:
    healthy, affected = (_configuration(account_id=account) for account in ("a", "b"))
    attempts: list[str] = []
    repaired = False

    def resolve(configuration: Any) -> Any:
        attempts.append(configuration.account_id)
        if configuration.account_id == affected.account_id:
            if attempts.count(affected.account_id) == 1:
                raise RuntimeError("temporary lookup with private-token")
            if not repaired:
                return _invalid_context(configuration, invalid)
        return _account_context(configuration)

    service = recovery_service_factory(resolve)
    await service.store.create_schema()
    for configuration in (healthy, affected):
        await service.store.put_configuration(configuration)
    try:
        await service.start()
        assert attempts == ["a", "b"]
        turn = await service.run_worker(now=NOW)
        assert service.ready
        assert set(turn.configurations) == {healthy.generation_key}
        failure = service.initialization_errors[affected.generation_key]
        expected = (
            TypeError
            if invalid in {"type", "projection_shape"}
            else (
                ValueError
                if invalid == "projection"
                else (
                    AttributeError
                    if invalid == "capability_shape"
                    else ReliableReportingConfigurationError
                )
            )
        )
        assert isinstance(failure, expected)
        assert turn.configuration_errors == {affected.generation_key: failure}
        assert "private-token" not in caplog.text

        repeated = await service.run_worker(now=NOW)
        assert set(repeated.configurations) == {healthy.generation_key}
        assert repeated.configuration_errors == {affected.generation_key: failure}
        assert attempts == ["a", "b", "b"]

        repaired = True
        await service.configure(affected)
        recovered = await service.run_worker(now=NOW)
        assert set(recovered.configurations) == {healthy.generation_key, affected.generation_key}
        assert not recovered.configuration_errors and not service.initialization_errors
        assert attempts == ["a", "b", "b", "b"]
        assert await service.store.list_all_configurations() == (healthy, affected)
    finally:
        await service.close()


@pytest.mark.parametrize("stage", ["producer", "scope"])
@pytest.mark.parametrize("admission", ["startup", "configure"])
async def test_initial_static_setup_failures_remain_fatal(
    stage: str, admission: str, monkeypatch: Any
) -> None:
    configuration = _configuration()
    service = _service(_account_context)
    await service.store.put_configuration(configuration)
    try:
        if stage == "producer":

            def fail_construction(**kwargs: Any) -> Any:
                raise RuntimeError("producer construction failed")

            monkeypatch.setattr(service, "_producer_factory", fail_construction)
        elif stage == "scope":
            registration = service.sources.get("gam")
            monkeypatch.setattr(
                registration.executor,
                "_capabilities",
                registration.executor.capabilities.model_copy(update={"scope": "catalog"}),
            )
        expected = (
            (ReliableReportingServiceError if admission == "startup" else RuntimeError)
            if stage == "producer"
            else ReliableReportingConfigurationError
        )
        with pytest.raises(expected):
            if admission == "startup":
                await service.start()
            else:
                await service.configure(configuration)
    finally:
        await service.close()


@pytest.mark.parametrize("permanent", [False, True])
async def test_retry_producer_failure_preserves_healthy_turns_and_recovers(
    recovery_service_factory: Any, permanent: bool, monkeypatch: Any
) -> None:
    healthy, affected = (_configuration(account_id=account) for account in ("a", "b"))
    resolutions = 0
    constructions = 0
    repaired = False
    failure = (
        ReliableReportingConfigurationError("invalid producer setup")
        if permanent
        else RuntimeError("temporary producer setup failure")
    )

    def resolve(configuration: Any) -> ReportingAccountContext:
        nonlocal resolutions
        if configuration == affected:
            resolutions += 1
            if resolutions == 1:
                raise RuntimeError("temporary lookup failure")
        return replace(
            _account_context(configuration), publication_namespace=configuration.account_id
        )

    def construct(**kwargs: Any) -> ReportingProducer:
        nonlocal constructions
        if kwargs["offerings"].publication_namespace == affected.account_id:
            constructions += 1
            if not repaired:
                raise failure
        return ReportingProducer(**kwargs)

    service = recovery_service_factory(resolve)
    monkeypatch.setattr(service, "_producer_factory", construct)
    await service.store.create_schema()
    for configuration in (healthy, affected):
        await service.store.put_configuration(configuration)
    try:
        await service.start()
        for _ in range(2):
            turn = await service.run_worker(now=NOW)
            assert service.ready
            assert set(turn.configurations) == {healthy.generation_key}
            assert turn.configuration_errors == {affected.generation_key: failure}
            assert service.initialization_errors == {affected.generation_key: failure}
        assert constructions == (1 if permanent else 2)
        repaired = True
        if permanent:
            await service.configure(affected)
        recovered = await service.run_worker(now=NOW)
        assert service.ready
        assert set(recovered.configurations) == {healthy.generation_key, affected.generation_key}
        assert not recovered.configuration_errors and not service.initialization_errors
        assert constructions == (2 if permanent else 3)
    finally:
        await service.close()


async def test_retry_registered_scope_error_stays_local_until_explicit_repair(
    monkeypatch: Any,
) -> None:
    healthy, affected = (_configuration(account_id=account) for account in ("a", "b"))
    attempts = 0

    def resolve(configuration: Any) -> ReportingAccountContext:
        nonlocal attempts
        if configuration == affected:
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary lookup failure")
        return _account_context(configuration)

    service = _service(resolve)
    for configuration in (healthy, affected):
        await service.store.put_configuration(configuration)
    try:
        await service.start()
        executor = service.sources.get("gam").executor
        capabilities = executor.capabilities
        monkeypatch.setattr(
            executor, "_capabilities", capabilities.model_copy(update={"scope": "catalog"})
        )
        turn = await service.run_worker(now=NOW)
        failure = service.initialization_errors[affected.generation_key]
        assert isinstance(failure, ReliableReportingConfigurationError)
        assert set(turn.configurations) == {healthy.generation_key}
        assert turn.configuration_errors[affected.generation_key] is failure
        assert service.ready
        await service.run_worker(now=NOW)
        assert attempts == 2
        monkeypatch.setattr(executor, "_capabilities", capabilities)
        await service.configure(affected)
        recovered = await service.run_worker(now=NOW)
        assert set(recovered.configurations) == {healthy.generation_key, affected.generation_key}
        assert not recovered.configuration_errors and not service.initialization_errors
    finally:
        await service.close()


async def test_managed_worker_keeps_healthy_accounts_running_across_factory_failures() -> None:
    configurations = tuple(_configuration(account_id=account) for account in ("a", "b", "c"))
    affected = configurations[-1]
    resolutions = 0
    construction_failures = 0
    repaired = False
    healthy_runs = {"a": 0, "b": 0}
    healthy_continues = asyncio.Event()
    recovered = asyncio.Event()
    failure = RuntimeError("temporary producer setup failure")

    def resolve(configuration: Any) -> ReportingAccountContext:
        nonlocal resolutions
        if configuration == affected:
            resolutions += 1
            if resolutions == 1:
                raise RuntimeError("temporary lookup failure")
        return replace(
            _account_context(configuration), publication_namespace=configuration.account_id
        )

    class RecordingProducer(ReportingProducer):
        async def run_configuration(self, configuration: Any, **kwargs: Any) -> WorkerTurn:
            if configuration == affected:
                recovered.set()
            else:
                healthy_runs[configuration.account_id] += 1
                if construction_failures >= 2 and min(healthy_runs.values()) >= 2:
                    healthy_continues.set()
            return WorkerTurn()

    def construct(**kwargs: Any) -> ReportingProducer:
        nonlocal construction_failures
        if kwargs["offerings"].publication_namespace == affected.account_id and not repaired:
            construction_failures += 1
            raise failure
        return RecordingProducer(**kwargs)

    service = ReliableReportingService.memory(
        account_context=resolve,
        clock=lambda: NOW,
        worker_interval=timedelta(milliseconds=10),
        producer_factory=construct,
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    for configuration in configurations:
        await service.store.put_configuration(configuration)
    try:
        await service.start()
        await asyncio.wait_for(healthy_continues.wait(), timeout=5)
        assert service.ready
        assert service.initialization_errors == {affected.generation_key: failure}
        healthy_before_repair = dict(healthy_runs)
        repaired = True
        await asyncio.wait_for(recovered.wait(), timeout=5)
        assert service.ready and not service.initialization_errors
        assert all(
            healthy_runs[account] > count for account, count in healthy_before_repair.items()
        )
    finally:
        await service.close()


async def test_queued_resolver_cancellation_still_propagates() -> None:
    attempts = 0

    def resolve(configuration: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary lookup failure")
        raise asyncio.CancelledError()

    service = _service(resolve)
    await service.store.put_configuration(_configuration())
    try:
        await service.start()
        with pytest.raises(asyncio.CancelledError):
            await service.run_worker(now=NOW)
        assert attempts == 2
    finally:
        await service.close()


async def test_store_enumeration_failure_still_prevents_startup(monkeypatch: Any) -> None:
    service = _service(_account_context)
    await service.store.put_configuration(_configuration())
    monkeypatch.setattr(
        service.store,
        "list_all_configurations",
        AsyncMock(side_effect=RuntimeError("stored configuration read failed")),
    )
    try:
        with pytest.raises(ReliableReportingServiceError, match="startup"):
            await service.start()
    finally:
        await service.close()
