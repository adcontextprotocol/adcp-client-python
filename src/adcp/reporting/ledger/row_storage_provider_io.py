"""Helpers shared by the bundled cloud row-object providers (GCS, S3, Azure Blob).

Not part of the public ``reporting.ledger`` surface.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError
from adcp.reporting.ledger.row_storage_object import ReportingRowObjectGetInputV1

__all__ = [
    "assert_key",
    "assert_location_keys",
    "assert_read_bounds",
    "client_map",
    "prefix_filter_overlaps",
    "select_client",
]

_T = TypeVar("_T")
_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def assert_location_keys(
    location: Mapping[str, str], required: tuple[str, ...], optional: tuple[str, ...] = ()
) -> None:
    """Raise ``INVALID_INPUT`` unless ``location`` has exactly the allowed string keys."""
    allowed = {*required, *optional}
    if (
        not isinstance(location, Mapping)
        or any(key not in allowed or not isinstance(value, str) for key, value in location.items())
        or any(key not in location for key in required)
    ):
        accepted = ", ".join([*required, *(f"{key}?" for key in optional)])
        raise ReportingRowStoreError("INVALID_INPUT", f"location accepts only {accepted}")


def assert_key(key: str, *, maximum_bytes: int = 1024) -> None:
    if not isinstance(key, str) or not key or len(key.encode("utf-8")) > maximum_bytes:
        raise ReportingRowStoreError("INVALID_INPUT", "object key is invalid")


def assert_read_bounds(request: ReportingRowObjectGetInputV1) -> None:
    """Validate a get request's range and cap before any provider I/O."""
    if type(request.max_bytes) is not int or request.max_bytes < 0:
        raise ReportingRowStoreError("INVALID_INPUT", "max_bytes must be a non-negative integer")
    if request.byte_range is None:
        return
    offset, length = request.byte_range
    if type(offset) is not int or type(length) is not int or offset < 0 or length < 0:
        raise ReportingRowStoreError("INVALID_INPUT", "range must use non-negative integers")
    if length > request.max_bytes:
        raise ReportingRowStoreError(
            "ROWS_INTEGRITY_FAILED", "requested range exceeds the read cap"
        )


def client_map(clients: Mapping[str, _T] | None) -> dict[str, _T]:
    """Copy a host client map into a closed lookup keyed by credential reference."""
    if clients is None:
        return {}
    result: dict[str, _T] = {}
    for reference, client in clients.items():
        if not _CREDENTIAL_REF.match(reference) or client is None:
            raise ReportingRowStoreError(
                "INVALID_INPUT", "clients must map credential references to clients"
            )
        result[reference] = client
    return result


def select_client(default: _T | None, clients: Mapping[str, _T], credential_ref: str | None) -> _T:
    """Pick the host-injected client for a binding.

    ``credential_ref`` selects from a closed map; without one the default client
    is used. An unknown reference fails closed and is never echoed.
    """
    client = default if credential_ref is None else clients.get(credential_ref)
    if client is None:
        raise ReportingRowStoreError("UNSAFE_BINDING", "no client is configured for this binding")
    return client


def prefix_filter_overlaps(prefix_filter: str | None, binding_prefix: str) -> bool:
    """Whether an object-store prefix filter can match keys under the binding's owned prefix.

    An absent or empty filter matches everything.
    """
    if not prefix_filter:
        return True
    owned = f"{binding_prefix}/"
    return owned.startswith(prefix_filter) or prefix_filter.startswith(owned)


def require_clients(
    default: Any, clients: Mapping[str, Any], is_client: Callable[[Any], bool], what: str
) -> None:
    """Refuse construction unless at least one valid official client was injected."""
    if (
        (default is not None and not is_client(default))
        or any(not is_client(client) for client in clients.values())
        or (default is None and not clients)
    ):
        raise ReportingRowStoreError(
            "INVALID_INPUT", f"inject an official {what} as client or clients"
        )
