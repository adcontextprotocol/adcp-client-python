"""Production discovery captures registered promises independently of admission."""

import asyncio

import pytest

from adcp.reporting import ReliableReportingService, ReliableReportingShutdownTimeoutError

from .test_reporting_production_service_factory import factory_harness


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("push", [False, True])
async def test_registered_production_declaration_survives_start_drain_and_close(
    backend, push, tmp_path, monkeypatch
):
    created, installed = [], []
    original = ReliableReportingService.install
    original_start = ReliableReportingService.start

    def install(service, application):
        # This is before graph construction, schema readiness or worker startup.
        assert service._production is None
        created.append(service.capability_block())
        return original(service, application)

    async def start(service):
        installed.append(await service._production.handler.get_adcp_capabilities({}))
        return await original_start(service)

    monkeypatch.setattr(ReliableReportingService, "install", install)
    monkeypatch.setattr(ReliableReportingService, "start", start)
    async with factory_harness(backend, tmp_path / "declaration.sqlite", push=push) as h:
        service, handler = h.service, h.production.handler
        (declared,) = created
        assert bool(declared) == (backend == "postgres")
        assert service.capability_block() == declared
        ready = await handler.get_adcp_capabilities({})
        assert installed == [ready]
        assert ready.get("media_buy", {}).get("reporting_delivery", {}) == declared
        entered, release = asyncio.Event(), asyncio.Event()

        async def admitted_work():
            entered.set()
            await release.wait()

        operation = asyncio.create_task(service._lifecycle.call(admitted_work))
        await entered.wait()
        try:
            with pytest.raises(ReliableReportingShutdownTimeoutError):
                await service.close(timeout=0)
            assert service.capability_block() == declared
            draining = await handler.get_adcp_capabilities({})
            assert draining.get("media_buy", {}).get("reporting_delivery", {}) == declared
        finally:
            release.set()
            await operation
            await service.close()
        closed = await handler.get_adcp_capabilities({})
        assert closed.get("media_buy", {}).get("reporting_delivery", {}) == declared
        assert service.capability_block() == declared
        assert closed.get("webhook_signing") == ready.get("webhook_signing")
        assert closed.get("identity") == ready.get("identity")
        if backend == "postgres":
            assert declared["managed_delivery"]
            assert bool(declared.get("status_notification")) == push
            closed["media_buy"]["reporting_delivery"]["offerings"][0]["offering_id"] = "changed"
            assert service.capability_block() == declared
            assert await handler.get_adcp_capabilities({}) == ready
        else:
            # The development memory production graph promises no durable tier.
            assert not declared
