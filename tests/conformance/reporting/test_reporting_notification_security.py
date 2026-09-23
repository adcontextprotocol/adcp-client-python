"""Authenticated routing, secret rejection, and the real SDK-owned transport."""

from __future__ import annotations

import logging
import socket
from dataclasses import asdict, fields, replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import InMemoryReportingLedgerStore
from adcp.reporting.outbox import (
    DeliveryBinding,
    ReportingLegacyAuthentication,
    ReportingNotificationError,
    validate_notification_payload,
)
from adcp.signing.jwks import SSRFValidationError
from adcp.webhook_auth import JwkSignerStrategy
from adcp.webhook_sender import PreparedWebhook, WebhookSender

from ._generation_support import revision_for
from ._reliable_support import notification_subscription
from .test_reporting_notification_outbox import seed


async def test_process_receiver_fixture_verifies_both_rotation_keys():
    from adcp.signing.jwks import StaticJwksResolver
    from adcp.signing.webhook_signer import sign_webhook
    from adcp.signing.webhook_verifier import WebhookVerifyOptions, verify_webhook_signature

    from ._reliable_support import (
        FailurePlan,
        ManualClock,
        ScriptedSigning,
        notification_verification_keys,
    )

    clock, signing = ManualClock(), ScriptedSigning(FailurePlan())
    options = WebhookVerifyOptions(
        jwks_resolver=StaticJwksResolver({"keys": notification_verification_keys()}),
        clock=lambda: clock().timestamp(),
    )
    url = notification_subscription().url
    body = b'{"idempotency_key":"receiver-fixture-self-check"}'
    for generation in (1, 2):
        signing.generation = generation
        material = await signing.resolve(
            account_id="acct_a", principal_id="buyer", signing_scope_id="scope"
        )
        headers = {"Content-Type": "application/json"}
        headers.update(
            sign_webhook(
                method="POST",
                url=url,
                headers=headers,
                body=body,
                private_key=material.private_key,
                key_id=material.key_id,
                alg=material.algorithm,
                created=int(clock().timestamp()),
            ).as_dict()
        )
        verified = verify_webhook_signature(
            method="POST", url=url, headers=headers, body=body, options=options
        )
        assert verified.key_id == material.key_id and verified.alg == "ed25519"
        clock.advance(timedelta(seconds=61))


async def replace_stored(h, old, changed):
    if isinstance(h.reliable.store, InMemoryReportingLedgerStore):
        state = h.reliable.store._notification_state
        _, work = state.deliveries.pop(
            (old.binding.account_id, old.binding.consumer_namespace, old.binding.delivery_id)
        )
        state.deliveries[
            (
                changed.binding.account_id,
                changed.binding.consumer_namespace,
                changed.binding.delivery_id,
            )
        ] = (
            changed,
            work,
        )
        return
    from psycopg import sql

    previous, updated = asdict(old.binding), asdict(changed.binding)
    differences = {name: value for name, value in updated.items() if previous[name] != value}
    if old.envelope != changed.envelope:
        differences["envelope"] = changed.envelope
    async with h.reliable.blobs.pool.connection() as conn:
        if changed.binding.account_id != old.binding.account_id:
            # Supply a valid destination-side parent so the account AAD check,
            # rather than an FK alone, is forced to defend the swapped row.
            await conn.execute(
                "INSERT INTO reporting_notification_events"
                " SELECT %s, notification_id, notification_type, cause_kind, cause_id,"
                " cause_generation, consumer_namespace, fired_at,"
                " jsonb_set(snapshot, '{account_id}', to_jsonb(%s::text))"
                " FROM reporting_notification_events"
                " WHERE account_id = %s AND notification_id = %s",
                (
                    changed.binding.account_id,
                    changed.binding.account_id,
                    old.binding.account_id,
                    old.binding.notification_id,
                ),
            )
            await conn.execute(
                "INSERT INTO reporting_notification_expansions"
                " (account_id, notification_id, emission_generation, due_at)"
                " SELECT %s, notification_id, emission_generation, due_at"
                " FROM reporting_notification_expansions"
                " WHERE account_id = %s AND notification_id = %s",
                (changed.binding.account_id, old.binding.account_id, old.binding.notification_id),
            )
        if changed.binding.consumer_namespace != old.binding.consumer_namespace:
            # A malicious database writer can also supply a namespace parent.
            # Authentication must still reject the single-column delivery swap.
            await conn.execute(
                "INSERT INTO reporting_notification_events"
                " SELECT account_id, notification_id,"
                " 'reporting.delivery_ready', 'materialization_ready',"
                " cause_id, cause_generation, %s, fired_at, snapshot"
                " FROM reporting_notification_events WHERE account_id = %s"
                " AND consumer_namespace = %s AND notification_id = %s",
                (
                    changed.binding.consumer_namespace,
                    old.binding.account_id,
                    old.binding.consumer_namespace,
                    old.binding.notification_id,
                ),
            )
            await conn.execute(
                "INSERT INTO reporting_notification_expansions"
                " (account_id, consumer_namespace, notification_id, emission_generation, due_at)"
                " SELECT account_id, %s, notification_id, emission_generation, due_at"
                " FROM reporting_notification_expansions WHERE account_id = %s"
                " AND consumer_namespace = %s AND notification_id = %s",
                (
                    changed.binding.consumer_namespace,
                    old.binding.account_id,
                    old.binding.consumer_namespace,
                    old.binding.notification_id,
                ),
            )
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(name)) for name in differences
        )
        await conn.execute(
            sql.SQL(
                "UPDATE reporting_notification_deliveries SET {}"
                " WHERE account_id = %s AND delivery_id = %s"
            ).format(assignments),
            (*differences.values(), old.binding.account_id, old.binding.delivery_id),
        )


@pytest.mark.parametrize("column", [item.name for item in fields(DeliveryBinding)])
async def test_every_bound_column_swap_fails_before_any_external_effect(
    notification_harness, column
):
    h = notification_harness
    obligation, _, _ = await seed(h)
    await h.worker().expand_one(account_id="acct_a")
    (row,) = await h.outbox.list_deliveries(account_id="acct_a")
    original = row.delivery
    current = getattr(original.binding, column)
    value = current + 1 if isinstance(current, int) else "swapped-value"
    if column == "notification_id":
        revision, rows = revision_for(obligation, suffix="another")
        await h.reliable.store.commit_revision(revision, rows)
        value = next(
            event.notification_id
            for event in await h.outbox.list_events(account_id="acct_a")
            if event.notification_id != original.binding.notification_id
        )
    if column == "emission_generation":
        value = await h.outbox.reemit(
            account_id="acct_a",
            notification_id=original.binding.notification_id,
            now=h.reliable.clock(),
        )
    changed = replace(original, binding=replace(original.binding, **{column: value}))
    await replace_stored(h, original, changed)
    status_operation_1 = await h.worker().deliver_one(account_id=changed.binding.account_id)
    assert status_operation_1
    (retained,) = await h.outbox.list_deliveries(account_id=changed.binding.account_id)
    assert retained.state == "quarantined" and retained.error_code == "integrity_failure"
    assert not h.subscriptions.gets and not h.signing.calls
    assert not h.receiver.connections and not h.receiver.dns_calls


@pytest.mark.parametrize("attack", ["ciphertext", "nonce", "truncated", "poison_json"])
async def test_poison_and_envelope_transplants_quarantine_without_starving_later_work(
    notification_harness, attack
):
    h = notification_harness
    await seed(h)
    h.subscriptions.put(notification_subscription(subscriber="healthy"))
    await h.worker().expand_one(account_id="acct_a")
    rows = await h.outbox.list_deliveries(account_id="acct_a")
    target = next(row.delivery for row in rows if row.delivery.binding.subscriber_id == "buyer")
    donor = next(row.delivery for row in rows if row.delivery.binding.subscriber_id == "healthy")
    envelope = {
        "ciphertext": donor.envelope,
        "nonce": donor.envelope[:12] + target.envelope[12:],
        "truncated": b"broken",
        "poison_json": b"not-an-encrypted-envelope",
    }[attack]
    await replace_stored(h, target, replace(target, envelope=envelope))
    await h.drain()
    states = {
        row.delivery.binding.subscriber_id: row.state
        for row in await h.outbox.list_deliveries(account_id="acct_a")
    }
    assert states == {"buyer": "quarantined", "healthy": "complete"}
    assert [item.subscriber_id for item in h.receiver.received] == ["healthy"]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fd00::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
    ],
)
async def test_ssrf_matrix_on_worker_owned_pinned_transport(
    notification_harness, monkeypatch, address
):
    h = notification_harness
    await seed(h)
    h.receiver.dns_addresses["receiver.example.test"] = [address]
    signed = []
    original = JwkSignerStrategy.build_auth_headers

    def sign(self, **kwargs):
        signed.append(True)
        return original(self, **kwargs)

    monkeypatch.setattr(JwkSignerStrategy, "build_auth_headers", sign)
    await h.drain()
    assert not signed and not h.receiver.connections
    assert (await h.outbox.list_deliveries(account_id="acct_a"))[0].state == "quarantined"


async def test_dns_rebinding_is_pinned_and_revalidation_blocks_the_next_attempt(
    notification_harness,
):
    h = notification_harness
    await seed(h)
    h.receiver.dns_addresses["receiver.example.test"] = ["8.8.8.8", "169.254.169.254"]
    h.receiver.responses["buyer"].append(503)
    await h.drain()
    assert h.receiver.dns_calls == ["receiver.example.test"]
    assert h.receiver.connections == [("8.8.8.8", 443)]
    h.reliable.clock.advance(timedelta(seconds=6))
    await h.drain()
    assert len(h.receiver.dns_calls) == 2
    assert len(h.receiver.connections) == 1
    assert (await h.outbox.list_deliveries(account_id="acct_a"))[0].state == "quarantined"


async def test_mixed_public_private_dns_answers_fail_closed(notification_harness, monkeypatch):
    h = notification_harness
    await seed(h)

    def mixed(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))
            for ip in ("8.8.8.8", "10.0.0.1")
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed)
    await h.drain()
    assert not h.receiver.connections
    assert (await h.outbox.list_deliveries(account_id="acct_a"))[0].state == "quarantined"


async def test_transient_dns_failure_retries_without_secret_diagnostics(
    notification_harness, monkeypatch
):
    h = notification_harness
    await seed(h)
    resolve = socket.getaddrinfo

    def fail(host, port, *args, **kwargs):
        if str(host).endswith(".example.test"):
            raise socket.gaierror("dns provider token=SECRET")
        return resolve(host, port, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(socket, "getaddrinfo", fail)
        await h.drain()
    (row,) = await h.outbox.list_deliveries(account_id="acct_a")
    assert row.state == "pending" and row.error_code == "network"
    assert "SECRET" not in repr(row)
    h.reliable.clock.advance(timedelta(seconds=6))
    await h.drain()
    assert len(h.receiver.received) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://receiver.example.test:80/",
        "https://receiver.example.test:8443/",
        "http://receiver.example.test/",
        "https://user:secret@receiver.example.test/",
        "https://receiver.example.test/#fragment",
    ],
)
def test_unsafe_registration_ports_schemes_userinfo_and_fragments_are_rejected(url):
    with pytest.raises(ReportingNotificationError):
        notification_subscription(url=url)


async def test_disallowed_port_is_also_enforced_by_owned_prepared_transport(notification_harness):
    h = notification_harness
    await seed(h)
    (event,) = await h.outbox.list_events(account_id="acct_a")
    material = await h.signing.resolve(
        account_id="acct_a", principal_id="buyer", signing_scope_id="scope"
    )
    sender = WebhookSender(
        private_key=material.private_key,
        key_id=material.key_id,
        alg=material.algorithm,
        allowed_destination_ports=frozenset({443}),
    )
    try:
        prepared = PreparedWebhook(
            "https://receiver.example.test:8443/",
            "0" * 32,
            event.body(subscriber_id="buyer", idempotency_key="0" * 32),
        )
        with pytest.raises(SSRFValidationError):
            await sender.send_prepared(prepared)
    finally:
        await sender.aclose()
    assert h.receiver.connections == []


@pytest.mark.parametrize("scheme", ["Bearer", "HMAC-SHA256"])
async def test_legacy_credentials_encrypted_modes_exclusive_and_logs_sanitized(
    notification_harness, caplog, scheme
):
    h = notification_harness
    await seed(h)
    h.subscriptions.put(
        notification_subscription(
            signing_scope_id=None,
            authentication=ReportingLegacyAuthentication(scheme, "LEGACY_CREDENTIAL_SECRET"),
        )
    )
    caplog.set_level(logging.DEBUG)
    await h.drain()
    (row,) = await h.outbox.list_deliveries(account_id="acct_a")
    assert row.state == "complete"
    assert not h.signing.calls
    assert "signature" not in h.receiver.received[0].headers
    assert ("authorization" in h.receiver.received[0].headers) == (scheme == "Bearer")
    rendered = caplog.text + repr(row)
    assert all(
        secret not in rendered
        for secret in ("LEGACY_CREDENTIAL_SECRET", "URL_SECRET", "DO_NOT_PERSIST")
    )
    assert b"URL_SECRET" not in row.delivery.envelope
    assert b"LEGACY_CREDENTIAL_SECRET" not in row.delivery.envelope
    with pytest.raises(ReportingNotificationError):
        notification_subscription(authentication=ReportingLegacyAuthentication(scheme, "secret"))


async def test_rfc_logs_never_retain_url_headers_signatures_or_provider_text(
    notification_harness, caplog
):
    h = notification_harness
    await seed(h)
    caplog.set_level(logging.DEBUG)
    await h.drain()
    assert len(h.receiver.received) == 1
    receipt = h.receiver.received[0]
    assert "URL_SECRET" not in caplog.text and "DO_NOT_PERSIST" not in caplog.text
    assert receipt.headers["signature"] not in caplog.text
    assert receipt.headers["signature-input"] not in caplog.text
    # Other concurrent traffic retains its normal logging behavior.
    logging.getLogger("httpx").info("unrelated-safe-application-log")
    assert "unrelated-safe-application-log" in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        "https://object.example.test/?token=secret",
        "token=secret",
        "Bearer secret",
        "eyJhbGciOiJ9.eyJzdWIiOiJ9.signature",
        "https%3A%2F%2Fsecret.example",
    ],
)
async def test_recursive_payload_secret_scan_rejects_even_schema_legal_account_strings(
    notification_harness, value
):
    h = notification_harness
    await seed(h)
    (event,) = await h.outbox.list_events(account_id="acct_a")
    import json

    body = json.loads(event.body(subscriber_id="buyer", idempotency_key="0" * 32))
    body["account_id"] = value
    with pytest.raises(ReportingNotificationError):
        validate_notification_payload(body)
