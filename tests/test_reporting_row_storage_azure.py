"""The Azure Blob row-object provider against an in-memory fake of the official client surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

core_exceptions = pytest.importorskip("azure.core.exceptions")
pytest.importorskip("azure.storage.blob")

from adcp.reporting.ledger.row_storage_azure import (  # noqa: E402
    create_azure_blob_reporting_row_object_provider_v1,
)
from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError  # noqa: E402
from adcp.reporting.ledger.row_storage_object import (  # noqa: E402
    ReportingRowObjectContextV1,
    ReportingRowObjectDeleteInputV1,
    ReportingRowObjectGetInputV1,
    ReportingRowObjectPutInputV1,
    run_reporting_row_object_provider_conformance_v1,
)

CONTEXT = ReportingRowObjectContextV1()
LOCATION = {"account": "rowsaccount", "container": "rows"}


def rest_error(status: int, code: str | None = None) -> Exception:
    error = core_exceptions.HttpResponseError(message=f"{status} secret-account-key")
    error.status_code = status
    error.error_code = code
    return error


@dataclass
class _Version:
    version_id: str
    data: bytes
    metadata: dict[str, str]
    content_type: str | None
    cache_control: str | None
    current: bool = True

    @property
    def etag(self) -> str:
        return f'"etag-{self.version_id}"'


@dataclass
class FakeBlobService:
    account_name: str = "rowsaccount"
    versioning: bool = True
    ignore_if_none_match: bool = False
    public_access: str | None = None
    containers: set[str] = field(default_factory=lambda: {"rows"})
    blobs: dict[tuple[str, str], list[_Version]] = field(default_factory=dict)
    tick: int = 0
    transport: list[dict[str, Any]] = field(default_factory=list)

    def get_container_client(self, name: str) -> Any:
        service = self

        class Container:
            def get_container_properties(self, **transport: Any) -> Any:
                service.transport.append(transport)
                if name not in service.containers:
                    raise rest_error(404, "ContainerNotFound")
                return SimpleNamespace(public_access=service.public_access)

            def get_blob_client(self, key: str) -> Any:
                return Blob(name, key)

        class Blob:
            def __init__(self, container: str, key: str) -> None:
                self.container, self.key = container, key

            def _versions(self) -> list[_Version]:
                return service.blobs.setdefault((self.container, self.key), [])

            def _select(self, version_id: str | None) -> _Version:
                versions = self._versions()
                if version_id is not None:
                    if not service.versioning:
                        # Without versioning the parameter is ignored: the current blob answers.
                        current = [v for v in versions if v.current]
                        if not current:
                            raise rest_error(404, "BlobNotFound")
                        return current[-1]
                    found = next((v for v in versions if v.version_id == version_id), None)
                    if found is None:
                        raise rest_error(404, "BlobNotFound")
                    return found
                current = [v for v in versions if v.current]
                if not current:
                    raise rest_error(404, "BlobNotFound")
                return current[-1]

            def upload_blob(self, data: bytes, **options: Any) -> dict[str, Any]:
                service.transport.append(options)
                versions = self._versions()
                if (
                    options.get("overwrite") is False
                    and any(v.current for v in versions)
                    and not service.ignore_if_none_match
                ):
                    raise (
                        core_exceptions.ResourceExistsError(message="exists")
                        if False
                        else rest_error(409, "BlobAlreadyExists")
                    )
                for existing in versions:
                    existing.current = False
                service.tick += 1
                settings = options.get("content_settings")
                version = _Version(
                    f"2026-10-09T12:00:{service.tick:02d}.0000000Z",
                    bytes(data),
                    dict(options.get("metadata") or {}),
                    getattr(settings, "content_type", None),
                    getattr(settings, "cache_control", None),
                )
                versions.append(version)
                result: dict[str, Any] = {"etag": version.etag}
                if service.versioning:
                    result["version_id"] = version.version_id
                return result

            def get_blob_properties(self, version_id: str | None = None, **transport: Any) -> Any:
                service.transport.append(transport)
                version = self._select(version_id)
                return SimpleNamespace(
                    version_id=version.version_id if service.versioning else None,
                    etag=version.etag,
                    size=len(version.data),
                )

            def download_blob(
                self,
                offset: int | None = None,
                length: int | None = None,
                version_id: str | None = None,
                **transport: Any,
            ) -> Any:
                service.transport.append(transport)
                version = self._select(version_id)
                start = offset or 0
                if start >= len(version.data) and len(version.data) > 0:
                    raise rest_error(416, "InvalidRange")
                data = version.data[start : None if length is None else start + length]
                return SimpleNamespace(
                    properties=SimpleNamespace(
                        version_id=version.version_id if service.versioning else None
                    ),
                    size=len(data),
                    readall=lambda: data,
                )

            def delete_blob(self, version_id: str | None = None, **options: Any) -> None:
                service.transport.append(options)
                versions = self._versions()
                if version_id is None:
                    version = self._select(None)
                    if options.get("etag") and options["etag"] != version.etag:
                        raise rest_error(412, "ConditionNotMet")
                    version.current = False
                    if not service.versioning:
                        versions.remove(version)
                    return
                version = self._select(version_id)
                versions.remove(version)

        return Container()


def put(key: str, data: bytes = b'{"a":1}\n', **extra: Any) -> ReportingRowObjectPutInputV1:
    return ReportingRowObjectPutInputV1(
        LOCATION,
        key,
        data,
        "application/gzip",
        {"adcp-installation": "inst", "adcp-intent": "intent"},
        **extra,
    )


async def test_every_sdk_call_carries_the_call_budget() -> None:
    """``wait_for`` cannot stop a worker thread, so the budget must reach the transport."""
    service = FakeBlobService()
    provider = create_azure_blob_reporting_row_object_provider_v1(client=service)
    context = ReportingRowObjectContextV1(7.5)
    await provider.probe(LOCATION, "adcp-rows", context)
    created = await provider.put_if_absent(put("adcp-rows/ns/b.jsonl"), context)
    await provider.get(
        ReportingRowObjectGetInputV1(
            LOCATION, "adcp-rows/ns/b.jsonl", created.native_version_ref, 100
        ),
        context,
    )
    await provider.delete(
        ReportingRowObjectDeleteInputV1(
            LOCATION, "adcp-rows/ns/b.jsonl", created.native_version_ref
        ),
        context,
    )
    assert service.transport
    for transport in service.transport:
        assert transport["timeout"] == 8
        assert transport["connection_timeout"] == 7.5 and transport["read_timeout"] == 7.5


async def test_passes_the_shared_provider_conformance_suite() -> None:
    provider = create_azure_blob_reporting_row_object_provider_v1(client=FakeBlobService())
    passed = await run_reporting_row_object_provider_conformance_v1(
        provider, location=LOCATION, prefix="adcp-rows"
    )
    assert passed[-1] == "write-unique-version" and "ranged-read" in passed


async def test_create_only_upload_sets_headers_and_identifier_safe_metadata() -> None:
    service = FakeBlobService()
    provider = create_azure_blob_reporting_row_object_provider_v1(client=service)
    created = await provider.put_if_absent(put("adcp-rows/ns/a.jsonl.gz"), CONTEXT)
    assert created.created and created.native_version_ref.startswith("2026-")
    (version,) = service.blobs[("rows", "adcp-rows/ns/a.jsonl.gz")]
    assert version.content_type == "application/gzip" and version.cache_control == "no-store"
    assert version.metadata == {"adcp_installation": "inst", "adcp_intent": "intent"}


async def test_an_existing_blob_is_adopted_not_replaced() -> None:
    provider = create_azure_blob_reporting_row_object_provider_v1(client=FakeBlobService())
    key = "adcp-rows/ns/a.jsonl"
    first = await provider.put_if_absent(put(key, b"first\n"), CONTEXT)
    second = await provider.put_if_absent(put(key, b"second\n"), CONTEXT)
    assert (second.created, second.native_version_ref) == (False, first.native_version_ref)
    read = ReportingRowObjectGetInputV1(LOCATION, key, first.native_version_ref, 100)
    assert await provider.get(read, CONTEXT) == b"first\n"


async def test_a_store_without_versioning_is_refused_on_write_and_adoption() -> None:
    service = FakeBlobService(versioning=False)
    provider = create_azure_blob_reporting_row_object_provider_v1(client=service)
    with pytest.raises(ReportingRowStoreError) as unversioned:
        await provider.put_if_absent(put("adcp-rows/ns/a.jsonl"), CONTEXT)
    assert unversioned.value.code == "UNSAFE_BINDING" and "versioning" in str(unversioned.value)
    with pytest.raises(ReportingRowStoreError) as adoption:
        await provider.put_if_absent(put("adcp-rows/ns/a.jsonl"), CONTEXT)
    assert adoption.value.code == "UNSAFE_BINDING"


async def test_reads_pin_the_version_and_honour_ranges_and_the_cap() -> None:
    provider = create_azure_blob_reporting_row_object_provider_v1(client=FakeBlobService())
    created = await provider.put_if_absent(
        put("adcp-rows/ns/r.jsonl", b"0123456789abcdef"), CONTEXT
    )

    def read(**kwargs: Any) -> Any:
        return provider.get(
            ReportingRowObjectGetInputV1(
                LOCATION, "adcp-rows/ns/r.jsonl", created.native_version_ref, **kwargs
            ),
            CONTEXT,
        )

    assert await read(max_bytes=6, byte_range=(4, 6)) == b"456789"
    assert await read(max_bytes=100) == b"0123456789abcdef"
    assert await read(max_bytes=0, byte_range=(2, 0)) == b""
    with pytest.raises(ReportingRowStoreError) as capped:
        await read(max_bytes=4)
    assert capped.value.code == "ROWS_INTEGRITY_FAILED"
    with pytest.raises(ReportingRowStoreError) as past:
        await read(max_bytes=5, byte_range=(99, 5))
    assert past.value.code == "ROWS_INTEGRITY_FAILED"
    outcome = await provider.get(
        ReportingRowObjectGetInputV1(LOCATION, "adcp-rows/ns/r.jsonl", "bad version!", 100),
        CONTEXT,
    )
    assert outcome is None


async def test_a_store_that_ignores_the_version_never_serves_or_deletes_another_version() -> None:
    service = FakeBlobService()
    provider = create_azure_blob_reporting_row_object_provider_v1(client=service)
    key = "adcp-rows/ns/v.jsonl"
    first = await provider.put_if_absent(put(key, b"one\n"), CONTEXT)
    service.versioning = False
    ghost = ReportingRowObjectGetInputV1(LOCATION, key, first.native_version_ref, 100)
    assert await provider.get(ghost, CONTEXT) is None
    outcome = await provider.delete(
        ReportingRowObjectDeleteInputV1(LOCATION, key, first.native_version_ref), CONTEXT
    )
    assert outcome == "absent"
    assert any(version.data == b"one\n" for version in service.blobs[("rows", key)])


async def test_a_delayed_delete_never_removes_an_identical_recreation() -> None:
    service = FakeBlobService()
    provider = create_azure_blob_reporting_row_object_provider_v1(client=service)
    key = "adcp-rows/ns/w.jsonl"
    first = await provider.put_if_absent(put(key, b"same\n"), CONTEXT)
    delete_first = ReportingRowObjectDeleteInputV1(LOCATION, key, first.native_version_ref)
    assert await provider.delete(delete_first, CONTEXT) == "deleted"
    second = await provider.put_if_absent(put(key, b"same\n"), CONTEXT)
    assert second.native_version_ref != first.native_version_ref
    assert await provider.delete(delete_first, CONTEXT) == "absent"
    survivor = ReportingRowObjectGetInputV1(LOCATION, key, second.native_version_ref, 100)
    assert await provider.get(survivor, CONTEXT) == b"same\n"
    outcome = await provider.delete(
        ReportingRowObjectDeleteInputV1(LOCATION, key, second.native_version_ref), CONTEXT
    )
    assert outcome == "deleted"
    assert service.blobs[("rows", key)] == []


async def test_the_probe_refuses_missing_public_unversioned_and_non_create_only_containers() -> (
    None
):
    async def probe(service: FakeBlobService) -> ReportingRowStoreError:
        provider = create_azure_blob_reporting_row_object_provider_v1(client=service)
        with pytest.raises(ReportingRowStoreError) as failure:
            await provider.probe(LOCATION, "adcp-rows", CONTEXT)
        return failure.value

    assert (await probe(FakeBlobService(containers=set()))).code == "UNSAFE_BINDING"
    public = await probe(FakeBlobService(public_access="blob"))
    assert public.code == "UNSAFE_BINDING" and "anonymous" in str(public)
    unversioned = await probe(FakeBlobService(versioning=False))
    assert unversioned.code == "UNSAFE_BINDING" and "versioning" in str(unversioned)
    replacing = await probe(FakeBlobService(ignore_if_none_match=True))
    assert replacing.code == "UNSAFE_BINDING" and "replaces" in str(replacing)
    good = FakeBlobService()
    await create_azure_blob_reporting_row_object_provider_v1(client=good).probe(
        LOCATION, "adcp-rows", CONTEXT
    )
    # The probe leaves no live blob and no versions behind.
    assert all(not versions for versions in good.blobs.values())


async def test_a_client_for_another_account_is_refused() -> None:
    provider = create_azure_blob_reporting_row_object_provider_v1(
        client=FakeBlobService(account_name="otheraccount")
    )
    with pytest.raises(ReportingRowStoreError) as failure:
        await provider.put_if_absent(put("adcp-rows/ns/a.jsonl"), CONTEXT)
    assert failure.value.code == "UNSAFE_BINDING"


async def test_credential_refs_select_from_a_closed_client_map() -> None:
    default, tenant = FakeBlobService(), FakeBlobService()
    provider = create_azure_blob_reporting_row_object_provider_v1(
        client=default, clients={"tenant": tenant}
    )
    await provider.put_if_absent(put("adcp-rows/ns/a.jsonl", credential_ref="tenant"), CONTEXT)
    assert tenant.blobs and not default.blobs
    with pytest.raises(ReportingRowStoreError) as unknown:
        await provider.put_if_absent(
            put("adcp-rows/ns/b.jsonl", credential_ref="stranger"), CONTEXT
        )
    assert unknown.value.code == "UNSAFE_BINDING" and "stranger" not in str(unknown.value)


def test_locations_are_closed_and_options_validated() -> None:
    provider = create_azure_blob_reporting_row_object_provider_v1(client=FakeBlobService())
    provider.validate_location(LOCATION)
    for bad in (
        {},
        {"account": "rowsaccount"},
        {"account": "Bad_Account", "container": "rows"},
        {"account": "rowsaccount", "container": "Bad"},
        {"account": "rowsaccount", "container": "rows", "endpoint": "https://evil.example"},
    ):
        with pytest.raises(ReportingRowStoreError) as failure:
            provider.validate_location(bad)
        assert failure.value.code == "INVALID_INPUT", bad
    for kwargs in ({}, {"client": object()}, {"clients": {"bad ref": FakeBlobService()}}):
        with pytest.raises(ReportingRowStoreError):
            create_azure_blob_reporting_row_object_provider_v1(**kwargs)


def test_the_official_client_exposes_everything_the_provider_calls() -> None:
    """Guards the provider against drifting from azure-storage-blob's real surface."""
    import inspect

    blob = pytest.importorskip("azure.storage.blob")
    for method in ("upload_blob", "download_blob", "get_blob_properties", "delete_blob"):
        assert hasattr(blob.BlobClient, method)
    assert "version_id" in inspect.signature(blob.BlobClient.download_blob).parameters or (
        "kwargs" in str(inspect.signature(blob.BlobClient.download_blob))
    )
    assert hasattr(blob.BlobServiceClient, "get_container_client")
    assert hasattr(blob.ContainerClient, "get_container_properties")
    assert hasattr(blob.ContentSettings, "__init__")
    from azure.core import MatchConditions

    assert hasattr(MatchConditions, "IfNotModified")
