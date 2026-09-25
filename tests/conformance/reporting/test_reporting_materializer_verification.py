"""Actual bytes, whole walks, typed totals and exact immutable destination paths."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import product

import pytest

from adcp.reporting.ledger import ReportingDeliveryPrincipal, revision_content_sha256
from adcp.reporting.materializer import (
    ReferenceReportingDestinationWriter,
    ReferenceReportingResolver,
    ReportingDestinationIO,
    ReportingRevisionVerifierRegistry,
    ReportingVerificationLimits,
    ReportingWriterCapability,
    ReportingWriterError,
    parse_reporting_json,
    reference_verifier,
    strict_reporting_json,
    validate_materialization_target,
)
from adcp.reporting.outbox import InMemoryReportingOutbox

from ._materializer_support import io_context, materializer_case

CAPABILITIES = [
    ReportingWriterCapability(
        method, "reference-memory", fmt, profile, path, immutable, "sha256", "conditional_create"
    )
    for method, fmt, path, immutable, profiles in (
        (
            "file_transfer",
            "jsonl",
            "producer",
            "immutable_location",
            ("canonical_digest", "manifest_checksums"),
        ),
        (
            "dataset_share",
            None,
            "representative_consumer",
            "native_version",
            ("canonical_digest", "native_commit"),
        ),
        (
            "warehouse_materialization",
            None,
            "destination",
            "native_version",
            ("canonical_digest", "native_commit"),
        ),
    )
    for profile in profiles
]


@pytest.mark.parametrize("count,capability", tuple(product((0, 1, 501), CAPABILITIES)))
async def test_entire_source_and_destination_walk_in_every_supported_profile(count, capability):
    case = await materializer_case(count, capability=capability)
    locator = await case.io.write(case.prepared, context=io_context())
    case.resolver.rotate()
    result = await case.io.verify(case.prepared, locator, context=io_context())
    assert result.verification.row_count == count
    assert result.verification.control_totals == case.revision.managed_control_totals
    assert result.request.principal == ReportingDeliveryPrincipal(
        "acct_a", "https://buyer.example.test/agents/reporting"
    )
    assert result.verification.verification_path == capability.verification_path
    destination_operation_1 = await case.io.write(case.prepared, context=io_context())
    assert destination_operation_1 == locator
    assert case.writer.write_effects == 1 and case.writer.open_count == case.writer.close_count == 3
    assert case.writer.production_eligible is False
    assert (await case.store.get_materialization(case.attempt.key)).outcome is None
    assert len(await InMemoryReportingOutbox(case.store).list_events(account_id="acct_a")) >= 1
    assert all(
        e.notification_type != "reporting.delivery_ready"
        for e in await InMemoryReportingOutbox(case.store).list_events(account_id="acct_a")
    )


@pytest.mark.parametrize(
    "damage",
    [
        "rows",
        "row-order",
        "row-missing",
        "row-extra",
        "object",
        "object-utf8",
        "object-missing",
        "object-extra",
        "object-order",
        "manifest",
        "manifest-schema",
        "manifest-count",
        "manifest-total-type",
        "manifest-total-unit",
        "manifest-total-missing",
        "manifest-period",
        "manifest-object-checksum",
        "manifest-object-count",
        "native-version",
        "native-path",
    ],
)
async def test_writer_locators_cannot_prove_corrupt_or_changed_destination(damage):
    cap = CAPABILITIES[2] if damage.startswith("native") else CAPABILITIES[0]
    case = await materializer_case(501, capability=cap)
    locator = await case.io.write(case.prepared, context=io_context())
    artifact = case.writer._artifacts[locator.external_id]
    if damage == "rows":
        row = json.loads(artifact.rows[0])
        row["details"]["active"] = 1
        artifact = replace(artifact, rows=(strict_reporting_json(row), *artifact.rows[1:]))
    elif damage == "row-order":
        artifact = replace(artifact, rows=artifact.rows[::-1])
    elif damage == "row-missing":
        artifact = replace(artifact, rows=artifact.rows[:-1])
    elif damage == "row-extra":
        artifact = replace(artifact, rows=(*artifact.rows, artifact.rows[0]))
    elif damage in {"object", "object-utf8"}:
        artifact = replace(
            artifact,
            objects=(
                (artifact.objects[0][0], b"\xff\n" if damage == "object-utf8" else b"{}\n"),
                *artifact.objects[1:],
            ),
        )
    elif damage == "object-missing":
        artifact = replace(artifact, objects=artifact.objects[:-1])
    elif damage == "object-extra":
        artifact = replace(artifact, objects=(*artifact.objects, ("extra.jsonl", b"{}\n")))
    elif damage == "object-order":
        artifact = replace(artifact, objects=artifact.objects[::-1])
    elif damage.startswith("manifest"):
        manifest = json.loads(artifact.manifest)
        if damage == "manifest":
            artifact = replace(artifact, manifest=artifact.manifest + b" ")
        else:
            if damage == "manifest-schema":
                manifest["unexpected"] = "provider prose"
            if damage == "manifest-count":
                manifest["row_count"] = True
            if damage == "manifest-total-type":
                manifest["control_totals"][0]["value_type"] = "decimal"
            if damage == "manifest-total-unit":
                manifest["control_totals"][1]["unit"] = "EUR"
            if damage == "manifest-total-missing":
                manifest["control_totals"].pop()
            if damage == "manifest-period":
                manifest["period"]["end"] = manifest["period"]["start"]
            if damage == "manifest-object-checksum":
                manifest["files"][0]["sha256"] = "0" * 64
            if damage == "manifest-object-count":
                manifest["files"][0]["row_count"] = 249
            raw = strict_reporting_json(manifest)
            locator = replace(
                locator,
                resource=replace(locator.resource, manifest_sha256=hashlib.sha256(raw).hexdigest()),
            )
            artifact = replace(artifact, manifest=raw)
    elif damage == "native-version":
        artifact = replace(
            artifact,
            locator=replace(
                locator, resource=replace(locator.resource, native_version_ref="changed-version")
            ),
        )
    elif damage == "native-path":
        artifact = replace(
            artifact,
            locator=replace(
                locator, resource=replace(locator.resource, location="different/table")
            ),
        )
    case.writer._artifacts[locator.external_id] = artifact
    with pytest.raises(ReportingWriterError):
        await case.io.verify(case.prepared, locator, context=io_context())
    assert case.writer.open_count == case.writer.close_count == 2
    assert (await case.store.get_materialization(case.attempt.key)).outcome is None


@pytest.mark.parametrize(
    "value",
    [
        1.0,
        float("nan"),
        float("inf"),
        Decimal("1"),
        datetime(2026, 1, 1),
        b"abc",
        {1},
        (1,),
        {1: "value"},
        2**53,
        "\ud800",
    ],
)
def test_exact_recursive_json_rejects_non_json_values(value):
    with pytest.raises(ReportingWriterError):
        strict_reporting_json({"nested": [value]})


@pytest.mark.parametrize("base,value", [(int, 1), (str, "x"), (dict, {}), (list, [])])
def test_json_subclasses_are_never_coerced(base, value):
    subclass = type("NotExact", (base,), {})
    with pytest.raises(ReportingWriterError):
        strict_reporting_json({"value": subclass(value)})


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":{"x":true,"x":1}}',
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"1.0",
        b'"\xff"',
        b'"\\udfff"',
    ],
)
def test_parser_rejects_ambiguous_or_invalid_bytes(raw):
    with pytest.raises(ReportingWriterError) as caught:
        parse_reporting_json(raw)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_type_distinction_unicode_and_bounded_json():
    assert strict_reporting_json(True) != strict_reporting_json(1)
    assert strict_reporting_json("é") != strict_reporting_json("e\u0301")
    assert strict_reporting_json({"\ue000": 1, "😀": 2}) == '{"😀":2,"\ue000":1}'.encode()
    for value, limits in (
        ([[[[]]]], ReportingVerificationLimits(max_depth=2)),
        ({"large": "x" * 30}, ReportingVerificationLimits(max_value_bytes=20)),
        ([1, 2, 3], ReportingVerificationLimits(max_items=2)),
    ):
        with pytest.raises(ReportingWriterError):
            strict_reporting_json(value, limits)
    with pytest.raises(ReportingWriterError):
        parse_reporting_json(b"[" * 1000 + b"]" * 1000)


@pytest.mark.parametrize("value", ['😀é\x00\n\\"', [True, False, None, -1], {"nested": [1, 2]}])
def test_exact_utf8_and_json_escape_budget(value):
    encoded = strict_reporting_json(value)
    assert (
        strict_reporting_json(value, ReportingVerificationLimits(max_value_bytes=len(encoded)))
        == encoded
    )
    with pytest.raises(ReportingWriterError, match="LIMIT_EXCEEDED"):
        strict_reporting_json(value, ReportingVerificationLimits(max_value_bytes=len(encoded) - 1))


def test_aggregate_byte_budget_rejects_before_allocating_canonical_output(monkeypatch):
    import adcp.reporting.materializer._json as boundary

    def forbidden(value):
        pytest.fail("oversized values must fail before canonical output allocation")

    monkeypatch.setattr(boundary, "canonical_json_utf8_v1", forbidden)
    with pytest.raises(ReportingWriterError, match="LIMIT_EXCEEDED"):
        boundary.strict_reporting_json(
            ["x" * 1000] * 1000, ReportingVerificationLimits(max_value_bytes=2000)
        )


@pytest.mark.parametrize(
    "part",
    [
        "definition_id",
        "profile",
        "definition_uri",
        "definition_hash",
        "schema_version",
        "schema_uri",
        "schema_hash",
        "dialect",
        "ref_policy",
        "canonical_id",
        "canonical_uri",
        "canonical_hash",
        "method",
        "transport",
        "format",
        "verification_profile",
        "path",
        "immutability",
        "write_semantics",
    ],
)
async def test_registry_key_is_the_complete_frozen_tuple(part):
    case = await materializer_case()
    key = case.verifier.key
    if part in {"definition_id", "profile"}:
        key = replace(
            key,
            **{
                (
                    "report_definition_id" if part == "definition_id" else "reporting_profile"
                ): "different"
            },
        )
    elif part.startswith("canonical"):
        field = {
            "canonical_id": "canonicalization_id",
            "canonical_uri": "canonicalization_uri",
            "canonical_hash": "canonicalization_sha256",
        }[part]
        key = replace(
            key,
            canonicalization=replace(
                key.canonicalization,
                **{
                    field: (
                        "0" * 64
                        if part.endswith("hash")
                        else (
                            "https://different.example.test/contract"
                            if part.endswith("uri")
                            else "different"
                        )
                    )
                },
            ),
        )
    elif part in {
        "method",
        "transport",
        "format",
        "verification_profile",
        "path",
        "immutability",
        "write_semantics",
    }:
        changes = {
            "method": "warehouse_materialization",
            "transport": "other",
            "format": "csv",
            "verification_profile": "manifest_checksums",
            "verification_path": "destination",
            "immutability": "native_version",
            "write_semantics": "idempotent",
        }
        field = "verification_path" if part == "path" else part
        values = {field: changes[field]}
        if part in {"method", "immutability"}:
            values["verification_path"] = "destination"
        key = replace(key, capability=replace(key.capability, **values))
    else:
        field = {
            "definition_uri": "report_definition_uri",
            "definition_hash": "report_definition_sha256",
            "schema_hash": "schema_sha256",
            "dialect": "schema_dialect",
            "ref_policy": "schema_ref_policy",
        }.get(part, part)
        value = (
            "0" * 64
            if part.endswith("hash")
            else (
                "https://different.example.test/schema"
                if part.endswith("uri") or part == "dialect"
                else "other"
            )
        )
        key = replace(key, definition=replace(key.definition, **{field: value}))
    with pytest.raises(ReportingWriterError, match="UNSUPPORTED_VERIFICATION"):
        await case.prepare(key=key)
    assert case.writer.open_count == 0


async def test_uppercase_digest_evidence_is_semantically_equal_and_output_is_lowercase():
    case = await materializer_case()
    revision = replace(
        case.revision,
        canonical_content_digest=replace(
            case.revision.canonical_content_digest,
            value=case.revision.canonical_content_digest.value.upper(),
            canonicalization_sha256=case.revision.canonical_content_digest.canonicalization_sha256.upper(),
        ),
    )
    prepared = await case.prepare(revisions=(revision,))
    locator = await case.io.write(prepared, context=io_context())
    verified = await case.io.verify(prepared, locator, context=io_context())
    assert (
        verified.verification.canonical_content_digest.value
        == case.revision.canonical_content_digest.value
    )


@pytest.mark.parametrize("claim", ["canonical", "core", "total-value", "total-missing"])
async def test_validly_shaped_ledger_claims_are_recomputed_before_resolver_io(claim):
    case = await materializer_case(501)
    if claim == "canonical":
        revision = replace(
            case.revision,
            canonical_content_digest=replace(
                case.revision.canonical_content_digest, value="a" * 64
            ),
        )
    elif claim == "core":
        revision = replace(case.revision, revision_content_sha256="b" * 64)
    else:
        totals = case.revision.managed_control_totals
        totals = (
            (replace(totals[0], value="0"), *totals[1:]) if claim == "total-value" else totals[:-1]
        )
        pairs = tuple((total.name, total.value) for total in totals)
        revision = replace(
            case.revision,
            managed_control_totals=totals,
            control_totals=pairs,
            revision_content_sha256=revision_content_sha256(
                reporting_revision_id=case.revision.reporting_revision_id,
                row_count=case.revision.row_count,
                control_totals=pairs,
                reporting_rows=case.rows,
                control_total_evidence=totals,
            ),
        )
    with pytest.raises(ReportingWriterError, match="SOURCE_INVALID"):
        await case.prepare(revisions=(revision,))
    assert case.writer.open_count == case.writer.write_effects == 0


async def test_rotation_revocation_is_checked_again_before_readback():
    case = await materializer_case()
    locator = await case.io.write(case.prepared, context=io_context())
    case.resolver.revoke(case.binding.principal)
    with pytest.raises(ReportingWriterError, match="AUTHORIZATION_DENIED"):
        await case.io.verify(case.prepared, locator, context=io_context())
    assert case.writer.open_count == case.writer.close_count == 2


async def test_binding_and_tenant_collisions_do_not_share_effect_identity_or_resolution():
    first, second = await materializer_case(account="acct_a"), await materializer_case(
        account="acct_b"
    )
    assert first.prepared.request.external_id != second.prepared.request.external_id
    wrong = ReportingDestinationIO(
        first.registry, ReferenceReportingResolver(first.writer, first.registry, (second.binding,))
    )
    with pytest.raises(ReportingWriterError, match="AUTHORIZATION_DENIED"):
        await wrong.write(first.prepared, context=io_context())
    assert first.writer.write_effects == 0
    revised = replace(first.prepared.request, reporting_revision_id="different")
    assert revised.external_id != first.prepared.request.external_id  # Equal attempt 1 is isolated.


@pytest.mark.parametrize("coordinate", ["account", "consumer"])
async def test_colliding_public_ids_in_one_destination_never_cross_principals(coordinate):
    first = await materializer_case()
    second = await materializer_case(
        **{
            coordinate: (
                "acct_b" if coordinate == "account" else "https://another.example.test/agent"
            )
        }
    )
    resolver = ReferenceReportingResolver(
        first.writer, first.registry, (first.binding, second.binding)
    )
    io = ReportingDestinationIO(first.registry, resolver)
    left = await io.write(first.prepared, context=io_context())
    right = await io.write(second.prepared, context=io_context())
    assert (
        left.external_id != right.external_id and left.resource.location != right.resource.location
    )
    assert first.writer.write_effects == 2
    with pytest.raises(ReportingWriterError, match="BINDING_MISMATCH"):
        await io.verify(first.prepared, right, context=io_context())
    assert first.writer.open_count == first.writer.close_count == 2
    for case, locator in ((first, left), (second, right)):
        result = await io.verify(case.prepared, locator, context=io_context())
        assert result.request.principal == case.binding.principal


async def test_registry_and_prepared_bytes_are_immutable_and_target_reselection_fails_closed():
    case = await materializer_case()
    with pytest.raises(FrozenInstanceError):
        case.registry.verifiers = ()
    assert all(type(r) is bytes for r in case.prepared.rows)
    official = replace(
        case.revision,
        reporting_revision_id="official",
        finality="official",
        finality_basis="source_final",
        finality_policy_id="reference-final",
        finalized_at=case.revision.created_at,
    )
    with pytest.raises(ReportingWriterError, match="CURRENT_REVISION_CHANGED"):
        validate_materialization_target(
            case.prepared, binding=case.binding, revisions=(case.revision, official)
        )
    with pytest.raises(ReportingWriterError, match="HISTORY_CORRUPT"):
        validate_materialization_target(
            case.prepared, binding=case.binding, revisions=(case.revision, case.revision)
        )
    assert case.writer.write_effects == 0


@pytest.mark.parametrize(
    "path",
    [
        "../private",
        "/private",
        "a//private",
        "a/./private",
        "a/%2e%2e/private",
        "a\\private",
        "C:/private",
        "C:private",
    ],
)
async def test_object_locator_traversal_fails_before_readback_authorization(path):
    case = await materializer_case()
    locator = await case.io.write(case.prepared, context=io_context())
    with pytest.raises((ReportingWriterError, ValueError)):
        locator = replace(locator, resource=replace(locator.resource, object_refs=(path,)))
        await case.io.verify(case.prepared, locator, context=io_context())
    assert case.writer.open_count == case.writer.close_count == 1


@pytest.mark.parametrize("count", [0, 501])
async def test_snapshot_to_official_retains_both_histories_and_separate_attempt_one_identity(count):
    case = await materializer_case(count)
    snapshot_locator = await case.io.write(case.prepared, context=io_context())
    official = replace(
        case.revision,
        reporting_revision_id="revision-official",
        finality="official",
        finality_basis="source_final",
        finality_policy_id="reference-final",
        finalized_at=case.revision.created_at,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id="revision-official",
            row_count=count,
            control_totals=case.revision.control_totals,
            reporting_rows=case.rows,
            control_total_evidence=case.revision.managed_control_totals,
        ),
    )
    await case.store.commit_revision(official, case.rows)
    history = await case.store.list_revisions(
        account_id=case.obligation.account_id,
        reporting_obligation_id=case.obligation.reporting_obligation_id,
    )
    assert len(history) == 2 and official.supersedes_reporting_revision_id is None
    with pytest.raises(ReportingWriterError, match="CURRENT_REVISION_CHANGED"):
        validate_materialization_target(case.prepared, binding=case.binding, revisions=history)
    attempt = replace(
        case.attempt,
        reporting_revision_id=official.reporting_revision_id,
        reporting_materialization_id="materialization-official",
        created_at=case.attempt.created_at + timedelta(seconds=1),
    )
    await case.store.commit_materialization_attempt(attempt)
    prepared = await case.prepare(revisions=history, attempt=attempt)
    locator = await case.io.write(prepared, context=io_context())
    verified = await case.io.verify(prepared, locator, context=io_context())
    assert verified.request.reporting_revision_id == official.reporting_revision_id
    assert attempt.attempt == case.attempt.attempt == 1
    assert locator.external_id != snapshot_locator.external_id
    assert locator.resource.location != snapshot_locator.resource.location
    assert case.writer.write_effects == 2
    assert (await case.store.get_materialization(case.attempt.key)).outcome is None
    assert (await case.store.get_materialization(attempt.key)).outcome is None


async def test_official_required_preparation_never_falls_back_to_a_snapshot():
    case = await materializer_case(finality="official")
    snapshot = replace(
        case.revision,
        reporting_revision_id="snapshot-only",
        finality="snapshot",
        finality_basis=None,
        finality_policy_id=None,
        finalized_at=None,
    )
    with pytest.raises(ReportingWriterError, match="REVISION_NOT_READY"):
        await case.prepare(revisions=(snapshot,))
    assert case.writer.open_count == 0
    locator = await case.io.write(case.prepared, context=io_context())
    result = await case.io.verify(case.prepared, locator, context=io_context())
    assert result.request.reporting_revision_id == case.revision.reporting_revision_id


def test_reference_writer_has_no_production_config_override():
    writer = ReferenceReportingDestinationWriter(())
    with pytest.raises(AttributeError):
        writer.production_eligible = True
    with pytest.raises(TypeError):
        ReferenceReportingDestinationWriter((), production_eligible=True)
    with pytest.raises(ReportingWriterError):
        ReportingRevisionVerifierRegistry((reference_verifier(), reference_verifier()))
