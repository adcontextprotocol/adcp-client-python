"""Payload reuse without collapsing actual source observations or retry identity."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.conformance import validate_reporting_source_execution
from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import (
    FileSystemStagingStore,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.source import ReportingSourceSliceRequestV1
from tests.test_reporting_inline_source import OBSERVED_AT, ROW


def request_for(key: str) -> ReportingSourceSliceRequestV1:
    original = redacted_snapshot_request()
    return original.model_copy(
        update={"identity": original.identity.model_copy(update={"source_execution_key": key})}
    )


@pytest.fixture(params=["memory", "filesystem"])
def staging(request: Any, tmp_path: Path) -> InMemoryStagingStore | FileSystemStagingStore:
    return InMemoryStagingStore() if request.param == "memory" else FileSystemStagingStore(tmp_path)


async def read_payload(
    staging: InMemoryStagingStore | FileSystemStagingStore,
    pair: tuple[str, str],
    *,
    account: str = "account-redacted",
) -> bytes:
    return await staging.read(
        account_id=account,
        object_ref=pair[0],
        object_generation=pair[1],
        source_scope=redacted_capabilities().source_scope,
        cancel=asyncio.Event(),
    )


@pytest.mark.parametrize("empty", [False, True])
async def test_new_observations_share_payload_but_keep_distinct_manifests(
    staging: InMemoryStagingStore | FileSystemStagingStore, tmp_path: Path, empty: bool
) -> None:
    now = OBSERVED_AT
    rows = [] if empty else [dict(ROW)]
    calls: list[str] = []

    async def fetch(request: ReportingSourceSliceRequestV1) -> list[dict[str, Any]]:
        calls.append(request.identity.source_execution_key)
        return rows

    source = InlineReportingSource(
        capabilities=redacted_capabilities(), fetch=fetch, staging=staging, clock=lambda: now
    )

    async def observe(key: str) -> Any:
        request = request_for(key)
        result = await source.execute(request, cancel=asyncio.Event())
        manifest = await validate_reporting_source_execution(
            capabilities=source.capabilities,
            request=request,
            result=result,
            object_reader=staging,
        )
        return result, manifest

    first_result, first = await observe("acquisition-one")
    now += timedelta(minutes=10)
    rows = [] if empty else [{key: ROW[key] for key in reversed(list(ROW))}]
    second_result, second = await observe("acquisition-two")
    first_object, second_object = first.objects[0], second.objects[0]
    pair = (first_object.object_ref, first_object.object_generation)
    assert (second_object.object_ref, second_object.object_generation) == pair
    assert first.publication_id != second.publication_id
    assert first_result.manifest_bytes != second_result.manifest_bytes
    assert second.observed_at > first.observed_at
    assert second.acquired_at > first.acquired_at
    assert (
        first.finality_evidence.basis == second.finality_evidence.basis == "provisional_observation"
    )
    assert first.publication_class == second.publication_class == "PROVISIONAL_SNAPSHOT"
    if isinstance(staging, FileSystemStagingStore):
        assert len(list(tmp_path.rglob("*.bin"))) == 1
    original_payload = await read_payload(staging, pair)
    assert original_payload == (
        b""
        if empty
        else b'{"campaign_id":"campaign-redacted-1","impressions":10,'
        b'"media_buy_id":"media-buy-redacted","spend":"1.25"}\n'
    )

    now += timedelta(minutes=10)
    replayed, _ = await observe("acquisition-two")
    assert replayed.manifest_bytes == second_result.manifest_bytes
    assert calls == ["acquisition-one", "acquisition-two"]

    rows = [{**ROW, "impressions": ROW["impressions"] + 1}]
    _, changed = await observe("acquisition-three")
    changed_object = changed.objects[0]
    assert changed_object.object_ref != pair[0]
    assert changed_object.object_generation != pair[1]
    assert await read_payload(staging, pair) == original_payload
    if isinstance(staging, FileSystemStagingStore):
        assert len(list(tmp_path.rglob("*.bin"))) == 2


async def test_equal_bytes_owned_by_another_account_do_not_authorize_the_first_ref(
    staging: InMemoryStagingStore | FileSystemStagingStore,
) -> None:
    first = await staging.stage(
        account_id="account-redacted", source_execution_key="same-key", ordinal=0, payload=b"rows"
    )
    other = await staging.stage(
        account_id="other-account", source_execution_key="same-key", ordinal=0, payload=b"rows"
    )
    assert first[0] != other[0]
    assert first[1] == other[1]
    with pytest.raises(OSError):
        await read_payload(staging, first, account="other-account")
    assert await read_payload(staging, other, account="other-account") == b"rows"


async def test_existing_corrupt_payload_is_never_claimed_or_overwritten(
    staging: InMemoryStagingStore | FileSystemStagingStore, tmp_path: Path
) -> None:
    pair = await staging.stage(
        account_id="account-redacted", source_execution_key="same-key", ordinal=0, payload=b"rows"
    )
    if isinstance(staging, FileSystemStagingStore):
        target = next(tmp_path.rglob("*.bin"))
        target.write_bytes(b"ro")  # an interrupted or external truncating writer
    else:
        staging._objects[("account-redacted", *pair)] = b"ro"
    with pytest.raises(OSError, match="pinned generation"):
        await read_payload(staging, pair)
    with pytest.raises(OSError, match="pinned generation"):
        await staging.stage(
            account_id="account-redacted",
            source_execution_key="same-key",
            ordinal=0,
            payload=b"rows",
        )
    if isinstance(staging, FileSystemStagingStore):
        assert target.read_bytes() == b"ro"
    else:
        assert staging._objects[("account-redacted", *pair)] == b"ro"


async def test_staging_under_traversable_unreadable_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("requires POSIX directory fsync")
    restricted = tmp_path / "restricted"
    owned = restricted / "app"
    owned.mkdir(parents=True)
    restricted.chmod(0o711)
    real_open = os.open
    opened: list[Path] = []

    def guarded_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(Path(path))
        if Path(path) == restricted:
            raise PermissionError("unreadable ancestor")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    values = dict(
        account_id="account-redacted", source_execution_key="key", ordinal=0, payload=b"rows"
    )
    try:
        # Enforce the access restriction even on runners with CAP_DAC_OVERRIDE.
        with pytest.raises(PermissionError, match="unreadable ancestor"):
            os.open(restricted, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        opened.clear()
        root = owned / "staging"
        staging = FileSystemStagingStore(root)
        pair = await staging.stage(**values)
        assert await read_payload(staging, pair) == b"rows"
        # A fresh instance must also reuse the retained bytes without opening
        # the unreadable ancestor during its first root durability check.
        opened.clear()
        assert await FileSystemStagingStore(root).stage(**values) == pair
        assert owned in opened  # repair a possible unsynced root name
        assert restricted not in opened
    finally:
        restricted.chmod(0o700)


async def test_legacy_opaque_ref_keeps_its_existing_path_after_new_staging(tmp_path: Path) -> None:
    account = "account-redacted"
    reference = "old-acquisition.0"
    payload = b"legacy payload\n"
    generation = hashlib.sha256(payload).hexdigest()
    # This is the persisted pre-content-addressing layout, not a call to the
    # new store's path helper. Its meaning cannot change during rollout.
    target = (
        tmp_path
        / hashlib.sha256(account.encode()).hexdigest()[:32]
        / hashlib.sha256(reference.encode()).hexdigest()[:32]
        / f"{generation}.bin"
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    staging = FileSystemStagingStore(tmp_path)
    legacy = (reference, generation)
    assert await read_payload(staging, legacy) == payload
    current = await staging.stage(
        account_id=account, source_execution_key="new-acquisition", ordinal=0, payload=payload
    )
    assert current[0] != reference and current[1] == generation
    assert await read_payload(staging, legacy) == await read_payload(staging, current)


async def test_filesystem_reuse_syncs_only_through_store_root(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = tmp_path / "new" / "staging"
    staging = FileSystemStagingStore(root)
    opened: list[Path] = []
    original_open = os.open

    def record_open(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(Path(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", record_open)
    values = dict(
        account_id="account-redacted", source_execution_key="first", ordinal=0, payload=b"rows"
    )
    reference, generation = await staging.stage(**values)
    assert root in opened and root.parent in opened  # initial root creation is durable

    opened.clear()
    assert await staging.stage(**{**values, "source_execution_key": "reuse"}) == (
        reference,
        generation,
    )
    target = staging._path(values["account_id"], reference, generation)
    assert opened == [target.parent, target.parent.parent, root]


async def test_pending_writer_and_cancelled_caller_cannot_replace_a_complete_winner(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    pending: list[Path] = []
    first_seals = InMemorySealStore()

    class Paused(FileSystemStagingStore):
        @staticmethod
        def _publish_file(temporary: Path, target: Path) -> None:
            pending.append(target)
            assert temporary.read_bytes()
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "test cleanup watchdog"
            FileSystemStagingStore._publish_file(temporary, target)

    paused = Paused(tmp_path)
    first = InlineReportingSource(
        capabilities=redacted_capabilities(),
        fetch=lambda _request: [ROW],
        staging=paused,
        seals=first_seals,
        clock=lambda: OBSERVED_AT,
    )
    request = request_for("paused-acquisition")
    task = asyncio.create_task(first.execute(request, cancel=asyncio.Event()))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        assert not pending[0].exists(), "the uncommitted name became readable"
        assert (
            await first_seals.get(
                account_id=request.identity.account_id,
                source_execution_key=request.identity.source_execution_key,
            )
            is None
        )
        winner_store = FileSystemStagingStore(tmp_path)
        winner = InlineReportingSource(
            capabilities=redacted_capabilities(),
            fetch=lambda _request: [ROW],
            staging=winner_store,
            clock=lambda: OBSERVED_AT + timedelta(minutes=1),
        )
        winner_request = request_for("winning-acquisition")
        result = await winner.execute(winner_request, cancel=asyncio.Event())
        manifest = await validate_reporting_source_execution(
            capabilities=winner.capabilities,
            request=winner_request,
            result=result,
            object_reader=winner_store,
        )
        obj = manifest.objects[0]
        pair = (obj.object_ref, obj.object_generation)
        assert await read_payload(paused, pair) == pending[0].read_bytes()
        inode = pending[0].stat().st_ino
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0)
        assert not task.done(), "cancelled caller abandoned its pending file operation"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert pending[0].stat().st_ino == inode
    assert len(list(tmp_path.rglob("*.bin"))) == 1
    assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 1
    assert (
        await first_seals.get(
            account_id=request.identity.account_id,
            source_execution_key=request.identity.source_execution_key,
        )
        is None
    )
    # An acquisition that was cancelled before sealing can now retry. It shares
    # the winner's payload but publishes its own immutable observation.
    retried = await first.execute(request, cancel=asyncio.Event())
    retried_manifest = await validate_reporting_source_execution(
        capabilities=first.capabilities, request=request, result=retried, object_reader=paused
    )
    assert (
        retried_manifest.objects[0].object_ref,
        retried_manifest.objects[0].object_generation,
    ) == pair
    assert retried_manifest.publication_id != manifest.publication_id
    assert pending[0].stat().st_ino == inode


@pytest.mark.parametrize("phase", ["before_link", "after_link", "directory_sync"])
async def test_failed_publication_does_not_seal_and_retry_verifies_complete_bytes(
    tmp_path: Path, phase: str
) -> None:
    failing = True
    seals = InMemorySealStore()

    class Failing(FileSystemStagingStore):
        @staticmethod
        def _publish_file(temporary: Path, target: Path) -> None:
            if failing and phase == "before_link":
                raise OSError("injected publication failure")
            FileSystemStagingStore._publish_file(temporary, target)
            if failing and phase == "after_link":
                raise OSError("injected publication failure")

        def _sync_directory(self, directory: Path) -> None:
            if failing and phase == "directory_sync":
                # The temporary name must be removed before committing the
                # final directory state; no failed stage may seal a manifest.
                assert len([path for path in directory.iterdir() if path.is_file()]) == 1
                raise OSError("injected directory fsync failure")
            super()._sync_directory(directory)

    staging = Failing(tmp_path)
    source = InlineReportingSource(
        capabilities=redacted_capabilities(),
        fetch=lambda _request: [ROW],
        staging=staging,
        seals=seals,
        clock=lambda: OBSERVED_AT,
    )
    request = request_for("retry-acquisition")
    with pytest.raises(OSError):
        await source.execute(request, cancel=asyncio.Event())
    assert (
        await seals.get(
            account_id=request.identity.account_id,
            source_execution_key=request.identity.source_execution_key,
        )
        is None
    )
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(files) == (0 if phase == "before_link" else 1)
    assert all(path.suffix == ".bin" for path in files)
    failing = False
    result = await source.execute(request, cancel=asyncio.Event())
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities, request=request, result=result, object_reader=staging
    )
    assert len(list(tmp_path.rglob("*.bin"))) == 1
    replayed = await source.execute(request, cancel=asyncio.Event())
    assert replayed.manifest_bytes == result.manifest_bytes
    # Reuse also performs the directory durability barrier. A concurrent writer
    # having linked the object is insufficient evidence that its name is durable.
    if phase == "directory_sync":
        failing = True
        with pytest.raises(OSError, match="fsync"):
            await source.execute(request_for("later-acquisition"), cancel=asyncio.Event())
        assert (
            await seals.get(
                account_id=request.identity.account_id, source_execution_key="later-acquisition"
            )
            is None
        )
        assert manifest.objects[0].sha256 == hashlib.sha256(files[0].read_bytes()).hexdigest()


async def test_inflight_truncating_writer_is_never_accepted_as_committed(tmp_path: Path) -> None:
    staging = FileSystemStagingStore(tmp_path)
    values = dict(
        account_id="account-redacted", source_execution_key="key", ordinal=0, payload=b"rows"
    )
    pair = await staging.stage(**values)
    target = next(tmp_path.rglob("*.bin"))
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def mutate() -> None:
        with target.open("wb") as stream:
            stream.write(b"ro")
            stream.flush()
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "test cleanup watchdog"
            stream.write(b"ws")
            stream.flush()
            os.fsync(stream.fileno())

    writer = asyncio.create_task(asyncio.to_thread(mutate))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        with pytest.raises(OSError, match="pinned generation"):
            await staging.stage(**values)
        with pytest.raises(OSError, match="pinned generation"):
            await read_payload(staging, pair)
    finally:
        release.set()
        # Propagate writer failures and finish the mutation before retrying.
        await writer
    assert await staging.stage(**values) == pair
    assert await read_payload(staging, pair) == b"rows"


_PROCESS = r"""
import asyncio, hashlib, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from adcp.reporting.conformance import validate_reporting_source_execution
from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import (
    FileSystemStagingStore, InlineReportingSource, InMemorySealStore,
)

root, mode, key = sys.argv[1:4]
class Staging(FileSystemStagingStore):
    @staticmethod
    def _publish_file(temporary, target):
        if mode == 'interrupted':
            event = {'phase': 'unpublished', 'complete_temporary': temporary.is_file(),
                     'target_exists': target.exists()}
            print(json.dumps(event), flush=True)
            sys.stdin.buffer.read(1)
        FileSystemStagingStore._publish_file(temporary, target)
    def _sync_directory(self, directory):
        if mode == 'linked':
            files = list(directory.iterdir())
            print(json.dumps({'phase': 'linked', 'files': len(files),
                              'complete_final': files[0].suffix == '.bin'}), flush=True)
            sys.stdin.buffer.read(1)
        super()._sync_directory(directory)

async def main():
    staging = Staging(root)
    request = redacted_snapshot_request()
    identity = request.identity.model_copy(update={'source_execution_key': key})
    request = request.model_copy(update={'identity': identity})
    if mode == 'read':
        pair = json.loads(sys.argv[4])
        data = await staging.read(account_id=request.identity.account_id,
            object_ref=pair[0], object_generation=pair[1],
            source_scope=request.identity.source_scope, cancel=asyncio.Event())
        print(json.dumps({'generation': hashlib.sha256(data).hexdigest()}), flush=True)
        return
    rows = [{'media_buy_id':'media-buy-redacted', 'campaign_id':'campaign-redacted-1',
             'impressions':10, 'spend':'1.25'}]
    now = datetime(2026,11,6,12,tzinfo=timezone.utc) + timedelta(minutes=int(sys.argv[4]))
    seals = InMemorySealStore()
    source = InlineReportingSource(capabilities=redacted_capabilities(), staging=staging,
        seals=seals, fetch=lambda _request: rows, clock=lambda: now)
    if mode == 'failed_write':
        # A real partial-file failure inside this owned POSIX process. No
        # application/global test-runner clock or filesystem calls are patched.
        import errno, resource, signal
        limits = resource.getrlimit(resource.RLIMIT_FSIZE)
        previous = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (1, limits[1]))
        try:
            await source.execute(request, cancel=asyncio.Event())
        except OSError as error:
            assert error.errno == errno.EFBIG
            sealed = await seals.get(account_id=request.identity.account_id,
                source_execution_key=request.identity.source_execution_key)
            files = [p for p in Path(root).rglob('*') if p.is_file()]
            print(json.dumps({'phase': 'failed_write', 'sealed': sealed is not None,
                              'retained_files': len(files)}), flush=True)
        else:
            raise AssertionError('partial write incorrectly returned a source result')
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, limits)
            signal.signal(signal.SIGXFSZ, previous)
        return
    result = await source.execute(request, cancel=asyncio.Event())
    manifest = await validate_reporting_source_execution(capabilities=source.capabilities,
        request=request, result=result, object_reader=staging)
    obj = manifest.objects[0]
    outcome = {'pair':[obj.object_ref,obj.object_generation],
        'publication':manifest.publication_id, 'observed':manifest.observed_at.isoformat(),
        'acquired':manifest.acquired_at.isoformat(),
        'manifest':hashlib.sha256(result.manifest_bytes).hexdigest()}
    print(json.dumps(outcome), flush=True)
asyncio.run(main())
"""


async def process(root: Path, mode: str, key: str, value: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _PROCESS,
        str(root),
        mode,
        key,
        value,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def completed_process(root: Path, mode: str, key: str, value: str) -> dict[str, Any]:
    child = await process(root, mode, key, value)
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 45)
        assert child.returncode == 0, stderr.decode()
        return json.loads(stdout)
    finally:
        await reap_process(child)


async def reap_process(child: asyncio.subprocess.Process) -> None:
    if child.returncode is None:
        try:
            child.kill()
        except ProcessLookupError:
            # The child may exit between the returncode check and kill().
            pass
    await asyncio.wait_for(child.communicate(), 5)


async def test_fresh_processes_reuse_payload_and_read_previous_immutable_pair(
    tmp_path: Path,
) -> None:
    first = await completed_process(tmp_path, "publish", "first-process", "0")
    second = await completed_process(tmp_path, "publish", "second-process", "1")
    assert first["pair"] == second["pair"]
    assert first["publication"] != second["publication"]
    assert first["manifest"] != second["manifest"]
    assert first["observed"] != second["observed"] and first["acquired"] != second["acquired"]
    assert len(list(tmp_path.rglob("*.bin"))) == 1
    reader = await completed_process(tmp_path, "read", "unused", json.dumps(first["pair"]))
    assert reader["generation"] == first["pair"][1]


@pytest.mark.parametrize("phase", ["interrupted", "linked"])
async def test_killed_writer_leaves_no_published_partial_and_restart_converges(
    tmp_path: Path, phase: str
) -> None:
    child = await process(tmp_path, phase, "interrupted-key", "0")
    try:
        assert child.stdout is not None
        event = json.loads(await asyncio.wait_for(child.stdout.readline(), 45))
        if phase == "interrupted":
            assert event == {
                "phase": "unpublished",
                "complete_temporary": True,
                "target_exists": False,
            }
            assert list(tmp_path.rglob("*.bin")) == []
        else:
            assert event == {"phase": "linked", "files": 1, "complete_final": True}
            assert len(list(tmp_path.rglob("*.bin"))) == 1
        child.kill()
        await asyncio.wait_for(child.communicate(), 5)
    finally:
        await reap_process(child)
    recovered = await completed_process(tmp_path, "publish", "interrupted-key", "1")
    assert len(list(tmp_path.rglob("*.bin"))) == 1
    reader = await completed_process(tmp_path, "read", "unused", json.dumps(recovered["pair"]))
    assert reader["generation"] == recovered["pair"][1]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file-size resource limit")
async def test_real_partial_write_failure_returns_no_seal_and_cleans_temporary(
    tmp_path: Path,
) -> None:
    failed = await completed_process(tmp_path, "failed_write", "failed-key", "0")
    assert failed == {"phase": "failed_write", "sealed": False, "retained_files": 0}
    recovered = await completed_process(tmp_path, "publish", "failed-key", "1")
    assert len(list(tmp_path.rglob("*.bin"))) == 1
    reader = await completed_process(tmp_path, "read", "unused", json.dumps(recovered["pair"]))
    assert reader["generation"] == recovered["pair"][1]
