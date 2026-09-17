"""Hostile source/destination pagination, bounded streams and exact contract assets."""

import asyncio
import base64
import hashlib
import json
from dataclasses import replace

import pytest

from adcp.reporting.ledger import LedgerConflictError
from adcp.reporting.ledger.store import encode_cursor
from adcp.reporting.materializer import (
    ReportingDestinationIO,
    ReportingDestinationPage,
    ReportingRevisionVerifierRegistry,
    ReportingVerificationLimits,
    ReportingWriterCapability,
    ReportingWriterError,
    reference_verifier,
    strict_reporting_json,
)
from adcp.reporting.materializer.reference import _Session

from ._materializer_support import io_context, materializer_case
from ._reliable_support import reliable_factory


@pytest.mark.parametrize(
    "field,value",
    [
        ("row_count", True),
        ("row_count", -1),
        ("readable", 1),
        ("readable", 0),
        ("readable_at_commit", 1),
        ("revision_content_sha256", "private-invalid-body"),
    ],
)
async def test_foundation_count_flags_and_digest_are_strict_before_source_io(field, value):
    case = await materializer_case(1)

    class Reader:
        async def read_revision_rows(self, **kwargs):
            pytest.fail("invalid frozen metadata must fail before source I/O")

    with pytest.raises(ReportingWriterError, match="SOURCE_INVALID") as caught:
        await case.prepare(reader=Reader(), revisions=(replace(case.revision, **{field: value}),))
    assert "private-invalid-body" not in str(caught.value)
    assert case.writer.open_count == 0


@pytest.mark.parametrize(
    "damage",
    [
        "revision",
        "cursor-revision",
        "cursor-offset",
        "cursor-cycle",
        "cursor-duplicate-keys",
        "cursor-type",
        "total-change",
        "bool-total",
        "unpaired-cursor",
        "empty-more",
        "truncated",
        "extra",
        "second-page",
        "wrong-page-type",
    ],
)
async def test_source_walk_rejects_incomplete_or_substituted_revision_pages_before_destination_io(
    damage,
):
    case = await materializer_case(501)
    calls = 0

    class Reader:
        async def read_revision_rows(self, **kwargs):
            nonlocal calls
            calls += 1
            page = await case.store.read_revision_rows(**kwargs)
            if damage == "revision":
                return replace(page, reporting_revision_id="other-revision")
            if damage == "cursor-revision" and page.has_more:
                return replace(
                    page, cursor=encode_cursor({"revision": "other-revision", "offset": 500})
                )
            if damage == "cursor-offset" and page.has_more:
                return replace(
                    page,
                    cursor=encode_cursor(
                        {"revision": case.revision.reporting_revision_id, "offset": 0}
                    ),
                )
            if damage == "cursor-cycle":
                return replace(
                    page,
                    rows=(case.rows[0],),
                    has_more=True,
                    cursor=encode_cursor(
                        {"revision": case.revision.reporting_revision_id, "offset": 1}
                    ),
                )
            if damage == "cursor-duplicate-keys":
                raw = b'{"revision":"other","revision":"revision-first","offset":500}'
                return replace(page, cursor=base64.urlsafe_b64encode(raw).decode())
            if damage == "cursor-type":
                return replace(page, cursor=True)
            if damage == "total-change" and calls == 2:
                return replace(page, total_count=500)
            if damage == "bool-total":
                return replace(page, total_count=True)
            if damage == "unpaired-cursor":
                return replace(page, has_more=False)
            if damage == "empty-more":
                return replace(page, rows=())
            if damage == "truncated":
                return replace(page, has_more=False, cursor=None)
            if damage == "extra":
                return replace(page, rows=(*page.rows, case.rows[0]))
            if damage == "second-page" and calls == 2:
                return replace(page, rows=({**case.rows[-1], "impressions": True},))
            if damage == "wrong-page-type":
                return {"rows": page.rows, "provider": "private-response"}
            return page

    with pytest.raises(ReportingWriterError, match="SOURCE_INVALID"):
        await case.prepare(reader=Reader())
    assert case.writer.open_count == case.writer.write_effects == 0
    assert calls == (2 if damage in {"cursor-cycle", "total-change", "second-page"} else 1)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_real_store_cursor_binds_revision_and_all_501_rows(backend):
    async with reliable_factory(backend, notifications=True) as h:
        case = await materializer_case(501, store=h.store)
        first = await case.store.read_revision_rows(
            account_id="acct_a", reporting_revision_id=case.revision.reporting_revision_id
        )
        second = await case.store.read_revision_rows(
            account_id="acct_a",
            reporting_revision_id=case.revision.reporting_revision_id,
            cursor=first.cursor,
        )
        assert (
            first.reporting_revision_id
            == second.reporting_revision_id
            == case.revision.reporting_revision_id
        )
        assert len(first.rows) == 500 and len(second.rows) == 1
        assert first.total_count == second.total_count == 501 and not second.has_more
        for cursor in (
            encode_cursor({"revision": "another", "offset": 500}),
            encode_cursor({"revision": case.revision.reporting_revision_id, "offset": True}),
            encode_cursor({"revision": case.revision.reporting_revision_id, "offset": -1}),
        ):
            with pytest.raises(LedgerConflictError, match="does not bind"):
                await case.store.read_revision_rows(
                    account_id="acct_a",
                    reporting_revision_id=case.revision.reporting_revision_id,
                    cursor=cursor,
                )
        for limit in (True, 0, 501):
            with pytest.raises(LedgerConflictError, match="page size"):
                await case.store.read_revision_rows(
                    account_id="acct_a",
                    reporting_revision_id=case.revision.reporting_revision_id,
                    limit=limit,
                )


@pytest.mark.parametrize(
    "damage",
    ["replay", "cycle", "revision", "path", "format", "totals", "wrong-type", "empty", "extra"],
)
async def test_destination_pages_are_verifier_controlled(damage):
    case = await materializer_case(3)
    locator = await case.io.write(case.prepared, context=io_context())
    requests = []

    class Session(_Session):
        async def read_rows(self, locator, *, cursor, limit):
            requests.append(cursor)
            index = len(requests) - 1
            next_cursor = f"page-{index}" if index < 2 else None
            page = ReportingDestinationPage(
                case.revision.reporting_revision_id,
                (case.prepared.rows[index],),
                3,
                next_cursor is not None,
                next_cursor,
                "jsonl",
                "producer",
            )
            if damage == "replay":
                return replace(page, rows=(case.prepared.rows[0],))
            if damage == "cycle":
                return replace(page, has_more=True, cursor="repeat")
            if damage == "revision":
                return replace(page, reporting_revision_id="other")
            if damage == "path":
                return replace(page, verification_path="destination")
            if damage == "format":
                return replace(page, format="csv")
            if damage == "totals":
                return replace(page, total_count=2)
            if damage == "wrong-type":
                return object()
            if damage == "empty":
                return replace(page, rows=())
            if damage == "extra":
                return replace(page, rows=tuple(case.prepared.rows) + (case.prepared.rows[0],))
            return page

    class Resolver:
        def resolve(self, request, *, phase, context):
            return Session(case.resolver, request, phase, context)

    with pytest.raises(ReportingWriterError, match="DESTINATION_CORRUPT"):
        await ReportingDestinationIO(case.registry, Resolver()).verify(
            case.prepared, locator, context=io_context()
        )
    assert len(requests) <= 2 and case.writer.open_count == case.writer.close_count == 2


@pytest.mark.parametrize(
    "bound", ["max_rows", "max_pages", "max_total_bytes", "max_items", "max_chunks", "max_objects"]
)
async def test_walks_and_repeating_streams_have_finite_budgets(bound):
    case = await materializer_case(501 if bound in {"max_pages", "max_objects"} else 100)
    locator = await case.io.write(case.prepared, context=io_context())
    limits = replace(
        ReportingVerificationLimits(),
        **{bound: 1 if bound in {"max_rows", "max_pages", "max_chunks", "max_objects"} else 1000},
    )
    # Registry assets have their own bounded construction. Item/byte budgets
    # deliberately exceed the vectors but not the full source/destination.
    if bound in {"max_rows", "max_total_bytes", "max_items"}:
        limits = replace(limits, max_rows=2 if bound == "max_rows" else 100_000)
        case = await materializer_case(100)
        locator = await case.io.write(case.prepared, context=io_context())
    verifier = replace(case.verifier, limits=limits)
    registry = ReportingRevisionVerifierRegistry((verifier,))
    if bound in {"max_rows", "max_pages", "max_total_bytes", "max_items"}:
        with pytest.raises(ReportingWriterError, match="LIMIT_EXCEEDED"):
            await registry.prepare(
                key=verifier.key,
                binding=case.binding,
                delivery=case.delivery,
                obligation=case.obligation,
                revisions=(case.revision,),
                attempt=case.attempt,
                reader=case.store,
                context=io_context(),
            )
    else:
        with pytest.raises(ReportingWriterError, match="LIMIT_EXCEEDED|DESTINATION_CORRUPT"):
            await ReportingDestinationIO(registry, case.resolver).verify(
                case.prepared, locator, context=io_context()
            )


async def test_infinite_object_stream_is_bounded_and_closed_without_background_tasks():
    case = await materializer_case()
    locator = await case.io.write(case.prepared, context=io_context())
    verifier = replace(case.verifier, limits=ReportingVerificationLimits(max_chunks=3))
    registry = ReportingRevisionVerifierRegistry((verifier,))
    chunks, closed = 0, 0

    class Session(_Session):
        async def read_object(self, locator, *, object_ref):
            nonlocal chunks, closed
            try:
                while True:
                    chunks += 1
                    yield b" "
            finally:
                closed += 1

    class Resolver:
        def resolve(self, request, *, phase, context):
            return Session(case.resolver, request, phase, context)

    before = set(asyncio.all_tasks())
    with pytest.raises(ReportingWriterError, match="LIMIT_EXCEEDED"):
        await ReportingDestinationIO(registry, Resolver()).verify(
            case.prepared, locator, context=io_context()
        )
    assert chunks == 4 and closed == 1
    assert not (set(asyncio.all_tasks()) - before)
    assert case.writer.open_count == case.writer.close_count == 2


@pytest.mark.parametrize("damage", ["page-version", "final-version", "final-path"])
async def test_native_identity_is_pinned_across_pages_and_after_readback(damage):
    capability = ReportingWriterCapability(
        "dataset_share",
        "reference-memory",
        None,
        "canonical_digest",
        "representative_consumer",
        "native_version",
        "sha256",
        "conditional_create",
    )
    case = await materializer_case(501, capability=capability)
    locator = await case.io.write(case.prepared, context=io_context())
    observations = 0

    class Session(_Session):
        async def read_rows(self, locator, *, cursor, limit):
            result = await super().read_rows(locator, cursor=cursor, limit=limit)
            if cursor is not None and damage == "page-version":
                return replace(result, native_version_ref="different-version")
            return result

        async def observe_native_version(self, locator):
            nonlocal observations
            observations += 1
            result = await super().observe_native_version(locator)
            if observations == 2:
                return replace(
                    result,
                    **{
                        "native_version_ref" if damage == "final-version" else "location": "changed"
                    },
                )
            return result

    class Resolver:
        def resolve(self, request, *, phase, context):
            return Session(case.resolver, request, phase, context)

    with pytest.raises(ReportingWriterError, match="DESTINATION_CORRUPT"):
        await ReportingDestinationIO(case.registry, Resolver()).verify(
            case.prepared, locator, context=io_context()
        )
    assert observations == (1 if damage == "page-version" else 2)
    assert case.writer.open_count == case.writer.close_count == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "remote-ref",
        "nested-id",
        "missing-total",
        "extra-total",
        "expression",
        "golden-digest",
        "golden-bytes",
        "golden-member-order",
        "golden-row-order",
        "golden-duplicate-name",
        "canonical-algorithm",
    ],
)
def test_installed_contracts_are_executable_complete_closed_and_network_free(mutation, monkeypatch):
    verifier = reference_verifier()
    definition, schema, contract = (
        json.loads(raw)
        for raw in (
            verifier.definition_bytes,
            verifier.schema_bytes,
            verifier.canonicalization_bytes,
        )
    )
    if mutation == "remote-ref":
        schema["properties"]["details"] = {"$ref": "https://never-fetch.example.test/private"}
    elif mutation == "nested-id":
        schema["properties"]["details"] = {"$id": "https://never-fetch.example.test/private"}
    elif mutation == "missing-total":
        del schema["properties"]["spend"]["x-adcp-control-total"]
    elif mutation == "extra-total":
        schema["properties"]["extra"] = {
            "type": "integer",
            "x-adcp-control-total": {"value_type": "integer"},
        }
    elif mutation == "expression":
        definition["metrics"][0]["source_expression"] = "impressions * 2"
    elif mutation == "golden-digest":
        contract["golden_vectors"]["ordering_encoding"]["sha256"] = "0" * 64
    elif mutation == "golden-bytes":
        contract["golden_vectors"]["ordering_encoding"]["canonical_utf8_base64"] = "e30="
    elif mutation == "golden-member-order":
        contract["golden_vectors"]["ordering_encoding"]["input_rows"] = json.loads(
            strict_reporting_json(contract["golden_vectors"]["ordering_encoding"]["input_rows"])
        )
    elif mutation == "golden-row-order":
        contract["golden_vectors"]["ordering_encoding"]["input_rows"].reverse()
    elif mutation == "golden-duplicate-name":
        contract["golden_vectors"]["ordering_encoding"]["name"] = contract["golden_vectors"][
            "empty_report"
        ]["name"]
    else:
        contract["algorithm"] = "provider-assertions-v1"
    raw_schema = json.dumps(schema).encode()
    contract["schema_sha256"] = hashlib.sha256(raw_schema).hexdigest()
    raw_definition, raw_contract = json.dumps(definition).encode(), json.dumps(contract).encode()
    key = replace(
        verifier.key,
        definition=replace(
            verifier.key.definition,
            report_definition_sha256=hashlib.sha256(raw_definition).hexdigest(),
            schema_sha256=hashlib.sha256(raw_schema).hexdigest(),
        ),
        canonicalization=replace(
            verifier.key.canonicalization,
            canonicalization_sha256=hashlib.sha256(raw_contract).hexdigest(),
        ),
    )
    import socket

    def network(*args, **kwargs):
        pytest.fail("canonicalizer attempted network fallback")

    monkeypatch.setattr(socket, "create_connection", network)
    with pytest.raises(ReportingWriterError, match="UNSUPPORTED_VERIFICATION"):
        replace(
            verifier,
            key=key,
            schema_bytes=raw_schema,
            definition_bytes=raw_definition,
            canonicalization_bytes=raw_contract,
        )
