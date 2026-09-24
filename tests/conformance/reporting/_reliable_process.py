"""Private service executable for the extensible cross-layer PG failure matrix.

All storage, transactions, pinning, HTTP framing, TLS, signatures and receiver
verification are real. Only the test socket address mapping and certificate
trust root differ from deployment. No DNS/SSRF or sender policy is bypassed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import ssl
import sys
from datetime import timedelta
from types import SimpleNamespace

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from adcp.reporting.ledger import PgReportingReconciliationStore
from adcp.reporting.outbox import (
    PgReportingOutbox,
    ReportingEnvelopeCipher,
    ReportingNotificationWorker,
)

from ._generation_support import configuration, obligation_for, revision_for
from ._reliable_support import (
    DeterministicReceiverStore,
    FailurePlan,
    ManualClock,
    ScriptedSigning,
    ScriptedSubscriptions,
    _BytesStore,
    notification_subscription,
    notification_verification_keys,
)

_INPUT = None
_INPUT_TRANSPORT = None
_STAGE = "startup"
_DEADLINES = {}


class ServiceDeadlineError(TimeoutError):
    def __init__(self, stage):
        self.stage = stage
        super().__init__("service_deadline")


async def bounded(awaitable, *, stage, seconds=10):
    global _STAGE
    _STAGE = stage
    try:
        return await asyncio.wait_for(awaitable, _DEADLINES.get(stage, seconds))
    except ServiceDeadlineError:
        # Preserve the innermost named barrier/receiver deadline. The outer
        # service watchdog must not relabel an already bounded failure.
        raise
    except (TimeoutError, asyncio.TimeoutError):
        raise ServiceDeadlineError(stage) from None


def emit(point, **data):
    print(json.dumps({"point": point, **data}), flush=True)


async def command(*, stage="command"):
    global _INPUT, _INPUT_TRANSPORT
    if _INPUT is None:
        _INPUT = asyncio.StreamReader(limit=65536)
        _INPUT_TRANSPORT, _ = await bounded(
            asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(_INPUT), sys.stdin.buffer
            ),
            stage="stdin_open",
        )
    # A cancellable pipe reader, not a to_thread(readline) that strands the
    # executor during asyncio.run shutdown after a barrier deadline.
    line = await bounded(_INPUT.readline(), stage=stage, seconds=40)
    if not line:
        raise EOFError
    return json.loads(line)


async def barrier(point, **data):
    emit(point, **data)
    status_operation_1 = await command(stage=f"barrier:{point}")
    assert (status_operation_1)["continue"] == point


def install_test_socket(settings, clock):
    from httpcore._backends.anyio import AnyIOBackend

    from adcp.signing import ip_pinned_transport, signer

    resolve = socket.getaddrinfo

    def addresses(host, port, *args, **kwargs):
        if host == "receiver.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))]
        return resolve(host, port, *args, **kwargs)

    connect = AnyIOBackend.connect_tcp

    async def connect_pinned(self, host, port, **kwargs):
        # The SDK already resolved, rejected unsafe addresses and pinned this
        # exact public endpoint before this test-only socket map runs.
        assert host == "8.8.8.8" and port == 443
        return await bounded(
            connect(self, "127.0.0.1", settings["receiver_port"], **kwargs),
            stage="pinned_socket_connect",
            seconds=5,
        )

    def context():
        value = ssl.create_default_context(cafile=settings["certificate"])
        assert value.check_hostname and value.verify_mode == ssl.CERT_REQUIRED
        return value

    socket.getaddrinfo = addresses
    AnyIOBackend.connect_tcp = connect_pinned
    ip_pinned_transport._build_ssl_context = context
    signer.time = SimpleNamespace(time=lambda: clock().timestamp())


async def receiver(pool, settings, clock):
    from adcp.signing.jwks import StaticJwksResolver
    from adcp.signing.webhook_verifier import WebhookVerifyOptions, verify_webhook_signature

    blobs = _BytesStore(pool)
    await blobs.create_schema()
    store = DeterministicReceiverStore(blobs, FailurePlan())
    options = WebhookVerifyOptions(
        jwks_resolver=StaticJwksResolver({"keys": notification_verification_keys()}),
        clock=lambda: clock().timestamp(),
    )
    attempts = 0
    responses = {}

    async def accept(reader, writer):
        nonlocal attempts
        try:
            head = await bounded(reader.readuntil(b"\r\n\r\n"), stage="receiver_headers", seconds=5)
            lines = head.decode("ascii").split("\r\n")
            headers = dict(line.split(": ", 1) for line in lines[1:] if line)
            headers = {name.lower(): value for name, value in headers.items()}
            size = int(headers["content-length"])
            assert 0 < size < 65536
            body = await bounded(reader.readexactly(size), stage="receiver_body", seconds=5)
            target = lines[0].split(" ")[1]
            identity = verify_webhook_signature(
                method="POST",
                url="https://receiver.example.test" + target,
                headers=headers,
                body=body,
                options=options,
            )
            value = json.loads(body)
            assert value["account_id"] == "acct_a"
            await bounded(
                store.write(value["account_id"], value["idempotency_key"], body),
                stage="receiver_accept_commit",
            )
            attempts += 1
            response_gate = responses[attempts] = asyncio.Event()
            await bounded(
                blobs.put(
                    "http-attempt",
                    "acct_a",
                    value["idempotency_key"],
                    json.dumps(
                        {
                            "body": base64.b64encode(body).decode(),
                            "headers": headers,
                            "verified_key": identity.key_id,
                        }
                    ).encode(),
                    str(attempts),
                ),
                stage="receiver_attempt_commit",
            )
            emit("http_accepted", attempt=attempts)
            if settings.get("pause_responses"):
                await bounded(response_gate.wait(), stage="receiver_response_gate", seconds=30)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await bounded(writer.drain(), stage="receiver_response_write", seconds=5)
        except Exception as exc:
            emit(
                "receiver_failed",
                classification=failure_code(exc),
                stage=getattr(exc, "stage", _STAGE),
            )
        finally:
            writer.close()
            await bounded(writer.wait_closed(), stage="receiver_socket_close", seconds=5)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(settings["certificate"], settings["certificate_key"])
    async with await bounded(
        asyncio.start_server(accept, "127.0.0.1", 0, ssl=context, ssl_handshake_timeout=5),
        stage="receiver_listen",
    ) as server:
        emit("listening", port=server.sockets[0].getsockname()[1])
        while True:
            value = await command(stage="receiver_control")
            if value.get("stop"):
                return
            if "release_http" in value:
                responses[value["release_http"]].set()
                emit("http_released")
                continue
            clock.advance(timedelta(seconds=value["advance_seconds"]))
            emit("clock_advanced")


async def main():
    global _DEADLINES
    settings, role = await command(), sys.argv[1]
    _DEADLINES = settings.get("deadlines", {})
    if role.startswith("status_"):
        from ._status_process import run_status_role

        await run_status_role(
            role,
            settings,
            barrier=barrier,
            emit=emit,
            bounded=bounded,
            receiver=receiver,
            install_test_socket=install_test_socket,
        )
        return
    clock = ManualClock()
    clock.advance(timedelta(seconds=settings.get("advance_seconds", 0)))
    pause = settings.get("pause")

    class CheckpointConnection(AsyncConnection):
        async def execute(self, query, params=None, **kwargs):
            cursor = await bounded(
                super().execute(query, params, **kwargs), stage="database_statement", seconds=18
            )
            if (
                pause == "ack_written"
                and isinstance(query, str)
                and query.startswith("UPDATE reporting_notification_deliveries SET state = %s")
                and params[0] == "complete"
            ):
                await barrier("ack_written", backend_pid=self.info.backend_pid)
            return cursor

    kwargs = {
        **settings["pool_kwargs"],
        "autocommit": True,
        "application_name": f"reporting-matrix-{role}-{os.getpid()}",
    }
    async with AsyncConnectionPool(
        settings["conninfo"],
        kwargs=kwargs,
        min_size=2,
        max_size=3,
        connection_class=CheckpointConnection,
        open=False,
    ) as pool:
        await bounded(pool.wait(timeout=10), stage="pool_open", seconds=12)
        if role == "barrier_probe":
            await barrier("held")
            return
        if role == "receiver":
            await receiver(pool, settings, clock)
            return
        if role == "producer":
            store = PgReportingReconciliationStore(pool=pool, clock=clock, notifications=True)
            config = configuration()
            await store.put_configuration(config)
            obligation = await store.commit_obligation(obligation_for(config))
            original = store._record_notification

            async def enqueue(conn, event):
                await original(conn, event)
                if pause == "event_inserted":
                    await barrier(pause)

            store._record_notification = enqueue
            revision, rows = revision_for(obligation, suffix="process")
            await store.commit_revision(revision, rows)
            if pause == "revision_committed":
                await barrier(pause)
            emit("done")
            return
        outbox = PgReportingOutbox(pool=pool, clock=clock)
        failures = FailurePlan()
        subscriptions = ScriptedSubscriptions(failures)
        for subscriber in settings.get("subscribers", ["buyer"]):
            subscriptions.put(notification_subscription(subscriber=subscriber))
        signing = ScriptedSigning(failures)
        signing.generation = settings.get("signing_generation", 1)
        worker = ReportingNotificationWorker(
            outbox=outbox,
            subscriptions=subscriptions,
            signing=signing,
            cipher=ReportingEnvelopeCipher(b"e" * 32),
            clock=clock,
        )
        if role == "fanout":
            original_insert = outbox._insert_delivery
            inserted = 0

            async def insert(conn, delivery, at):
                nonlocal inserted
                await original_insert(conn, delivery, at)
                inserted += 1
                if inserted == 1 and pause == "fanout_partial":
                    await barrier(pause)

            outbox._insert_delivery = insert
            result = await worker.expand_one(account_id="acct_a")
        else:
            assert role == "worker"
            install_test_socket(settings, clock)
            if settings.get("start_paused"):
                await barrier("worker_ready")
            finish = outbox.finish_delivery

            async def ack(lease, **kwargs):
                if pause in {"before_ack", "ack_written"} and kwargs["state"] != "complete":
                    emit("attempt_released", classification=kwargs["error_code"] or "unknown_state")
                if pause == "before_ack" and kwargs["state"] == "complete":
                    await barrier(pause)
                return await finish(lease, **kwargs)

            outbox.finish_delivery = ack
            result = await worker.deliver_one(account_id="acct_a")
        emit("done", did_work=result)


def failure_code(error):
    from psycopg import Error

    from adcp.signing.errors import SignatureVerificationError

    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "deadline"
    if isinstance(error, SignatureVerificationError):
        return error.code
    if isinstance(error, Error):
        return "database_failure"
    if isinstance(error, AssertionError):
        return "assertion_failure"
    if isinstance(error, (EOFError, ValueError, TypeError, KeyError)):
        return "protocol_failure"
    return "service_failure"


async def run_service():
    try:
        await bounded(main(), stage="service_lifetime", seconds=80)
    finally:
        if _INPUT_TRANSPORT is not None:
            _INPUT_TRANSPORT.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_service())
    except Exception as error:
        emit(
            "service_failed",
            classification=failure_code(error),
            stage=getattr(error, "stage", _STAGE),
        )
        sys.exit(1)
