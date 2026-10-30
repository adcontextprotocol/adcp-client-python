"""Grants for reporting subscriptions: wind down instead of revoking (AdCP 3.3, experimental).

Revocation means no access (adcp#8607): once a principal's grant on an
account is gone, every task is refused, ``get_reporting_status``, receipts and
statuses included. A seller that wants a buyer to finish reconciling keeps a
reduced grant instead, listing only the reconciliation tasks. Coverage
continues while the grant lists ``get_reporting_status``, so periods already
owed are still delivered and can still be acknowledged.

The TypeScript SDK spells these ``windDownGrant`` and
``REPORTING_WIND_DOWN_TASKS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The tasks a wind-down grant keeps: reading status, and the two writes a
#: buyer needs to finish reconciling.
REPORTING_WIND_DOWN_TASKS: tuple[str, ...] = (
    "get_reporting_status",
    "sync_reporting_receipts",
    "sync_reporting_status",
)


def wind_down_grant(current: Any = None) -> Any:
    """The reduced grant that ends a relationship without cutting reconciliation.

    Returns an :class:`~adcp.types.AccountAuthorization` listing only
    :data:`REPORTING_WIND_DOWN_TASKS`, with ``read_only`` false because
    receipts and statuses are writes. Pass the account's current
    authorization (a model or a mapping) to keep its ``authorized_at``: a
    reduced grant continues the authorization chain, and sellers advertising
    reporting subscriptions must keep serving it. The old ``field_scopes`` and
    ``scope_name`` describe the old task set, so they are dropped::

        account.authorization = wind_down_grant(account.authorization)
    """
    from adcp.types import AccountAuthorization

    payload: dict[str, Any] = {
        "allowed_tasks": list(REPORTING_WIND_DOWN_TASKS),
        "read_only": False,
    }
    authorized_at = (
        current.get("authorized_at")
        if isinstance(current, Mapping)
        else getattr(current, "authorized_at", None)
    )
    if authorized_at is not None:
        payload["authorized_at"] = authorized_at
    return AccountAuthorization.model_validate(payload)


__all__ = ["REPORTING_WIND_DOWN_TASKS", "wind_down_grant"]
