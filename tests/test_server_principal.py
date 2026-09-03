from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from adcp.server import InMemoryPrincipalRecordStore, PrincipalIdentity, PrincipalService

IDENTITY = PrincipalIdentity("https://buyer.example/agent", "buyer_agent")
DESTINATION = {
    "pattern": "file_transfer",
    "destination_id": "archive",
    "active": True,
    "provider": {"domain": "object-store.example"},
    "transport": "s3",
    "location": "s3://buyer-reporting/adcp/",
    "accepted_formats": ["parquet"],
    "accepted_verification_profiles": ["manifest_checksums"],
}


def _request(key: str, configuration, **extra):
    return {
        "idempotency_key": key,
        "configuration": configuration,
        **extra,
    }


@pytest.mark.asyncio
async def test_recognized_and_unconfigured_read_variants() -> None:
    service = PrincipalService()
    assert (await service.get_principal(IDENTITY)).result.kind == "unconfigured"

    principal_id = await service.recognize(IDENTITY)
    response = await service.get_principal(IDENTITY)

    assert response.result.kind == "recognized"
    assert response.result.principal_id == principal_id


@pytest.mark.asyncio
async def test_sync_persists_state_negotiation_and_exact_replay() -> None:
    service = PrincipalService(
        accepted_async_adcp_versions=("3.2",),
        accepted_experimental_features=("protocol.principal",),
    )
    request = _request(
        "principal-server-key-0001",
        {
            "reporting_destinations": [DESTINATION],
            "declarations": {
                "async_adcp_versions": ["3.2", "3.1"],
                "experimental_features": ["protocol.principal", "future.feature"],
            },
        },
        context={"correlation_id": "principal--apply"},
    )

    first = await service.sync_principal(IDENTITY, request)
    replay_request = dict(request, context={"correlation_id": "principal--replay"})
    replay = await service.sync_principal(IDENTITY, replay_request)
    current = await service.get_principal(
        IDENTITY, {"context": {"correlation_id": "principal--read"}}
    )

    assert first.model_dump(mode="json", exclude={"replayed"}) == replay.model_dump(
        mode="json", exclude={"replayed"}
    )
    assert first.replayed is False
    assert replay.replayed is True
    assert first.result.kind == "applied"
    assert first.context.correlation_id == "principal--apply"
    assert current.context.correlation_id == "principal--read"
    assert current.result.kind == "current"
    destination = current.result.configuration.reporting_destinations[0]
    assert destination.state == "validating"
    declarations = current.result.configuration.declarations
    assert declarations.selected_async_adcp_version == "3.2"
    assert [item.value for item in declarations.exclusions] == ["3.1", "future.feature"]


@pytest.mark.asyncio
async def test_version_fence_rejects_stale_replacement() -> None:
    service = PrincipalService()
    await service.sync_principal(
        IDENTITY, _request("principal-server-key-0002", {"declarations": {}})
    )

    response = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0003",
            {"declarations": {}},
            expected_configuration_version="stale",
        ),
    )

    assert response.result.kind == "failed"
    assert response.result.errors[0].code == "CONFLICT"


@pytest.mark.asyncio
async def test_dry_run_does_not_call_proof_hook_or_persist() -> None:
    calls = 0

    async def proof(identity, desired, previous):
        nonlocal calls
        calls += 1
        raise AssertionError("dry run must not issue proof")

    service = PrincipalService(destination_proof=proof)
    response = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0004",
            {"reporting_destinations": [DESTINATION]},
            dry_run=True,
        ),
    )

    assert response.result.kind == "validated"
    assert calls == 0
    assert (await service.get_principal(IDENTITY)).result.kind == "unconfigured"


@pytest.mark.asyncio
async def test_destination_transition_emits_invalidation_without_version_churn() -> None:
    changes = []

    async def emit(change):
        changes.append(change)

    service = PrincipalService(emit_change=emit)
    applied = await service.sync_principal(
        IDENTITY,
        _request("principal-server-key-0005", {"reporting_destinations": [DESTINATION]}),
    )
    version = applied.result.configuration_version

    transitioned = await service.transition_destination(IDENTITY, "archive", "ready")
    current = await service.get_principal(IDENTITY)

    assert transitioned.state == "ready"
    assert current.result.configuration_version == version
    assert changes[0].reason == "destination_state_changed"
    assert changes[0].destination_id == "archive"


@pytest.mark.asyncio
async def test_suspension_preserves_generation_and_revocation_retires_it() -> None:
    service = PrincipalService()
    first = await service.sync_principal(
        IDENTITY,
        _request("principal-server-key-0006", {"reporting_destinations": [DESTINATION]}),
    )
    original = first.result.configuration.reporting_destinations[0]
    suspended = dict(DESTINATION, active=False)

    second = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0007",
            {"reporting_destinations": [suspended]},
            expected_configuration_version=first.result.configuration_version,
        ),
    )
    inactive = second.result.configuration.reporting_destinations[0]
    assert inactive.destination_ref == original.destination_ref
    assert inactive.state == "inactive"

    third = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0008",
            {"reporting_destinations": []},
            expected_configuration_version=second.result.configuration_version,
        ),
    )
    assert third.result.configuration.reporting_destinations == []
    retired = third.result.configuration.retired_destinations[0]
    assert retired.destination_id == "archive"
    assert retired.destination_refs[0].root == original.destination_ref


@pytest.mark.asyncio
async def test_active_notification_requires_proof_hook() -> None:
    service = PrincipalService()
    response = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0009",
            {
                "notification_configs": [
                    {
                        "subscriber_id": "events",
                        "url": "https://buyer.example/webhooks",
                        "event_types": ["principal.changed"],
                        "active": True,
                    }
                ]
            },
        ),
    )

    assert response.result.kind == "failed"
    assert "notification_proof" in response.result.errors[0].message


@pytest.mark.asyncio
async def test_seller_declaration_change_emits_without_version_churn() -> None:
    changes = []

    async def emit(change):
        changes.append(change)

    service = PrincipalService(emit_change=emit, accepted_async_adcp_versions=("3.2", "3.1"))
    applied = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0010",
            {"declarations": {"async_adcp_versions": ["3.2", "3.1"]}},
        ),
    )
    version = applied.result.configuration_version
    service.set_declaration_support(async_adcp_versions=("3.2",))

    declarations = await service.refresh_declarations(IDENTITY)
    current = await service.get_principal(IDENTITY)

    assert [item.root for item in declarations.accepted.async_adcp_versions] == ["3.2"]
    assert current.result.configuration_version == version
    assert changes[0].reason == "declarations_intersection_changed"


@pytest.mark.asyncio
async def test_rejects_duplicate_logical_keys_before_state_is_persisted() -> None:
    service = PrincipalService()
    destination = dict(DESTINATION, active=False)
    response = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0011",
            {"reporting_destinations": [destination, destination]},
        ),
    )
    assert response.result.kind == "failed"
    assert "duplicates" in response.result.errors[0].message

    response = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0012",
            {
                "notification_configs": [
                    {
                        "subscriber_id": "duplicate",
                        "url": "https://127.0.0.1/hook",
                        "event_types": ["principal.changed"],
                        "active": False,
                    }
                ]
                * 2
            },
        ),
    )
    assert response.result.kind == "failed"
    assert "duplicates" in response.result.errors[0].message


@pytest.mark.asyncio
async def test_inactive_config_still_enforces_webhook_and_destination_security() -> None:
    service = PrincipalService()
    bad_webhook = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0013",
            {
                "notification_configs": [
                    {
                        "subscriber_id": "events",
                        "url": "https://127.0.0.1/hook",
                        "event_types": ["principal.changed"],
                        "active": False,
                    }
                ]
            },
        ),
    )
    assert bad_webhook.result.kind == "failed"
    assert "SSRF" in bad_webhook.result.errors[0].message

    secret_webhook = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0013a",
            {
                "notification_configs": [
                    {
                        "subscriber_id": "events",
                        "url": "https://buyer.example/hook?token=not-a-credential",
                        "event_types": ["principal.changed"],
                        "active": False,
                    }
                ]
            },
        ),
    )
    assert secret_webhook.result.kind == "failed"
    assert "credential or secret" in secret_webhook.result.errors[0].message

    bad_destination = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0014",
            {
                "reporting_destinations": [
                    dict(DESTINATION, active=False, location="s3://key:password@bucket/path")
                ]
            },
        ),
    )
    assert bad_destination.result.kind == "failed"
    assert "credentials" in bad_destination.result.errors[0].message

    userinfo_destination = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0014a",
            {
                "reporting_destinations": [
                    dict(DESTINATION, active=False, location="s3://access-key@bucket/path")
                ]
            },
        ),
    )
    assert userinfo_destination.result.kind == "failed"
    assert "userinfo credentials" in userinfo_destination.result.errors[0].message


@pytest.mark.asyncio
async def test_rejects_invalid_notification_event_scope_and_missing_acknowledgment() -> None:
    service = PrincipalService()
    for key, event_type, expected in (
        ("principal-server-key-0015", "scheduled", "media-buy-anchored"),
        ("principal-server-key-0016", "creative.status_changed", "all_authorized_accounts"),
    ):
        response = await service.sync_principal(
            IDENTITY,
            _request(
                key,
                {
                    "notification_configs": [
                        {
                            "subscriber_id": "events",
                            "url": "https://127.0.0.1/hook",
                            "event_types": [event_type],
                            "active": False,
                        }
                    ]
                },
            ),
        )
        assert response.result.kind == "failed"
        assert expected in response.result.errors[0].message


@pytest.mark.asyncio
async def test_unsupported_declarations_persist_empty_intersection_and_exclusions() -> None:
    service = PrincipalService()
    response = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0017",
            {"declarations": {"async_adcp_versions": ["3.1"]}},
        ),
    )

    assert response.result.kind == "applied"
    declarations = response.result.configuration.declarations
    assert declarations.accepted.async_adcp_versions is None
    assert declarations.exclusions[0].value == "3.1"


@pytest.mark.asyncio
async def test_active_notifications_need_an_accepted_signing_algorithm(monkeypatch) -> None:
    def valid_url(url, *, field):
        return SimpleNamespace(effective_url=url, hostname="buyer.example")

    monkeypatch.setattr("adcp.server.principal.validate_webhook_destination_url", valid_url)

    async def prove(identity, config):
        from adcp.server.principal import _notification_state

        return _notification_state(config)

    service = PrincipalService(notification_proof=prove)
    response = await service.sync_principal(
        IDENTITY,
        _request(
            "principal-server-key-0018",
            {
                "notification_configs": [
                    {
                        "subscriber_id": "events",
                        "url": "https://buyer.example/hook",
                        "event_types": ["principal.changed"],
                    }
                ]
            },
        ),
    )
    assert response.result.kind == "failed"
    assert response.result.errors[0].code == "UNSUPPORTED_FEATURE"


class _RacingStore:
    def __init__(self) -> None:
        self._store = InMemoryPrincipalRecordStore()
        self._race = False
        self._readers = 0
        self._gate = asyncio.Event()

    async def get(self, subject):
        record = await self._store.get(subject)
        if self._race and record is not None:
            self._readers += 1
            if self._readers == 2:
                self._gate.set()
            await self._gate.wait()
        return record

    async def compare_and_swap(self, subject, expected_store_revision, record):
        return await self._store.compare_and_swap(subject, expected_store_revision, record)


@pytest.mark.asyncio
async def test_shared_store_cas_rejects_one_of_two_concurrent_fenced_writes() -> None:
    store = _RacingStore()
    first = PrincipalService(store, accepted_async_adcp_versions=("3.1", "3.2"))
    second = PrincipalService(store, accepted_async_adcp_versions=("3.1", "3.2"))
    initial = await first.sync_principal(
        IDENTITY, _request("principal-server-key-0019", {"declarations": {}})
    )
    store._race = True
    version = initial.result.configuration_version
    one, two = await asyncio.gather(
        first.sync_principal(
            IDENTITY,
            _request(
                "principal-server-key-0020",
                {"declarations": {"async_adcp_versions": ["3.1"]}},
                expected_configuration_version=version,
            ),
        ),
        second.sync_principal(
            IDENTITY,
            _request(
                "principal-server-key-0021",
                {"declarations": {"async_adcp_versions": ["3.2"]}},
                expected_configuration_version=version,
            ),
        ),
    )
    assert {one.result.kind, two.result.kind} == {"applied", "failed"}
    failed = next(item for item in (one, two) if item.result.kind == "failed")
    assert failed.result.errors[0].code == "CONFLICT"
