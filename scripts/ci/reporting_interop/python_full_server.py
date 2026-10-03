#!/usr/bin/env python3
"""Installed-wheel PostgreSQL seller for the full #1199 reporting lane.

Only the fixture driver comes from this checkout. The SDK is imported from the
selected installed wheel, and the runner gives each invocation a fresh database.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

import adcp
from adcp.server import ADCPHandler, serve
from adcp.server.auth import BearerTokenAuth, Principal, auth_context_factory

ROOT = Path(__file__).resolve().parents[3]
SDK_SOURCE = ROOT / "src" / "adcp"
if Path(adcp.__file__).resolve().is_relative_to(SDK_SOURCE):
    raise RuntimeError("full matrix seller must use the installed Python wheel")

sys.path.insert(0, str(ROOT))
from tests.conformance.reporting._generation_support import (  # noqa: E402
    configuration,
    obligation_for,
    revision_for,
)
from tests.conformance.reporting._production_support import production_harness  # noqa: E402
from tests.conformance.reporting._projection_support import drain  # noqa: E402
from tests.conformance.reporting._reliable_support import (  # noqa: E402
    DeterministicReceiverStore,
    ScriptedNotificationReceiver,
    SimulatedCrash,
    _BytesStore,
    notification_subscription,
    notification_verification_keys,
)

EVENTS = (
    "reporting.ledger_changed",
    "reporting.status_changed",
    "reporting.delivery_ready",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    return parser.parse_args()


async def _verify_notification_replay(harness: Any) -> dict[str, Any]:
    """Exercise signed replay and account activity in this cell's installed SDK."""
    from adcp.reporting.production.notifications import production_notification_workers
    from adcp.signing.jwks import StaticJwksResolver
    from adcp.signing.webhook_verifier import WebhookVerifyOptions, verify_webhook_signature

    h = harness
    h.subscriptions.put(
        notification_subscription(
            subscriber="buyer",
            principal=h.item.binding.consumer_id,
            events=EVENTS,
            url="https://receiver.example.test/reporting",
        )
    )
    patcher = pytest.MonkeyPatch()
    blobs = _BytesStore(h.pool)
    await blobs.create_schema()
    receiver_store = DeterministicReceiverStore(blobs, h.notification_failures)
    receiver = ScriptedNotificationReceiver(
        SimpleNamespace(clock=h.clock, failures=h.notification_failures, receiver=receiver_store)
    )
    receiver.install(patcher)
    try:
        # The original revision was committed before the subscriber existed.
        # A distinct ordinary revision supplies a real post-registration event.
        core = replace(
            configuration(consumer_id=h.item.binding.consumer_id),
            delivery_config_id="interop-webhook",
        )
        await h.store.put_configuration(core)
        obligation = await h.store.commit_obligation(
            replace(obligation_for(core), reporting_obligation_id="interop-webhook-obligation")
        )
        revision, rows = revision_for(obligation, suffix="interop-webhook")
        await h.store.commit_revision(revision, rows)
        worker = h.production.notification_workers[0]
        for _ in range(100):
            if not await worker.expand_one(account_id="acct_a"):
                break
        else:
            raise RuntimeError("webhook expansion did not reach idle")
        if not await worker.outbox.list_deliveries(account_id="acct_a"):
            raise RuntimeError("webhook delivery was not queued")
        h.notification_failures.at("http.accepted", SimulatedCrash())
        try:
            await worker.deliver_one(account_id="acct_a")
        except SimulatedCrash:
            pass
        else:
            raise RuntimeError("webhook crash boundary did not fire")
        first = receiver.received[-1]
        if await receiver_store.read("acct_a", first.idempotency_key) != first.body:
            raise RuntimeError("receiver did not retain accepted webhook bytes")
        async with worker.outbox._connection() as connection:
            await connection.execute(
                "UPDATE reporting_notification_deliveries"
                " SET lease_expires_at=clock_timestamp()-interval '1 second'"
                " WHERE account_id=%s AND state='leased'",
                ("acct_a",),
            )
        h.signing.generation = 2
        restarted = production_notification_workers(
            h.store,
            h.projection,
            subscriptions=h.subscriptions,
            cipher=worker.cipher,
            signing=worker.signing,
        )[0]
        for _ in range(100):
            if not await restarted.deliver_one(account_id="acct_a"):
                break
        else:
            raise RuntimeError("webhook retry did not reach idle")
        replay = [r for r in receiver.received if r.idempotency_key == first.idempotency_key]
        if len(replay) != 2 or replay[0].body != replay[1].body:
            raise RuntimeError("webhook retry changed the signed body or idempotency key")
        for generation, attempt in enumerate(replay, start=1):
            signature_input = next(
                (
                    value
                    for key, value in attempt.headers.items()
                    if key.lower() == "signature-input"
                ),
                "",
            )
            if f"#key-{generation}" not in signature_input:
                raise RuntimeError("webhook retry did not use the expected signing generation")
        options = WebhookVerifyOptions(
            jwks_resolver=StaticJwksResolver({"keys": notification_verification_keys()}),
            clock=lambda: h.clock().timestamp(),
        )
        for attempt in replay:
            verify_webhook_signature(
                method="POST",
                url="https://receiver.example.test" + attempt.target,
                headers=attempt.headers,
                body=attempt.body,
                options=options,
            )
        activity = await restarted.outbox.list_activity(
            account_id="acct_a", consumer_id=h.item.binding.consumer_id, limit=20
        )
        if not activity or not any(
            record.outcome and record.outcome.status == "success" for record in activity
        ):
            raise RuntimeError("account activity omitted the successful webhook replay")
        return {
            "attempt_count": len(replay),
            "body_unchanged": True,
            "idempotency_key_unchanged": True,
            "signature_generations": [1, 2],
            "verified_signature_count": len(replay),
        }
    finally:
        patcher.undo()


def main() -> None:
    args = _arguments()
    holder: dict[str, Any] = {}
    stack = AsyncExitStack()
    pool = AsyncConnectionPool(
        args.database_url,
        min_size=1,
        max_size=4,
        open=False,
        kwargs={"autocommit": True},
    )

    async def startup() -> None:
        await pool.open()
        await pool.wait(timeout=15)
        harness = await stack.enter_async_context(
            production_harness(
                "postgres",
                args.destination,
                existing_pool=pool,
                reconciled=True,
                notifications=True,
                notification_delivery=True,
                feedback=True,
                count=2,
            )
        )
        await harness.production.activate(account_id=harness.item.config.account_id)
        result = await harness.production.materializer.run_once()
        if result.state not in ("verified", "idle"):
            raise RuntimeError(f"materialization did not verify: {result}")
        await drain(harness.projection, harness.item.config.account_id)
        holder["harness"] = harness

    async def shutdown() -> None:
        try:
            harness = holder.get("harness")
            if harness is not None:
                await harness.production.aclose()
                result = await _verify_notification_replay(harness)
                args.evidence.write_text(
                    json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
                )
        finally:
            await stack.aclose()
            await pool.close()

    class Handler(ADCPHandler):
        advertised_tools = {
            "get_adcp_capabilities",
            "get_reporting_status",
            "sync_reporting_receipts",
            "sync_reporting_status",
            "get_media_buy_delivery",
            "sync_accounts",
        }

        async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
            return await holder["harness"].production.handler.get_adcp_capabilities(params, context)

        async def get_reporting_status(self, params: Any, context: Any = None) -> Any:
            return await holder["harness"].production.handler.get_reporting_status(params, context)

        async def sync_reporting_receipts(self, params: Any, context: Any = None) -> Any:
            return await holder["harness"].production.handler.sync_reporting_receipts(
                params, context
            )

        async def sync_reporting_status(self, params: Any, context: Any = None) -> Any:
            return await holder["harness"].production.handler.sync_reporting_status(params, context)

        async def get_media_buy_delivery(self, params: Any, context: Any = None) -> Any:
            return await holder["harness"].production.handler.get_media_buy_delivery(
                params, context
            )

        async def sync_accounts(self, params: Any, context: Any = None) -> Any:
            return await holder["harness"].production.handler.sync_accounts(params, context)

    def validate_token(token: str) -> Principal | None:
        if token != "full-matrix-token":
            return None
        return Principal(
            caller_identity="https://buyer.example.test/agent",
            tenant_id="matrix-tenant",
            metadata={"account_id": "acct_a"},
        )

    serve(
        Handler(),
        name="reporting-full-installed-seller",
        host="127.0.0.1",
        port=args.port,
        transport="both",
        auth=BearerTokenAuth(validate_token=validate_token),
        context_factory=auth_context_factory,
        stateless_http=True,
        allowed_hosts=("127.0.0.1", "localhost"),
        on_startup=(startup,),
        on_shutdown=(shutdown,),
    )


if __name__ == "__main__":
    main()
