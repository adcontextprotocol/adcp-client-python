"""Schema-variant marker for cross-class entity overrides (#710).

When an adopter subclasses an auto-generated response type and substitutes
a **shape-compatible but distinct** entity class for the canonical one,
mypy's Liskov substitution check rejects the assignment because the two
classes are siblings, not parent-child. The override is *semantically*
correct — every method that reads the field works against the adopter's
class — but mypy can't see that without explicit help.

The historical workaround was ``# type: ignore[assignment]`` on every
override line. PR #644's docs codify the pattern as legitimate. This
module ships the typed escape hatch that retires the ignores.

Usage::

    from adcp.types import SchemaVariant
    from adcp.types import GetMediaBuyDeliveryResponse as LibraryGetMediaBuyDeliveryResponse

    class GetMediaBuyDeliveryResponse(LibraryGetMediaBuyDeliveryResponse):
        # No ``# type: ignore[assignment]`` needed — SchemaVariant marks
        # this as an intentional cross-class override.
        media_buy_deliveries: SchemaVariant[list[MediaBuyDeliveryData]]

At runtime ``SchemaVariant[T]`` collapses to ``T`` — Pydantic sees the
wrapped type and validates against it unchanged. Static type-checkers
that load :mod:`adcp.types.mypy_plugin` treat the field as ``Any`` for
override-compat purposes, eliminating the LSP error.

To activate the plugin, add::

    # pyproject.toml
    [tool.mypy]
    plugins = ["adcp.types.mypy_plugin"]

Then run ``mypy --strict`` over the adopter code — no ``# type: ignore``
needed on the override line.

**Tradeoff**: inside the override, the field's type is widened to
``Any`` for mypy's purposes. If precise inference matters (e.g. you
call entity-specific methods on each item), use :func:`typing.cast`::

    from typing import cast

    for delivery in cast(list[MediaBuyDeliveryData], self.media_buy_deliveries):
        delivery.local_method()  # inference restored

The runtime field type is the wrapped type, not ``Any`` — Pydantic
validation, ``model_dump``, and dataclass introspection all see the
true type.

**When not to use this**: subclass overrides where the child IS a
proper subclass of the parent's field type. Those already type-check
cleanly via ``Sequence[T]`` covariance (PR #635); using
``SchemaVariant`` there obscures the fact that the override is
sub-typing, not substitution.
"""

from __future__ import annotations

from typing import Any, TypeVar

__all__ = ["SchemaVariant"]

T = TypeVar("T")


class _SchemaVariantMeta(type):
    """Metaclass: ``SchemaVariant[X]`` returns ``X`` at runtime.

    Implementing the subscription via the metaclass rather than
    :meth:`__class_getitem__` keeps the class non-Generic — adopters
    can use ``SchemaVariant[X]`` exactly once per field annotation
    without TypeVar binding complications. Pydantic introspects the
    field's annotation through ``typing.get_type_hints`` which evaluates
    ``SchemaVariant[X]`` and reads the returned ``X`` as the field type.
    """

    def __getitem__(cls, item: Any) -> Any:
        return item


class SchemaVariant(metaclass=_SchemaVariantMeta):
    """Marker type for intentional cross-class entity overrides.

    See the module docstring for the full rationale and usage. Two-line
    contract:

    * Runtime: ``SchemaVariant[T]`` evaluates to ``T``. Pydantic uses
      ``T`` for validation, serialization, and dump output.
    * Static (with :mod:`adcp.types.mypy_plugin` active): ``SchemaVariant[T]``
      is treated as ``Any`` for assignment-compat purposes, suppressing
      the override LSP error.

    Without the plugin, mypy sees ``SchemaVariant[T]`` as an unanalyzed
    generic and may report ``Bracketed expression`` errors. The plugin is
    not optional — adopters who want the override-compat benefit must
    enable it in their mypy config.
    """
