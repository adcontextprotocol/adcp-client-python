"""Capability fields a seller serving reporting subscriptions advertises.

``core/reporting-delivery-capabilities.json`` (3.3):

* ``configuration_tasks`` lists every task that accepts reporting
  registrations: ``sync_principal`` for subscriptions, ``sync_accounts`` for
  account-level ``reporting_delivery_configs``.
* The deprecated ``configuration_task: "sync_accounts"`` is present when and
  only when ``configuration_tasks`` lists ``sync_accounts`` or is absent.
* ``subscriptions`` (``supported``, optional ``max_accounts``,
  ``backfill_on_regrant``, ``dimensions``) is present when and only when
  ``configuration_tasks`` lists ``sync_principal``.
* Advertising subscriptions also requires the ``media_buy.reporting_subscriptions``
  experimental feature and ``reporting_subscriptions`` in
  ``adcp.principal.supported_sections``.

A seller that does not serve subscriptions keeps its existing block: these
helpers leave it byte-for-byte unchanged when ``support`` is ``None``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from adcp.reporting.subscriptions.resolution import STANDARD_REPORTING_DIMENSIONS

#: Experimental feature id for principal-level reporting subscriptions.
REPORTING_SUBSCRIPTIONS_FEATURE = "media_buy.reporting_subscriptions"
#: ``adcp.principal.supported_sections`` value for the section.
REPORTING_SUBSCRIPTIONS_SECTION = "reporting_subscriptions"
_DIMENSION = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class ReportingSubscriptionSupport:
    """What a seller declares about its reporting-subscription runtime.

    ``account_configuration`` keeps ``sync_accounts`` in ``configuration_tasks``
    (and the deprecated ``configuration_task``) for sellers that still serve
    account-level registration; set it ``False`` only when the seller accepts
    reporting registration through ``sync_principal`` alone, which serves 3.3
    callers only.
    """

    max_accounts: int | None = None
    backfill_on_regrant: bool = False
    dimensions: tuple[str, ...] = ()
    account_configuration: bool = True

    def __post_init__(self) -> None:
        if self.max_accounts is not None and self.max_accounts < 1:
            raise ValueError("max_accounts must be at least 1")
        dimensions = tuple(dict.fromkeys(self.dimensions))
        for value in dimensions:
            if not _DIMENSION.fullmatch(value):
                raise ValueError(f"dimension {value!r} is not a vocabulary token")
        object.__setattr__(self, "dimensions", dimensions)

    @property
    def standard_dimensions(self) -> tuple[str, ...]:
        return tuple(item for item in self.dimensions if item in STANDARD_REPORTING_DIMENSIONS)

    def block(self) -> dict[str, Any]:
        """The ``reporting_delivery.subscriptions`` capability object."""
        payload: dict[str, Any] = {"supported": True}
        if self.max_accounts is not None:
            payload["max_accounts"] = self.max_accounts
        if self.backfill_on_regrant:
            payload["backfill_on_regrant"] = True
        if self.dimensions:
            payload["dimensions"] = list(self.dimensions)
        return payload


def reporting_configuration_tasks(
    *, subscriptions: bool, account_configuration: bool = True
) -> list[str]:
    """``configuration_tasks`` in the schema's order of preference."""
    tasks: list[str] = []
    if subscriptions:
        tasks.append("sync_principal")
    if account_configuration:
        tasks.append("sync_accounts")
    if not tasks:
        raise ValueError("a reporting seller accepts registration through at least one task")
    return tasks


def with_subscription_dimensions(offering: Mapping[str, Any] | Any, mapping: Any) -> dict[str, Any]:
    """An offering's wire form with ``standard_dimensions`` and ``unmapped_dimensions``.

    ``mapping`` is the offering's
    :class:`~adcp.reporting.subscriptions.SubscriptionOffering` (or anything
    with ``dimensions`` and ``unmapped_dimensions``). Sellers advertising
    ``media_buy.reporting_subscriptions`` MUST publish ``standard_dimensions``
    on every offering (adcp#8585); ``unmapped_dimensions`` appears only when
    any exist. Values are sorted so the declaration is stable. Serve the block
    through :func:`~adcp.reporting.subscriptions.wire.project_reporting_delivery`
    so callers pinned below 3.3.0-beta.2 do not receive these fields.
    """
    payload: dict[str, Any] = (
        dict(offering)
        if isinstance(offering, Mapping)
        else offering.model_dump(mode="json", exclude_none=True)
    )
    payload["standard_dimensions"] = sorted(getattr(mapping, "dimensions"))
    unmapped = sorted(getattr(mapping, "unmapped_dimensions", ()) or ())
    if unmapped:
        payload["unmapped_dimensions"] = unmapped
    else:
        payload.pop("unmapped_dimensions", None)
    return payload


def subscription_capabilities(
    reporting_delivery: Mapping[str, Any], support: ReportingSubscriptionSupport | None
) -> dict[str, Any]:
    """Return ``reporting_delivery`` with the registration fields for ``support``.

    ``None`` returns an unchanged copy, so a seller without subscriptions
    advertises exactly what it did before. With ``support``, every offering
    must already carry ``standard_dimensions`` (see
    :func:`with_subscription_dimensions`).
    """
    payload = dict(reporting_delivery)
    if support is None:
        return payload
    missing = [
        str(item.get("offering_id"))
        for item in payload.get("offerings") or ()
        if "standard_dimensions" not in item
    ]
    if missing:
        raise ValueError(
            "sellers advertising reporting subscriptions must publish standard_dimensions "
            f"on every offering; missing on {missing}"
        )
    tasks = reporting_configuration_tasks(
        subscriptions=True, account_configuration=support.account_configuration
    )
    payload["configuration_tasks"] = tasks
    if "sync_accounts" in tasks:
        payload["configuration_task"] = "sync_accounts"
    else:
        payload.pop("configuration_task", None)
    payload["subscriptions"] = support.block()
    return payload


def _append_unique(values: Iterable[Any], value: str) -> list[str]:
    result = [str(getattr(item, "value", item)) for item in values]
    if value not in result:
        result.append(value)
    return result


def advertise_reporting_subscriptions(
    capabilities: Mapping[str, Any], support: ReportingSubscriptionSupport | None
) -> dict[str, Any]:
    """Add the feature id and principal section to a capabilities response.

    Expects ``media_buy.reporting_delivery`` already projected through
    :func:`subscription_capabilities`. The ``adcp.principal`` block is touched
    only when present: a seller advertising subscriptions must already
    advertise the principal layer, which owns its own block.
    """
    payload = dict(capabilities)
    if support is None:
        return payload
    payload["experimental_features"] = _append_unique(
        payload.get("experimental_features") or (), REPORTING_SUBSCRIPTIONS_FEATURE
    )
    adcp = payload.get("adcp")
    if isinstance(adcp, Mapping) and isinstance(adcp.get("principal"), Mapping):
        principal = dict(adcp["principal"])
        principal["supported_sections"] = _append_unique(
            principal.get("supported_sections") or (), REPORTING_SUBSCRIPTIONS_SECTION
        )
        adcp = dict(adcp)
        adcp["principal"] = principal
        payload["adcp"] = adcp
    return payload


__all__ = [
    "REPORTING_SUBSCRIPTIONS_FEATURE",
    "REPORTING_SUBSCRIPTIONS_SECTION",
    "ReportingSubscriptionSupport",
    "advertise_reporting_subscriptions",
    "reporting_configuration_tasks",
    "subscription_capabilities",
    "with_subscription_dimensions",
]
