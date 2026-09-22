"""Separate HTTP process using the real production composition and rc.4 MCP mount."""

import asyncio
import copy
import importlib.metadata
import json
import socket
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import uvicorn

import adcp
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.production.configuration import ReportingConfigurationAdmission
from adcp.server.auth import BearerTokenAuth, Principal, auth_context_factory
from adcp.server.serve import _build_mcp_and_a2a_app

from ._feed_support import MountedFeed, feed_harness, feed_request, mixed_case
from ._production_support import production_harness
from .test_reporting_production_configuration import state_for, wire_configuration


@asynccontextmanager
async def scope_fixture(backend, root):
    audit = {"calls": [], "admissions": []}
    selected = {}

    def save():
        temporary = root / "audit.next"
        temporary.write_text(json.dumps(audit))
        temporary.replace(root / "audit.json")

    async def account_task(request, context, admit):
        h = selected["h"]
        audit["calls"].append(copy.deepcopy(request))
        save()
        accounts = []
        for entry in request["accounts"]:
            who = await h.production.handler._authorize(entry, context)
            config, binding = selected[who.account_id]
            wire = entry["reporting_delivery_configs"][0]
            if set(wire["scope"]) == {"all_media_buys"}:
                # The accepted production fixture supports explicit full scopes.
                # Reaching this branch proves MCP input acceptance, not dynamic
                # all-buy production support or admission of a broader scope.
                raise LedgerConflictError("UNSUPPORTED_FEATURE", "explicit scope required")
            await admit(
                ReportingConfigurationAdmission(
                    h.production.offerings[0].offering_id,
                    config,
                    binding,
                    configuration_wire=wire,
                )
            )
            selected["accepted"][who.account_id] = copy.deepcopy(wire)
            audit["admissions"].append(
                {
                    "account_id": who.account_id,
                    "consumer_id": who.consumer_id,
                    "scope": wire["scope"],
                }
            )
            state = state_for(h, copy.deepcopy(wire))
            accounts.append(
                {
                    "account_id": who.account_id,
                    "brand": {"domain": "advertiser.example.test"},
                    "operator": "buyer.example.test",
                    "action": "unchanged",
                    "status": "active",
                    "billing": "operator",
                    "timezone": "UTC",
                    "reporting_delivery_configs": [state],
                }
            )
        save()
        return {"accounts": accounts}

    async with production_harness(
        backend, root / "destination.sqlite", count=0, account_handler=account_task
    ) as h:
        selected.update(h=h, accepted={})
        requests = {}
        tokens = {}
        for account, caller in (("acct_a", "urn:buyer:alpha"), ("acct_b", "urn:buyer:beta")):
            config = replace(
                h.item.config,
                account_id=account,
                delivery_config_id="shared-config",
                media_buy_ids=("shared-media-buy",),
            )
            binding = replace(
                h.item.binding, generation_key=config.generation_key, consumer_id=caller
            )
            h.item.writer.grant(binding)
            h.production.offerings[0].producer._source.bind_generation(config)
            h.authorized_bindings.add((account, caller))
            selected[account] = config, binding
            wire = wire_configuration(h)
            wire.update(
                delivery_config_id="shared-config", scope={"media_buy_ids": ["shared-media-buy"]}
            )
            requests[account] = {
                "adcp_version": "3.2-rc.4",
                "idempotency_key": "public-scope-" + account,
                "accounts": [
                    {"account": {"account_id": account}, "reporting_delivery_configs": [wire]}
                ],
            }
            tokens[account] = Principal(caller_identity=caller, tenant_id="shared-tenant")
        # Coverage in the returned states is the requested denominator too.
        h.item.config = selected["acct_a"][0]
        yield h, h.production.handler, tokens, {"requests": requests}, audit, save


@asynccontextmanager
async def exact_fixture(backend, root, count):
    # This fixture runs the owned production worker; it neither seeds a
    # successful revision nor calls the materializer manually. The source
    # clock is fixed at the first period's end; the configuration stays active
    # so autonomous materialization is eligible.
    async with production_harness(
        backend,
        root / "destination.sqlite",
        count=count,
        source_publication=True,
        reconciled=True,
        poll_seconds=0.02,
    ) as h:
        await h.production.activate(account_id=h.item.config.account_id)
        for _ in range(600):
            revisions = await h.store.list_revisions(
                account_id=h.item.config.account_id,
                reporting_obligation_id=h.item.obligation.reporting_obligation_id,
            )
            if len(revisions) == 1 and h.item.writer.writes == 1:
                outcomes = await h.item.outcomes()
                if outcomes:
                    break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(
                {
                    "reason": "public production worker did not materialize within 30 seconds",
                    "revisions": len(revisions),
                    "writes": h.item.writer.writes,
                    "source_requests": len(h.production.offerings[0].producer._source.requests),
                }
            )
        revision = revisions[0]
        assert revision.finality == "official" and revision.row_count == count
        audit = {"http": []}

        def save():
            temporary = root / "audit.next"
            temporary.write_text(json.dumps(audit))
            temporary.replace(root / "audit.json")

        yield h, h.production.handler, {
            "acct_a": Principal(
                caller_identity=h.item.binding.consumer_id, tenant_id="shared-tenant"
            ),
            "acct_b": Principal(caller_identity="urn:buyer:other", tenant_id="shared-tenant"),
        }, {
            "request": {
                "account": {"account_id": h.item.config.account_id},
                "reporting_revision_id": revision.reporting_revision_id,
                "pagination": {"max_results": 100},
            },
            "revision": {"row_count": count, "content_sha256": revision.revision_content_sha256},
            "source_requests": len(h.production.offerings[0].producer._source.requests),
            "destination_writes": h.item.writer.writes,
        }, audit, save


@asynccontextmanager
async def feed_fixture(backend, root, notifications):
    async with feed_harness(backend, notifications=notifications) as h:
        case, _, _ = await mixed_case(h)
        mounted = MountedFeed(h, version="3.2-rc.4")
        mounted.authorize(case, token="acct_a")
        audit = {"http": []}

        def save():
            temporary = root / "audit.next"
            temporary.write_text(json.dumps(audit))
            temporary.replace(root / "audit.json")

        yield h, mounted.handler, mounted.tokens, {
            "request": feed_request(case, adcp_version="3.2-rc.4"),
        }, audit, save


async def run(backend, root, socket_fd, scenario, count, notifications):
    fixture = (
        scope_fixture(backend, root)
        if scenario == "scope"
        else (
            exact_fixture(backend, root, count)
            if scenario == "exact"
            else feed_fixture(backend, root, notifications)
        )
    )
    async with fixture as (h, handler, tokens, ready, audit, save):
        sock = socket.socket(fileno=socket_fd)
        port = sock.getsockname()[1]
        app = _build_mcp_and_a2a_app(
            handler,
            name="public-reporting-wire",
            port=port,
            host="127.0.0.1",
            instructions=None,
            test_controller=None,
            context_factory=auth_context_factory,
            auth=BearerTokenAuth(validate_token=tokens.get),
            allowed_hosts=["127.0.0.1", "localhost"],
            public_url=f"http://127.0.0.1:{port}",
            stateless_http=True,
        )

        async def observed(scope, receive, send):
            if scope["type"] != "http" or scope["method"] != "POST":
                return await app(scope, receive, send)
            before = await h.image()
            chunks = []

            async def capture():
                message = await receive()
                if message["type"] == "http.request":
                    chunks.append(message.get("body", b""))
                return message

            await app(scope, capture, send)
            try:
                envelope = json.loads(b"".join(chunks), parse_float=Decimal)
            except ValueError:
                envelope = {}
            method = envelope.get("method")
            if method in {"tools/call", "message/send", "SendMessage"}:
                # Request-local comparison includes snapshots and continuation
                # state. It records no authentication headers or body values.
                record = {
                    "method": method,
                    "store_unchanged": before == await h.image(),
                }
                if scenario == "feed":
                    params = envelope["params"]
                    if method == "tools/call":
                        params = params["arguments"]
                    else:
                        params = params["message"]["parts"][0]["data"]["parameters"]
                    number = params.get("ext", {}).get("vendor", {}).get("n")
                    # Only the fixture's numeric control is observed, before
                    # server conversion. This distinguishes client rounding.
                    record["received_number"] = {"type": type(number).__name__, "text": str(number)}
                audit.setdefault("http", []).append(record)
                save()

        server = uvicorn.Server(uvicorn.Config(observed, log_level="warning", lifespan="on"))
        running = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                if running.done():
                    await running
                    raise RuntimeError("HTTP server stopped before startup")
                await asyncio.sleep(0.01)
            save()
            (root / "ready.json").write_text(
                json.dumps(
                    {
                        **ready,
                        "python": list(sys.version_info[:3]),
                        "adcp_file": adcp.__file__,
                        "pydantic": importlib.metadata.version("pydantic"),
                        "mcp": importlib.metadata.version("mcp"),
                    }
                )
            )
            await running
        finally:
            server.should_exit = True
            await running


def main():
    backend, root, descriptor, scenario, count, notifications = sys.argv[1:]
    asyncio.run(
        run(backend, Path(root), int(descriptor), scenario, int(count), notifications == "true")
    )
