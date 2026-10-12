"""Azure Blob Storage row-object provider (``adcp[reporting-azure]``).

Inject official ``azure.storage.blob.BlobServiceClient`` instances created by the host
with managed or workload identity. Bindings use ``location={"account": ..., "container":
...}``; the selected client must belong to ``account``. Endpoint and credentials come
only from the injected clients.

Writes are block-blob uploads with ``If-None-Match: *`` (``overwrite=False``). **Blob
versioning must be enabled on the storage account**: the recorded native version is the
write-unique ``version_id`` and pins every read and delete, so a delayed delete can
never remove a later write that re-created identical bytes at the same key. An ETag is
content-derived in practice and is never accepted as a version. The probe refuses
containers that allow anonymous read access and proves create-only behaviour
empirically.

The host-installed ``azure-storage-blob`` is imported only inside the calls that need
its helper types.
"""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError
from adcp.reporting.ledger.row_storage_object import (
    ReportingRowObjectContextV1,
    ReportingRowObjectDeleteInputV1,
    ReportingRowObjectGetInputV1,
    ReportingRowObjectProviderV1,
    ReportingRowObjectPutInputV1,
    ReportingRowObjectPutResultV1,
    ReportingRowObjectStatInputV1,
    ReportingRowObjectStatResultV1,
)
from adcp.reporting.ledger.row_storage_provider_io import (
    assert_location_keys,
    assert_read_bounds,
    client_map,
    require_clients,
    select_client,
)

__all__ = ["create_azure_blob_reporting_row_object_provider_v1"]

_ACCOUNT = re.compile(r"^[a-z0-9]{3,24}$")
_CONTAINER = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){2,62}$")
# Version IDs are RFC 3339 timestamps.
_VERSION_ID = re.compile(r"^[0-9A-Za-z:.+-]{1,64}$")


class _Budgeted:
    """Carries the call budget into every Azure SDK call made through a container or blob.

    ``asyncio.wait_for`` cancels the awaiter, not the worker thread, so the budget must reach
    the transport or a hung call would hold a thread past it. ``timeout`` is the service-side
    limit in whole seconds; ``connection_timeout`` and ``read_timeout`` bound the socket.
    """

    __slots__ = ("_target", "_seconds")

    def __init__(self, target: Any, seconds: float) -> None:
        self._target = target
        self._seconds = seconds

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name == "get_blob_client":
            return lambda *args, **kwargs: _Budgeted(attribute(*args, **kwargs), self._seconds)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("timeout", max(1, math.ceil(self._seconds)))
            kwargs.setdefault("connection_timeout", self._seconds)
            kwargs.setdefault("read_timeout", self._seconds)
            return attribute(*args, **kwargs)

        return call


def _status(error: BaseException) -> int | None:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _code(error: BaseException) -> str:
    value = getattr(error, "error_code", None)
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _is_conflict(error: BaseException) -> bool:
    return _status(error) in (409, 412) or _code(error) == "BlobAlreadyExists"


def _is_missing(error: BaseException) -> bool:
    return _status(error) in (404, 412)


def _forbidden(error: BaseException) -> bool:
    return _status(error) in (401, 403)


def _version_of(properties: Any) -> str:
    """The write-unique ``version_id``; an ETag is never a version."""
    value = (
        properties.get("version_id")
        if isinstance(properties, Mapping)
        else getattr(properties, "version_id", None)
    )
    if not isinstance(value, str) or not _VERSION_ID.match(value):
        raise ReportingRowStoreError(
            "UNSAFE_BINDING",
            "store returned no blob version_id; blob versioning must be enabled on the storage"
            " account so deletes are fenced to one write",
        )
    return value


def _served(properties: Any, version: str) -> bool:
    """Whether a response came from the pinned version (echoed ``version_id``)."""
    value = (
        properties.get("version_id")
        if isinstance(properties, Mapping)
        else getattr(properties, "version_id", None)
    )
    return bool(value == version)


def _metadata(metadata: Mapping[str, str]) -> dict[str, str]:
    """Blob metadata names must be C# identifiers; ``adcp-intent`` is stored as ``adcp_intent``."""
    result: dict[str, str] = {}
    for name, value in metadata.items():
        identifier = re.sub(r"[^A-Za-z0-9_]", "_", name)
        result[identifier if re.match(r"^[A-Za-z_]", identifier) else f"_{identifier}"] = value
    return result


def _pinned(native_version_ref: str) -> str | None:
    if isinstance(native_version_ref, str) and _VERSION_ID.match(native_version_ref):
        return native_version_ref
    return None


class _AzureProvider:
    name = "azure_blob"
    ranged_reads = True

    def __init__(self, client: Any, clients: dict[str, Any]) -> None:
        self._client = client
        self._clients = clients

    def validate_location(self, location: Mapping[str, str]) -> None:
        assert_location_keys(location, ("account", "container"))
        if not _ACCOUNT.match(location["account"]):
            raise ReportingRowStoreError("INVALID_INPUT", "Azure storage account name is invalid")
        if not _CONTAINER.match(location["container"]):
            raise ReportingRowStoreError("INVALID_INPUT", "Azure container name is invalid")

    def _container(self, location: Mapping[str, str], credential_ref: str | None) -> Any:
        self.validate_location(location)
        client = select_client(self._client, self._clients, credential_ref)
        if client.account_name != location["account"]:
            raise ReportingRowStoreError(
                "UNSAFE_BINDING", "client does not belong to the binding account"
            )
        return client.get_container_client(location["container"])

    def _blob(self, location: Mapping[str, str], credential_ref: str | None, key: str) -> Any:
        if not isinstance(key, str) or not 1 <= len(key) <= 1024:
            raise ReportingRowStoreError("INVALID_INPUT", "object key is invalid")
        return self._container(location, credential_ref).get_blob_client(key)

    # -- probe ------------------------------------------------------------

    async def probe(
        self,
        location: Mapping[str, str],
        prefix: str,
        context: ReportingRowObjectContextV1,
        credential_ref: str | None = None,
    ) -> None:
        container = _Budgeted(self._container(location, credential_ref), context.timeout_seconds)
        await asyncio.to_thread(self._probe, container, prefix)

    def _probe(self, container: Any, prefix: str) -> None:
        try:
            properties = container.get_container_properties()
        except Exception as error:
            if _status(error) == 404:
                raise ReportingRowStoreError("UNSAFE_BINDING", "container does not exist") from None
            if _forbidden(error):
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING", "container is not accessible"
                ) from None
            raise
        if getattr(properties, "public_access", None):
            raise ReportingRowStoreError("UNSAFE_BINDING", "container allows anonymous read access")
        self._assert_create_only_enforced(container, prefix)

    def _upload(self, blob: Any, data: bytes, content_type: str, **extra: Any) -> Any:
        from azure.storage.blob import ContentSettings

        return blob.upload_blob(
            data,
            overwrite=False,
            length=len(data),
            content_settings=ContentSettings(content_type=content_type, cache_control="no-store"),
            **extra,
        )

    def _assert_create_only_enforced(self, container: Any, prefix: str) -> None:
        blob = container.get_blob_client(f"{prefix}/.adcp-probe/{uuid.uuid4()}")
        version_ids: list[str] = []
        written = False

        def upload(body: bytes) -> None:
            nonlocal written
            response = self._upload(blob, body, "application/x-ndjson")
            written = True
            if response.get("version_id"):
                version_ids.append(response["version_id"])
            _version_of(response)

        try:
            upload(b'{"probe":1}\n')
            try:
                upload(b'{"probe":2}\n')
            except Exception as error:
                if isinstance(error, ReportingRowStoreError):
                    raise
                if _is_conflict(error):
                    return
                raise
            raise ReportingRowStoreError(
                "UNSAFE_BINDING", "store ignores If-None-Match and replaces blobs"
            )
        finally:
            # Best-effort cleanup of the fresh probe blob and any versions it produced.
            if written:
                try:
                    blob.delete_blob()
                except Exception:  # noqa: S110  # nosec B110
                    pass
                for version_id in version_ids:
                    try:
                        blob.delete_blob(version_id=version_id)
                    except Exception:  # noqa: S110  # nosec B110
                        pass

    # -- put / get / delete -------------------------------------------------

    async def put_if_absent(
        self, put: ReportingRowObjectPutInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectPutResultV1:
        blob = _Budgeted(
            self._blob(put.location, put.credential_ref, put.object_ref), context.timeout_seconds
        )
        return await asyncio.to_thread(self._put, blob, put)

    def _put(self, blob: Any, put: ReportingRowObjectPutInputV1) -> ReportingRowObjectPutResultV1:
        try:
            response = self._upload(
                blob, put.data, put.content_type, metadata=_metadata(put.metadata)
            )
            return ReportingRowObjectPutResultV1(True, _version_of(response))
        except Exception as error:
            if isinstance(error, ReportingRowStoreError) or not _is_conflict(error):
                raise
        # The key is taken. Report the existing version; the caller adopts it only on
        # identical bytes.
        return ReportingRowObjectPutResultV1(False, _version_of(blob.get_blob_properties()))

    async def get(
        self, get: ReportingRowObjectGetInputV1, context: ReportingRowObjectContextV1
    ) -> bytes | None:
        blob = _Budgeted(
            self._blob(get.location, get.credential_ref, get.object_ref), context.timeout_seconds
        )
        assert_read_bounds(get)
        version = _pinned(get.native_version_ref)
        if version is None:
            return None
        return await asyncio.to_thread(self._get, blob, get, version)

    @staticmethod
    def _get(blob: Any, get: ReportingRowObjectGetInputV1, version: str) -> bytes | None:
        try:
            if get.byte_range is not None and get.byte_range[1] == 0:
                properties = blob.get_blob_properties(version_id=version)
                if not _served(properties, version):
                    return None
                if get.byte_range[0] > int(getattr(properties, "size", 0) or 0):
                    raise ReportingRowStoreError(
                        "ROWS_INTEGRITY_FAILED", "requested range exceeds the stored object"
                    )
                return b""
            offset, length = get.byte_range if get.byte_range is not None else (0, None)
            downloader = blob.download_blob(
                offset=offset,
                # One byte past the cap proves an object larger than its recorded size.
                length=length if length is not None else get.max_bytes + 1,
                version_id=version,
            )
            # Stores without blob versioning ignore the version; only an echoed exact
            # version counts.
            if not _served(getattr(downloader, "properties", None), version):
                return None
            size = getattr(downloader, "size", None)
            if isinstance(size, int) and size > get.max_bytes:
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "stored object exceeds its recorded size"
                )
            data = bytes(downloader.readall())
        except ReportingRowStoreError:
            raise
        except Exception as error:
            if _is_missing(error):
                return None
            # A malformed or foreign version ID is not this blob.
            if _status(error) == 400:
                return None
            if _status(error) == 416 or _code(error) == "InvalidRange":
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "requested range exceeds the stored object"
                ) from None
            raise
        if len(data) > get.max_bytes:
            raise ReportingRowStoreError(
                "ROWS_INTEGRITY_FAILED", "stored object exceeds its recorded size"
            )
        return data

    async def stat(
        self, stat: ReportingRowObjectStatInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectStatResultV1 | None:
        blob = _Budgeted(
            self._blob(stat.location, stat.credential_ref, stat.object_ref),
            context.timeout_seconds,
        )
        version = _pinned(stat.native_version_ref)
        if version is None:
            return None
        return await asyncio.to_thread(self._stat, blob, version)

    @staticmethod
    def _stat(blob: Any, version: str) -> ReportingRowObjectStatResultV1 | None:
        # A version_id-pinned properties read: only an echoed exact version counts (stores
        # without blob versioning ignore it), and versions are unique per write.
        try:
            properties = blob.get_blob_properties(version_id=version)
        except Exception as error:
            if _is_missing(error) or _status(error) == 400:
                return None
            raise
        if not _served(properties, version):
            return None
        size = getattr(properties, "size", None)
        if type(size) is not int or size < 0:
            raise ReportingRowStoreError("PROVIDER_UNAVAILABLE", "blob metadata is malformed")
        return ReportingRowObjectStatResultV1(version, size)

    async def delete(
        self, delete: ReportingRowObjectDeleteInputV1, context: ReportingRowObjectContextV1
    ) -> Literal["deleted", "absent"]:
        blob = _Budgeted(
            self._blob(delete.location, delete.credential_ref, delete.object_ref),
            context.timeout_seconds,
        )
        version = _pinned(delete.native_version_ref)
        if version is None:
            return "absent"
        return await asyncio.to_thread(self._delete, blob, version)

    @staticmethod
    def _delete(blob: Any, version: str) -> Literal["deleted", "absent"]:
        from azure.core import MatchConditions

        # Confirm the store resolved the exact version; stores without versioning ignore it.
        try:
            recorded = blob.get_blob_properties(version_id=version)
            if getattr(recorded, "version_id", None) != version:
                return "absent"
        except Exception as error:
            if _is_missing(error) or _status(error) == 400:
                return "absent"
            raise
        # When the recorded version is still current, delete the base blob first (if it
        # is still that version), then remove the version itself.
        deleted_current = False
        try:
            current = blob.get_blob_properties()
            if getattr(current, "version_id", None) == version and getattr(current, "etag", None):
                blob.delete_blob(etag=current.etag, match_condition=MatchConditions.IfNotModified)
                deleted_current = True
        except Exception as error:
            if not _is_missing(error):
                raise
        try:
            blob.delete_blob(version_id=version)
            return "deleted"
        except Exception as error:
            if _is_missing(error) or _status(error) == 400:
                return "deleted" if deleted_current else "absent"
            raise


def create_azure_blob_reporting_row_object_provider_v1(
    *, client: Any = None, clients: Mapping[str, Any] | None = None
) -> ReportingRowObjectProviderV1:
    """Azure Blob Storage row-object provider.

    ``client`` is the official ``BlobServiceClient`` used by bindings without a
    ``credential_ref``; ``clients`` maps credential references to clients and is the
    closed set of references a binding may name.
    """
    mapped = client_map(clients)
    require_clients(
        client,
        mapped,
        lambda value: callable(getattr(value, "get_container_client", None))
        and isinstance(getattr(value, "account_name", None), str),
        "azure.storage.blob.BlobServiceClient",
    )
    return _AzureProvider(client, mapped)
