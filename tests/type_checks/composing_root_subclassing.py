"""Adopter-facing type contract for schema roots that compose.

A root that composes — a union of models, or one model — is published as what it
composes, so an adopter extends it the way every other generated model is
extended. See https://github.com/adcontextprotocol/adcp-client-python/issues/1077.
"""

from collections.abc import Sequence

from pydantic import AnyUrl, ConfigDict, Field, TypeAdapter

from adcp.types import (
    AccountReference,
    AccountReferenceById,
    CheckGovernanceRequest,
    Deployment,
    PlatformDeployment,
    Signal,
)


class StrictAccountReference(AccountReferenceById):
    """An arm of a union root takes extra='forbid'."""

    model_config = ConfigDict(extra="forbid")


class StrictCheckGovernanceRequest(CheckGovernanceRequest):
    """A single-model root takes extra='forbid'."""

    model_config = ConfigDict(extra="forbid")


class SellerDeployment(PlatformDeployment):
    """One arm of a discriminated union, plus a seller-internal field."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(default="platform-wide", exclude=True)


class SellerSignal(Signal):
    """The narrowed element type is accepted without a type: ignore."""

    deployments: Sequence[SellerDeployment] = Field(..., min_length=1)


account: AccountReference = StrictAccountReference(account_id="acct-1")
governance: CheckGovernanceRequest = StrictCheckGovernanceRequest(
    caller=AnyUrl("https://buyer.example/agent")
)
deployment: Deployment = SellerDeployment(platform="the-trade-desk", is_live=True)

# A union root validates raw data through an adapter and keeps its discriminator.
validated: Deployment = TypeAdapter(Deployment).validate_python(
    {"type": "agent", "agent_url": "https://agent.example/a", "is_live": False}
)
