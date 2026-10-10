"""String keys for geographic place-system maps, independent of model import order."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, AnyUrl, TypeAdapter, UrlConstraints

from adcp._deferred_adapters import deferred_adapter

_REGISTERED_SYSTEMS = frozenset({"geonames", "google_ads", "microsoft_ads"})


@deferred_adapter
def _https_system_adapter() -> TypeAdapter[AnyUrl]:
    return TypeAdapter(
        Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"], host_required=True)]
    )


def _validate_geo_system_key(value: str) -> str:
    """Validate the namespace and preserve its exact opaque wire spelling."""
    if value not in _REGISTERED_SYSTEMS:
        # URL parsing normalizes schemes, but the schema's ^https:// pattern
        # applies to the original opaque identifier, before any normalization.
        if not value.startswith("https://"):
            raise ValueError("geographic place system URLs must start with https://")
        _https_system_adapter().validate_python(value)
    return value


GeoPlaceSystemKey = Annotated[str, AfterValidator(_validate_geo_system_key)]
