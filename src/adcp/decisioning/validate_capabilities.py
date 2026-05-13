"""Boot-time validation of the projected ``get_adcp_capabilities`` response.

The framework auto-projects :class:`DecisioningCapabilities` into a
spec-shaped ``get_adcp_capabilities`` response (see
:meth:`adcp.decisioning.handler.PlatformHandler.get_adcp_capabilities`).
Adopters may also override the projection on a subclass. Either way,
the response that ships on the wire must satisfy the
``protocol/get-adcp-capabilities-response.json`` schema **and** the
spec invariants the schema cannot fully express on its own (e.g.
"``account.supported_billing`` must exist and be non-empty whenever the
seller claims ``media_buy``").

This module exercises the projection at boot — invokes
``handler.get_adcp_capabilities()`` with a synthetic request and
validates the returned dict — so misconfiguration surfaces as a
structured :class:`AdcpError` before the server starts accepting
traffic. The historical motivator is the v3 reference seller, which
shipped a non-conformant capabilities response until #402 added
``supported_billing`` manually; the validator below would have caught
that at boot.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from adcp.decisioning.dispatch import validate_platform
from adcp.decisioning.types import AdcpError
from adcp.validation.schema_validator import validate_response

if TYPE_CHECKING:
    from adcp.decisioning.handler import PlatformHandler


def _invoke_capabilities(handler: PlatformHandler) -> dict[str, Any]:
    """Call ``handler.get_adcp_capabilities()`` synchronously.

    The handler method is async but never blocks (no I/O — pure
    projection over ``platform.capabilities``). We drive it via
    :func:`asyncio.run` so this validator stays callable from the
    synchronous server-boot path. **Cannot be called from inside a
    running event loop** — see #700; the stdlib's ``RuntimeError`` is
    re-raised with an SDK-specific message pointing at the opt-out.
    """
    try:
        return asyncio.run(handler.get_adcp_capabilities())
    except RuntimeError as exc:
        # Stdlib's ``asyncio.run`` raises ``RuntimeError("asyncio.run()
        # cannot be called from a running event loop")``. That message
        # is opaque to adopters — they have to find the issue / PR to
        # learn the opt-out. Re-raise with the fix inline so the
        # diagnostic answers the question "what do I do?".
        if "running event loop" not in str(exc):
            raise
        raise RuntimeError(
            "validate_capabilities_response_shape() was called from "
            "inside a running event loop, which is incompatible with "
            "the sync validator's internal asyncio.run(). Two fixes:\n"
            "  1. In an async context (test fixture, Starlette "
            "lifespan, in-process A2A client): construct the server "
            "with create_adcp_server_from_platform(..., "
            "validate_at_init=False) and `await "
            "validate_capabilities_response_shape_async(handler)`.\n"
            "  2. In a sync boot path that is not yet inside a loop: "
            "no change needed — the validator runs as before.\n"
            "See https://github.com/adcontextprotocol/adcp-client-python/issues/700."
        ) from exc


def _violation(reason: str, *, details: dict[str, Any]) -> AdcpError:
    """Build a uniform :class:`AdcpError` for a capabilities violation.

    Same shape as the other server-boot fail-fast errors in
    :func:`adcp.decisioning.serve.create_adcp_server_from_platform`
    (terminal recovery + structured ``details``).
    """
    return AdcpError(
        "INVALID_REQUEST",
        message=(
            "get_adcp_capabilities response failed boot-time spec "
            f"validation: {reason}. Fix the platform's capabilities "
            "declaration (or the handler override) before starting "
            "the server — buyers reading this response would otherwise "
            "see a non-conformant capabilities envelope."
        ),
        recovery="terminal",
        details=details,
    )


def _validate_response_dict(response: Any) -> None:
    """Shared validation logic — operates on the already-resolved
    capabilities response dict. Both the sync and async entry points
    funnel through here so the diagnostic surface stays identical."""
    if not isinstance(response, dict):
        raise _violation(
            "handler.get_adcp_capabilities() returned a " f"{type(response).__name__}, not a dict",
            details={"response_type": type(response).__name__},
        )

    # 1. Schema-driven validation against the bundled spec schema.
    outcome = validate_response("get_adcp_capabilities", response)
    if not outcome.valid:
        raise _violation(
            "response does not conform to " "protocol/get-adcp-capabilities-response.json",
            details={
                "issues": [
                    {
                        "pointer": issue.pointer,
                        "message": issue.message,
                        "keyword": issue.keyword,
                        "schema_path": issue.schema_path,
                    }
                    for issue in outcome.issues
                ],
            },
        )

    # 2. supported_protocols invariant — minItems: 1 + required.
    protocols = response.get("supported_protocols")
    if not isinstance(protocols, list) or not protocols:
        raise _violation(
            "supported_protocols is missing or empty (spec requires " "minItems: 1)",
            details={"supported_protocols": protocols},
        )

    # 3. media_buy → account.supported_billing required + non-empty.
    if "media_buy" in protocols:
        account = response.get("account")
        if not isinstance(account, dict):
            raise _violation(
                "seller claims supported_protocols=['media_buy', ...] "
                "but the response is missing the ``account`` block "
                "(spec: ``account.supported_billing`` is required when "
                "media_buy is claimed)",
                details={"supported_protocols": protocols},
            )
        billing = account.get("supported_billing")
        if not isinstance(billing, list) or not billing:
            raise _violation(
                "seller claims supported_protocols=['media_buy', ...] "
                "but ``account.supported_billing`` is missing or empty "
                "(spec: minItems: 1). Set "
                "``DecisioningCapabilities.supported_billing=(...)`` "
                "with at least one of {'operator', 'agent', 'advertiser'}",
                details={
                    "supported_protocols": protocols,
                    "account.supported_billing": billing,
                },
            )


def validate_capabilities_response_shape(handler: PlatformHandler) -> None:
    """Boot-time validator for the projected capabilities response.

    Calls ``handler.get_adcp_capabilities()`` with a synthetic request,
    then enforces:

    1. The response validates against the bundled
       ``protocol/get-adcp-capabilities-response.json`` schema (via
       :func:`adcp.validation.schema_validator.validate_response`).
    2. ``supported_protocols`` is present and non-empty
       (spec ``minItems: 1``; doubled-up here so the diagnostic names
       the invariant directly).
    3. When the seller claims ``media_buy``, ``account.supported_billing``
       is present and non-empty (the invariant the v3 ref seller
       violated pre-#402; spec
       ``protocol/get-adcp-capabilities-response.json`` requires
       ``account.required: ["supported_billing"]`` with
       ``minItems: 1``).

    Synchronous entry point — drives the async handler via
    :func:`asyncio.run`, which means **this function cannot be called
    from inside a running event loop**. Async callers (test fixtures,
    Starlette ``lifespan`` handlers, anything inside ``asyncio.run``)
    should use :func:`validate_capabilities_response_shape_async`
    instead and pair it with
    ``create_adcp_server_from_platform(..., validate_at_init=False)``.

    :raises AdcpError: ``INVALID_REQUEST`` with ``recovery="terminal"``
        on any violation; ``details`` carry the offending response and
        a structured issue list so operators can index the failure
        programmatically.
    :raises RuntimeError: when called from inside a running event loop
        (the ``asyncio.run`` machinery raises this directly).
    """
    _validate_response_dict(_invoke_capabilities(handler))


async def validate_capabilities_response_shape_async(handler: PlatformHandler) -> None:
    """Async sibling of :func:`validate_capabilities_response_shape`.

    Identical diagnostic surface; awaits ``handler.get_adcp_capabilities()``
    directly instead of driving it through :func:`asyncio.run`. Use this
    from async contexts (test fixtures, Starlette ``lifespan``,
    in-process A2A test clients) so the SDK doesn't try to spin up a
    second event loop and crash with ``RuntimeError: asyncio.run()
    cannot be called from a running event loop``.

    Typical pairing — async caller bypasses the init-time sync
    validation and runs the async validator themselves::

        handler, executor, registry = create_adcp_server_from_platform(
            platform, validate_at_init=False,
        )
        await validate_capabilities_response_shape_async(handler)
    """
    _validate_response_dict(await handler.get_adcp_capabilities())


__all__ = [
    "validate_capabilities_response_shape",
    "validate_capabilities_response_shape_async",
    "validate_platform",
]
