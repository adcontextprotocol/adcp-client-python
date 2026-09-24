"""Real worker process and conditional file destination, solely for crash tests."""

import asyncio
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4


def emit(point, **values):
    print(json.dumps({"point": point, **values}), flush=True)


async def read():
    return json.loads(await asyncio.to_thread(sys.stdin.readline))


async def main():
    settings = await read()
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool
    from pydantic import TypeAdapter

    from adcp.reporting.ledger._delivery_state import decode_record
    from adcp.reporting.materializer import (
        PgReportingMaterializerStore,
        ReferenceReportingDestinationWriter,
        ReferenceReportingResolver,
        ReportingDestinationIO,
        ReportingMaterializerService,
        ReportingRevisionVerifierRegistry,
        reference_verifier,
    )
    from adcp.reporting.materializer.reference import _Artifact, _Session

    origins = {}
    if settings.get("installed"):
        installed = settings["installed"]
        workspace = Path(installed["workspace"]).resolve()
        for name, expected in installed["modules"].items():
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
            origins[name] = str(path)
        assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
        for name, module in tuple(sys.modules.items()):
            if name == "adcp" or name.startswith("adcp."):
                if getattr(module, "__file__", None):
                    assert not Path(module.__file__).resolve().is_relative_to(workspace)
                    assert "site-packages" in module.__file__
        assert list(sys.version_info[:2]) == installed["python"]

    if settings.get("action") == "install":
        from adcp.reporting.ledger import LedgerConflictError

        async with AsyncConnectionPool(
            settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=1, open=False
        ) as pool:
            store = PgReportingMaterializerStore(pool=pool, notifications=settings["notifications"])
            try:
                await store.materializer_ready()
            except LedgerConflictError:
                # An empty schema must reject readiness before installation.
                pass
            else:
                raise AssertionError("empty schema must be unready")
            await store.create_schema()
            await store.create_schema()
            assert await store.materializer_ready()
        emit("done", state="installed", origins=origins)
        return

    async def hit(point):
        if point == settings.get("pause"):
            emit(point)
            materializer_operation_1 = await read()
            assert (materializer_operation_1)["continue"]

    class Connection(AsyncConnection):
        async def execute(self, query, params=None, **kwargs):
            result = await super().execute(query, params, **kwargs)
            if isinstance(query, str) and query.startswith(
                "INSERT INTO reporting_materializer_notification_events"
            ):
                await hit("after_event")
            return result

    class Store(PgReportingMaterializerStore):
        async def claim_materialization(self, **kwargs):
            result = await super().claim_materialization(**kwargs)
            if hasattr(result, "attempt"):
                await hit("reserved")
            return result

        async def _commit_record_on(self, connection, record, **kwargs):
            result = await super()._commit_record_on(connection, record, **kwargs)
            if record.kind == "materialization":
                await hit("after_outcome")
            return result

        async def _materializer_dirty_on(self, *args):
            await super()._materializer_dirty_on(*args)
            await hit("after_capture")

        async def finish_materialization(self, *args, **kwargs):
            if kwargs.get("verified") is not None:
                await hit("after_readback")
            result = await super().finish_materialization(*args, **kwargs)
            if result.state == "verified":
                await hit("after_commit")
            return result

    verifier = reference_verifier()
    registry = ReportingRevisionVerifierRegistry((verifier,))
    writer = ReferenceReportingDestinationWriter((verifier.key.capability,))
    reference = ReferenceReportingResolver(writer, registry, (decode_record(settings["binding"]),))
    artifact_codec = TypeAdapter(_Artifact)
    directory = Path(settings["destination"])

    class Session(_Session):
        async def write(self, content):
            await hit("before_write")
            path = directory / self.request.external_id
            if path.exists():
                writer._artifacts[self.request.external_id] = artifact_codec.validate_json(
                    path.read_bytes()
                )
            locator = await super().write(content)
            if not path.exists():
                temporary = directory / ("staged-" + uuid4().hex)
                temporary.write_bytes(
                    artifact_codec.dump_json(writer._artifacts[self.request.external_id])
                )
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)  # Conditional atomic publication, no overwrite.
                except FileExistsError:
                    writer._artifacts[self.request.external_id] = artifact_codec.validate_json(
                        path.read_bytes()
                    )
                    locator = await super().write(content)
                finally:
                    temporary.unlink()
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            await hit("after_write")
            return locator

    class Resolver:
        def resolve(self, request, *, phase, context):
            return Session(reference, request, phase, context)

    async with AsyncConnectionPool(
        settings["conninfo"],
        kwargs=settings["kwargs"],
        connection_class=Connection,
        min_size=1,
        max_size=1,
        open=False,
    ) as pool:
        store = Store(pool=pool, notifications=settings["notifications"])
        service = ReportingMaterializerService(
            store, ReportingDestinationIO(registry, Resolver()), writer, lease_seconds=90
        )
        result = await service.run_once()
        emit("done", state=result.state, reason=result.reason, origins=origins)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException as error:
        emit("failed", classification=type(error).__name__)
        sys.exit(1)
