"""Credential-free trusted namespaces and monotonic issue scope ownership."""

import base64
from dataclasses import replace
from urllib.parse import quote

import pytest

from adcp.decisioning.context import AuthInfo
from adcp.decisioning.registry import BuyerAgent, HttpSigCredential
from adcp.reporting.ledger import (
    PgReportingReconciliationStore,
    ReportingDeliveryPrincipal,
    ReportingIssueLifecycle,
)
from adcp.reporting.outbox import (
    ReportingNotificationError,
    ReportingStatusScope,
    resolve_reporting_consumer,
)
from adcp.reporting.outbox.identity import canonical_consumer

from . import _reconciliation_support as _reconciliation
from . import test_reporting_status_projection_contract as _contract
from ._generation_support import NOW
from ._reconciliation_support import scenario
from .test_reporting_notification_outbox import statement

status_harness = _contract.status_harness
reconciliation_store = _reconciliation.reconciliation_store
BUYER = "https://buyer.example.test/agents/buyer"
AUDITOR = "https://buyer.example.test/agents/auditor"
JOSE_HEADER = base64.urlsafe_b64encode(b'{\n "alg": "HS256"}').decode().rstrip("=")
CREDENTIAL_SHAPES = (
    "eyJhbGciOiJIUzI1NiJ9.e30.signature",
    JOSE_HEADER + ".e30.signature",
    "AKIA" + "A" * 16,
    "ASIA" + "B" * 16,
    "consumer token=synthetic-test-value",
    "Basic " + base64.b64encode(b"test-user:synthetic-password").decode(),
    "ghp_" + "x" * 36,
    "github_pat_" + "x" * 40,
    "xoxb-" + "1" * 24,
    "sk_live_" + "a" * 24,
    "https://buyer.example.test/agents/buyer?session=synthetic",
    "https://user:password@buyer.example.test/agents/buyer",
    "buyer?opaque_parameter=synthetic",
    " ".join(("-----BEGIN", "PRIVATE", "KEY-----")),  # A header shape without key material.
)


@pytest.mark.parametrize(
    "value", CREDENTIAL_SHAPES, ids=[f"credential-shape-{i}" for i in range(len(CREDENTIAL_SHAPES))]
)
def test_raw_and_repeated_percent_decoded_credentials_have_secret_free_errors(value):
    for raw in (value, quote(value, safe=""), quote(quote(value, safe=""), safe="")):
        with pytest.raises(ReportingNotificationError) as caught:
            canonical_consumer(raw)
        assert caught.value.args == ("activity_identity_required",)
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        with pytest.raises(ValueError) as scope_error:
            ReportingStatusScope("acct_a", consumer_id=raw)
        assert str(scope_error.value) == "reporting metadata requires non-secret public text"
        with pytest.raises(ValueError) as lifecycle_error:
            ReportingIssueLifecycle("opaque", "issue-public", "acct_a", NOW, consumer_id=raw)
        assert str(lifecycle_error.value) == "reporting metadata requires non-secret public text"
        with pytest.raises(ValueError, match="non-secret public text"):
            ReportingDeliveryPrincipal("acct_a", raw)


@pytest.mark.parametrize(
    "principal",
    [
        BUYER,
        AUDITOR,
        "warehouse/tenant/consumer",
        "tokenized_inventory_daily",
        "secretariat-report-v17",
    ],
)
def test_canonical_urls_and_public_principals_preserve_byte_identity(principal):
    assert canonical_consumer(principal) == principal
    assert (
        resolve_reporting_consumer(auth_info=AuthInfo(kind="bearer", principal=principal))
        == principal
    )
    assert ReportingDeliveryPrincipal("acct_a", principal).consumer_id == principal


def test_all_trusted_auth_coordinates_must_agree_without_choosing_body_identity():
    agent = BuyerAgent(agent_url=BUYER, display_name="Buyer", status="active")
    credential = HttpSigCredential(
        kind="http_sig", keyid="public-key-1", agent_url=BUYER, verified_at=1
    )
    info = AuthInfo(kind="http_sig", principal=BUYER, agent_url=BUYER, credential=credential)
    assert resolve_reporting_consumer(auth_info=info, agent=agent) == BUYER
    for changed in (
        AuthInfo(kind="bearer", principal=AUDITOR),
        AuthInfo(
            kind="http_sig", principal=BUYER, credential=replace(credential, agent_url=AUDITOR)
        ),
    ):
        with pytest.raises(ReportingNotificationError) as caught:
            resolve_reporting_consumer(auth_info=changed, agent=agent)
        assert caught.value.args == ("activity_identity_conflict",)
        assert caught.value.__context__ is None


async def test_status_record_rejects_credential_namespace_before_any_durable_write(status_harness):
    h = status_harness
    obligation, _, _ = await h.seed()
    for consumer in CREDENTIAL_SHAPES:
        with pytest.raises(ValueError, match="non-secret public text"):
            record = replace(statement(obligation), consumer_id=consumer)
            await h.ledger.record_consumer_status_with_lifecycle(record)
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    assert not snapshot.statuses and not snapshot.lifecycles


async def test_issue_refinement_never_broadens_or_retargets_across_recurrence(status_harness):
    h = status_harness
    obligation, _, _ = await h.seed()
    base = ReportingStatusScope("acct_a", consumer_id=BUYER)
    config = replace(
        base, generation_key=obligation.generation_key, feed_purpose=obligation.feed_purpose
    )
    precise = replace(config, reporting_obligation_id=obligation.reporting_obligation_id)
    arguments = dict(
        issue_key="opaque-condition", account_id="acct_a", consumer_id=BUYER, observed_at=h.clock()
    )
    first = await h.ledger.ensure_issue_opened(**arguments, status_scope=base)
    status_operation_1 = await h.ledger.ensure_issue_opened(**arguments, status_scope=config)
    assert status_operation_1 == first
    status_operation_2 = await h.ledger.ensure_issue_opened(**arguments, status_scope=precise)
    assert status_operation_2 == first
    for invalid in (
        base,
        config,
        replace(precise, consumer_id=AUDITOR),
        replace(precise, reporting_obligation_id="rpo_unavailable"),
        replace(precise, feed_purpose="billing"),
    ):
        with pytest.raises(ReportingNotificationError, match="invalid_status_scope"):
            await h.ledger.ensure_issue_opened(**arguments, status_scope=invalid)
    await h.ledger.retire_issue(account_id="acct_a", issue_key="opaque-condition", at=h.clock())
    with pytest.raises(ReportingNotificationError, match="invalid_status_scope"):
        await h.ledger.ensure_issue_opened(**arguments, status_scope=base)
    with pytest.raises(ReportingNotificationError, match="invalid_status_scope"):
        await h.ledger.ensure_issue_opened(**{**arguments, "consumer_id": AUDITOR})
    again = await h.ledger.ensure_issue_opened(**arguments)
    assert again.issue_id != first.issue_id and again.generation == first.generation + 1
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    assert dict(snapshot.issue_scopes)[again.issue_id] == precise


async def test_url_principals_roundtrip_reconciliation_with_colliding_record_ids(
    reconciliation_store,
):
    store, clock = reconciliation_store
    scenarios = [await scenario(store, consumer_id=consumer) for consumer in (BUYER, AUDITOR)]
    for i, item in enumerate(scenarios):
        await store.commit_materialization(item.outcome)
        receipt, recorded = await store.record_revision_receipt(item.receipt)
        assert recorded and receipt.scope == item.receipt.scope and receipt.key == item.receipt.key
        scenarios[i] = replace(item, receipt=receipt)
    # The same materialization/receipt IDs must stay in separate canonical URL
    # namespaces throughout storage, decoding, paging and restart/replay.
    pages = []
    for item in scenarios:
        assert item.binding.principal == ReportingDeliveryPrincipal(
            "acct_a", item.binding.consumer_id
        )
        snapshot = await store.read_reconciliation_snapshot(caller=item.binding.principal)
        assert len(snapshot.records) == 5
        from adcp.reporting.ledger._delivery_state import principal

        assert all(principal(record) == item.binding.principal for record in snapshot.records)
        page = await store.read_reconciliation_changes(caller=item.binding.principal, limit=2)
        assert page.has_more and page.cursor is not None
        pages.append(page)
    if isinstance(store, PgReportingReconciliationStore):
        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(
            store._pool.conninfo, kwargs=store._pool.kwargs, open=False
        ) as restarted_pool:
            restarted = PgReportingReconciliationStore(pool=restarted_pool, clock=clock)
            for item, page in zip(scenarios, pages):
                assert await restarted.get_receipt(item.receipt.key) == item.receipt
                status_operation_4 = await restarted.record_revision_receipt(item.receipt)
                assert status_operation_4 == (
                    item.receipt,
                    False,
                )
                cursor, changes = page.cursor, []
                while cursor is not None:
                    remaining = await restarted.read_reconciliation_changes(
                        caller=item.binding.principal, cursor=cursor
                    )
                    changes.extend(remaining.changes)
                    cursor = remaining.cursor
                assert len(changes) == 3
    else:
        for item in scenarios:
            assert await store.get_receipt(item.receipt.key) == item.receipt
            status_operation_3 = await store.record_revision_receipt(item.receipt)
            assert status_operation_3 == (item.receipt, False)
