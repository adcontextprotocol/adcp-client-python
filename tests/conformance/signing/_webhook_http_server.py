"""A real loopback HTTP receiver for installed webhook boundary regressions."""

import asyncio
import hashlib
import json
import socket
import sys
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.webhooks import WebhookReceiver, WebhookReceiverConfig, WebhookVerifyOptions


async def run(root, socket_fd):
    key_rows = json.loads(
        files("adcp")
        .joinpath("_compliance/3.2.0-rc.6/test-vectors/webhook-signing/keys.json")
        .read_text()
    )["keys"]
    keys = {row["kid"]: {k: v for k, v in row.items() if not k.startswith("_")} for row in key_rows}
    observations = {"key_lookups": 0, "processed": 0}

    def resolve(kid):
        observations["key_lookups"] += 1
        return keys.get(kid)

    receiver = WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(jwks_resolver=resolve),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="loopback-test-receiver",
            publisher_scope_for=lambda _signer: "loopback-test-publisher",
        )
    )

    async def receive(request):
        body = await request.body()
        outcome = await receiver.receive(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            body=body,
        )
        if outcome.http_status is None:
            observations["processed"] += 1
            outcome = await receiver.acknowledge(outcome)
        with (root / "received.jsonl").open("a") as received:
            received.write(
                json.dumps(
                    {
                        "body_sha256": hashlib.sha256(body).hexdigest(),
                        "signature": request.headers.get("signature"),
                        "http_status": outcome.http_status or 200,
                    }
                )
                + "\n"
            )
        return JSONResponse(
            {**observations, "rejected": outcome.rejected, "reason": outcome.rejection_reason},
            status_code=outcome.http_status or 200,
            headers=dict(outcome.response_headers),
        )

    @asynccontextmanager
    async def lifespan(app):
        yield
        (root / "stopped").write_text("stopped\n")

    app = Starlette(routes=[Route("/webhook", receive, methods=["POST"])], lifespan=lifespan)
    sock = socket.socket(fileno=socket_fd)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    running = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if running.done():
                await running
                raise RuntimeError("webhook HTTP server stopped before startup")
            await asyncio.sleep(0.01)
        (root / "ready").write_text("ready\n")
        await running
    finally:
        server.should_exit = True
        await running


def main():
    asyncio.run(run(Path(sys.argv[1]), int(sys.argv[2])))
