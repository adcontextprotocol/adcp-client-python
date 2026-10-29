"""AdCP 3.3 reporting-subscription fields settled after 3.3.0-beta.1 (adcp#8585, adcp#8588).

adcontextprotocol/adcp#8585 (merged to the spec's main, not yet in a signed
bundle) adds these experimental fields:

* ``core/reporting-delivery-offering.json``: ``standard_dimensions`` and
  ``unmapped_dimensions``, so a buyer resolves a selector's ``match`` from the
  wire;
* ``core/reporting-subscription-state.json``: ``inactive_intervals[]``, so a
  reactivation in place can be recomputed after local-state loss;
* ``core/reporting-subscription-coverage.json``: ``previous_timezone`` and
  ``offering_id`` on ``timezone_cutovers[]``, and ``offering_id`` on ``gaps[]``;
* ``prior_versions[].configuration`` on the readback (adcp#8588, pending);
* the extensible ``reporting-dimension`` vocabulary on selectors, offerings and
  the capability block, which the generator closed.

The pinned 3.3.0-beta.1 models close those objects
(``additionalProperties: false``), so the fields are added to the generated
classes here, the same way :mod:`adcp.types._forward_compat` carries other
forward-looking surfaces. TODO(3.3.0-beta.2): drop this module when the SDK
adopts a bundle that contains adcp#8585 and the fields are generated.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from pydantic import BaseModel, ConfigDict, Field
from pydantic.fields import FieldInfo

from adcp.types.base import AdCPBaseModel, NoExplicitNull, WireDatetime
from adcp.types.domains.bundled.protocol import (
    get_adcp_capabilities_response as _bundled_capabilities,
)
from adcp.types.domains.core import reporting_delivery_capabilities as _capabilities
from adcp.types.domains.core import reporting_delivery_offering as _offering
from adcp.types.domains.core import reporting_subscription as _subscription
from adcp.types.domains.core import reporting_subscription_coverage as _coverage
from adcp.types.domains.core import reporting_subscription_state as _state

#: One value of the extensible ``enums/reporting-dimension.json`` vocabulary.
#: Receivers MUST accept unknown values (``x-extensible``); a selector naming a
#: value no offering maps simply matches nothing.
ReportingDimensionToken = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


class ReportingSubscriptionInactiveInterval(AdCPBaseModel):
    """One stretch during which a subscription was inactive (adcp#8585)."""

    model_config = ConfigDict(extra="forbid")
    deactivated_at: Annotated[WireDatetime, Field(description="When active false was applied.")]
    reactivated_at: Annotated[
        WireDatetime | None,
        Field(
            description=(
                "When active true was applied again. Absent for the current interval "
                "while the subscription is inactive."
            )
        ),
        NoExplicitNull,
    ] = None


def _add_field(model: type[BaseModel], name: str, annotation: Any, field: FieldInfo) -> None:
    if name in model.model_fields:
        return  # the generated model already declares it: this preview is stale
    field.annotation = annotation
    model.model_fields[name] = field
    model.__annotations__[name] = annotation
    model.model_rebuild(force=True)


def _replace_field(model: type[BaseModel], name: str, annotation: Any, field: FieldInfo) -> None:
    field.annotation = annotation
    model.model_fields[name] = field
    model.__annotations__[name] = annotation
    model.model_rebuild(force=True)


def _optional(annotation: Any, **kwargs: Any) -> tuple[Any, FieldInfo]:
    wrapped = Annotated[annotation | None, Field(**kwargs), NoExplicitNull]
    return wrapped, FieldInfo.from_annotated_attribute(cast(Any, wrapped), None)


def apply_reporting_subscription_preview() -> None:
    """Add the adcp#8585 fields to the pinned 3.3.0-beta.1 models (idempotent)."""
    dimensions_doc = (
        "Experimental (media_buy.reporting_subscriptions). The offering's breakdown "
        "mapped onto the standard reporting dimension vocabulary."
    )
    unmapped_doc = (
        "Experimental (media_buy.reporting_subscriptions). Native report-definition "
        "dimensions with no standard mapping."
    )
    for offering_model in (_offering.ReportingDeliveryOffering, _bundled_capabilities.Offering):
        _add_field(
            offering_model,
            "standard_dimensions",
            *_optional(list[ReportingDimensionToken], max_length=16, description=dimensions_doc),
        )
        _add_field(
            offering_model,
            "unmapped_dimensions",
            *_optional(list[str], max_length=16, description=unmapped_doc),
        )
    # The generated models close the extensible dimension vocabulary.
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
    # adcp#8588: each prior version carries the configuration it had.
    _add_field(
        _state.PriorVersion,
        "configuration",
        *_optional(
            _subscription.ReportingSubscription,
            description="The configuration this version had.",
        ),
    )
    _add_field(
        _state.ReportingSubscriptionState,
        "inactive_intervals",
        *_optional(
            list[ReportingSubscriptionInactiveInterval],
            max_length=64,
            description="Intervals during which the subscription was inactive, oldest first.",
        ),
    )
    _add_field(
        _coverage.TimezoneCutover,
        "previous_timezone",
        *_optional(str, min_length=1, max_length=255, description="The zone before the change."),
    )
    for model in (_coverage.TimezoneCutover, _coverage.Gap):
        _add_field(
            model,
            "offering_id",
            *_optional(
                str,
                min_length=1,
                max_length=255,
                description="The offering this entry applies to; absent for every offering.",
            ),
        )


__all__ = [
    "ReportingDimensionToken",
    "ReportingSubscriptionInactiveInterval",
    "apply_reporting_subscription_preview",
]
