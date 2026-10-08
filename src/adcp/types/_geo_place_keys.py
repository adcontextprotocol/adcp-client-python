"""String keys for geographic place-system maps, independent of model import order."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AfterValidator, AnyUrl, TypeAdapter, UrlConstraints

_REGISTERED_SYSTEMS = frozenset({"geonames", "google_ads", "microsoft_ads"})


@lru_cache(maxsize=1)
def _https_system_adapter() -> TypeAdapter[AnyUrl]:
    return TypeAdapter(
        Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"], host_required=True)]
    )


def _validate_geo_system_key(value: str) -> str:
    """Validate the namespace and preserve its exact opaque wire spelling."""
    if value not in _REGISTERED_SYSTEMS:
        _https_system_adapter().validate_python(value)
    return value


GeoPlaceSystemKey = Annotated[str, AfterValidator(_validate_geo_system_key)]
