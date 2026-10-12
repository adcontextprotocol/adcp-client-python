"""The S3 row-object provider against an emulated S3 (moto) using the real boto3 client."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError  # noqa: E402
from adcp.reporting.ledger.row_storage_object import (  # noqa: E402
    ReportingRowObjectContextV1,
    ReportingRowObjectDeleteInputV1,
    ReportingRowObjectGetInputV1,
    ReportingRowObjectPutInputV1,
    run_reporting_row_object_provider_conformance_v1,
)
from adcp.reporting.ledger.row_storage_s3 import (  # noqa: E402
    create_s3_reporting_row_object_provider_v1,
)

CONTEXT = ReportingRowObjectContextV1()
LOCATION = {"bucket": "rows-bucket"}


@pytest.fixture
def s3() -> Iterator[Any]:
    with moto.mock_aws():
        client = boto3.client(
            "s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
        )
        client.create_bucket(Bucket="rows-bucket")
        client.put_bucket_versioning(
            Bucket="rows-bucket", VersioningConfiguration={"Status": "Enabled"}
        )
        yield client


def put(key: str, data: bytes = b'{"a":1}\n') -> ReportingRowObjectPutInputV1:
    return ReportingRowObjectPutInputV1(
        LOCATION, key, data, "application/gzip", {"adcp-installation": "i", "adcp-intent": "t"}
    )


async def test_passes_the_shared_provider_conformance_suite(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=s3)
    passed = await run_reporting_row_object_provider_conformance_v1(
        provider, location=LOCATION, prefix="adcp-rows"
    )
    assert passed[-1] == "write-unique-version" and "ranged-read" in passed


async def test_create_only_writes_record_the_version_id_and_adopt_on_conflict(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=s3)
    created = await provider.put_if_absent(put("adcp-rows/ns/a.jsonl.gz", b"first\n"), CONTEXT)
    taken = await provider.put_if_absent(put("adcp-rows/ns/a.jsonl.gz", b"second\n"), CONTEXT)
    assert created.created and not taken.created
    assert taken.native_version == created.native_version
    head = s3.head_object(Bucket="rows-bucket", Key="adcp-rows/ns/a.jsonl.gz")
    assert head["VersionId"] == created.native_version
    assert head["Metadata"] == {"adcp-installation": "i", "adcp-intent": "t"}
    assert head["CacheControl"] == "no-store" and head["ContentType"] == "application/gzip"
    read = ReportingRowObjectGetInputV1(
        LOCATION, "adcp-rows/ns/a.jsonl.gz", created.native_version, 100
    )
    assert await provider.get(read, CONTEXT) == b"first\n"


async def test_reads_pin_the_version_and_honour_ranges_and_the_cap(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=s3)
    created = await provider.put_if_absent(
        put("adcp-rows/ns/r.jsonl", b"0123456789abcdef"), CONTEXT
    )

    def read(**kwargs: Any) -> Any:
        return provider.get(
            ReportingRowObjectGetInputV1(
                LOCATION, "adcp-rows/ns/r.jsonl", created.native_version, **kwargs
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
        await read(max_bytes=10, byte_range=(100, 2))
    assert past.value.code == "ROWS_INTEGRITY_FAILED"
    # A version this key never had is not this object.
    outcome = await provider.get(
        ReportingRowObjectGetInputV1(LOCATION, "adcp-rows/ns/r.jsonl", "bogus-version", 100),
        CONTEXT,
    )
    assert outcome is None


async def test_a_delayed_delete_never_removes_an_identical_recreation(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=s3)
    key = "adcp-rows/ns/w.jsonl"
    first = await provider.put_if_absent(put(key, b"same\n"), CONTEXT)
    delete_first = ReportingRowObjectDeleteInputV1(LOCATION, key, first.native_version)
    assert await provider.delete(delete_first, CONTEXT) == "deleted"
    second = await provider.put_if_absent(put(key, b"same\n"), CONTEXT)
    assert second.native_version != first.native_version
    # The delayed delete of the first write finds nothing and leaves the replacement.
    assert await provider.delete(delete_first, CONTEXT) == "absent"
    survivor = ReportingRowObjectGetInputV1(LOCATION, key, second.native_version, 100)
    assert await provider.get(survivor, CONTEXT) == b"same\n"
    # Deleting an exact version leaves no delete marker behind.
    await provider.delete(
        ReportingRowObjectDeleteInputV1(LOCATION, key, second.native_version), CONTEXT
    )
    assert s3.list_object_versions(Bucket="rows-bucket", Prefix=key).get("DeleteMarkers") is None


async def test_the_probe_refuses_unversioned_missing_and_suspended_buckets(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=s3)
    s3.create_bucket(Bucket="plain-bucket")
    with pytest.raises(ReportingRowStoreError) as unversioned:
        await provider.probe({"bucket": "plain-bucket"}, "adcp-rows", CONTEXT)
    assert unversioned.value.code == "UNSAFE_BINDING" and "versioning" in str(unversioned.value)
    s3.put_bucket_versioning(Bucket="plain-bucket", VersioningConfiguration={"Status": "Suspended"})
    with pytest.raises(ReportingRowStoreError) as suspended:
        await provider.probe({"bucket": "plain-bucket"}, "adcp-rows", CONTEXT)
    assert suspended.value.code == "UNSAFE_BINDING"
    with pytest.raises(ReportingRowStoreError) as missing:
        await provider.probe({"bucket": "no-such-bucket"}, "adcp-rows", CONTEXT)
    assert missing.value.code == "UNSAFE_BINDING"
    # The probe leaves nothing behind on a good bucket.
    await provider.probe(LOCATION, "adcp-rows", CONTEXT)
    assert s3.list_object_versions(Bucket="rows-bucket").get("Versions") is None


async def test_the_probe_refuses_lifecycle_rules_that_expire_live_row_objects(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=s3)

    def expire(prefix: str, **extra: Any) -> None:
        s3.put_bucket_lifecycle_configuration(
            Bucket="rows-bucket",
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "r",
                        "Status": "Enabled",
                        "Filter": {"Prefix": prefix},
                        "Expiration": {"Days": 30},
                        **extra,
                    }
                ]
            },
        )

    expire("adcp-rows/")
    with pytest.raises(ReportingRowStoreError) as covered:
        await provider.probe(LOCATION, "adcp-rows", CONTEXT)
    assert covered.value.code == "UNSAFE_BINDING"
    expire("")
    with pytest.raises(ReportingRowStoreError):
        await provider.probe(LOCATION, "adcp-rows", CONTEXT)
    # A rule elsewhere in the bucket is none of this binding's business.
    expire("logs/")
    await provider.probe(LOCATION, "adcp-rows", CONTEXT)


async def test_the_probe_refuses_a_client_for_another_region(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=s3)
    await provider.probe({**LOCATION, "region": "us-east-1"}, "adcp-rows", CONTEXT)
    with pytest.raises(ReportingRowStoreError) as failure:
        await provider.probe({**LOCATION, "region": "eu-west-1"}, "adcp-rows", CONTEXT)
    assert failure.value.code == "UNSAFE_BINDING"


class StoreThatIgnoresIfNoneMatch:
    """A store whose put_object silently replaces: the probe must refuse it."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.meta = real.meta

    def put_object(self, **parameters: Any) -> Any:
        parameters.pop("IfNoneMatch", None)
        return self._real.put_object(**parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def test_the_probe_proves_create_only_behaviour_empirically(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=StoreThatIgnoresIfNoneMatch(s3))
    with pytest.raises(ReportingRowStoreError) as failure:
        await provider.probe(LOCATION, "adcp-rows", CONTEXT)
    assert failure.value.code == "UNSAFE_BINDING" and "replaces" in str(failure.value)
    assert s3.list_object_versions(Bucket="rows-bucket").get("Versions") is None


class StoreWithoutVersionIds:
    def __init__(self, real: Any) -> None:
        self._real = real
        self.meta = real.meta

    def put_object(self, **parameters: Any) -> Any:
        output = dict(self._real.put_object(**parameters))
        output.pop("VersionId", None)
        return output

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def test_a_write_that_returns_no_version_id_is_refused(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=StoreWithoutVersionIds(s3))
    with pytest.raises(ReportingRowStoreError) as failure:
        await provider.put_if_absent(put("adcp-rows/ns/n.jsonl"), CONTEXT)
    assert failure.value.code == "UNSAFE_BINDING"


async def test_credential_refs_select_from_a_closed_client_map(s3: Any) -> None:
    other = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )
    provider = create_s3_reporting_row_object_provider_v1(client=s3, clients={"tenant": other})
    await provider.put_if_absent(
        ReportingRowObjectPutInputV1(
            LOCATION, "adcp-rows/ns/c.jsonl", b"x\n", "application/gzip", {}, "tenant"
        ),
        CONTEXT,
    )
    with pytest.raises(ReportingRowStoreError) as unknown:
        await provider.put_if_absent(
            ReportingRowObjectPutInputV1(
                LOCATION, "adcp-rows/ns/c2.jsonl", b"x\n", "application/gzip", {}, "stranger"
            ),
            CONTEXT,
        )
    assert unknown.value.code == "UNSAFE_BINDING"
    assert "stranger" not in str(unknown.value)


def test_locations_are_closed_and_options_validated(s3: Any) -> None:
    provider = create_s3_reporting_row_object_provider_v1(client=s3)
    provider.validate_location({"bucket": "rows-bucket", "region": "eu-west-1"})
    for bad in (
        {},
        {"bucket": "Rows_Bucket"},
        {"bucket": "a..b"},
        {"bucket": "10.0.0.1"},
        {"bucket": "rows-bucket", "endpoint": "https://evil.example"},
        {"bucket": "rows-bucket", "region": "EU"},
    ):
        with pytest.raises(ReportingRowStoreError) as failure:
            provider.validate_location(bad)
        assert failure.value.code == "INVALID_INPUT", bad
    for kwargs in ({}, {"client": object()}, {"clients": {"bad ref": s3}}):
        with pytest.raises(ReportingRowStoreError):
            create_s3_reporting_row_object_provider_v1(**kwargs)


async def test_a_client_without_finite_socket_timeouts_is_refused_at_the_probe() -> None:
    """The call budget cannot stop a worker thread, so the client must bound its own calls."""
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106  # nosec B106
        config=Config(read_timeout=None),
    )
    provider = create_s3_reporting_row_object_provider_v1(client=client)
    with pytest.raises(ReportingRowStoreError) as refused:
        await provider.probe(LOCATION, "adcp-rows", CONTEXT)
    assert refused.value.code == "UNSAFE_BINDING" and "read_timeout" in str(refused.value)
