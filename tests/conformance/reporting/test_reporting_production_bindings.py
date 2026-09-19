"""Full provider methods and account-qualified frozen source mappings."""

import json
from dataclasses import replace

import pytest

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer.contracts import ReportingWriterError
from adcp.reporting.production.contracts import ReportingProductionMethod

from ._generation_support import END, obligation_for
from ._production_support import production_harness
from ._production_transport import MountedProduction
from .test_reporting_production_lock_order import source_turn


async def source_documents(h):
    if h.pool is None:
        return sorted(
            (key.account_id, key.delivery_config_id, key.delivery_config_version, value.document())
            for key, value in h.store._production_source_bindings.items()
        )
    async with h.pool.connection() as connection:
        return await (
            await connection.execute(
                "SELECT account_id,delivery_config_id,delivery_config_version,source_binding"
                " FROM reporting_production_generations"
                " ORDER BY account_id,delivery_config_id,delivery_config_version"
            )
        ).fetchall()


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_full_method_controls_admission_acquisition_and_raw_discovery(
    backend, notifications, tmp_path
):
    async with production_harness(
        backend,
        tmp_path / "destination.sqlite",
        count=0,
        source_publication=True,
        notifications=notifications,
    ) as h:
        support, item = h.production, h.item
        offering = support.offerings[0]
        producer, provider = offering.producer, item.writer
        await support.activate(account_id=item.config.account_id)
        mounted = MountedProduction(h)
        mounted.authorize(item)
        original = provider.delivery_methods
        new = replace(item.config, delivery_config_version=2)
        new_binding = replace(item.binding, generation_key=new.generation_key)
        provider.grant(new_binding)
        producer._source.bind_generation(new)
        before = await source_documents(h)
        async with mounted.client() as client:
            for field, different in (
                ("provider", {"domain": "different-provider.example.test"}),
                ("destination_modes", ["existing"]),
                ("access_mode", "read_only"),
                ("reader_compatibility", ["different-reader-v2"]),
            ):
                raw = original[0].wire()
                raw[field] = different
                provider.delivery_methods = (
                    ReportingProductionMethod(item.verifier.key.capability, raw),
                )
                assert provider.capabilities == (item.verifier.key.capability,)
                try:
                    with pytest.raises(ReportingWriterError) as denied:
                        await h.store.admit_production_configuration(
                            new, new_binding, offering_id=offering.offering_id
                        )
                    assert denied.value.failure.code == "BINDING_MISMATCH"
                    token = support._producer_turn.set(producer)
                    try:
                        with pytest.raises((LedgerConflictError, ReportingWriterError)):
                            await producer.acquire_obligation(item.config, item.obligation, now=END)
                    finally:
                        support._producer_turn.reset(token)
                    assert producer._source.requests == []
                    assert (await source_turn(support)).leased is None
                    for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                        _, caps = await mounted.call(
                            client, "get_adcp_capabilities", {}, transport=transport
                        )
                        assert caps.get("status") == "completed", caps
                        assert "reporting_delivery" not in caps.get("media_buy", {}), caps
                        assert "different-provider" not in json.dumps(caps)
                        _, status = await mounted.call(
                            client,
                            "get_reporting_status",
                            {"account": {"account_id": item.config.account_id}, "view": "summary"},
                            transport=transport,
                        )
                        assert status.get("status") == "completed", status
                        assert status["health"] != "complete"
                finally:
                    provider.delivery_methods = original
                assert await source_documents(h) == before
            _, restored = await mounted.call(client, "get_adcp_capabilities", {})
            assert restored.get("status") == "completed", restored
            claims = restored.get("media_buy", {}).get("reporting_delivery", {})
            assert bool(claims.get("managed_delivery")) == (backend == "postgres")
            assert "reconciled_billing" not in claims and "receipt_task" not in claims
        result = await source_turn(support)
        assert len(result.revisions_committed) == 1 and not result.slices_failed


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_products_are_explicit_for_shared_definition_and_colliding_account_keys(
    backend, tmp_path
):
    path = tmp_path / "destination.sqlite"
    async with production_harness(
        backend, path, count=0, source_publication=True, second_source=True
    ) as h:
        support, item = h.production, h.item
        first, second = support.offerings
        outside = replace(item.config, account_id="acct_b")
        peer = replace(item.config, delivery_config_id="other-source")
        entries = (
            (item.config, item.obligation, item.binding, first, "catalog-7391"),
            (
                outside,
                obligation_for(outside),
                replace(item.binding, generation_key=outside.generation_key),
                first,
                "catalog-5820",
            ),
            (
                peer,
                replace(obligation_for(peer), reporting_obligation_id="peer-obligation"),
                replace(item.binding, generation_key=peer.generation_key),
                second,
                "catalog-5820",
            ),
        )
        assert first.verification_key.definition == second.verification_key.definition
        assert outside.media_buy_ids == item.config.media_buy_ids
        assert outside.delivery_config_id == item.config.delivery_config_id
        # Supported discovery does not need a first trusted account binding.
        assert not first.producer._source.bindings and not second.producer._source.bindings
        assert bool(await support.reporting_delivery()) == (backend == "postgres")
        for config, obligation, binding, offering, product_id in entries:
            source = offering.producer._source
            source.rows = item.rows
            source.bind_generation(config, product_id=product_id)
            item.writer.grant(binding)
            assert product_id != config.report_definition_id
            await h.store.admit_production_configuration(
                config, binding, offering_id=offering.offering_id
            )
            await h.store.commit_obligation(obligation)
        frozen = await source_documents(h)
        assert len(frozen) == 3
        # Admission fixes bindings even before activation can claim work.
        assert (await source_turn(support)).leased is None
        for account in ("acct_a", "acct_b"):
            await support.activate(account_id=account)
        for config, obligation, binding, offering, product_id in entries:
            producer = offering.producer
            token = support._producer_turn.set(producer)
            try:
                revision = await producer.acquire_obligation(config, obligation, now=END)
            finally:
                support._producer_turn.reset(token)
            assert revision is not None
            request = producer._source.requests[-1]
            assert request.identity.account_id == config.account_id
            assert request.identity.delivery_config_id == config.delivery_config_id
            assert {c.product_id for c in request.coverage.constituents} == {product_id}
            assert {c.media_buy_id for c in request.coverage.constituents} == set(
                config.media_buy_ids
            )
        assert await source_documents(h) == frozen
        await support.aclose()
        async with production_harness(
            backend,
            path,
            count=0,
            source_publication=True,
            second_source=True,
            existing_store=h.store if h.pool is None else None,
            existing_pool=h.pool,
            source_bindings=tuple(
                (config, offering.offering_id, product)
                for config, _, _, offering, product in entries
            ),
        ) as restarted:
            fresh = restarted.production
            assert await source_documents(restarted) == frozen
            for config, obligation, binding, old_offering, product_id in entries:
                offering = next(
                    o for o in fresh.offerings if o.offering_id == old_offering.offering_id
                )
                offering.producer._source.bind_generation(config, product_id=product_id)
                await restarted.store.admit_production_configuration(
                    config, binding, offering_id=offering.offering_id
                )
            first_fresh = fresh.offerings[0]
            source = first_fresh.producer._source
            old = source.bindings[item.config.generation_key]
            source.bind_generation(item.config, product_id="catalog-5820")
            try:
                with pytest.raises(ReportingNotificationError, match="source_conflict"):
                    await restarted.store.admit_production_configuration(
                        item.config, item.binding, offering_id=first_fresh.offering_id
                    )
                token = fresh._producer_turn.set(first_fresh.producer)
                try:
                    with pytest.raises((LedgerConflictError, ReportingWriterError)):
                        await restarted.store.producer_constituents(item.config, item.obligation)
                finally:
                    fresh._producer_turn.reset(token)
                assert await source_documents(restarted) == frozen
            finally:
                source.bindings[item.config.generation_key] = old
            token = fresh._producer_turn.set(first_fresh.producer)
            try:
                restored = await restarted.store.producer_constituents(item.config, item.obligation)
            finally:
                fresh._producer_turn.reset(token)
            assert {c.product_id for c in restored} == {"catalog-7391"}
            assert await source_documents(restarted) == frozen
