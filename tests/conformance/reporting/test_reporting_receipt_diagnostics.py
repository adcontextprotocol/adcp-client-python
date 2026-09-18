"""Unexpected original failures are useful to operators without retaining payloads."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.reporting.receipts import ReportingReceiptError, ReportingReceiptHandler
from adcp.server.base import ToolContext

from ._receipt_support import receipt_case, receipt_harness, request_for
from ._receipt_transport import MountedReceipts, error_code

SECRETS = (
    "unexpected-request-secret-64ee282a",
    "private-account-64ee282a",
    "private-consumer-64ee282a",
    "private-idempotency-64ee282a",
    "private-continuation-64ee282a",
    "private-receipt-64ee282a",
    "postgresql://private-auth-token-64ee282a@provider.invalid/financial",
    "SELECT private_financial_value_64ee282a FROM provider_payload",
)
SAFE_MESSAGE = "receipt storage is unavailable; retry the same batch and key"
DIAGNOSTIC_FIELDS = {
    "code",
    "boundary",
    "exception_type",
    "origin_module",
    "origin_function",
    "origin_line",
}


def operator_errors(caplog):
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def assert_diagnostic(caplog, boundary, exception_type, *, origin_function=None):
    records = operator_errors(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.name == "adcp.reporting.receipts"
    assert record.code == "RECEIPT_STORAGE_UNAVAILABLE"
    assert record.boundary == boundary
    assert record.exception_type == exception_type
    assert record.origin_module == __name__
    if origin_function is not None:
        assert record.origin_function == origin_function
    assert type(record.origin_line) is int and record.origin_line > 0
    assert record.args == () and record.exc_info is None and record.exc_text is None
    assert record.stack_info is None
    standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    assert set(record.__dict__) - standard == DIAGNOSTIC_FIELDS
    # No repr fallback: every retained value must itself be JSON serializable.
    serialized = json.dumps(record.__dict__, sort_keys=True)
    for secret in SECRETS:
        assert secret not in serialized and secret not in caplog.text
    return record


async def sensitive_case(h):
    s = await receipt_case(h, account_id=SECRETS[1], consumer_id=SECRETS[2])
    request = request_for(s, key=SECRETS[3])
    request["receipts"][0]["reporting_receipt_id"] = SECRETS[5]
    request["context"] = {"request_secret": SECRETS[0], "continuation": SECRETS[4]}
    return s, request


async def mounted_call(mount, transport, request):
    async with mount.client() as client:
        if transport == "mcp":
            return await mount.mcp(client, request)
        return await mount.a2a(client, request, v1=transport == "a2a-1.0")


def assert_safe_wire(status, payload, request):
    assert status == 200
    assert error_code(payload) == "RECEIPT_STORAGE_UNAVAILABLE"
    error = payload["adcp_error"] if "adcp_error" in payload else payload["errors"][0]
    expected = (
        "sync_reporting_receipts failed: " + SAFE_MESSAGE
        if "adcp_error" in payload
        else SAFE_MESSAGE
    )
    assert error["message"] == expected
    # The existing transport echoes caller context. It must remain unchanged;
    # only the safe error, not the already supplied context, is diagnostic text.
    assert payload.get("context") == request.get("context")
    assert set(payload) <= {"context", "adcp_error", "errors"}
    serialized = json.dumps(error, sort_keys=True)
    for secret in SECRETS:
        assert secret not in serialized


@pytest.mark.parametrize("transport", ["mcp", "a2a-0.3", "a2a-1.0"])
@pytest.mark.parametrize("boundary", ["resolver", "custom-store"])
async def test_mounted_unexpected_original_failure_logs_once_and_keeps_safe_wire(
    transport, boundary, caplog
):
    caplog.set_level(logging.ERROR)
    async with receipt_harness("memory") as h:
        s, request = await sensitive_case(h)

        class CustomStore:
            async def ingest_receipt_batch(self, request, *, caller):
                raise RuntimeError(" | ".join(SECRETS))

        mount = MountedReceipts(
            h if boundary == "resolver" else SimpleNamespace(store=CustomStore())
        )
        mount.authorize(s)

        async def resolver(reference, context, consumer):
            # The private chain must not survive on the buyer error or record.
            try:
                raise ValueError(SECRETS[6])
            except ValueError as cause:
                raise RuntimeError(" | ".join(SECRETS)) from cause

        if boundary == "resolver":
            mount.handler._receipt_account_resolver = resolver
        status, payload = await mounted_call(mount, transport, request)
        assert_safe_wire(status, payload, request)
        assert_diagnostic(
            caplog,
            "handler",
            "RuntimeError",
            origin_function="resolver" if boundary == "resolver" else "ingest_receipt_batch",
        )


def inject_driver_failure(monkeypatch, point, failure):
    from psycopg import AsyncConnection

    fired = []
    original_execute = AsyncConnection.execute
    original_command = AsyncConnection._exec_command

    async def execute(connection, query, *args, **kwargs):
        result = await original_execute(connection, query, *args, **kwargs)
        if (
            not fired
            and isinstance(query, str)
            and query.startswith("INSERT INTO reporting_receipt_ingestion_results")
        ):
            fired.append(point)
            raise failure
        return result

    def commit(connection, command, *args, **kwargs):
        if not fired and command == b"COMMIT":
            fired.append(point)
            raise failure
        return (yield from original_command(connection, command, *args, **kwargs))

    if point == "execute":
        monkeypatch.setattr(AsyncConnection, "execute", execute)
    else:
        monkeypatch.setattr(AsyncConnection, "_exec_command", commit)
    return fired


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("point", ["execute", "commit"])
@pytest.mark.parametrize("transport", ["store", "handler", "mcp", "a2a-0.3", "a2a-1.0"])
async def test_original_pg_execute_and_commit_failures_log_once_across_translation(
    notifications, point, transport, monkeypatch, caplog
):
    caplog.set_level(logging.ERROR)
    async with receipt_harness("postgres", notifications=notifications) as h:
        from psycopg import OperationalError

        s, request = await sensitive_case(h)
        before = await h.image()
        mount = MountedReceipts(h)
        mount.authorize(s)
        with monkeypatch.context() as patch:
            fired = inject_driver_failure(patch, point, OperationalError(" | ".join(SECRETS)))
            if transport in {"store", "handler"}:
                error_type = ReportingReceiptError if transport == "store" else ADCPTaskError
                with pytest.raises(error_type) as caught:
                    if transport == "store":
                        await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
                    else:
                        await mount.handler.sync_reporting_receipts(
                            request, ToolContext(caller_identity=s.binding.consumer_id)
                        )
                assert caught.value.__cause__ is None and caught.value.__context__ is None
                if transport == "store":
                    assert caught.value.code == "RECEIPT_STORAGE_UNAVAILABLE"
                    assert str(caught.value) == SAFE_MESSAGE
                else:
                    assert caught.value.errors[0].code == "RECEIPT_STORAGE_UNAVAILABLE"
                    assert caught.value.errors[0].message == SAFE_MESSAGE
            else:
                status, payload = await mounted_call(mount, transport, request)
                assert_safe_wire(status, payload, request)
        assert fired == [point]
        assert_diagnostic(
            caplog, "store.ingest_receipt_batch", "OperationalError", origin_function=point
        )
        assert await h.image() == before
        assert (await h.store.ingest_receipt_batch(request, caller=s.binding.principal))["results"][
            0
        ]["result"] == "recorded"


@pytest.mark.parametrize(
    "code",
    [
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "IDEMPOTENCY_CONFLICT",
        "RECEIPT_SCHEMA_UNREADY",
        "RECEIPT_HISTORY_CORRUPT",
    ],
)
@pytest.mark.parametrize("transport", ["mcp", "a2a-0.3", "a2a-1.0"])
async def test_expected_closed_receipt_errors_are_silent_on_mounted_transports(
    code, transport, caplog
):
    caplog.set_level(logging.ERROR)
    async with receipt_harness("memory") as h:
        s, request = await sensitive_case(h)

        class ExpectedFailureStore:
            async def ingest_receipt_batch(self, request, *, caller):
                raise ReportingReceiptError(code)

        mount = MountedReceipts(SimpleNamespace(store=ExpectedFailureStore()))
        mount.authorize(s)
        status, payload = await mounted_call(mount, transport, request)
        assert status == 200 and error_code(payload) == code
        assert operator_errors(caplog) == []


@pytest.mark.parametrize("boundary", ["resolver", "custom-store", "pg"])
async def test_cancellation_propagates_without_translation_or_diagnostic(
    boundary, caplog, monkeypatch
):
    caplog.set_level(logging.ERROR)
    async with receipt_harness("postgres" if boundary == "pg" else "memory") as h:
        s, request = await sensitive_case(h)
        mount = MountedReceipts(h)
        mount.authorize(s)

        async def cancel(*args, **kwargs):
            raise asyncio.CancelledError(SECRETS[0])

        with monkeypatch.context() as patch:
            if boundary == "resolver":
                mount.handler._receipt_account_resolver = cancel
            elif boundary == "custom-store":
                patch.setattr(h.store, "ingest_receipt_batch", cancel)
            else:
                inject_driver_failure(patch, "execute", asyncio.CancelledError(SECRETS[0]))
            with pytest.raises(asyncio.CancelledError):
                await mount.handler.sync_reporting_receipts(
                    request, ToolContext(caller_identity=s.binding.consumer_id)
                )
        assert operator_errors(caplog) == []


async def test_unexpected_exception_is_never_formatted_and_translation_context_is_empty(caplog):
    caplog.set_level(logging.ERROR)

    class UnformattableError(RuntimeError):
        def __str__(self):
            raise AssertionError("unexpected exception was stringified")

        def __repr__(self):
            raise AssertionError("unexpected exception was represented")

    async with receipt_harness("memory") as h:
        _, request = await sensitive_case(h)

        async def resolver(reference, context, consumer):
            raise UnformattableError(*SECRETS)

        handler = ReportingReceiptHandler(h.store, resolve_account=resolver)
        with pytest.raises(ADCPTaskError) as caught:
            await handler.sync_reporting_receipts(request, ToolContext(caller_identity=SECRETS[2]))
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert_diagnostic(caplog, "handler", "UnformattableError", origin_function="resolver")


@pytest.mark.parametrize("transport", ["mcp", "a2a-0.3", "a2a-1.0"])
@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize(
    "code",
    [
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "IDEMPOTENCY_CONFLICT",
        "RECEIPT_SCHEMA_UNREADY",
        "RECEIPT_HISTORY_CORRUPT",
    ],
)
async def test_actual_domain_rejections_do_not_emit_operator_errors(
    transport, notifications, code, caplog
):
    caplog.set_level(logging.ERROR)
    async with receipt_harness("postgres", notifications=notifications) as h:
        s, request = await sensitive_case(h)
        mount = MountedReceipts(h)
        mount.authorize(s)
        if code != "INVALID_REQUEST":
            await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
        if code == "INVALID_REQUEST":
            request["receipts"][0]["received_at"] = "2026-09-18T00:00:00Z"
        elif code == "UNAUTHORIZED":
            mount.grants.clear()
        elif code == "IDEMPOTENCY_CONFLICT":
            request["context"]["changed"] = True
        elif code == "RECEIPT_SCHEMA_UNREADY":
            async with h.pool.connection() as connection:
                await connection.execute(
                    "ALTER TABLE reporting_receipt_ingestion_results"
                    " DISABLE TRIGGER reporting_receipt_ingestion_result"
                )
        else:
            # Privileged corruption fixture, with all schema guards restored
            # before invoking the production decoder. This is a domain error.
            from psycopg.types.json import Jsonb

            from adcp.reporting.canonical_json import canonical_json_sha256_v1

            async with h.pool.connection() as connection, connection.transaction():
                value = (
                    await (
                        await connection.execute(
                            "SELECT final_response FROM reporting_receipt_ingestion_batches"
                        )
                    ).fetchone()
                )[0]
                value["results"][0]["receipt"]["reporting_receipt_id"] = "corrupt-other-receipt"
                await connection.execute("SET LOCAL session_replication_role = replica")
                await connection.execute(
                    "UPDATE reporting_receipt_ingestion_batches"
                    " SET final_response=%s,final_sha256=%s",
                    (Jsonb(value), canonical_json_sha256_v1(value)),
                )
        before = await h.image()
        status, payload = await mounted_call(mount, transport, request)
        assert status == 200 and error_code(payload) == code
        assert operator_errors(caplog) == []
        assert await h.image() == before


async def test_diagnostic_does_not_capture_task_names_or_ambient_record_context(caplog):
    caplog.set_level(logging.ERROR)
    original_factory = logging.getLogRecordFactory()
    task = asyncio.current_task()
    original_name = task.get_name()

    def request_factory(*args, **kwargs):
        record = original_factory(*args, **kwargs)
        record.request_context = SECRETS
        return record

    async with receipt_harness("memory") as h:
        _, request = await sensitive_case(h)

        async def resolver(reference, context, consumer):
            raise RuntimeError(*SECRETS)

        handler = ReportingReceiptHandler(h.store, resolve_account=resolver)
        try:
            logging.setLogRecordFactory(request_factory)
            task.set_name(SECRETS[1])
            with pytest.raises(ADCPTaskError):
                await handler.sync_reporting_receipts(
                    request, ToolContext(caller_identity=SECRETS[2])
                )
        finally:
            logging.setLogRecordFactory(original_factory)
            task.set_name(original_name)
        record = assert_diagnostic(caplog, "handler", "RuntimeError", origin_function="resolver")
        assert record.threadName is None and record.processName is None
        assert getattr(record, "taskName", None) is None
        assert not hasattr(record, "request_context")


async def test_origin_sanitizer_discards_paths_and_invalid_module_names(caplog):
    caplog.set_level(logging.ERROR)
    namespace = {"__name__": SECRETS[6], "RuntimeError": RuntimeError}
    source = "async def resolver(*args):\n    raise RuntimeError('private provider failure')\n"
    exec(compile(source, "/private/provider/" + SECRETS[3] + ".py", "exec"), namespace)
    async with receipt_harness("memory") as h:
        _, request = await sensitive_case(h)
        handler = ReportingReceiptHandler(h.store, resolve_account=namespace["resolver"])
        with pytest.raises(ADCPTaskError) as caught:
            await handler.sync_reporting_receipts(request, ToolContext(caller_identity=SECRETS[2]))
        assert caught.value.__cause__ is None and caught.value.__context__ is None
    records = operator_errors(caplog)
    assert len(records) == 1
    record = records[0]
    assert (record.origin_module, record.origin_function, record.origin_line) == (
        "unknown",
        "resolver",
        2,
    )
    assert record.pathname == "" and record.exc_info is None and record.exc_text is None
    serialized = json.dumps(record.__dict__)
    assert "private provider failure" not in serialized
    assert all(secret not in serialized for secret in SECRETS)


@pytest.mark.parametrize("boundary", ["resolver", "pg"])
async def test_failing_operator_sink_cannot_replace_safe_translation(boundary, monkeypatch):
    logger = logging.getLogger("adcp.reporting.receipts")
    emitted = []

    class BrokenSink(logging.Handler):
        def emit(self, record):
            emitted.append(record)
            raise RuntimeError(SECRETS[0])

    sink = BrokenSink()
    async with receipt_harness("postgres" if boundary == "pg" else "memory") as h:
        s, request = await sensitive_case(h)
        mount = MountedReceipts(h)
        mount.authorize(s)

        async def resolver(*args):
            raise RuntimeError(*SECRETS)

        with monkeypatch.context() as patch:
            if boundary == "resolver":
                mount.handler._receipt_account_resolver = resolver
            else:
                fired = inject_driver_failure(patch, "execute", RuntimeError(*SECRETS))
            logger.addHandler(sink)
            try:
                with pytest.raises(ADCPTaskError) as caught:
                    await mount.handler.sync_reporting_receipts(
                        request, ToolContext(caller_identity=s.binding.consumer_id)
                    )
            finally:
                logger.removeHandler(sink)
            if boundary == "pg":
                assert fired == ["execute"]
        assert caught.value.errors[0].code == "RECEIPT_STORAGE_UNAVAILABLE"
        assert caught.value.errors[0].message == SAFE_MESSAGE
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert len(emitted) == 1
        assert emitted[0].exc_info is None and emitted[0].exc_text is None
        assert all(secret not in json.dumps(emitted[0].__dict__) for secret in SECRETS)
