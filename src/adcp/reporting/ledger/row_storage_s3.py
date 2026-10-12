"""Amazon S3 and S3-compatible row-object provider (``adcp[reporting-s3]``).

Inject official ``boto3`` S3 clients configured by the host (region, endpoint,
credentials, path style). Bindings use ``location={"bucket": ..., "region": ...}``;
when ``region`` is set the probe requires the selected client to be configured for it.
Endpoint, region and credentials come only from the injected clients, never from a
binding.

Writes use ``put_object`` with ``IfNoneMatch="*"``. **Bucket versioning must be
enabled**: the recorded native version is the write-unique ``VersionId`` and every
read and delete is pinned to it, so a delayed delete can never remove a later write
that re-created identical bytes at the same key. An ETag is content-derived and
cannot fence that, so it is never accepted as a version. The probe refuses unversioned
or suspended buckets, and, because S3-compatible stores may silently ignore
``If-None-Match``, proves create-only behaviour empirically before a binding is used.

This module never imports ``boto3``; the host passes the clients.
"""

from __future__ import annotations

import asyncio
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
    assert_key,
    assert_location_keys,
    assert_read_bounds,
    client_map,
    prefix_filter_overlaps,
    require_clients,
    select_client,
)

__all__ = ["create_s3_reporting_row_object_provider_v1"]

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")
# Version IDs are opaque printable tokens, never the literal ``null`` of an unversioned object.
_VERSION_ID = re.compile(r"^[\x21\x23-\x7e]{1,1024}$")


def _status(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _name(error: BaseException) -> str:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return ""
    value = (response.get("Error") or {}).get("Code")
    return value if isinstance(value, str) else ""


def _precondition_failed(error: BaseException) -> bool:
    return _status(error) == 412 or _name(error) == "PreconditionFailed"


def _conditional_conflict(error: BaseException) -> bool:
    return _status(error) == 409 or _name(error) == "ConditionalRequestConflict"


def _not_found(error: BaseException) -> bool:
    return _status(error) == 404 or _name(error) in {
        "NoSuchKey",
        "NotFound",
        "NoSuchVersion",
        "NoSuchBucket",
    }


def _not_implemented(error: BaseException) -> bool:
    return _status(error) == 501 or _name(error) == "NotImplemented"


def _forbidden(error: BaseException) -> bool:
    return _status(error) in (401, 403)


def _range_error(error: BaseException) -> bool:
    return _status(error) == 416 or _name(error) == "InvalidRange"


def _version_of(output: Mapping[str, Any]) -> str:
    """The write-unique ``VersionId``; an ETag is never a version."""
    version = output.get("VersionId")
    if not isinstance(version, str) or version == "null" or not _VERSION_ID.match(version):
        raise ReportingRowStoreError(
            "UNSAFE_BINDING",
            "store returned no object VersionId; S3 bucket versioning must be enabled so deletes"
            " are fenced to one write",
        )
    return version


def _selector(native_version_ref: str) -> str | None:
    if not isinstance(native_version_ref, str) or native_version_ref == "null":
        return None
    return native_version_ref if _VERSION_ID.match(native_version_ref) else None


def _live_expiration_covers(rule: Mapping[str, Any], prefix: str) -> bool:
    if rule.get("Status") != "Enabled":
        return False
    expiration = rule.get("Expiration") or {}
    if expiration.get("Days") is None and expiration.get("Date") is None:
        return False
    rule_filter = rule.get("Filter") or {}
    # Row objects are never tagged, so a rule that requires tags cannot match them.
    if rule_filter.get("Tag") is not None or (rule_filter.get("And") or {}).get("Tags"):
        return False
    prefix_filter = (
        rule_filter.get("Prefix")
        or (rule_filter.get("And") or {}).get("Prefix")
        or rule.get("Prefix")
    )
    return prefix_filter_overlaps(prefix_filter, prefix)


class _S3Provider:
    name = "s3"
    ranged_reads = True

    def __init__(self, client: Any, clients: dict[str, Any]) -> None:
        self._client = client
        self._clients = clients

    def validate_location(self, location: Mapping[str, str]) -> None:
        assert_location_keys(location, ("bucket",), ("region",))
        bucket = location["bucket"]
        if (
            not _BUCKET.match(bucket)
            or ".." in bucket
            or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", bucket) is not None
        ):
            raise ReportingRowStoreError("INVALID_INPUT", "S3 bucket name is invalid")
        if "region" in location and not _REGION.match(location["region"]):
            raise ReportingRowStoreError("INVALID_INPUT", "S3 region is invalid")

    def _target(self, location: Mapping[str, str], credential_ref: str | None) -> tuple[Any, str]:
        self.validate_location(location)
        return select_client(self._client, self._clients, credential_ref), location["bucket"]

    # -- probe ------------------------------------------------------------

    async def probe(
        self,
        location: Mapping[str, str],
        prefix: str,
        context: ReportingRowObjectContextV1,
        credential_ref: str | None = None,
    ) -> None:
        client, bucket = self._target(location, credential_ref)
        await asyncio.to_thread(self._probe, client, bucket, location.get("region"), prefix)

    @staticmethod
    def _assert_bounded_transport(client: Any) -> None:
        """Refuse a client whose socket calls can block forever.

        botocore takes timeouts from the client's ``Config``, not per call, and
        ``asyncio.wait_for`` cannot stop the worker thread running a hung call, so the call
        budget is only honoured when the injected client bounds its own connect and read.
        botocore defaults both to 60 seconds; an explicit ``None`` disables them.
        """
        config = getattr(getattr(client, "meta", None), "config", None)
        if config is None:
            return
        for name in ("connect_timeout", "read_timeout"):
            value = getattr(config, name, None)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING",
                    f"the S3 client has no finite {name}; build it with a botocore Config "
                    "that sets connect and read timeouts",
                )

    def _probe(self, client: Any, bucket: str, region: str | None, prefix: str) -> None:
        self._assert_bounded_transport(client)
        try:
            client.head_bucket(Bucket=bucket)
        except Exception as error:
            if _not_found(error):
                raise ReportingRowStoreError("UNSAFE_BINDING", "bucket does not exist") from None
            if _forbidden(error):
                raise ReportingRowStoreError("UNSAFE_BINDING", "bucket is not accessible") from None
            if _status(error) == 301:
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING", "bucket is in a different region"
                ) from None
            raise
        if region is not None:
            configured = getattr(getattr(client, "meta", None), "region_name", None)
            if configured != region:
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING", "client region does not match the binding"
                )
        # Best effort: S3-compatible stores often lack this API. A positive "public"
        # answer is refused.
        try:
            status = client.get_bucket_policy_status(Bucket=bucket)
        except Exception:
            status = None
        if status and (status.get("PolicyStatus") or {}).get("IsPublic") is True:
            raise ReportingRowStoreError("UNSAFE_BINDING", "bucket policy makes objects public")
        rules: list[Mapping[str, Any]] = []
        try:
            rules = list(
                client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules") or []
            )
        except Exception as error:
            if _forbidden(error):
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING", "bucket lifecycle cannot be read with this client"
                ) from None
            # No configuration, or a store without lifecycle support.
            if (
                not _not_found(error)
                and not _not_implemented(error)
                and _name(error) != "NoSuchLifecycleConfiguration"
            ):
                raise
        if any(_live_expiration_covers(rule, prefix) for rule in rules):
            raise ReportingRowStoreError(
                "UNSAFE_BINDING", "a bucket lifecycle rule can expire live row objects"
            )
        self._assert_versioning_enabled(client, bucket)
        self._assert_create_only_enforced(client, bucket, prefix)

    @staticmethod
    def _assert_versioning_enabled(client: Any, bucket: str) -> None:
        """Fail closed unless versioning is ``Enabled``; suspended or absent yields no unique id."""
        try:
            status = client.get_bucket_versioning(Bucket=bucket).get("Status")
        except Exception as error:
            if _forbidden(error) or _not_implemented(error):
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING", "bucket versioning status cannot be verified"
                ) from None
            raise
        if status != "Enabled":
            raise ReportingRowStoreError(
                "UNSAFE_BINDING",
                "S3 bucket versioning must be enabled so row object deletes are fenced to"
                " one write",
            )

    def _put_create_only(self, client: Any, **parameters: Any) -> Mapping[str, Any]:
        """Create-only put; a 409 ConditionalRequestConflict is retried once."""
        for attempt in range(2):
            try:
                return client.put_object(IfNoneMatch="*", **parameters)  # type: ignore[no-any-return]
            except Exception as error:
                if attempt == 0 and _conditional_conflict(error):
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _assert_create_only_enforced(self, client: Any, bucket: str, prefix: str) -> None:
        key = f"{prefix}/.adcp-probe/{uuid.uuid4()}"
        written: list[Mapping[str, Any]] = []

        def put(body: bytes) -> None:
            output = self._put_create_only(
                client,
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/x-ndjson",
                CacheControl="no-store",
            )
            written.append(output)
            _version_of(output)

        try:
            try:
                put(b'{"probe":1}\n')
            except Exception as error:
                if isinstance(error, ReportingRowStoreError):
                    raise
                if _not_implemented(error) or _precondition_failed(error):
                    raise ReportingRowStoreError(
                        "UNSAFE_BINDING", "store does not support create-only writes"
                    ) from None
                raise
            try:
                put(b'{"probe":2}\n')
            except Exception as error:
                if isinstance(error, ReportingRowStoreError):
                    raise
                if _precondition_failed(error):
                    return
                if _not_implemented(error):
                    raise ReportingRowStoreError(
                        "UNSAFE_BINDING", "store does not support create-only writes"
                    ) from None
                raise
            raise ReportingRowStoreError(
                "UNSAFE_BINDING", "store ignores If-None-Match and replaces objects"
            )
        finally:
            # Best-effort cleanup of the probe key: a fresh random key under the owned prefix.
            version_ids = [item["VersionId"] for item in written if item.get("VersionId")]
            for selector in [{"VersionId": version} for version in version_ids] or (
                [{}] if written else []
            ):
                try:
                    client.delete_object(Bucket=bucket, Key=key, **selector)
                except Exception:  # noqa: S110  # nosec B110
                    pass

    # -- put / get / delete -------------------------------------------------

    async def put_if_absent(
        self, put: ReportingRowObjectPutInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectPutResultV1:
        client, bucket = self._target(put.location, put.credential_ref)
        assert_key(put.object_ref)
        return await asyncio.to_thread(self._put, client, bucket, put)

    def _put(
        self, client: Any, bucket: str, put: ReportingRowObjectPutInputV1
    ) -> ReportingRowObjectPutResultV1:
        try:
            output = self._put_create_only(
                client,
                Bucket=bucket,
                Key=put.object_ref,
                Body=put.data,
                ContentLength=len(put.data),
                ContentType=put.content_type,
                CacheControl="no-store",
                Metadata=dict(put.metadata),
            )
            return ReportingRowObjectPutResultV1(True, _version_of(output))
        except Exception as error:
            if isinstance(error, ReportingRowStoreError) or not _precondition_failed(error):
                raise
        # The key is taken. Report the existing version; the caller adopts it only on
        # identical bytes.
        head = client.head_object(Bucket=bucket, Key=put.object_ref)
        return ReportingRowObjectPutResultV1(False, _version_of(head))

    async def get(
        self, get: ReportingRowObjectGetInputV1, context: ReportingRowObjectContextV1
    ) -> bytes | None:
        client, bucket = self._target(get.location, get.credential_ref)
        assert_key(get.object_ref)
        assert_read_bounds(get)
        version = _selector(get.native_version_ref)
        if version is None:
            return None
        return await asyncio.to_thread(self._get, client, bucket, get, version)

    @staticmethod
    def _get(
        client: Any, bucket: str, get: ReportingRowObjectGetInputV1, version: str
    ) -> bytes | None:
        try:
            if get.byte_range is not None and get.byte_range[1] == 0:
                head = client.head_object(Bucket=bucket, Key=get.object_ref, VersionId=version)
                if head.get("VersionId") != version:
                    return None
                if get.byte_range[0] > int(head.get("ContentLength") or 0):
                    raise ReportingRowStoreError(
                        "ROWS_INTEGRITY_FAILED", "requested range exceeds the stored object"
                    )
                return b""
            parameters: dict[str, Any] = {
                "Bucket": bucket,
                "Key": get.object_ref,
                "VersionId": version,
            }
            if get.byte_range is not None:
                offset, length = get.byte_range
                parameters["Range"] = f"bytes={offset}-{offset + length - 1}"
            output = client.get_object(**parameters)
            body = output["Body"]
            try:
                # Some S3-compatible stores ignore VersionId; only an echoed exact version counts.
                if output.get("VersionId") != version:
                    return None
                length = output.get("ContentLength")
                if isinstance(length, int) and length > get.max_bytes:
                    raise ReportingRowStoreError(
                        "ROWS_INTEGRITY_FAILED", "stored object exceeds its recorded size"
                    )
                data = bytes(body.read(get.max_bytes + 1))
            finally:
                close = getattr(body, "close", None)
                if close is not None:
                    close()
            if len(data) > get.max_bytes:
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "stored object exceeds its recorded size"
                )
            return data
        except ReportingRowStoreError:
            raise
        except Exception as error:
            if _not_found(error) or _precondition_failed(error):
                return None
            # A malformed or foreign version ID is not this object.
            if _status(error) == 400 and _name(error) != "InvalidRange":
                return None
            if _range_error(error):
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "requested range exceeds the stored object"
                ) from None
            raise

    async def stat(
        self, stat: ReportingRowObjectStatInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectStatResultV1 | None:
        client, bucket = self._target(stat.location, stat.credential_ref)
        assert_key(stat.object_ref)
        version = _selector(stat.native_version_ref)
        if version is None:
            return None
        return await asyncio.to_thread(self._stat, client, bucket, stat.object_ref, version)

    @staticmethod
    def _stat(
        client: Any, bucket: str, key: str, version: str
    ) -> ReportingRowObjectStatResultV1 | None:
        # A VersionId-pinned HEAD: S3 versions are unique per write, and only an echoed exact
        # version counts (some S3-compatible stores ignore ``VersionId``).
        try:
            head = client.head_object(Bucket=bucket, Key=key, VersionId=version)
        except Exception as error:
            if _not_found(error) or _precondition_failed(error):
                return None
            # 400: malformed or foreign version ID; 405: the version is a delete marker.
            if _status(error) in (400, 405):
                return None
            raise
        if head.get("VersionId") != version:
            return None
        size = head.get("ContentLength")
        if type(size) is not int or size < 0:
            raise ReportingRowStoreError("PROVIDER_UNAVAILABLE", "object metadata is malformed")
        return ReportingRowObjectStatResultV1(version, size)

    async def delete(
        self, delete: ReportingRowObjectDeleteInputV1, context: ReportingRowObjectContextV1
    ) -> Literal["deleted", "absent"]:
        client, bucket = self._target(delete.location, delete.credential_ref)
        assert_key(delete.object_ref)
        version = _selector(delete.native_version_ref)
        if version is None:
            return "absent"
        return await asyncio.to_thread(self._delete, client, bucket, delete.object_ref, version)

    @staticmethod
    def _delete(client: Any, bucket: str, key: str, version: str) -> Literal["deleted", "absent"]:
        # S3 deletes succeed for missing keys and versions, so confirm the exact version first.
        try:
            head = client.head_object(Bucket=bucket, Key=key, VersionId=version)
            if head.get("VersionId") != version:
                return "absent"
        except Exception as error:
            if _not_found(error) or _precondition_failed(error):
                return "absent"
            # 400: malformed or foreign version ID. 405: the version is a delete marker.
            if _status(error) in (400, 405):
                return "absent"
            raise
        # Deleting a specific VersionId permanently removes that version (no delete marker).
        client.delete_object(Bucket=bucket, Key=key, VersionId=version)
        return "deleted"


def create_s3_reporting_row_object_provider_v1(
    *, client: Any = None, clients: Mapping[str, Any] | None = None
) -> ReportingRowObjectProviderV1:
    """Amazon S3 (and S3-compatible) row-object provider.

    ``client`` is the official boto3 S3 client used by bindings without a
    ``credential_ref``; ``clients`` maps credential references to clients and is the
    closed set of references a binding may name.
    """
    mapped = client_map(clients)
    require_clients(
        client,
        mapped,
        lambda value: callable(getattr(value, "put_object", None)),
        "boto3 S3 client",
    )
    return _S3Provider(client, mapped)
