"""Adopter-facing type contract for public object-union aliases."""

from pydantic import ConfigDict, TypeAdapter

from adcp.types import (
    AccountReference,
    AccountReferenceById,
    PostalArea,
    SignalRef,
)


class StrictAccountReference(AccountReferenceById):
    model_config = ConfigDict(extra="forbid")


account: AccountReference = StrictAccountReference(account_id="acct-1")
postal_area: PostalArea = TypeAdapter(PostalArea).validate_python(
    {"country": "US", "system": "zip", "values": ["10001"]}
)
signal_ref: SignalRef = TypeAdapter(SignalRef).validate_python(
    {"scope": "product", "signal_id": "high-intent"}
)
