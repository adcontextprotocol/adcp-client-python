"""Response-shape projections that strip write-only fields.

The AdCP spec marks certain fields as ``writeOnly: true`` — present in
requests so adopters can populate them, but MUST NOT be echoed in
responses. ``BusinessEntity.bank`` and notification authentication credentials
flow into the seller during account setup and stay there. Pydantic's default
serialization round-trips everything, so an adopter who reuses an internal
``Account`` model on the response path can leak secrets without realizing it.

The projections here type-narrow the write-only fields to ``None``:
construction with a non-None value raises ``ValidationError``, and the
serialization path excludes the fields regardless. Adopters opt in by
constructing the ``*Response`` variant on the response edge, or by
piping through ``to_account_response()``.

Out of scope:

* ``GovernanceAgent.authentication`` (also write-only): generated nested
  type, separate adopter contract; track separately.
* ``reporting_bucket``: NOT write-only — seller-provisioned, buyer-readable
  for offline delivery coordinates. Preserved as-is by the projection.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Annotated, Any

from pydantic import ConfigDict, Field, field_validator

from adcp._version import normalize_to_release_precision
from adcp.types import Account, BusinessEntity, NotificationAuthentication, NotificationConfig
from adcp.types.capabilities import GeoPostalAreas, LegacyPostalCodeSystem
from adcp.types.variants import SchemaVariant

_NATIVE_TO_LEGACY_POSTAL: dict[tuple[str, str], LegacyPostalCodeSystem] = {
    ("US", "zip"): LegacyPostalCodeSystem.us_zip,
    ("US", "zip_plus_four"): LegacyPostalCodeSystem.us_zip_plus_four,
    ("GB", "outward"): LegacyPostalCodeSystem.gb_outward,
    ("GB", "full"): LegacyPostalCodeSystem.gb_full,
    ("CA", "fsa"): LegacyPostalCodeSystem.ca_fsa,
    ("CA", "full"): LegacyPostalCodeSystem.ca_full,
    ("DE", "plz"): LegacyPostalCodeSystem.de_plz,
    ("FR", "code_postal"): LegacyPostalCodeSystem.fr_code_postal,
    ("AU", "postcode"): LegacyPostalCodeSystem.au_postcode,
    ("CH", "plz"): LegacyPostalCodeSystem.ch_plz,
    ("AT", "plz"): LegacyPostalCodeSystem.at_plz,
}
_LEGACY_TO_NATIVE_POSTAL: dict[str, tuple[str, str]] = {
    legacy.value: native for native, legacy in _NATIVE_TO_LEGACY_POSTAL.items()
}
_COUNTRY_KEY_RE = re.compile(r"^[A-Z]{2}$")


def _is_native_geo_postal_version(version: str | None) -> bool:
    """Return whether ``version`` should emit the AdCP 3.1 postal shape."""
    if version is None:
        return False
    try:
        normalized = normalize_to_release_precision(version)
    except ValueError:
        return False
    release = normalized.split("-", 1)[0]
    major_raw, minor_raw = release.split(".", 1)
    try:
        major = int(major_raw)
        minor = int(minor_raw)
    except ValueError:
        return False
    return major > 3 or (major == 3 and minor >= 1)


def _postal_system_value(system: Any) -> str:
    return str(system.value if hasattr(system, "value") else system)


def _append_unique(target: dict[str, list[str]], country: str, system: str) -> None:
    systems = target.setdefault(country, [])
    if system not in systems:
        systems.append(system)


def _geo_postal_payload(value: GeoPostalAreas | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return value.model_dump(mode="json", exclude_none=True)


def _iter_native_postal_systems(payload: Mapping[str, Any]) -> Iterator[tuple[str, Any]]:
    for country, systems in payload.items():
        if not _COUNTRY_KEY_RE.fullmatch(country) or not systems:
            continue
        yield country, systems


def project_geo_postal_areas(
    value: GeoPostalAreas | Mapping[str, Any],
    version: str | None,
) -> dict[str, Any]:
    """Project postal capability declarations for the caller's AdCP version.

    AdCP 3.1 introduced native country-keyed postal capabilities such as
    ``{"US": ["zip"]}``. AdCP 3.0 clients expect the deprecated fused
    booleans such as ``{"us_zip": true}``. This helper lets sellers keep
    one typed :class:`GeoPostalAreas` declaration and serializes only the
    shape the caller negotiated.

    Native systems with no legacy 3.0 alias (currently BR ``cep``, IN
    ``pin``, and ZA ``postal_code``) are omitted from 3.0 projections.
    Legacy booleans set to ``False`` are treated as absent so projection
    never invents support.
    """
    payload = _geo_postal_payload(value)
    if _is_native_geo_postal_version(version):
        projected: dict[str, list[str]] = {}
        for country, systems in _iter_native_postal_systems(payload):
            for system in systems:
                _append_unique(projected, country, _postal_system_value(system))
        for legacy in LegacyPostalCodeSystem:
            if payload.get(legacy.value) is not True:
                continue
            country, system = _LEGACY_TO_NATIVE_POSTAL[legacy.value]
            _append_unique(projected, country, system)
        return projected

    projected_legacy: dict[str, bool] = {}
    for country, systems in _iter_native_postal_systems(payload):
        for system in systems:
            legacy_alias = _NATIVE_TO_LEGACY_POSTAL.get((country, _postal_system_value(system)))
            if legacy_alias is not None:
                projected_legacy[legacy_alias.value] = True
    for legacy in LegacyPostalCodeSystem:
        if payload.get(legacy.value) is True:
            projected_legacy[legacy.value] = True
    return projected_legacy


class BusinessEntityResponse(BusinessEntity):
    """Response projection of :class:`BusinessEntity` with bank details stripped.

    Per AdCP 3.0.x ``core/business-entity.json``: ``bank.*`` fields carry
    ``writeOnly: true`` and MUST NOT appear in responses. Sellers store
    bank coordinates and confirm receipt without echoing them.

    This subclass enforces the contract two ways:

    * Construction: passing ``bank=...`` raises ``ValidationError``.
    * Serialization: the field is excluded from ``model_dump()`` output
      even if some path mutated it post-construction (defense in depth
      against ``model_copy()``, idempotency replay caches, etc.).
    """

    bank: Any = Field(default=None, exclude=True)

    @field_validator("bank", mode="before")
    @classmethod
    def _reject_bank(cls, v: Any) -> None:
        if v is not None:
            raise ValueError(
                "BusinessEntityResponse must not carry bank details — bank is "
                "write-only per AdCP spec. Drop the field before constructing "
                "a response, or use to_account_response() to strip it."
            )
        return None


class _NotificationAuthenticationResponse(NotificationAuthentication):
    """Response projection of legacy notification authentication."""

    model_config = ConfigDict(extra="forbid")

    credentials: Any = Field(default=None, exclude=True)

    @field_validator("credentials", mode="before")
    @classmethod
    def _reject_credentials(cls, v: Any) -> None:
        if v is not None:
            raise ValueError(
                "Notification authentication credentials are write-only and "
                "must not be included in an AccountResponse. Drop the field "
                "before constructing a response, or use to_account_response() "
                "to strip it."
            )
        return None


class _NotificationConfigResponse(NotificationConfig):
    """Account notification config with write-only credentials stripped."""

    authentication: SchemaVariant[_NotificationAuthenticationResponse | None] = None


class AccountResponse(Account):
    """Response projection of :class:`Account` — billing_entity is the
    bank-stripped variant.

    Use this on the response edge of any handler that returns account
    state (``list_accounts``, ``get_account_financials``, etc.) when
    your internal ``Account`` records carry bank details. For convenience,
    :func:`to_account_response` projects an existing ``Account`` instance
    to an ``AccountResponse`` and drops bank along the way.
    """

    billing_entity: BusinessEntityResponse | None = None
    notification_configs: SchemaVariant[
        Annotated[list[_NotificationConfigResponse], Field(max_length=16)] | None
    ] = None


def to_account_response(account: Account) -> AccountResponse:
    """Project an internal ``Account`` to its response shape.

    Strips ``billing_entity.bank`` and notification authentication credentials,
    then returns an :class:`AccountResponse`.
    The remaining fields (legal_name, tax_id, address, contacts, vat_id,
    registration_number, ext) round-trip unchanged. ``reporting_bucket``,
    ``governance_agents``, and other non-write-only fields are preserved.

    Raises:
        ValidationError: if the source ``Account`` fails revalidation
            against the response shape (other than the bank strip).
    """
    payload = account.model_dump(mode="python")
    if isinstance(payload.get("billing_entity"), dict):
        payload["billing_entity"].pop("bank", None)
    for config in payload.get("notification_configs") or []:
        if isinstance(config, dict) and isinstance(config.get("authentication"), dict):
            config["authentication"].pop("credentials", None)
    return AccountResponse.model_validate(payload)


__all__ = [
    "AccountResponse",
    "BusinessEntityResponse",
    "project_geo_postal_areas",
    "to_account_response",
]
