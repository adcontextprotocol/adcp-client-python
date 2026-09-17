"""Adversarial asynchronous ownership: safe errors, partial opens and cancellation."""

import asyncio
import pickle
import traceback
from dataclasses import replace

import pytest

from adcp.reporting.materializer import (
    ReportingDestinationIO,
    ReportingDestinationSession,
    ReportingWriterCapability,
    ReportingWriterError,
    ReportingWriterFailure,
)
from adcp.reporting.materializer.reference import _Session

from ._materializer_support import io_context, materializer_case

SECRET = "https://provider.example.test/private?token=credential-sentinel&signature=provider-body"


class Phases:
    def __init__(self, case, tmp_path, stage, mode):
        self.case, self.tmp_path, self.stage, self.mode = case, tmp_path, stage, mode
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.sessions = []
        self.spools = []
        self.stream_closes = 0

    async def hit(self, stage):
        if stage != self.stage:
            return
        self.entered.set()
        if self.mode == "error":
            try:
                raise RuntimeError(SECRET)
            except RuntimeError as exc:
                raise ValueError(SECRET) from exc
        await self.release.wait()

    def resolve(self, request, *, phase, context):
        owner = self

        class Session(_Session):
            async def _open(self):
                await super()._open()
                self._credential = SECRET
                self.spool = owner.tmp_path / f"{len(owner.spools)}.spool"
                self.spool.write_text(SECRET)
                owner.spools.append(self.spool)
                await owner.hit("resolve" if self.phase == "write" else "readback-resolve")

            async def _close(self):
                try:
                    await owner.hit("close")
                finally:
                    if hasattr(self, "spool"):
                        self.spool.unlink(missing_ok=True)
                    await super()._close()

            async def write(self, content):
                await owner.hit("write-before")
                locator = await super().write(content)
                await owner.hit("write-after")
                return locator

            async def read_rows(self, locator, *, cursor, limit):
                await owner.hit("rows")
                return await super().read_rows(locator, cursor=cursor, limit=limit)

            async def read_manifest(self, locator):
                await owner.hit("manifest")
                return await super().read_manifest(locator)

            async def observe_native_version(self, locator):
                self.native_reads = getattr(self, "native_reads", 0) + 1
                await owner.hit("native-before" if self.native_reads == 1 else "native-after")
                return await super().observe_native_version(locator)

            async def list_objects(self, locator):
                await owner.hit("inventory")
                return await super().list_objects(locator)

            async def read_object(self, locator, *, object_ref):
                try:
                    await owner.hit("object")
                    async for chunk in super().read_object(locator, object_ref=object_ref):
                        yield chunk
                finally:
                    owner.stream_closes += 1
                    await owner.hit("stream-close")

        session = Session(self.case.resolver, request, phase, context)
        self.sessions.append(session)
        return session


def safe_exception(error):
    rendered = (
        str(error)
        + repr(error)
        + "".join(traceback.format_exception(type(error), error, error.__traceback__))
    )
    assert (
        SECRET not in rendered
        and "credential-sentinel" not in rendered
        and "provider-body" not in rendered
    )
    assert error.__cause__ is None
    if isinstance(error, asyncio.CancelledError):
        # Python 3.10 Task adds a clean CancelledError context even to an
        # unconditionally canceled coroutine. Inspect the entire chain: no
        # provider exception or cancellation message may survive that wrapper.
        seen = set()
        while error is not None:
            assert id(error) not in seen
            seen.add(id(error))
            assert type(error) is asyncio.CancelledError and error.args == ()
            assert error.__cause__ is None
            error = error.__context__
    else:
        assert error.__context__ is None


@pytest.mark.parametrize(
    "stage",
    [
        "resolve",
        "readback-resolve",
        "write-before",
        "write-after",
        "rows",
        "manifest",
        "inventory",
        "object",
        "close",
        "stream-close",
        "native-before",
        "native-after",
    ],
)
@pytest.mark.parametrize("mode", ["cancel", "signal", "timeout", "error"])
async def test_each_io_phase_closes_once_redacts_errors_and_leaves_no_tasks_or_spools(
    tmp_path, caplog, stage, mode
):
    capability = (
        ReportingWriterCapability(
            "dataset_share",
            "reference-memory",
            None,
            "canonical_digest",
            "representative_consumer",
            "native_version",
            "sha256",
            "conditional_create",
        )
        if stage.startswith("native")
        else None
    )
    case = await materializer_case(capability=capability)
    locator = await case.io.write(case.prepared, context=io_context())
    owner = Phases(case, tmp_path, stage, mode)
    io = ReportingDestinationIO(case.registry, owner)
    context = io_context(0.5 if mode == "timeout" else 20)
    if stage in {"close", "stream-close"}:
        context = replace(context, close_timeout_seconds=0.08 if mode == "timeout" else 5)
    before = set(asyncio.all_tasks())
    writing = stage in {"resolve", "write-before", "write-after", "close"}
    call = (
        io.write(case.prepared, context=context)
        if writing
        else io.verify(case.prepared, locator, context=context)
    )
    task = asyncio.create_task(call)
    await asyncio.wait_for(owner.entered.wait(), 10)
    if mode == "cancel":
        task.cancel(SECRET)
        await asyncio.sleep(0)
        task.cancel(SECRET)  # Repeated cancellation cannot abandon cleanup.
    elif mode == "signal":
        context.cancel.set()
    if mode in {"cancel", "signal"} and stage in {"close", "stream-close"}:
        await asyncio.sleep(0)
        owner.release.set()
    expected = asyncio.CancelledError if mode in {"cancel", "signal"} else ReportingWriterError
    with pytest.raises(expected) as caught:
        await asyncio.wait_for(task, 10)
    safe_exception(caught.value)
    assert all(session._closed and session._credential is None for session in owner.sessions)
    assert case.writer.open_count == case.writer.close_count == 2
    assert all(not p.exists() for p in owner.spools)
    assert list(tmp_path.iterdir()) == []
    assert not (set(asyncio.all_tasks()) - before)
    assert SECRET not in caplog.text
    if stage in {"object", "stream-close"}:
        assert owner.stream_closes == 1
    for session in owner.sessions:
        await session.aclose()  # A repeated close is physically idle.
    assert case.writer.close_count == 2


@pytest.mark.parametrize("mode", ["cancel", "timeout", "error"])
async def test_source_read_cancellation_removes_its_spool_before_any_resolver_io(tmp_path, mode):
    case = await materializer_case()
    owner = Phases(case, tmp_path, "source", mode)
    path = tmp_path / "source.spool"
    closed = 0

    class Reader:
        async def read_revision_rows(self, **kwargs):
            nonlocal closed
            path.write_text(SECRET)
            try:
                await owner.hit("source")
                return await case.store.read_revision_rows(**kwargs)
            finally:
                path.unlink()
                closed += 1

    before = set(asyncio.all_tasks())
    task = asyncio.create_task(
        case.prepare(reader=Reader(), context=io_context(0.05 if mode == "timeout" else 20))
    )
    await asyncio.wait_for(owner.entered.wait(), 5)
    if mode == "cancel":
        task.cancel(SECRET)
    with pytest.raises(
        asyncio.CancelledError if mode == "cancel" else ReportingWriterError
    ) as caught:
        await task
    safe_exception(caught.value)
    assert closed == 1 and not path.exists() and case.writer.open_count == 0
    assert not (set(asyncio.all_tasks()) - before)


@pytest.mark.parametrize("phase", ["open", "write"])
@pytest.mark.parametrize("mode", ["cancel", "signal"])
async def test_cancellation_during_failed_operation_cleanup_takes_precedence(tmp_path, phase, mode):
    case = await materializer_case()
    closing, release = asyncio.Event(), asyncio.Event()
    spool = tmp_path / "failure.spool"

    class Session(_Session):
        async def _open(self):
            await super()._open()
            spool.write_text(SECRET)
            if phase == "open":
                raise ValueError(SECRET)

        async def write(self, content):
            raise ValueError(SECRET)

        async def _close(self):
            closing.set()
            try:
                await release.wait()
            finally:
                spool.unlink(missing_ok=True)
                await super()._close()

    class Resolver:
        def resolve(self, request, *, phase, context):
            return Session(case.resolver, request, phase, context)

    before = set(asyncio.all_tasks())
    context = io_context()
    task = asyncio.create_task(
        ReportingDestinationIO(case.registry, Resolver()).write(case.prepared, context=context)
    )
    await asyncio.wait_for(closing.wait(), 5)
    if mode == "cancel":
        task.cancel(SECRET)
        await asyncio.sleep(0)
        task.cancel(SECRET)
    else:
        context.cancel.set()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    safe_exception(caught.value)
    assert not spool.exists() and case.writer.open_count == case.writer.close_count == 1
    assert not (set(asyncio.all_tasks()) - before)


async def test_invalid_locator_after_write_retains_unknown_effect_and_original_identity():
    case = await materializer_case()

    class Session(_Session):
        async def write(self, content):
            locator = await super().write(content)
            return replace(locator, external_id="other-tenant-identity")

    class Resolver:
        def resolve(self, request, *, phase, context):
            return Session(case.resolver, request, phase, context)

    with pytest.raises(ReportingWriterError) as caught:
        await ReportingDestinationIO(case.registry, Resolver()).write(
            case.prepared, context=io_context()
        )
    assert caught.value.failure == ReportingWriterFailure(
        "BINDING_MISMATCH", "same_identity", "unknown"
    )
    safe_exception(caught.value)
    assert case.writer.write_effects == 1 and case.writer.open_count == case.writer.close_count == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_resolver_factory_never_exposes_provider_exception_or_cancel_message(cancel):
    case = await materializer_case()

    class BrokenResolver:
        def resolve(self, *args, **kwargs):
            raise asyncio.CancelledError(SECRET) if cancel else ValueError(SECRET)

    with pytest.raises(asyncio.CancelledError if cancel else ReportingWriterError) as caught:
        await ReportingDestinationIO(case.registry, BrokenResolver()).write(
            case.prepared, context=io_context()
        )
    safe_exception(caught.value)
    assert case.writer.open_count == 0


async def test_session_binding_is_checked_before_open_and_lifecycle_is_sdk_owned(tmp_path):
    case = await materializer_case()
    owner = Phases(case, tmp_path, "unused", "error")

    class WrongResolver:
        def resolve(self, request, *, phase, context):
            return owner.resolve(
                replace(request, destination_ref="wrong-destination"), phase=phase, context=context
            )

    with pytest.raises(ReportingWriterError, match="BINDING_MISMATCH"):
        await ReportingDestinationIO(case.registry, WrongResolver()).write(
            case.prepared, context=io_context()
        )
    assert case.writer.open_count == 0 and case.writer.close_count == 1
    session = owner.sessions[0]
    assert str(session) == repr(session) == "<ReportingDestinationSession redacted>"
    with pytest.raises(TypeError, match="cannot be persisted"):
        pickle.dumps(session)
    for name in ("request", "phase", "context"):
        with pytest.raises(AttributeError):
            setattr(session, name, SECRET)
    for name in ("__repr__", "__aenter__", "__aexit__", "aclose", "__reduce__"):
        with pytest.raises(TypeError, match="belong to the SDK"):
            type("UnsafeSession", (ReportingDestinationSession,), {name: lambda *args: SECRET})


async def test_closed_failures_preserve_known_failure_and_unknown_identity_contract():
    async def known():
        raise ReportingWriterError(
            ReportingWriterFailure("WRITE_FAILED", "new_attempt", "not_started")
        )

    async def connection_loss():
        raise ConnectionError(SECRET)

    for operation, expected in (
        (known, ("WRITE_FAILED", "new_attempt", "not_started")),
        (connection_loss, ("RESOURCE_UNAVAILABLE", "same_identity", "unknown")),
    ):
        with pytest.raises(ReportingWriterError) as caught:
            await io_context().run(operation, effect="unknown")
        record = caught.value.failure
        assert (record.code, record.retry, record.effect) == expected
        safe_exception(caught.value)
    with pytest.raises(ValueError):
        ReportingWriterFailure("RESOURCE_UNAVAILABLE", "new_attempt", "unknown")
