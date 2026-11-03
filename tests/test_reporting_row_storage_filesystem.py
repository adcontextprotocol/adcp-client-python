"""The filesystem provider: create-only writes, exact-version reads and deletes, key safety."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from adcp_reporting_storage.ledger.row_storage_object import (
    REPORTING_ROW_DEFAULT_KEY_TEMPLATE,
    ReportingRowObjectContextV1,
    ReportingRowObjectDeleteInputV1,
    ReportingRowObjectGetInputV1,
    ReportingRowObjectListInputV1,
    ReportingRowObjectPutInputV1,
    ReportingRowObjectStatInputV1,
    assert_reporting_row_key_template_v1,
    assert_reporting_row_prefix_v1,
    create_filesystem_reporting_row_object_provider_v1,
    is_row_object_key_shape,
    parse_row_instant,
    render_reporting_row_object_key_v1,
    reporting_row_account_key_v1,
    run_reporting_row_object_provider_conformance_v1,
)

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError

CONTEXT = ReportingRowObjectContextV1()
NAMESPACE = "a" * 64


def provider_in(tmp_path: Path):
    return create_filesystem_reporting_row_object_provider_v1(roots={"rows": tmp_path / "rows"})


async def test_the_provider_passes_the_shared_conformance_suite(tmp_path: Path) -> None:
    passed = await run_reporting_row_object_provider_conformance_v1(
        provider_in(tmp_path), location={"root": "rows"}, prefix="adcp-rows"
    )
    assert passed == [
        "probe",
        "create",
        "create-only",
        "ranged-read",
        "version-pinned-read",
        "version-pinned-stat",
        "exact-version-delete",
        "write-unique-version",
        "list",
    ]


async def test_a_second_write_never_replaces_the_first(tmp_path: Path) -> None:
    provider = provider_in(tmp_path)
    location = {"root": "rows"}
    await provider.probe(location, "p", CONTEXT)
    put = ReportingRowObjectPutInputV1(
        location, "p/ns/a.jsonl", b"one\n", "application/x-ndjson", {}
    )
    first = await provider.put_if_absent(put, CONTEXT)
    second = await provider.put_if_absent(
        ReportingRowObjectPutInputV1(
            location, "p/ns/a.jsonl", b"two\n", "application/x-ndjson", {}
        ),
        CONTEXT,
    )
    assert first.created and not second.created
    assert second.native_version_ref == first.native_version_ref
    read = ReportingRowObjectGetInputV1(location, "p/ns/a.jsonl", first.native_version_ref, 100)
    assert await provider.get(read, CONTEXT) == b"one\n"
    # No temporary files are left behind by the losing write.
    assert sorted(os.listdir(tmp_path / "rows" / "p" / "ns")) == ["a.jsonl"]


async def test_reads_and_deletes_are_pinned_to_the_exact_version(tmp_path: Path) -> None:
    provider = provider_in(tmp_path)
    location = {"root": "rows"}
    await provider.probe(location, "p", CONTEXT)
    created = await provider.put_if_absent(
        ReportingRowObjectPutInputV1(
            location, "p/ns/b.jsonl", b"bytes\n", "application/x-ndjson", {}
        ),
        CONTEXT,
    )
    stale = created.native_version_ref.replace(":", ":9", 1)
    stale_read = await provider.get(
        ReportingRowObjectGetInputV1(location, "p/ns/b.jsonl", stale, 100), CONTEXT
    )
    assert stale_read is None
    stale_delete = await provider.delete(
        ReportingRowObjectDeleteInputV1(location, "p/ns/b.jsonl", stale), CONTEXT
    )
    assert stale_delete == "absent"
    inode, birth, mtime, size = created.native_version_ref.split(":")
    assert int(size) == len(b"bytes\n") and int(inode) > 0 and int(mtime) > 0


async def test_the_native_version_uses_the_format_the_javascript_provider_records(
    tmp_path: Path,
) -> None:
    provider = provider_in(tmp_path)
    location = {"root": "rows"}
    await provider.probe(location, "p", CONTEXT)
    created = await provider.put_if_absent(
        ReportingRowObjectPutInputV1(location, "p/ns/c.jsonl", b"x\n", "application/x-ndjson", {}),
        CONTEXT,
    )
    stat = os.stat(tmp_path / "rows" / "p" / "ns" / "c.jsonl")
    inode, birth, mtime, size = created.native_version_ref.split(":")
    assert (int(inode), int(mtime), int(size)) == (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    # Birth time comes from statx, which Node reports as birthtimeNs.
    assert int(birth) > 0


async def test_ranged_reads_are_bounded(tmp_path: Path) -> None:
    provider = provider_in(tmp_path)
    location = {"root": "rows"}
    await provider.probe(location, "p", CONTEXT)
    created = await provider.put_if_absent(
        ReportingRowObjectPutInputV1(
            location, "p/ns/d.jsonl", b"0123456789", "application/x-ndjson", {}
        ),
        CONTEXT,
    )

    def read(**kwargs):
        return provider.get(
            ReportingRowObjectGetInputV1(
                location, "p/ns/d.jsonl", created.native_version_ref, **kwargs
            ),
            CONTEXT,
        )

    assert await read(max_bytes=4, byte_range=(2, 4)) == b"2345"
    with pytest.raises(ReportingRowStoreError) as past_end:
        await read(max_bytes=100, byte_range=(8, 5))
    assert past_end.value.code == "ROWS_INTEGRITY_FAILED"
    with pytest.raises(ReportingRowStoreError) as oversized:
        await read(max_bytes=3)
    assert oversized.value.code == "ROWS_INTEGRITY_FAILED"


def test_locations_name_exactly_one_configured_root(tmp_path: Path) -> None:
    provider = provider_in(tmp_path)
    provider.validate_location({"root": "rows"})
    for bad in ({}, {"root": "elsewhere"}, {"root": "rows", "path": "/etc"}, {"path": "/etc"}):
        with pytest.raises(ReportingRowStoreError) as failure:
            provider.validate_location(bad)
        assert failure.value.code == "INVALID_INPUT"
    for roots in ({"bad name": "/tmp/x"}, {"rows": "relative/dir"}):
        with pytest.raises(ReportingRowStoreError):
            create_filesystem_reporting_row_object_provider_v1(roots=roots)


async def test_a_key_cannot_escape_the_root(tmp_path: Path) -> None:
    provider = provider_in(tmp_path)
    with pytest.raises(ReportingRowStoreError) as failure:
        await provider.put_if_absent(
            ReportingRowObjectPutInputV1(
                {"root": "rows"}, "../escape.jsonl", b"x", "application/x-ndjson", {}
            ),
            CONTEXT,
        )
    assert failure.value.code == "INVALID_INPUT"


def test_rendered_keys_are_hashed_confined_and_stable() -> None:
    key = render_reporting_row_object_key_v1(
        template=REPORTING_ROW_DEFAULT_KEY_TEMPLATE,
        prefix="adcp-rows",
        namespace_key=NAMESPACE,
        account_id="acct_secret_name",
        row_set_id="rpr_1",
        period_start="2026-10-01T00:00:00Z",
        source_timezone="America/Los_Angeles",
        finality="snapshot",
        content_sha256="b" * 64,
        chunk_index=3,
        compression="gzip",
    )
    parts = key.split("/")
    assert parts[:2] == ["adcp-rows", NAMESPACE]
    assert parts[2] == reporting_row_account_key_v1(NAMESPACE, "acct_secret_name")
    assert "acct_secret_name" not in key
    # AdCP's YYYY-MM-DD, one segment, in the period's own timezone: still the 30th in Los Angeles.
    assert parts[3] == "2026-09-30" and parts[4] == "snapshot"
    assert parts[-1] == "000003.jsonl.gz"
    undated = render_reporting_row_object_key_v1(
        template=REPORTING_ROW_DEFAULT_KEY_TEMPLATE,
        prefix="adcp-rows",
        namespace_key=NAMESPACE,
        account_id="a",
        row_set_id="rpr_2",
        period_start=None,
        finality=None,
        content_sha256="c" * 64,
        chunk_index=0,
        compression="none",
    )
    assert "/undated/adjustment/" in undated and undated.endswith("000000.jsonl")


@pytest.mark.parametrize(
    "template",
    [
        "{namespace_key}/{content_sha256}/{chunk_index}",
        "{prefix}/{namespace_key}/{chunk_index}",
        "{prefix}/{namespace_key}/{content_sha256}",
        "{prefix}/{namespace_key}/{content_sha256}/{chunk_index}/{unknown}",
        "{prefix}/{namespace_key}/../{content_sha256}/{chunk_index}",
        "{prefix}/{namespace_key}/a b/{content_sha256}/{chunk_index}",
    ],
)
def test_unsafe_key_templates_are_refused(template: str) -> None:
    with pytest.raises(ReportingRowStoreError) as failure:
        assert_reporting_row_key_template_v1(template)
    assert failure.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("prefix", ["", "/abs", "a/../b", "a//b", "a b", "x" * 600])
def test_unsafe_prefixes_are_refused(prefix: str) -> None:
    with pytest.raises(ReportingRowStoreError):
        assert_reporting_row_prefix_v1(prefix)


# -- every writable template includes {revision_key} (mirrors adcp-client #3226) --------------

OLD_TEMPLATE = "{prefix}/{namespace_key}/{account_key}/{content_sha256}/{chunk_index}"
NEW_TEMPLATE = "{prefix}/{namespace_key}/{revision_key}/{content_sha256}/{chunk_index}"


def object_options(tmp_path: Path, **binding: object):
    from adcp_reporting_storage.ledger.row_storage import (
        ReportingObjectRowBindingV1,
        ReportingRowStorageOptionsV1,
    )

    return ReportingRowStorageOptionsV1(
        deployment_namespace="test",
        bindings={
            "objects": ReportingObjectRowBindingV1(
                provider="filesystem",
                location={"root": "rows"},
                prefix="adcp-rows",
                **binding,  # type: ignore[arg-type]
            )
        },
        providers={"filesystem": provider_in(tmp_path)},
    )


def test_a_writable_template_without_revision_key_is_refused_at_configuration(
    tmp_path: Path,
) -> None:
    from adcp_reporting_storage.ledger.row_storage import ReportingRowStorageV1

    with pytest.raises(ReportingRowStoreError) as refused:
        ReportingRowStorageV1(object_options(tmp_path, key_template=OLD_TEMPLATE))
    assert refused.value.code == "INVALID_INPUT"
    message = refused.value.detail or ""
    assert message.startswith("row binding objects: ")
    # Actionable: names the placeholder, the identity trap and the read-only remedy.
    assert (
        "{revision_key}" in message and "NEW binding ID" in message and "read_only=True" in message
    )


def test_the_default_and_an_explicit_template_with_revision_key_are_accepted(
    tmp_path: Path,
) -> None:
    from adcp_reporting_storage.ledger.row_storage import ReportingRowStorageV1

    ReportingRowStorageV1(object_options(tmp_path))
    ReportingRowStorageV1(object_options(tmp_path, key_template=NEW_TEMPLATE))
    assert "{revision_key}" in REPORTING_ROW_DEFAULT_KEY_TEMPLATE


def test_a_read_only_binding_is_exempt_but_its_template_is_otherwise_validated(
    tmp_path: Path,
) -> None:
    from adcp_reporting_storage.ledger.row_storage import ReportingRowStorageV1

    ReportingRowStorageV1(object_options(tmp_path, key_template=OLD_TEMPLATE, read_only=True))
    for template in (
        "{prefix}/{namespace_key}/{account_key}/{chunk_index}",  # no {content_sha256}
        "{namespace_key}/{content_sha256}/{chunk_index}",  # no prefix
    ):
        with pytest.raises(ReportingRowStoreError) as refused:
            ReportingRowStorageV1(object_options(tmp_path, key_template=template, read_only=True))
        assert refused.value.code == "INVALID_INPUT"


def test_read_only_is_not_part_of_the_binding_identity(tmp_path: Path) -> None:
    from adcp_reporting_storage.ledger.row_storage import (
        ReportingObjectRowBindingV1,
        _binding_identity_sha256,
    )

    def binding(read_only: bool) -> ReportingObjectRowBindingV1:
        return ReportingObjectRowBindingV1(
            provider="filesystem",
            location={"root": "rows"},
            prefix="adcp-rows",
            key_template=OLD_TEMPLATE,
            read_only=read_only,
        )

    # A recorded row set keeps matching its binding when the definition becomes read-only.
    assert _binding_identity_sha256(binding(False)) == _binding_identity_sha256(binding(True))


def test_a_template_with_revision_key_gives_identical_rows_distinct_keys() -> None:
    def key(row_set_id: str) -> str:
        return render_reporting_row_object_key_v1(
            template=REPORTING_ROW_DEFAULT_KEY_TEMPLATE,
            prefix="adcp-rows",
            namespace_key=NAMESPACE,
            account_id="acct",
            row_set_id=row_set_id,
            period_start=None,
            finality="snapshot",
            content_sha256="c" * 64,
            chunk_index=0,
            compression="gzip",
        )

    assert key("rrev_1") != key("rrev_2")


# -- list (the orphan collector's capability) -------------------------------------------------

LIST_NAMESPACE = f"adcp-rows/{NAMESPACE}"


async def _put_all(provider: Any, keys: list[str]) -> dict[str, str]:
    versions = {}
    for key in keys:
        created = await provider.put_if_absent(
            ReportingRowObjectPutInputV1(
                {"root": "rows"}, key, f"{key}\n".encode(), "application/x-ndjson", {}
            ),
            CONTEXT,
        )
        versions[key] = created.native_version_ref
    return versions


async def _list_all(provider: Any, prefix: str, page_size: int) -> list[Any]:
    found: list[Any] = []
    token = None
    for _ in range(200):
        page = await provider.list(
            ReportingRowObjectListInputV1({"root": "rows"}, prefix, page_size, token), CONTEXT
        )
        assert len(page.objects) <= page_size
        found.extend(page.objects)
        token = page.next_page_token
        if token is None:
            return found
    raise AssertionError("listing did not finish")


async def test_list_returns_every_object_under_the_prefix_once_in_a_stable_order(
    tmp_path: Path,
) -> None:
    provider = provider_in(tmp_path)
    keys = [
        f"{LIST_NAMESPACE}/a/1.jsonl",
        f"{LIST_NAMESPACE}/a/2.jsonl.gz",
        f"{LIST_NAMESPACE}/a/b/3.jsonl",
        f"{LIST_NAMESPACE}/a.jsonl",
        f"{LIST_NAMESPACE}/b.jsonl",
        f"{LIST_NAMESPACE}/z/9.jsonl",
    ]
    await _put_all(provider, keys)
    expected = [entry.object_ref for entry in await _list_all(provider, f"{LIST_NAMESPACE}/", 100)]
    assert sorted(expected) == sorted(keys) and len(set(expected)) == len(keys)
    for page_size in (1, 2, 3, 5, 100):
        seen = [e.object_ref for e in await _list_all(provider, f"{LIST_NAMESPACE}/", page_size)]
        assert seen == expected, page_size
    assert [e.object_ref for e in await _list_all(provider, f"{LIST_NAMESPACE}/a/", 2)] == [
        f"{LIST_NAMESPACE}/a/1.jsonl",
        f"{LIST_NAMESPACE}/a/2.jsonl.gz",
        f"{LIST_NAMESPACE}/a/b/3.jsonl",
    ]
    assert await _list_all(provider, f"{LIST_NAMESPACE}/zz", 5) == []


async def test_list_reports_the_version_size_and_write_time_the_other_operations_use(
    tmp_path: Path,
) -> None:
    provider = provider_in(tmp_path)
    key = f"{LIST_NAMESPACE}/acct/x.jsonl"
    created = await provider.put_if_absent(
        ReportingRowObjectPutInputV1({"root": "rows"}, key, b"0123456789", "application/gzip", {}),
        CONTEXT,
    )
    (entry,) = await _list_all(provider, f"{LIST_NAMESPACE}/", 10)
    assert entry.native_version_ref == created.native_version_ref
    stat = await provider.stat(
        ReportingRowObjectStatInputV1({"root": "rows"}, key, created.native_version_ref), CONTEXT
    )
    assert stat is not None and entry.size_bytes == stat.size_bytes == 10
    written = parse_row_instant(entry.created_at)
    assert written is not None and abs((written - datetime.now(timezone.utc)).total_seconds()) < 60
    assert entry.created_at.endswith("Z")


async def test_list_never_returns_a_temporary_file_or_a_key_outside_the_prefix(
    tmp_path: Path,
) -> None:
    provider = provider_in(tmp_path)
    await _put_all(provider, [f"{LIST_NAMESPACE}/ok.jsonl", "other/prefix.jsonl"])
    leftover = tmp_path / "rows" / LIST_NAMESPACE / "half-written.jsonl.123.tmp"
    leftover.write_bytes(b"x")
    keys = [e.object_ref for e in await _list_all(provider, f"{LIST_NAMESPACE}/", 10)]
    assert keys == [f"{LIST_NAMESPACE}/ok.jsonl"]


async def test_list_resumes_after_the_object_a_previous_page_ended_on_was_deleted(
    tmp_path: Path,
) -> None:
    provider = provider_in(tmp_path)
    keys = [f"{LIST_NAMESPACE}/o{index}.jsonl" for index in range(5)]
    versions = await _put_all(provider, keys)
    first = await provider.list(
        ReportingRowObjectListInputV1({"root": "rows"}, f"{LIST_NAMESPACE}/", 2), CONTEXT
    )
    assert first.next_page_token == keys[1]
    for key in keys[:2]:
        await provider.delete(
            ReportingRowObjectDeleteInputV1({"root": "rows"}, key, versions[key]), CONTEXT
        )
    rest = await _list_all_from(provider, f"{LIST_NAMESPACE}/", first.next_page_token)
    assert rest == keys[2:]


async def _list_all_from(provider: Any, prefix: str, token: str | None) -> list[str]:
    found: list[str] = []
    while True:
        page = await provider.list(
            ReportingRowObjectListInputV1({"root": "rows"}, prefix, 2, token), CONTEXT
        )
        found.extend(entry.object_ref for entry in page.objects)
        token = page.next_page_token
        if token is None:
            return found


async def test_list_refuses_a_bad_page_size_an_unknown_root_and_a_prefix_that_climbs_out(
    tmp_path: Path,
) -> None:
    provider = provider_in(tmp_path)
    for bad in (0, -1, 1.5, "5"):
        with pytest.raises(ReportingRowStoreError) as failure:
            await provider.list(
                ReportingRowObjectListInputV1({"root": "rows"}, "adcp-rows/", bad), CONTEXT  # type: ignore[arg-type]
            )
        assert failure.value.code == "INVALID_INPUT"
    with pytest.raises(ReportingRowStoreError) as unknown:
        await provider.list(
            ReportingRowObjectListInputV1({"root": "nope"}, "adcp-rows/", 5), CONTEXT
        )
    assert unknown.value.code == "UNSAFE_BINDING"
    with pytest.raises(ReportingRowStoreError) as climbing:
        await provider.list(
            ReportingRowObjectListInputV1({"root": "rows"}, "adcp-rows/../x/", 5), CONTEXT
        )
    assert climbing.value.code == "INVALID_INPUT"


class _BrokenLister:
    """A filesystem provider whose ``list`` breaks one rule the collector relies on."""

    name = "filesystem"
    ranged_reads = True

    def __init__(self, inner: Any, defect: str) -> None:
        self._inner, self._defect = inner, defect
        for method in ("validate_location", "probe", "put_if_absent", "get", "stat", "delete"):
            setattr(self, method, getattr(inner, method))

    async def list(self, request: Any, context: Any) -> Any:
        page = await self._inner.list(request, context)
        objects = list(page.objects)
        if self._defect == "duplicate" and objects:
            objects.append(objects[0])
        elif self._defect == "wrong_version" and objects:
            objects[0] = replace(objects[0], native_version_ref="9:9:9:9")
        elif self._defect == "wrong_size" and objects:
            objects[0] = replace(objects[0], size_bytes=objects[0].size_bytes + 1)
        elif self._defect == "no_time" and objects:
            objects[0] = replace(objects[0], created_at="2000-01-01T00:00:00.000Z")
        elif self._defect == "ignores_prefix":
            return await self._inner.list(replace(request, prefix=request.prefix[:-1]), context)
        elif self._defect == "too_many" and objects:
            objects = objects * (request.page_size + 1)
        elif self._defect == "repeat_token":
            return replace(page, next_page_token="same")
        elif self._defect == "drops_last" and len(objects) > 1:
            objects.pop()
        return replace(page, objects=tuple(objects))


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate",
        "wrong_version",
        "wrong_size",
        "no_time",
        "too_many",
        "repeat_token",
        "drops_last",
    ],
)
async def test_the_conformance_runner_rejects_a_list_that_breaks_the_collectors_contract(
    tmp_path: Path, defect: str
) -> None:
    provider = _BrokenLister(provider_in(tmp_path), defect)
    with pytest.raises(AssertionError):
        await run_reporting_row_object_provider_conformance_v1(
            provider, location={"root": "rows"}, prefix="adcp-rows"  # type: ignore[arg-type]
        )


async def test_a_provider_without_list_still_passes_conformance_and_reports_no_list_check(
    tmp_path: Path,
) -> None:
    inner = provider_in(tmp_path)

    class WithoutList:
        name = "filesystem"
        ranged_reads = True

        def __init__(self) -> None:
            for method in ("validate_location", "probe", "put_if_absent", "get", "stat", "delete"):
                setattr(self, method, getattr(inner, method))

    passed = await run_reporting_row_object_provider_conformance_v1(
        WithoutList(), location={"root": "rows"}, prefix="adcp-rows"  # type: ignore[arg-type]
    )
    assert "list" not in passed and passed[-1] == "write-unique-version"


def test_strict_instants_and_key_shapes() -> None:
    assert parse_row_instant("2026-10-01T00:00:00Z") is not None
    assert parse_row_instant("2026-10-01T00:00:00.123456789Z") is not None
    for bad in (
        "2026-10-01",
        "2026-10-01T00:00:00",
        "2026-10-01T00:00:00+00:00",
        "2026-10-01 00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "",
        None,
        7,
    ):
        assert parse_row_instant(bad) is None, bad
    assert is_row_object_key_shape("adcp-rows/ns/acct/0001.jsonl.gz")
    for bad_key in ("", "a//b", "a/./b", "a/../b", "a b", "a\n", "/a", "a/", "x" * 1025):
        assert not is_row_object_key_shape(bad_key), bad_key


async def test_list_does_not_follow_symlinks_out_of_the_root(tmp_path: Path) -> None:
    provider = provider_in(tmp_path)
    await _put_all(provider, [f"{LIST_NAMESPACE}/real.jsonl"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.jsonl").write_bytes(b"x")
    owned = tmp_path / "rows" / LIST_NAMESPACE
    (owned / "linked-dir").symlink_to(outside, target_is_directory=True)
    (owned / "linked-file.jsonl").symlink_to(outside / "secret.jsonl")
    keys = [e.object_ref for e in await _list_all(provider, f"{LIST_NAMESPACE}/", 10)]
    assert keys == [f"{LIST_NAMESPACE}/real.jsonl"]
