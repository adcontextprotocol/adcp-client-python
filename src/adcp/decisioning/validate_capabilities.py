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

from adcp.decisioning.types import AdcpError
from adcp.validation.schema_validator import validate_response

if TYPE_CHECKING:
    from adcp.decisioning.handler import PlatformHandler


def _invoke_capabilities(handler: PlatformHandler) -> dict[str, Any]:
    """Call ``handler.get_adcp_capabilities()`` synchronously.

    The handler method is async but never blocks (no I/O — pure
    projection over ``platform.capabilities``). We drive it via
    :func:`asyncio.run` so this validator stays callable from the
    synchronous server-boot path.
    """
    return asyncio.run(handler.get_adcp_capabilities())


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

    :raises AdcpError: ``INVALID_REQUEST`` with ``recovery="terminal"``
        on any violation; ``details`` carry the offending response and
        a structured issue list so operators can index the failure
        programmatically.
    """
    response = _invoke_capabilities(handler)

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


__all__ = ["validate_capabilities_response_shape"]
