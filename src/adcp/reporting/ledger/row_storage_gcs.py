"""Google Cloud Storage row-object provider (``adcp[reporting-gcs]``).

Inject official ``google.cloud.storage.Client`` instances configured by the host with
keyless credentials (workload identity, ADC). Bindings use ``location={"bucket": ...}``;
``credential_ref`` selects an injected client.

Writes are single-request uploads with ``if_generation_match=0`` and a declared MD5, so
an existing object is never replaced and a corrupted upload is refused server side. The
recorded native version is the object *generation*; reads and deletes pin it. Objects
are stored without ``Content-Encoding`` and reads request raw stored bytes, so GCS never
transcodes them. This module never imports the client library: the host passes
clients, so the extra only needs to be installed where they are constructed.
"""

from __future__ import annotations

import asyncio
import re
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
    assert_key,
    assert_location_keys,
    assert_read_bounds,
    client_map,
    prefix_filter_overlaps,
    require_clients,
    select_client,
)

__all__ = [
    "assert_gcs_reporting_row_bucket_policy_v1",
    "create_gcs_reporting_row_object_provider_v1",
]

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
# Generations are microsecond timestamps.
_GENERATION = re.compile(r"^[1-9][0-9]{0,15}$")
_NONCURRENT_ONLY_CONDITIONS = (
    "numNewerVersions",
    "daysSinceNoncurrentTime",
    "noncurrentTimeBefore",
)


def _status(error: BaseException) -> int | None:
    code = getattr(error, "code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _generation_of(blob: Any) -> str:
    generation = str(getattr(blob, "generation", "") or "")
    if not _GENERATION.match(generation):
        raise ReportingRowStoreError("PROVIDER_UNAVAILABLE", "object generation is missing")
    return generation


def _pinned_generation(native_version_ref: str) -> int | None:
    if isinstance(native_version_ref, str) and _GENERATION.match(native_version_ref):
        return int(native_version_ref)
    return None


def assert_gcs_reporting_row_bucket_policy_v1(
    *,
    uniform_bucket_level_access: bool,
    public_access_prevention: str | None,
    lifecycle_rules: list[Mapping[str, Any]],
    prefix: str,
) -> None:
    """Row-binding bucket policy (shared persistence spec, section 3.4).

    Versioning, soft delete and retention are allowed as backstops. Refused: buckets
    without uniform bucket-level access or enforced public access prevention, and any
    lifecycle ``Delete`` rule that can match a live object under the binding prefix
    (no prefix filter, or a prefix that overlaps it). Rules limited to noncurrent
    versions are allowed.
    """
    if uniform_bucket_level_access is not True:
        raise ReportingRowStoreError(
            "UNSAFE_BINDING", "bucket must enable uniform bucket-level access"
        )
    if public_access_prevention != "enforced":
        raise ReportingRowStoreError(
            "UNSAFE_BINDING", "bucket must enforce public access prevention"
        )
    for rule in lifecycle_rules:
        if (rule.get("action") or {}).get("type") != "Delete":
            continue
        condition = rule.get("condition") or {}
        if condition.get("isLive") is False:
            continue
        if any(condition.get(name) is not None for name in _NONCURRENT_ONLY_CONDITIONS):
            continue
        prefixes = condition.get("matchesPrefix") or []
        if not prefixes or any(prefix_filter_overlaps(item, prefix) for item in prefixes):
            raise ReportingRowStoreError(
                "UNSAFE_BINDING", "a bucket lifecycle rule can delete live row objects"
            )


class _GcsProvider:
    name = "gcs"
    ranged_reads = True

    def __init__(self, storage: Any, clients: dict[str, Any]) -> None:
        self._storage = storage
        self._clients = clients

    def validate_location(self, location: Mapping[str, str]) -> None:
        assert_location_keys(location, ("bucket",))
        bucket = location["bucket"]
        if not _BUCKET.match(bucket) or ".." in bucket:
            raise ReportingRowStoreError("INVALID_INPUT", "GCS bucket name is invalid")

    def _bucket(self, location: Mapping[str, str], credential_ref: str | None) -> Any:
        self.validate_location(location)
        client = select_client(self._storage, self._clients, credential_ref)
        return client.bucket(location["bucket"])

    async def probe(
        self,
        location: Mapping[str, str],
        prefix: str,
        context: ReportingRowObjectContextV1,
        credential_ref: str | None = None,
    ) -> None:
        bucket = self._bucket(location, credential_ref)
        await asyncio.to_thread(self._probe, bucket, prefix, context.timeout_seconds)

    @staticmethod
    def _probe(bucket: Any, prefix: str, timeout: float) -> None:
        try:
            bucket.reload(timeout=timeout)
        except Exception as error:
            code = _status(error)
            if code == 404:
                raise ReportingRowStoreError("UNSAFE_BINDING", "bucket does not exist") from None
            if code in (401, 403):
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING", "bucket policy cannot be read with this client"
                ) from None
            raise
        iam = bucket.iam_configuration
        assert_gcs_reporting_row_bucket_policy_v1(
            uniform_bucket_level_access=bool(iam.uniform_bucket_level_access_enabled),
            public_access_prevention=iam.public_access_prevention,
            lifecycle_rules=[dict(rule) for rule in bucket.lifecycle_rules],
            prefix=prefix,
        )

    async def put_if_absent(
        self, put: ReportingRowObjectPutInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectPutResultV1:
        bucket = self._bucket(put.location, put.credential_ref)
        assert_key(put.object_ref)
        return await asyncio.to_thread(self._put, bucket, put, context.timeout_seconds)

    @staticmethod
    def _put(
        bucket: Any, put: ReportingRowObjectPutInputV1, timeout: float
    ) -> ReportingRowObjectPutResultV1:
        blob = bucket.blob(put.object_ref)
        blob.cache_control = "no-store"
        blob.metadata = dict(put.metadata)
        try:
            # GCS verifies the declared MD5 before creating the object. The client
            # never deletes on a mismatch without a generation precondition.
            blob.upload_from_string(
                put.data,
                content_type=put.content_type,
                if_generation_match=0,
                checksum="md5",
                timeout=timeout,
            )
        except Exception as error:
            if _status(error) != 412:
                raise
            # The key is taken. Report the existing generation; the caller adopts it
            # only on identical bytes.
            existing = bucket.get_blob(put.object_ref, timeout=timeout)
            if existing is None:
                raise
            return ReportingRowObjectPutResultV1(False, _generation_of(existing))
        return ReportingRowObjectPutResultV1(True, _generation_of(blob))

    async def get(
        self, get: ReportingRowObjectGetInputV1, context: ReportingRowObjectContextV1
    ) -> bytes | None:
        bucket = self._bucket(get.location, get.credential_ref)
        assert_key(get.object_ref)
        assert_read_bounds(get)
        generation = _pinned_generation(get.native_version_ref)
        if generation is None:
            return None
        return await asyncio.to_thread(self._get, bucket, get, generation, context.timeout_seconds)

    @staticmethod
    def _get(
        bucket: Any, get: ReportingRowObjectGetInputV1, generation: int, timeout: float
    ) -> bytes | None:
        blob = bucket.blob(get.object_ref, generation=generation)
        try:
            if get.byte_range is not None:
                offset, length = get.byte_range
                if length == 0:
                    blob.reload(timeout=timeout)
                    if offset > int(blob.size or 0):
                        raise ReportingRowStoreError(
                            "ROWS_INTEGRITY_FAILED", "requested range exceeds the stored object"
                        )
                    return b""
                # ``end`` is inclusive. ``raw_download`` keeps GCS from transcoding and
                # the digests recorded in PostgreSQL verify every byte returned.
                data = blob.download_as_bytes(
                    start=offset,
                    end=offset + length - 1,
                    raw_download=True,
                    checksum=None,
                    timeout=timeout,
                )
            else:
                # One byte past the cap proves an object larger than its recorded size.
                data = blob.download_as_bytes(
                    start=0,
                    end=get.max_bytes,
                    raw_download=True,
                    checksum=None,
                    timeout=timeout,
                )
        except ReportingRowStoreError:
            raise
        except Exception as error:
            code = _status(error)
            if code == 404:
                return None
            if code == 416:
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "requested range exceeds the stored object"
                ) from None
            raise
        if len(data) > get.max_bytes:
            raise ReportingRowStoreError(
                "ROWS_INTEGRITY_FAILED", "stored object exceeds its recorded size"
            )
        return bytes(data)

    async def stat(
        self, stat: ReportingRowObjectStatInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectStatResultV1 | None:
        bucket = self._bucket(stat.location, stat.credential_ref)
        assert_key(stat.object_ref)
        generation = _pinned_generation(stat.native_version_ref)
        if generation is None:
            return None
        return await asyncio.to_thread(
            self._stat, bucket, stat.object_ref, generation, context.timeout_seconds
        )

    @staticmethod
    def _stat(
        bucket: Any, key: str, generation: int, timeout: float
    ) -> ReportingRowObjectStatResultV1 | None:
        # A generation-pinned metadata read: 404 for any other generation, so a hit proves this
        # exact write (generations are unique per write) and its stored size without reading
        # the bytes.
        blob = bucket.blob(key, generation=generation)
        try:
            blob.reload(timeout=timeout)
        except Exception as error:
            if _status(error) == 404:
                return None
            raise
        # A store that ignores the pin and answers for another generation is not this object.
        if str(getattr(blob, "generation", "")) != str(generation):
            return None
        try:
            size = int(blob.size)
        except (TypeError, ValueError):
            size = -1
        if size < 0:
            raise ReportingRowStoreError("PROVIDER_UNAVAILABLE", "object metadata is malformed")
        return ReportingRowObjectStatResultV1(str(generation), size)

    async def delete(
        self, delete: ReportingRowObjectDeleteInputV1, context: ReportingRowObjectContextV1
    ) -> Literal["deleted", "absent"]:
        bucket = self._bucket(delete.location, delete.credential_ref)
        assert_key(delete.object_ref)
        generation = _pinned_generation(delete.native_version_ref)
        if generation is None:
            return "absent"
        return await asyncio.to_thread(
            self._delete, bucket, delete.object_ref, generation, context.timeout_seconds
        )

    @staticmethod
    def _delete(
        bucket: Any, key: str, generation: int, timeout: float
    ) -> Literal["deleted", "absent"]:
        try:
            # The generation is the exact-version precondition: it deletes only the
            # recorded object, live or noncurrent, and answers 404 for any other
            # generation. An extra if_generation_match would strand a recorded
            # noncurrent generation on versioned buckets, so it is not added.
            bucket.blob(key, generation=generation).delete(timeout=timeout)
            return "deleted"
        except Exception as error:
            if _status(error) in (404, 412):
                return "absent"
            raise


def create_gcs_reporting_row_object_provider_v1(
    *, storage: Any = None, clients: Mapping[str, Any] | None = None
) -> ReportingRowObjectProviderV1:
    """Google Cloud Storage row-object provider.

    ``storage`` is the official client used by bindings without a ``credential_ref``;
    ``clients`` maps credential references to official clients and is the closed set of
    references a binding may name.
    """
    mapped = client_map(clients)
    require_clients(
        storage,
        mapped,
        lambda value: callable(getattr(value, "bucket", None)),
        "google.cloud.storage.Client",
    )
    return _GcsProvider(storage, mapped)
