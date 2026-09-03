"""Buyer-side orchestration for the AdCP 3.2 principal layer.

The generated request and response models describe one wire exchange.  This
module implements the stateful client workflow adopters otherwise have to
repeat: read the current version, submit a guarded section replacement, and
poll asynchronous destination proof until it reaches an actionable state.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from adcp.types import (
    AgentReportingDestinationState,
    GetPrincipalRequest,
    GetPrincipalResponse,
    PrincipalConfiguration,
    PrincipalDeclarationsState,
    PrincipalState,
    SyncPrincipalRequest,
    SyncPrincipalResponse,
)
from adcp.types.core import TaskResult


class PrincipalClient(Protocol):
    """Minimal client surface required by :class:`PrincipalManager`."""

    async def get_principal(self, request: GetPrincipalRequest) -> TaskResult[GetPrincipalResponse]:
        raise NotImplementedError

    async def sync_principal(
        self, request: SyncPrincipalRequest
    ) -> TaskResult[SyncPrincipalResponse]:
        raise NotImplementedError


class PrincipalConfigurationError(RuntimeError):
    """A principal read, mutation, or destination setup operation failed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _enum(value: object) -> str:
    return str(getattr(value, "value", value))


def _failure_message(result: object, fallback: str) -> str:
    errors = getattr(result, "errors", None)
    if errors:
        messages = [str(getattr(error, "message", "")) for error in errors]
        detail = "; ".join(message for message in messages if message)
        if detail:
            return detail
    return fallback


@dataclass(frozen=True)
class DestinationSetupSnapshot:
    """Authoritative setup states from one principal readback."""

    states: dict[str, AgentReportingDestinationState]

    @property
    def validating(self) -> tuple[str, ...]:
        return self._with_state("validating")

    @property
    def ready(self) -> tuple[str, ...]:
        return self._with_state("ready")

    @property
    def action_required(self) -> tuple[str, ...]:
        return self._with_state("action_required")

    @property
    def inactive(self) -> tuple[str, ...]:
        return self._with_state("inactive")

    @property
    def rejected(self) -> tuple[str, ...]:
        return self._with_state("rejected")

    @property
    def settled(self) -> bool:
        """Whether no destination remains in provider-side validation."""
        return not self.validating

    def _with_state(self, expected: str) -> tuple[str, ...]:
        return tuple(
            destination_id
            for destination_id, state in self.states.items()
            if _enum(state.state) == expected
        )


@dataclass(frozen=True)
class PrincipalSyncOutcome:
    """Applied configuration plus negotiated and setup-state projections."""

    response: SyncPrincipalResponse
    principal_id: str | None
    configuration_version: str | None
    configuration: PrincipalState | None
    destinations: DestinationSetupSnapshot
    declarations: PrincipalDeclarationsState | None

    @property
    def selected_async_adcp_version(self) -> str | None:
        if self.declarations is None:
            return None
        return self.declarations.selected_async_adcp_version


def _destination_snapshot(
    configuration: PrincipalState | None,
    destination_ids: set[str] | None = None,
) -> DestinationSetupSnapshot:
    destinations = configuration.reporting_destinations if configuration else None
    states = {
        item.destination_id: item
        for item in destinations or []
        if destination_ids is None or item.destination_id in destination_ids
    }
    return DestinationSetupSnapshot(states)


class PrincipalManager:
    """Coordinate version-fenced principal configuration for one seller."""

    def __init__(self, client: PrincipalClient) -> None:
        self._client = client

    async def read(self) -> GetPrincipalResponse:
        """Read principal state and normalize transport/payload failures."""
        task = await self._client.get_principal(GetPrincipalRequest())
        if not task.success or task.data is None:
            raise PrincipalConfigurationError(
                "PRINCIPAL_READ_FAILED", task.error or "get_principal failed"
            )
        response = task.data
        if response.result.kind == "failed":
            raise PrincipalConfigurationError(
                "PRINCIPAL_READ_FAILED",
                _failure_message(response.result, "get_principal returned a failed result"),
            )
        return response

    async def sync(
        self,
        configuration: PrincipalConfiguration | Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        use_version_fence: bool = True,
        expected_configuration_version: str | None = None,
        expected_principal_kind: object | None = None,
        dry_run: bool = False,
        wait_for_setup: bool = False,
        setup_timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> PrincipalSyncOutcome:
        """Replace selected sections, optionally waiting for destination proof.

        With ``use_version_fence`` (the default), this first calls
        ``get_principal``.  A current configuration version is copied to
        ``expected_configuration_version`` and the seller-resolved principal
        kind is asserted.  Callers targeting a seller that explicitly declares
        ``optimistic_concurrency: false`` should disable the fence.
        """
        desired = PrincipalConfiguration.model_validate(configuration)
        current: GetPrincipalResponse | None = None
        if use_version_fence:
            current = await self.read()
            current_result = current.result
            if expected_configuration_version is None and current_result.kind == "current":
                expected_configuration_version = current_result.configuration_version
            if expected_principal_kind is None:
                if current_result.kind == "current":
                    expected_principal_kind = current_result.principal_kind
                elif current_result.kind == "recognized":
                    expected_principal_kind = current_result.principal_kind

        payload: dict[str, Any] = {
            "idempotency_key": idempotency_key or str(uuid4()),
            "configuration": desired,
            "dry_run": dry_run,
        }
        if expected_configuration_version is not None:
            payload["expected_configuration_version"] = expected_configuration_version
        if expected_principal_kind is not None:
            payload["expected_principal_kind"] = expected_principal_kind

        task = await self._client.sync_principal(SyncPrincipalRequest.model_validate(payload))
        if not task.success or task.data is None:
            raise PrincipalConfigurationError(
                "PRINCIPAL_SYNC_FAILED", task.error or "sync_principal failed"
            )
        response = task.data
        result = response.result
        if result.kind == "failed":
            raise PrincipalConfigurationError(
                "PRINCIPAL_SYNC_FAILED",
                _failure_message(result, "sync_principal returned a failed result"),
            )
        if result.kind == "validated":
            return PrincipalSyncOutcome(
                response, None, None, None, DestinationSetupSnapshot({}), None
            )

        destination_ids = {
            destination.destination_id for destination in desired.reporting_destinations or []
        }
        configuration_state = result.configuration
        destination_states = _destination_snapshot(configuration_state, destination_ids)
        if wait_for_setup and destination_states.validating:
            expected_destination_refs = {
                destination_id: state.destination_ref
                for destination_id, state in destination_states.states.items()
            }
            readback, destination_states = await self.wait_for_destinations(
                destination_ids,
                timeout=setup_timeout,
                poll_interval=poll_interval,
                expected_destination_refs=expected_destination_refs,
            )
            read_result = readback.result
            if read_result.kind != "current":
                raise PrincipalConfigurationError(
                    "PRINCIPAL_STATE_LOST",
                    "get_principal stopped returning current state during setup polling",
                )
            configuration_state = read_result.configuration
            principal_id = read_result.principal_id
            configuration_version = read_result.configuration_version
        else:
            principal_id = result.principal_id
            configuration_version = result.configuration_version

        return PrincipalSyncOutcome(
            response,
            principal_id,
            configuration_version,
            configuration_state,
            destination_states,
            configuration_state.declarations,
        )

    async def wait_for_destinations(
        self,
        destination_ids: set[str],
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
        expected_destination_refs: Mapping[str, str] | None = None,
    ) -> tuple[GetPrincipalResponse, DestinationSetupSnapshot]:
        """Poll while requested destinations are ``validating``.

        ``ready``, ``action_required``, ``inactive``, and ``rejected`` are
        returned to the caller as actionable settled states.  The helper never
        follows or executes an untrusted setup URL.
        """
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        if not destination_ids:
            raise ValueError("destination_ids must not be empty")

        deadline = time.monotonic() + timeout
        while True:
            response = await self.read()
            result = response.result
            if result.kind != "current":
                raise PrincipalConfigurationError(
                    "PRINCIPAL_NOT_CONFIGURED",
                    "get_principal did not return current state during setup polling",
                )
            snapshot = _destination_snapshot(result.configuration, destination_ids)
            missing = destination_ids.difference(snapshot.states)
            if missing:
                raise PrincipalConfigurationError(
                    "DESTINATION_STATE_MISSING",
                    "get_principal omitted requested destination(s): " + ", ".join(sorted(missing)),
                )
            if expected_destination_refs:
                replaced = [
                    destination_id
                    for destination_id, destination_ref in expected_destination_refs.items()
                    if snapshot.states[destination_id].destination_ref != destination_ref
                ]
                if replaced:
                    raise PrincipalConfigurationError(
                        "DESTINATION_GENERATION_CHANGED",
                        "get_principal replaced requested destination generation(s): "
                        + ", ".join(sorted(replaced)),
                    )
            if snapshot.settled:
                return response, snapshot
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending = ", ".join(snapshot.validating)
                raise TimeoutError(f"reporting destination setup did not settle in time: {pending}")
            await asyncio.sleep(min(poll_interval, remaining))


async def sync_principal_configuration(
    client: PrincipalClient,
    configuration: PrincipalConfiguration | Mapping[str, Any],
    **kwargs: Any,
) -> PrincipalSyncOutcome:
    """Functional wrapper for :meth:`PrincipalManager.sync`."""
    return await PrincipalManager(client).sync(configuration, **kwargs)


__all__ = [
    "DestinationSetupSnapshot",
    "PrincipalClient",
    "PrincipalConfigurationError",
    "PrincipalManager",
    "PrincipalSyncOutcome",
    "sync_principal_configuration",
]
