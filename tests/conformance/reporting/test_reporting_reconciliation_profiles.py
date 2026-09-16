"""Every frozen method/profile cell and provider-native operational identities."""

from dataclasses import replace
from typing import get_args

import pytest

from adcp.reporting.ledger import (
    LedgerConflictError,
    ReportingDeliveryPrincipal,
    ReportingMaterializationView,
    ReportingPhysicalChecksum,
    receipt_to_wire,
)
from adcp.reporting.ledger.delivery_models import DeliveryMethod, VerificationProfile
from adcp.types import ReportingMaterialization, ReportingReceipt

from ._reconciliation_support import Clock, Store, scenario


@pytest.mark.parametrize("method", get_args(DeliveryMethod))
@pytest.mark.parametrize("profile", get_args(VerificationProfile))
@pytest.mark.parametrize("billing", [False, True])
async def test_frozen_method_profile_matrix(
    reconciliation_store: tuple[Store, Clock],
    method: DeliveryMethod,
    profile: VerificationProfile,
    billing: bool,
) -> None:
    store, _ = reconciliation_store
    supported = (profile != "manifest_checksums" or method == "file_transfer") and (
        not billing or profile == "canonical_digest"
    )
    if not supported:
        with pytest.raises(ValueError, match="manifest|billing"):
            await scenario(store, method=method, profile=profile, billing=billing)
        assert not (
            await store.read_reconciliation_changes(
                caller=ReportingDeliveryPrincipal("acct_a", "buyer")
            )
        ).changes
        return
    s = await scenario(store, method=method, profile=profile, billing=billing)
    await store.commit_materialization(s.outcome)
    accepted, _ = await store.record_revision_receipt(s.receipt)
    ReportingMaterialization.model_validate(
        (await store.get_materialization(s.attempt.key)).to_wire()
    )
    ReportingReceipt.model_validate(receipt_to_wire(accepted))


@pytest.mark.parametrize("method", get_args(DeliveryMethod))
async def test_native_commit_requires_native_immutability_even_with_matching_refs(
    reconciliation_store: tuple[Store, Clock], method: DeliveryMethod
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store, method=method, profile="native_commit", billing=False)
    wrong = replace(
        s.outcome, resource=replace(s.outcome.resource, immutability="immutable_location")
    )
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_materialization(wrong)
    assert error.value.code == "NATIVE_COMMIT_MISMATCH"
    assert (await store.get_materialization(s.attempt.key)).outcome is None
    assert (await store.commit_materialization(s.outcome))[1]


@pytest.mark.parametrize(
    "provider,method",
    [
        ("GAM", "file_transfer"),
        ("FreeWheel", "file_transfer"),
        ("Warehouse 欧州", "warehouse_materialization"),
    ],
)
async def test_generated_provider_identifiers_round_trip_without_rewriting(
    reconciliation_store: tuple[Store, Clock], provider: str, method: DeliveryMethod
) -> None:
    store, _ = reconciliation_store
    reader = "Parquet / 2.6 + decimal=38"
    s = await scenario(
        store, method=method, profile="native_commit", billing=False, reader_compatibility=(reader,)
    )
    location = f"{provider} / account=123 + daily report"
    native = f"/{provider} / run + version=2026-09-01=="
    object_ref = f"{provider} / day=2026-09-01/report + part=000.jsonl"
    commit = f"{provider} / consumer load + job=42=="
    wire = ReportingMaterializationView(s.attempt, s.binding, s.outcome, ()).to_wire()
    wire["resource"].update(location=location, native_version_ref=native)
    wire["verification"]["native_commit_evidence"]["native_version_ref"] = native
    if method == "file_transfer":
        wire["verification"]["physical_checksums"][0]["object_ref"] = object_ref
    generated = ReportingMaterialization.model_validate(wire)
    resource = replace(
        s.outcome.resource,
        location=generated.resource.location,
        native_version_ref=generated.resource.native_version_ref.root,
        reader_compatibility=tuple(item.root for item in generated.resource.reader_compatibility),
        object_refs=(object_ref,) if method == "file_transfer" else (),
    )
    verification = replace(
        s.outcome.verification,
        native_version_ref=generated.verification.native_commit_evidence.native_version_ref.root,
        physical_checksums=tuple(
            ReportingPhysicalChecksum(item.object_ref.root, item.algorithm, item.value)
            for item in (generated.verification.physical_checksums or [])
        ),
    )
    await store.commit_materialization(
        replace(s.outcome, resource=resource, verification=verification)
    )
    projected = ReportingMaterialization.model_validate(
        (await store.get_materialization(s.attempt.key)).to_wire()
    )
    assert projected.model_dump(mode="json") == generated.model_dump(mode="json")
    receipt_model = ReportingReceipt.model_validate(
        receipt_to_wire(
            replace(s.receipt, consumer_commit_ref=commit, observed_native_version_ref=native)
        )
    )
    receipt, _ = await store.record_revision_receipt(
        replace(
            s.receipt,
            consumer_commit_ref=receipt_model.consumer_commit_ref,
            observed_native_version_ref=receipt_model.observed_native_version_ref.root,
        )
    )
    assert ReportingReceipt.model_validate(receipt_to_wire(receipt)).model_dump(
        mode="json", exclude={"received_at"}
    ) == receipt_model.model_dump(mode="json", exclude={"received_at"})


@pytest.mark.parametrize(
    "field",
    [
        "location",
        "object_refs",
        "native_version_ref",
        "consumer_commit_ref",
        "reader_compatibility",
    ],
)
@pytest.mark.parametrize(
    "unsafe",
    [
        "Bearer DO_NOT_RETAIN",
        "password=DO_NOT_RETAIN",
        "https://provider.example/path",
        "reports?signature=DO_NOT_RETAIN",
        "user:DO_NOT_RETAIN@provider.example",
        "https%253A%252F%252Fprovider.example/path",
        "Bearer%2520DO_NOT_RETAIN",
        "private-key=DO_NOT_RETAIN",
        "load\nDO_NOT_RETAIN",
    ],
)
async def test_provider_fields_reject_credentials_and_urls_without_echo(
    reconciliation_store: tuple[Store, Clock], field: str, unsafe: str
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    with pytest.raises(ValueError) as error:
        if field == "consumer_commit_ref":
            replace(s.receipt, consumer_commit_ref=unsafe)
        else:
            replace(
                s.outcome.resource,
                **{
                    field: (unsafe,) if field in {"object_refs", "reader_compatibility"} else unsafe
                },
            )
    assert unsafe not in str(error.value) and "DO_NOT_RETAIN" not in str(error.value)


@pytest.mark.parametrize(
    "field",
    [
        "location",
        "object_refs",
        "native_version_ref",
        "consumer_commit_ref",
        "reader_compatibility",
    ],
)
@pytest.mark.parametrize(
    "signed",
    [
        "container/report.csv?sv=2024-11-04&sp=r&se=2027-01-01&sig=DO_NOT_RETAIN",
        "export.parquet?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=DO_NOT_RETAIN",
        "object?GoogleAccessId=DO_NOT_RETAIN&Expires=1893456000",
        "share&Signature=DO_NOT_RETAIN",
    ],
)
async def test_signed_query_material_is_refused_without_a_recognizable_keyword(
    reconciliation_store: tuple[Store, Clock], field: str, signed: str
) -> None:
    """Presigned/SAS query material must never persist, keyword or not."""
    store, _ = reconciliation_store
    s = await scenario(store)
    with pytest.raises(ValueError) as error:
        if field == "consumer_commit_ref":
            replace(s.receipt, consumer_commit_ref=signed)
        else:
            replace(
                s.outcome.resource,
                **{
                    field: (
                        (signed.replace("?", "-"),)
                        if field in {"object_refs", "reader_compatibility"}
                        else signed
                    )
                },
            )
    assert signed not in str(error.value) and "DO_NOT_RETAIN" not in str(error.value)


@pytest.mark.parametrize(
    "benign",
    [
        "warehouse/tokenized_inventory_daily/part-000.jsonl",
        "secretariat-report-v17/part-000.jsonl",
        "authorization_metrics_v2/part-000.jsonl",
        "credentials_review_2026/part-000.jsonl",
        "Parquet & decimal=38 / secretariat.jsonl",
    ],
)
async def test_operational_names_that_merely_contain_credential_words_survive(
    reconciliation_store: tuple[Store, Clock], benign: str
) -> None:
    """A keyword only condemns a value when it stands alone and introduces one."""
    store, _ = reconciliation_store
    s = await scenario(store)
    resource = replace(s.outcome.resource, location=benign, object_refs=(benign,))
    outcome = replace(
        s.outcome,
        resource=resource,
        verification=replace(
            s.outcome.verification,
            physical_checksums=(ReportingPhysicalChecksum(benign, "sha256", "d" * 64),),
        ),
    )
    stored, created = await store.commit_materialization(outcome)
    assert created and stored.resource is not None
    assert stored.resource.location == benign and stored.resource.object_refs == (benign,)
    view = await store.get_materialization(s.attempt.key)
    assert isinstance(view, ReportingMaterializationView)
    assert ReportingMaterialization.model_validate(view.to_wire()).resource.location == benign
