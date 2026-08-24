"""Governance authorization middleware for MCP and A2A service handlers."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, TypeAlias

from adcp.decisioning.errors import PermissionDeniedError
from adcp.governance import (
    GovernanceAuthorizationFailure,
    GovernanceAuthorizationSuccess,
    GovernanceCommitment,
    GovernanceReplayStore,
    read_governance_authorization_issuer,
    verify_governance_authorization,
)
from adcp.server.base import ToolContext
from adcp.server.idempotency import IdempotencyStore
from adcp.server.serve import SkillMiddleware


@dataclass(frozen=True)
class GovernanceEnforcementDecision:
    """Authoritative applicability and commitment held under one lease."""

    required: bool
    commitment: GovernanceCommitment | Mapping[str, Any] | None = None


EnforcementResolver: TypeAlias = Callable[
    [str, Mapping[str, Any], ToolContext],
    AbstractAsyncContextManager[GovernanceEnforcementDecision],
]
AuthorizationCallback: TypeAlias = Callable[
    [GovernanceAuthorizationSuccess, ToolContext], None | Awaitable[None]
]
RejectionCallback: TypeAlias = Callable[
    [GovernanceAuthorizationFailure, ToolContext], None | Awaitable[None]
]
CallerResolver: TypeAlias = Callable[[ToolContext], str | None | Awaitable[str | None]]
IssuerJwksResolver: TypeAlias = Callable[
    [str, str, Mapping[str, Any], ToolContext], object | None | Awaitable[object | None]
]


def make_governance_enforcement_middleware(
    *,
    tasks: Collection[str],
    expected_audience: str,
    resolve_issuer_jwks: IssuerJwksResolver,
    idempotency_store: IdempotencyStore,
    replay_store: GovernanceReplayStore,
    resolve_enforcement: EnforcementResolver,
    resolve_caller: CallerResolver | None = None,
    on_authorized: AuthorizationCallback | None = None,
    on_rejected: RejectionCallback | None = None,
    **verifier_options: Any,
) -> SkillMiddleware:
    """Build a fail-closed governed-service middleware.

    Verification and atomic replay consumption finish before ``call_next`` can
    invoke the handler. ``resolve_enforcement`` must hold the authoritative
    resource revision/write lock from applicability and commitment resolution
    through ``call_next``; it must not trust buyer-provided state. The default
    caller resolver reads ``ToolContext.caller_identity``.
    """
    if isinstance(tasks, (str, bytes)):
        raise TypeError("tasks must be a collection of task names, not a string")
    governed_tasks = frozenset(tasks)
    if not governed_tasks or any(not isinstance(task, str) or not task for task in governed_tasks):
        raise ValueError("tasks must contain at least one non-empty task name")
    if not expected_audience:
        raise ValueError("expected_audience is required")
    if "expected_phase" in verifier_options or "expected_media_buy_id" in verifier_options:
        raise ValueError("the inbound enforcement gate always verifies intent-phase tokens")

    async def middleware(
        skill_name: str,
        params: dict[str, Any],
        context: ToolContext,
        call_next: Callable[[], Awaitable[Any]],
    ) -> Any:
        if skill_name not in governed_tasks:
            return await call_next()

        async def verify_then_call(
            _handler_self: object,
            _params: Mapping[str, Any],
            _context: ToolContext,
        ) -> Any:
            token = params.get("governance_context")
            async with resolve_enforcement(skill_name, params, context) as enforcement:
                if not isinstance(enforcement, GovernanceEnforcementDecision):
                    raise TypeError("resolve_enforcement must yield GovernanceEnforcementDecision")
                if type(enforcement.required) is not bool:
                    raise TypeError("GovernanceEnforcementDecision.required must be a boolean")
                # A supplied token is always verified, even when current account
                # registration or an authoritative risk-reducing delta says that
                # governance is not required for this invocation.
                if not enforcement.required and token is None:
                    return await call_next()
                caller = await _maybe_await(
                    resolve_caller(context)
                    if resolve_caller is not None
                    else context.caller_identity
                )
                claimed_issuer = read_governance_authorization_issuer(token)
                jwks = (
                    await _maybe_await(
                        resolve_issuer_jwks(claimed_issuer, skill_name, params, context)
                    )
                    if claimed_issuer is not None
                    else None
                )
                if claimed_issuer is None or jwks is None:
                    result: GovernanceAuthorizationFailure | GovernanceAuthorizationSuccess = (
                        GovernanceAuthorizationFailure(
                            False,
                            "governance_key_unknown",
                            "issuer is not authorized by the authenticated buyer",
                        )
                    )
                elif enforcement.commitment is None:
                    result = GovernanceAuthorizationFailure(
                        False,
                        "governance_token_invalid",
                        "governed operation has no authoritative commitment",
                    )
                else:
                    result = await verify_governance_authorization(
                        token=token,
                        expected_issuer=claimed_issuer,
                        expected_audience=expected_audience,
                        authenticated_caller=caller or "",
                        expected_task=skill_name,
                        payload=params,
                        actual_commitment=enforcement.commitment,
                        jwks=jwks,
                        replay_store=replay_store,
                        expected_phase="intent",
                        **verifier_options,
                    )
                if not result.ok:
                    if on_rejected is not None:
                        await _maybe_await(on_rejected(result, context))
                    raise PermissionDeniedError(
                        message="Governance authorization rejected.",
                        field="governance_context",
                        suggestion="Obtain a fresh authorization for this exact request.",
                    )
                if on_authorized is not None:
                    await _maybe_await(on_authorized(result, context))
                return await call_next()

        # Idempotency is the outer gate: a completed identical retry returns
        # the cached result even after token expiry; a miss holds the request
        # key through verification, side effect, and cache commit. Bind the
        # resolved skill into the internal hash input so one key cannot replay
        # a different task whose parameter object happens to be identical.
        guarded = idempotency_store.wrap(verify_then_call)
        idempotency_params = {**params, "__adcp_governance_skill": skill_name}
        return await guarded(None, idempotency_params, context)

    return middleware


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


__all__ = [
    "AuthorizationCallback",
    "CallerResolver",
    "EnforcementResolver",
    "GovernanceEnforcementDecision",
    "IssuerJwksResolver",
    "RejectionCallback",
    "make_governance_enforcement_middleware",
]
