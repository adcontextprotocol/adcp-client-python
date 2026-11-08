"""Refuse an undeclared ``start_requires_all_creatives_approved: true`` (#1869).

AdCP 3.3 lets a buyer hold a package's first enablement for serving until every
assigned creative is approved: ``packages[].start_requires_all_creatives_approved``
on ``control_media_buy``. Opting in requires the seller's
``media_buy.features.creative_start_policy: true``; a seller that does not
declare it MUST reject ``true`` with ``UNSUPPORTED_FEATURE`` before mutation.
``false`` is the default and always allowed.

The rule follows from the seller's own capabilities, so the dispatcher applies
it before the handler runs. Without it, a seller that does not know the field
ignores it and the buy starts serving before its creatives are approved, the
opposite of what the buyer asked for.

``create_media_buy`` and ``update_media_buy`` are not gated: AdCP 3.3 withdraws
the field from their packages (adcontextprotocol/adcp#8767), and their open
schemas ignore it (DR-0009).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The package field that opts a package into the initial-start gate.
POLICY_FIELD = "start_requires_all_creatives_approved"

#: The capability a seller declares to accept ``true``.
POLICY_FEATURE = "media_buy.features.creative_start_policy"


def _member(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def opted_in_fields(params: Any) -> list[str]:
    """Field paths of every package that sets the policy to ``true``."""
    packages = _member(params, "packages")
    if not isinstance(packages, list):
        return []
    return [
        f"packages[{index}].{POLICY_FIELD}"
        for index, package in enumerate(packages)
        if _member(package, POLICY_FIELD) is True
    ]


def declares_creative_start_policy(capabilities: Any) -> bool:
    """Whether ``capabilities`` declare ``media_buy.features.creative_start_policy: true``.

    Reads a ``get_adcp_capabilities`` response (dict or model) or
    :class:`~adcp.decisioning.DecisioningCapabilities`.
    """
    features = _member(_member(capabilities, "media_buy"), "features")
    return _member(features, "creative_start_policy") is True


async def refuse_undeclared_creative_start_policy(handler: Any, params: Any, context: Any) -> None:
    """Fail a ``control_media_buy`` that opts in when the seller has not declared it.

    ``params`` is the request as the handler will receive it, a model or a
    dict. The seller's capabilities are read only when a package sets the
    policy to ``true``.
    """
    fields = opted_in_fields(params)
    if not fields:
        return
    capabilities = await handler._declared_capabilities_for_request(params, context)
    if declares_creative_start_policy(capabilities):
        return

    from adcp.exceptions import ADCPTaskError
    from adcp.types import Error, Recovery

    raise ADCPTaskError(
        operation="control_media_buy",
        errors=[
            Error(
                code="UNSUPPORTED_FEATURE",
                message=(
                    f"{POLICY_FIELD}: true requires {POLICY_FEATURE}, "
                    "which this seller does not declare"
                ),
                field=fields[0],
                suggestion=(
                    f"Omit {POLICY_FIELD} or send false; check {POLICY_FEATURE} in "
                    "get_adcp_capabilities before opting in"
                ),
                recovery=Recovery.correctable,
                details={"feature": POLICY_FEATURE, "fields": fields},
            )
        ],
    )
