"""The extensible ``reporting-dimension`` vocabulary on reporting-subscription models.

The signed 3.3.0-beta.3 bundle generates every reporting-subscription field that
earlier betas (and the spec's main) settled after 3.3.0-beta.1: offering
``standard_dimensions`` / ``unmapped_dimensions``, ``inactive_intervals[]``,
``prior_versions[].configuration``, ``previous_timezone`` and ``offering_id`` on
coverage entries, and the ``inactive`` coverage status. What the generator still
does wrong is ``enums/reporting-dimension.json``, which is ``x-extensible``:
receivers MUST accept unknown values, but the generated models close the
vocabulary into a ``StrEnum``. A selector naming a value no offering maps simply
matches nothing, so the dimension fields are widened here to any token of the
vocabulary's shape, the same way :mod:`adcp.types._forward_compat` carries other
corrections the generator cannot express.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from adcp.types.base import NoExplicitNull, defer_model_rebuild
from adcp.types.domains.bundled.protocol import (
    get_adcp_capabilities_response as _bundled_capabilities,
)
from adcp.types.domains.core import reporting_delivery_capabilities as _capabilities
from adcp.types.domains.core import reporting_delivery_offering as _offering
from adcp.types.domains.core import reporting_subscription as _subscription

#: One value of the extensible ``enums/reporting-dimension.json`` vocabulary.
#: Receivers MUST accept unknown values (``x-extensible``); a selector naming a
#: value no offering maps simply matches nothing.
ReportingDimensionToken = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


def _replace_field(model: type[BaseModel], name: str, annotation: Any, field: FieldInfo) -> None:
    field.annotation = annotation
    model.model_fields[name] = field
    model.__annotations__[name] = annotation
    defer_model_rebuild(model)


def _optional(annotation: Any, **kwargs: Any) -> tuple[Any, FieldInfo]:
    wrapped = Annotated[annotation | None, Field(**kwargs), NoExplicitNull]
    return wrapped, FieldInfo.from_annotated_attribute(cast(Any, wrapped), None)


def apply_reporting_subscription_preview() -> None:
    """Widen the closed reporting-dimension vocabulary to its extensible form (idempotent)."""
    dimensions_doc = (
        "Experimental (media_buy.reporting_subscriptions). The offering's breakdown "
        "mapped onto the standard reporting dimension vocabulary."
    )
    for offering_model in (_offering.ReportingDeliveryOffering, _bundled_capabilities.Offering):
        _replace_field(
            offering_model,
            "standard_dimensions",
            *_optional(list[ReportingDimensionToken], max_length=16, description=dimensions_doc),
        )
    _replace_field(
        _subscription.Selector,
        "dimensions",
        *_optional(
            list[ReportingDimensionToken],
            min_length=1,
            max_length=16,
            description="Breakdown in the standard (extensible) vocabulary.",
        ),
    )
    for subscriptions_model in (_capabilities.Subscriptions, _bundled_capabilities.Subscriptions):
        _replace_field(
            subscriptions_model,
            "dimensions",
            *_optional(
                list[ReportingDimensionToken],
                min_length=1,
                description="Standard dimensions this seller maps its report definitions onto.",
            ),
        )


__all__ = [
    "ReportingDimensionToken",
    "apply_reporting_subscription_preview",
]
