# Manual extension for pricing options with adapter support fields

from __future__ import annotations

from typing import Annotated

from adcp.types.base import AdCPBaseModel
from pydantic import Field


class PricingOptionBase(AdCPBaseModel):
    """Base class for pricing options with adapter support fields.

    Sales agents can use these fields to indicate whether a pricing option
    is supported by the current adapter.
    """

    supported: Annotated[
        bool | None,
        Field(description="Whether this pricing option is supported by the current adapter"),
    ] = None
    unsupported_reason: Annotated[
        str | None,
        Field(
            description="Human-readable reason why this pricing option is not supported (only when supported=False)"
        ),
    ] = None
