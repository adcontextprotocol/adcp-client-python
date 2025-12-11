# Pricing option base class with support fields
# These fields are not in upstream schemas but are used by adapters
# to indicate whether a pricing option is supported

from __future__ import annotations

from typing import Annotated

from adcp.types.base import AdCPBaseModel
from pydantic import Field


class PricingOptionBase(AdCPBaseModel):
    """Base class for pricing options with support indicator fields.

    These fields allow adapters to indicate whether a particular pricing
    option is supported by the underlying ad platform.
    """

    supported: Annotated[
        bool | None,
        Field(
            description="Whether this pricing option is supported by the current adapter"
        ),
    ] = None
    unsupported_reason: Annotated[
        str | None,
        Field(
            description="Human-readable reason why this pricing option is not supported (only when supported=False)"
        ),
    ] = None
