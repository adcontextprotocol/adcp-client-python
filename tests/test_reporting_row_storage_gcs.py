"""The GCS row-object provider against an in-memory fake of the official client surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

exceptions = pytest.importorskip("google.api_core.exceptions")

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError  # noqa: E402
from adcp.reporting.ledger.row_storage_gcs import (  # noqa: E402
    assert_gcs_reporting_row_bucket_policy_v1,
    create_gcs_reporting_row_object_provider_v1,
)
from adcp.reporting.ledger.row_storage_object import (  # noqa: E402
    ReportingRowObjectContextV1,
    ReportingRowObjectDeleteInputV1,
    ReportingRowObjectGetInputV1,
    ReportingRowObjectPutInputV1,
    run_reporting_row_object_provider_conformance_v1,
)

CONTEXT = ReportingRowObjectContextV1()
LOCATION = {"bucket": "rows-bucket"}


@dataclass
class _Version:
    generation: int
    data: bytes
    metadata: dict[str, str]
    content_type: str
    cache_control: str | None


@dataclass
class FakeStorage:
    buckets: dict[str, dict[str, Any]] = field(default_factory=dict)
    objects: dict[tuple[str, str], list[_Version]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    next_generation: int = 1_700_000_000_000_000

    def add_bucket(
        self,
        name: str = "rows-bucket",
        *,
        uniform: bool = True,
        public_access_prevention: str | None = "enforced",
        lifecycle: list[dict[str, Any]] | None = None,
    ) -> FakeStorage:
        self.buckets[name] = {
            "uniform": uniform,
            "pap": public_access_prevention,
            "lifecycle": lifecycle or [],
        }
        return self

    def bucket(self, name: str) -> Any:
        storage = self

        class Bucket:
            def reload(self, timeout: Any = None) -> None:
                if name not in storage.buckets:
                    raise exceptions.NotFound("no bucket")

            @property
            def iam_configuration(self) -> Any:
                config = storage.buckets[name]
                return SimpleNamespace(
                    uniform_bucket_level_access_enabled=config["uniform"],
                    public_access_prevention=config["pap"],
                )

            @property
            def lifecycle_rules(self) -> Any:
                return iter(storage.buckets[name]["lifecycle"])

            def blob(self, key: str, generation: int | None = None) -> Any:
                return Blob(name, key, generation)

            def get_blob(self, key: str, timeout: Any = None) -> Any:
                live = storage.objects.get((name, key))
                if not live:
                    return None
                blob = Blob(name, key, None)
                blob.generation = live[-1].generation
                return blob

        class Blob:
            def __init__(self, bucket: str, key: str, pinned: int | None) -> None:
                self.bucket, self.key, self.pinned = bucket, key, pinned
                self.generation: int | None = pinned
                self.metadata: dict[str, str] = {}
                self.cache_control: str | None = None
                self.size: int | None = None

            def _at(self) -> _Version | None:
                versions = storage.objects.get((self.bucket, self.key), [])
                if self.pinned is None:
                    return versions[-1] if versions else None
                return next((v for v in versions if v.generation == self.pinned), None)

            def upload_from_string(self, data: bytes, **options: Any) -> None:
                storage.calls.append(("upload", {"key": self.key, **options}))
                if options.get("if_generation_match") == 0 and storage.objects.get(
                    (self.bucket, self.key)
                ):
                    raise exceptions.PreconditionFailed("object exists")
                version = _Version(
                    storage.next_generation,
                    bytes(data),
                    dict(self.metadata),
                    options["content_type"],
                    self.cache_control,
                )
                storage.next_generation += 1
                storage.objects.setdefault((self.bucket, self.key), []).append(version)
                self.generation = version.generation

            def reload(self, timeout: Any = None) -> None:
                version = self._at()
                if version is None:
                    raise exceptions.NotFound("no object")
                self.generation, self.size = version.generation, len(version.data)

            def download_as_bytes(self, **options: Any) -> bytes:
                storage.calls.append(
                    ("download", {"key": self.key, "generation": self.pinned, **options})
                )
                version = self._at()
                if version is None:
                    raise exceptions.NotFound("no object")
                start = options.get("start") or 0
                if start >= len(version.data) and len(version.data) > 0:
                    raise exceptions.RequestRangeNotSatisfiable("range")
                end = options.get("end")
                return version.data[start : None if end is None else end + 1]

            def delete(self, timeout: Any = None) -> None:
                storage.calls.append(("delete", {"key": self.key, "generation": self.pinned}))
                version = self._at()
                if version is None:
                    raise exceptions.NotFound("no object")
                storage.objects[(self.bucket, self.key)].remove(version)

        return Bucket()


def put(key: str, data: bytes = b'{"a":1}\n', **extra: Any) -> ReportingRowObjectPutInputV1:
    return ReportingRowObjectPutInputV1(
        LOCATION,
        key,
        data,
        "application/gzip",
        {"adcp-installation": "inst", "adcp-intent": "intent"},
        **extra,
    )


async def test_passes_the_shared_provider_conformance_suite() -> None:
    provider = create_gcs_reporting_row_object_provider_v1(storage=FakeStorage().add_bucket())
    passed = await run_reporting_row_object_provider_conformance_v1(
        provider, location=LOCATION, prefix="adcp-rows"
    )
    assert passed == [
        "probe",
        "create",
        "create-only",
        "ranged-read",
        "version-pinned-read",
        "exact-version-delete",
        "write-unique-version",
    ]


async def test_create_only_upload_sends_the_precondition_and_a_declared_checksum() -> None:
    storage = FakeStorage().add_bucket()
    provider = create_gcs_reporting_row_object_provider_v1(storage=storage)
    created = await provider.put_if_absent(put("adcp-rows/ns/a.jsonl.gz"), CONTEXT)
    assert created.created and created.native_version.isdigit()
    (_, options) = next(call for call in storage.calls if call[0] == "upload")
    assert options["if_generation_match"] == 0
    assert options["content_type"] == "application/gzip" and options["checksum"] == "md5"
    stored = storage.objects[("rows-bucket", "adcp-rows/ns/a.jsonl.gz")][0]
    assert stored.cache_control == "no-store"
    assert stored.metadata == {"adcp-installation": "inst", "adcp-intent": "intent"}


async def test_conflict_adopts_the_existing_generation_without_replacing_it() -> None:
    provider = create_gcs_reporting_row_object_provider_v1(storage=FakeStorage().add_bucket())
    first = await provider.put_if_absent(put("adcp-rows/ns/a.jsonl", b"first\n"), CONTEXT)
    second = await provider.put_if_absent(put("adcp-rows/ns/a.jsonl", b"second\n"), CONTEXT)
    assert (second.created, second.native_version) == (False, first.native_version)
    read = ReportingRowObjectGetInputV1(LOCATION, "adcp-rows/ns/a.jsonl", first.native_version, 100)
    assert await provider.get(read, CONTEXT) == b"first\n"


async def test_reads_pin_the_generation_request_raw_bytes_and_honour_ranges_and_the_cap() -> None:
    storage = FakeStorage().add_bucket()
    provider = create_gcs_reporting_row_object_provider_v1(storage=storage)
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
    (_, options) = [call for call in storage.calls if call[0] == "download"][-1]
    assert options["generation"] == int(created.native_version)
    assert options["raw_download"] is True and options["checksum"] is None
    assert (options["start"], options["end"]) == (4, 9)
    assert await read(max_bytes=100) == b"0123456789abcdef"
    assert await read(max_bytes=0, byte_range=(3, 0)) == b""
    with pytest.raises(ReportingRowStoreError) as capped:
        await read(max_bytes=4)
    assert capped.value.code == "ROWS_INTEGRITY_FAILED"
    with pytest.raises(ReportingRowStoreError) as past_end:
        await read(max_bytes=5, byte_range=(99, 5))
    assert past_end.value.code == "ROWS_INTEGRITY_FAILED"
    for stale in ("1", "not-a-generation", "0"):
        outcome = await provider.get(
            ReportingRowObjectGetInputV1(LOCATION, "adcp-rows/ns/r.jsonl", stale, 100), CONTEXT
        )
        assert outcome is None


async def test_delete_targets_the_exact_generation_and_is_idempotent() -> None:
    storage = FakeStorage().add_bucket()
    provider = create_gcs_reporting_row_object_provider_v1(storage=storage)
    key = "adcp-rows/ns/d.jsonl"
    first = await provider.put_if_absent(put(key, b"same\n"), CONTEXT)
    delete = ReportingRowObjectDeleteInputV1(LOCATION, key, first.native_version)
    assert await provider.delete(delete, CONTEXT) == "deleted"
    assert await provider.delete(delete, CONTEXT) == "absent"
    second = await provider.put_if_absent(put(key, b"same\n"), CONTEXT)
    assert second.native_version != first.native_version
    assert await provider.delete(delete, CONTEXT) == "absent"
    assert storage.objects[("rows-bucket", key)][0].generation == int(second.native_version)
    outcome = await provider.delete(ReportingRowObjectDeleteInputV1(LOCATION, key, "bad"), CONTEXT)
    assert outcome == "absent"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"uniform": False}, "uniform bucket-level access"),
        ({"public_access_prevention": "inherited"}, "public access prevention"),
        (
            {"lifecycle": [{"action": {"type": "Delete"}, "condition": {"age": 30}}]},
            "lifecycle rule",
        ),
        (
            {
                "lifecycle": [
                    {
                        "action": {"type": "Delete"},
                        "condition": {"age": 30, "matchesPrefix": ["adcp-rows/"]},
                    }
                ]
            },
            "lifecycle rule",
        ),
        (
            {
                "lifecycle": [
                    {
                        "action": {"type": "Delete"},
                        "condition": {"age": 30, "matchesPrefix": ["adcp-"]},
                    }
                ]
            },
            "lifecycle rule",
        ),
    ],
)
async def test_the_probe_refuses_unsafe_buckets(kwargs: dict[str, Any], message: str) -> None:
    provider = create_gcs_reporting_row_object_provider_v1(
        storage=FakeStorage().add_bucket(**kwargs)
    )
    with pytest.raises(ReportingRowStoreError) as failure:
        await provider.probe(LOCATION, "adcp-rows", CONTEXT)
    assert failure.value.code == "UNSAFE_BINDING" and message in str(failure.value)


async def test_the_probe_allows_noncurrent_and_unrelated_lifecycle_rules() -> None:
    allowed = [
        {"action": {"type": "Delete"}, "condition": {"isLive": False, "age": 30}},
        {"action": {"type": "Delete"}, "condition": {"numNewerVersions": 3}},
        {"action": {"type": "Delete"}, "condition": {"age": 30, "matchesPrefix": ["logs/"]}},
        {"action": {"type": "SetStorageClass"}, "condition": {"age": 30}},
    ]
    provider = create_gcs_reporting_row_object_provider_v1(
        storage=FakeStorage().add_bucket(lifecycle=allowed)
    )
    await provider.probe(LOCATION, "adcp-rows", CONTEXT)


async def test_the_probe_refuses_missing_and_inaccessible_buckets() -> None:
    storage = FakeStorage()
    provider = create_gcs_reporting_row_object_provider_v1(storage=storage)
    with pytest.raises(ReportingRowStoreError) as missing:
        await provider.probe(LOCATION, "adcp-rows", CONTEXT)
    assert missing.value.code == "UNSAFE_BINDING"

    class Forbidden(FakeStorage):
        def bucket(self, name: str) -> Any:
            bucket = super().bucket(name)

            def reload(timeout: Any = None) -> None:
                raise exceptions.Forbidden("secret-project-name")

            bucket.reload = reload
            return bucket

    forbidden = create_gcs_reporting_row_object_provider_v1(storage=Forbidden().add_bucket())
    with pytest.raises(ReportingRowStoreError) as denied:
        await forbidden.probe(LOCATION, "adcp-rows", CONTEXT)
    assert denied.value.code == "UNSAFE_BINDING" and "secret" not in str(denied.value)


def test_the_policy_check_is_a_pure_function() -> None:
    assert_gcs_reporting_row_bucket_policy_v1(
        uniform_bucket_level_access=True,
        public_access_prevention="enforced",
        lifecycle_rules=[],
        prefix="adcp-rows",
    )


async def test_credential_refs_select_from_a_closed_client_map() -> None:
    default = FakeStorage().add_bucket()
    tenant = FakeStorage().add_bucket()
    provider = create_gcs_reporting_row_object_provider_v1(
        storage=default, clients={"tenant": tenant}
    )
    await provider.put_if_absent(put("adcp-rows/ns/a.jsonl", credential_ref="tenant"), CONTEXT)
    assert ("rows-bucket", "adcp-rows/ns/a.jsonl") in tenant.objects
    assert not default.objects
    with pytest.raises(ReportingRowStoreError) as unknown:
        await provider.put_if_absent(
            put("adcp-rows/ns/b.jsonl", credential_ref="stranger"), CONTEXT
        )
    assert unknown.value.code == "UNSAFE_BINDING" and "stranger" not in str(unknown.value)


def test_locations_are_closed_and_options_validated() -> None:
    provider = create_gcs_reporting_row_object_provider_v1(storage=FakeStorage())
    provider.validate_location({"bucket": "rows-bucket"})
    for bad in ({}, {"bucket": "UPPER"}, {"bucket": "a..b"}, {"bucket": "ok-bucket", "path": "/x"}):
        with pytest.raises(ReportingRowStoreError) as failure:
            provider.validate_location(bad)
        assert failure.value.code == "INVALID_INPUT", bad
    for kwargs in ({}, {"storage": object()}, {"clients": {"bad ref": FakeStorage()}}):
        with pytest.raises(ReportingRowStoreError):
            create_gcs_reporting_row_object_provider_v1(**kwargs)


async def test_provider_errors_never_leak_their_messages() -> None:
    from adcp.reporting.ledger import ReportingObjectRowBindingV1, ReportingRowStorageOptionsV1
    from adcp.reporting.ledger.row_storage import ReportingRowStorageV1

    class Broken(FakeStorage):
        def bucket(self, name: str) -> Any:
            bucket = super().bucket(name)

            def blob(key: str, generation: int | None = None) -> Any:
                raise RuntimeError("gs://secret-bucket token=SECRET")

            bucket.blob = blob
            return bucket

    provider = create_gcs_reporting_row_object_provider_v1(storage=Broken().add_bucket())
    storage = ReportingRowStorageV1(
        ReportingRowStorageOptionsV1(
            bindings={
                "b": ReportingObjectRowBindingV1(
                    provider="gcs", location=LOCATION, prefix="adcp-rows"
                )
            },
            providers={"gcs": provider},
        )
    )
    with pytest.raises(ReportingRowStoreError) as failure:
        await storage._provider_call(  # the storage layer collapses provider failures
            provider.put_if_absent(put("adcp-rows/ns/a.jsonl"), CONTEXT), 5
        )
    assert failure.value.code == "PROVIDER_UNAVAILABLE"
    assert "SECRET" not in str(failure.value) and "secret" not in str(failure.value)


def test_the_official_client_accepts_every_argument_the_provider_sends() -> None:
    """Guards the provider against drifting from google-cloud-storage's real surface."""
    import inspect

    storage = pytest.importorskip("google.cloud.storage")
    parameters = lambda function: set(inspect.signature(function).parameters)  # noqa: E731
    assert {"if_generation_match", "content_type", "checksum"} <= parameters(
        storage.Blob.upload_from_string
    )
    assert {"start", "end", "raw_download", "checksum"} <= parameters(
        storage.Blob.download_as_bytes
    )
    assert "generation" in parameters(storage.Bucket.blob)
    for attribute in ("cache_control", "metadata", "generation", "size"):
        assert hasattr(storage.Blob, attribute)
    for attribute in ("iam_configuration", "lifecycle_rules", "reload", "get_blob"):
        assert hasattr(storage.Bucket, attribute)
    iam = storage.bucket.IAMConfiguration
    assert hasattr(iam, "uniform_bucket_level_access_enabled")
    assert hasattr(iam, "public_access_prevention")


async def test_probe_and_delete_carry_the_call_budget() -> None:
    """``wait_for`` cannot stop a worker thread, so every GCS call gets ``timeout=``."""
    seen: list[tuple[str, Any]] = []

    class Blob:
        def delete(self, timeout: Any = None) -> None:
            seen.append(("delete", timeout))

    class Bucket:
        iam_configuration = SimpleNamespace(
            uniform_bucket_level_access_enabled=True, public_access_prevention="enforced"
        )
        lifecycle_rules: list[dict[str, Any]] = []

        def reload(self, timeout: Any = None) -> None:
            seen.append(("reload", timeout))

        def blob(self, key: str, generation: int | None = None) -> Any:
            return Blob()

    class Storage:
        def bucket(self, name: str) -> Any:
            return Bucket()

    provider = create_gcs_reporting_row_object_provider_v1(storage=Storage())
    context = ReportingRowObjectContextV1(4.5)
    await provider.probe(LOCATION, "adcp-rows", context)
    await provider.delete(
        ReportingRowObjectDeleteInputV1(LOCATION, "adcp-rows/ns/x", "123"), context
    )
    assert seen == [("reload", 4.5), ("delete", 4.5)]
