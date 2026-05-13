"""Schema-variant marker for cross-class entity overrides (#710).

When an adopter subclasses an auto-generated response type and assigns
a shape-compatible-but-distinct entity class to a parent field, mypy
flags the override as a Liskov violation. ``SchemaVariant[T]`` marks
the override as intentional — at runtime it collapses to ``T`` (Pydantic
validates against the wrapped type); with :mod:`adcp.types.mypy_plugin`
active it rewrites to ``Any`` for override-compat purposes, retiring
the ``# type: ignore[assignment]`` adopters used to stamp::

    class GetMediaBuyDeliveryResponse(LibraryGetMediaBuyDeliveryResponse):
        media_buy_deliveries: SchemaVariant[list[MediaBuyDeliveryData]]

Inside the override the field's mypy type is ``Any``; cast to recover
inference::

    for d in cast(list[MediaBuyDeliveryData], self.media_buy_deliveries):
        d.local_method()

Don't use ``SchemaVariant`` for subclass overrides — those already
type-check via ``Sequence[T]`` covariance (PR #635) and the marker
would obscure that the override is sub-typing, not substitution.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SchemaVariant"]


class _SchemaVariantMeta(type):
    def __getitem__(cls, item: Any) -> Any:
        return item


class SchemaVariant(metaclass=_SchemaVariantMeta):
    """Marker for intentional cross-class entity overrides — see module docstring."""
