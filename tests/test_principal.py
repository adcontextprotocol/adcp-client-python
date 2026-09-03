from __future__ import annotations

from collections.abc import Iterable

import pytest

from adcp.principal import PrincipalConfigurationError, PrincipalManager
from adcp.types import (
    GetPrincipalResponse,
    PrincipalConfiguration,
    PrincipalCurrentResult,
    PrincipalState,
    SyncPrincipalRequest,
    SyncPrincipalResponse,
)
from adcp.types.core import TaskResult, TaskStatus

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


def _state(destination_state: str = "ready", destination_ref: str = "dest-1") -> dict[str, object]:
    return {
        "reporting_destinations": [
            {
                "destination_id": "archive",
                "destination_ref": destination_ref,
                "state": destination_state,
                "configuration": DESTINATION,
                **(
                    {"setup": {"action": "grant_access", "setup_url": "https://setup.example/"}}
                    if destination_state == "action_required"
                    else {}
                ),
            }
        ],
        "declarations": {
            "declared": {"async_adcp_versions": ["3.2", "3.1"]},
            "accepted": {"async_adcp_versions": ["3.2"]},
            "selected_async_adcp_version": "3.2",
            "exclusions": [
                {
                    "axis": "async_adcp_versions",
                    "value": "3.1",
                    "reason": "unsupported by this seller",
                }
            ],
        },
    }


def _current(
    destination_state: str = "ready", destination_ref: str = "dest-1"
) -> GetPrincipalResponse:
    return GetPrincipalResponse.model_validate(
        {
            "result": {
                "kind": "current",
                "principal_id": "principal-1",
                "principal_kind": "buyer_agent",
                "configuration_version": "version-7",
                "configuration": _state(destination_state, destination_ref),
            }
        }
    )


def _applied(destination_state: str = "ready") -> SyncPrincipalResponse:
    return SyncPrincipalResponse.model_validate(
        {
            "result": {
                "kind": "applied",
                "action": "updated",
                "dry_run": False,
                "principal_id": "principal-1",
                "principal_kind": "buyer_agent",
                "configuration_version": "version-8",
                "configuration": _state(destination_state),
            }
        }
    )


def _completed(data):
    return TaskResult(status=TaskStatus.COMPLETED, data=data)


class _Client:
    def __init__(
        self,
        reads: Iterable[GetPrincipalResponse],
        sync_response: SyncPrincipalResponse | None = None,
    ) -> None:
        self.reads = iter(reads)
        self.sync_response = sync_response or _applied()
        self.sync_requests: list[SyncPrincipalRequest] = []

    async def get_principal(self, request):
        return _completed(next(self.reads))

    async def sync_principal(self, request: SyncPrincipalRequest):
        self.sync_requests.append(request)
        return _completed(self.sync_response)


@pytest.mark.asyncio
async def test_sync_bootstraps_version_and_projects_negotiation() -> None:
    client = _Client([_current()])

    outcome = await PrincipalManager(client).sync(
        {
            "reporting_destinations": [DESTINATION],
            "declarations": {"async_adcp_versions": ["3.2", "3.1"]},
        },
        idempotency_key="principal-sync-key-0001",
    )

    request = client.sync_requests[0]
    assert request.expected_configuration_version == "version-7"
    assert request.expected_principal_kind == "buyer_agent"
    assert outcome.configuration_version == "version-8"
    assert outcome.destinations.ready == ("archive",)
    assert outcome.selected_async_adcp_version == "3.2"
    assert outcome.declarations is not None
    assert outcome.declarations.exclusions[0].value == "3.1"


@pytest.mark.asyncio
async def test_sync_can_disable_version_fence() -> None:
    client = _Client([])

    await PrincipalManager(client).sync(
        {"declarations": {}},
        idempotency_key="principal-sync-key-0002",
        use_version_fence=False,
    )

    request = client.sync_requests[0]
    assert request.expected_configuration_version is None
    assert request.expected_principal_kind is None


@pytest.mark.asyncio
async def test_sync_polls_validating_destination_to_action_required() -> None:
    client = _Client(
        [_current(), _current("validating"), _current("action_required")],
        _applied("validating"),
    )

    outcome = await PrincipalManager(client).sync(
        {"reporting_destinations": [DESTINATION]},
        idempotency_key="principal-sync-key-0003",
        wait_for_setup=True,
        poll_interval=0.001,
    )

    assert outcome.destinations.settled
    assert outcome.destinations.action_required == ("archive",)
    state = outcome.destinations.states["archive"]
    assert str(state.setup.setup_url) == "https://setup.example/"


@pytest.mark.asyncio
async def test_wait_for_destinations_rejects_missing_readback() -> None:
    response = GetPrincipalResponse.model_validate(
        {
            "result": {
                "kind": "current",
                "principal_id": "principal-1",
                "principal_kind": "buyer_agent",
                "configuration_version": "version-7",
                "configuration": {},
            }
        }
    )

    with pytest.raises(PrincipalConfigurationError) as error:
        await PrincipalManager(_Client([response])).wait_for_destinations(
            {"archive"}, poll_interval=0.001
        )

    assert error.value.code == "DESTINATION_STATE_MISSING"


@pytest.mark.asyncio
async def test_wait_for_destinations_rejects_a_replaced_generation() -> None:
    client = _Client(
        [_current(), _current("validating"), _current("ready", "dest-replaced")],
        _applied("validating"),
    )

    with pytest.raises(PrincipalConfigurationError) as error:
        await PrincipalManager(client).sync(
            {"reporting_destinations": [DESTINATION]},
            idempotency_key="principal-sync-key-0005",
            wait_for_setup=True,
            poll_interval=0.001,
        )

    assert error.value.code == "DESTINATION_GENERATION_CHANGED"


@pytest.mark.asyncio
async def test_payload_failure_is_not_treated_as_success() -> None:
    failed = SyncPrincipalResponse.model_validate(
        {
            "result": {
                "kind": "failed",
                "errors": [{"code": "CONFLICT", "message": "stale configuration"}],
            }
        }
    )

    with pytest.raises(PrincipalConfigurationError, match="stale configuration"):
        await PrincipalManager(_Client([_current()], failed)).sync(
            {"declarations": {}}, idempotency_key="principal-sync-key-0004"
        )


def test_principal_semantic_types_are_public() -> None:
    from adcp import PrincipalCurrentResult as RootPrincipalCurrentResult
    from adcp.types.protocol import PrincipalState as PartialPrincipalState

    assert RootPrincipalCurrentResult is PrincipalCurrentResult
    assert PartialPrincipalState is PrincipalState
    assert PrincipalConfiguration.model_fields["declarations"]
