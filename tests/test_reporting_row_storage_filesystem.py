"""The filesystem provider: create-only writes, exact-version reads and deletes, key safety."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError
from adcp.reporting.ledger.row_storage_object import (
    REPORTING_ROW_DEFAULT_KEY_TEMPLATE,
    ReportingRowObjectContextV1,
    ReportingRowObjectDeleteInputV1,
    ReportingRowObjectGetInputV1,
    ReportingRowObjectPutInputV1,
    assert_reporting_row_key_template_v1,
    assert_reporting_row_prefix_v1,
    create_filesystem_reporting_row_object_provider_v1,
    render_reporting_row_object_key_v1,
    reporting_row_account_key_v1,
    run_reporting_row_object_provider_conformance_v1,
)

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
    from adcp.reporting.ledger.row_storage import (
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
    from adcp.reporting.ledger.row_storage import ReportingRowStorageV1

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
    from adcp.reporting.ledger.row_storage import ReportingRowStorageV1

    ReportingRowStorageV1(object_options(tmp_path))
    ReportingRowStorageV1(object_options(tmp_path, key_template=NEW_TEMPLATE))
    assert "{revision_key}" in REPORTING_ROW_DEFAULT_KEY_TEMPLATE


def test_a_read_only_binding_is_exempt_but_its_template_is_otherwise_validated(
    tmp_path: Path,
) -> None:
    from adcp.reporting.ledger.row_storage import ReportingRowStorageV1

    ReportingRowStorageV1(object_options(tmp_path, key_template=OLD_TEMPLATE, read_only=True))
    for template in (
        "{prefix}/{namespace_key}/{account_key}/{chunk_index}",  # no {content_sha256}
        "{namespace_key}/{content_sha256}/{chunk_index}",  # no prefix
    ):
        with pytest.raises(ReportingRowStoreError) as refused:
            ReportingRowStorageV1(object_options(tmp_path, key_template=template, read_only=True))
        assert refused.value.code == "INVALID_INPUT"


def test_read_only_is_not_part_of_the_binding_identity(tmp_path: Path) -> None:
    from adcp.reporting.ledger.row_storage import (
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
