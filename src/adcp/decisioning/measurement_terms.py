"""Advertise only the measurement terms the seller accepts (AdCP 3.3).

``core/measurement-terms.json`` ``billing_measurement.measurement_window``:
"When present, MUST reference a window_id declared in the product's
reporting_capabilities.measurement_windows. A seller MUST NOT advertise a
window (or other term value) in a product's measurement_terms that it would
reject with TERMS_REJECTED when a buyer replays those terms unchanged on
create_media_buy."

Two halves:

* :func:`check_advertised_measurement_terms` checks advertised products. The
  framework runs it on synchronous ``get_products`` and ``list_products``
  responses, so a product whose ``measurement_window`` names an undeclared
  window never reaches a buyer.
* :func:`is_advertised_terms_replay` tells a seller whether a buyer's proposed
  ``measurement_terms`` replay what the product advertised.
  :class:`~adcp.decisioning.errors.TermsRejectedError` refuses to mint
  ``TERMS_REJECTED`` for such a replay when given the advertised terms.

The framework does not look products up at ``create_media_buy``, so the replay
rule stays with the seller's code; pass ``advertised_terms`` to
``TermsRejectedError`` to have it enforced.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from adcp.decisioning.types import AdcpError

__all__ = [
    "check_advertised_measurement_terms",
    "is_advertised_terms_replay",
    "undeclared_measurement_window",
]


def _wire(value: Any) -> Any:
    """Return the JSON form of a model or mapping (``None`` members dropped)."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {key: _wire(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


def _member(value: Any, name: str) -> Any:
    """Read one member off a generated model or a wire mapping."""
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def undeclared_measurement_window(product: Any) -> str | None:
    """Return the advertised ``measurement_window`` a product does not declare.

    ``None`` when the product advertises no window or the window is one of its
    ``reporting_capabilities.measurement_windows[].window_id`` values. Accepts a
    generated ``Product`` / ``CanonicalProduct`` or its wire mapping, and reads
    only those members (no model dump), so it is cheap on every discovery call.
    """
    billing = _member(_member(product, "measurement_terms"), "billing_measurement")
    window = _member(billing, "measurement_window")
    if not isinstance(window, str):
        return None
    declared = _member(_member(product, "reporting_capabilities"), "measurement_windows")
    window_ids = {_member(entry, "window_id") for entry in declared or ()}
    return None if window in window_ids else window


def check_advertised_measurement_terms(products: Iterable[Any] | None) -> None:
    """Refuse advertised products whose ``measurement_window`` is undeclared.

    Raises ``AdcpError("INTERNAL_ERROR")`` naming the first offending product:
    the seller's catalog is misconfigured, and a buyer replaying the advertised
    terms would otherwise be rejected (or reconciled against a window the
    product never defined). Declare the window under the product's
    ``reporting_capabilities.measurement_windows``, or stop advertising it.
    """
    for index, product in enumerate(products or ()):
        window = undeclared_measurement_window(product)
        if window is None:
            continue
        product_id = _member(product, "product_id")
        raise AdcpError(
            "INTERNAL_ERROR",
            message=(
                f"Product {product_id!r} advertises measurement_terms."
                f"billing_measurement.measurement_window {window!r}, which is not a "
                "window_id in its reporting_capabilities.measurement_windows. AdCP 3.3 "
                "requires an advertised window to reference a declared window_id."
            ),
            recovery="terminal",
            field=f"products[{index}].measurement_terms.billing_measurement.measurement_window",
        )


def is_advertised_terms_replay(proposed: Any, advertised: Any) -> bool:
    """Whether buyer-proposed ``measurement_terms`` replay the advertised terms.

    A replay proposes at least one term (``billing_measurement``,
    ``makegood_policy``, or an extension member) and every term it proposes
    equals the product's advertised value for that term, compared in wire form.
    Changing any value inside a term (dropping the ``measurement_window``,
    another vendor, a different remedy list) is a new proposal, not a replay.
    """
    proposed_wire, advertised_wire = _wire(proposed), _wire(advertised)
    if not isinstance(proposed_wire, Mapping) or not isinstance(advertised_wire, Mapping):
        return False
    if not proposed_wire:
        return False
    return all(
        key in advertised_wire and advertised_wire[key] == value
        for key, value in proposed_wire.items()
    )
