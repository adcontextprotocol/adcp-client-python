"""The seller-side offering checks run before reporting subscriptions are enabled.

AdCP 3.3.0-beta.4 (``offering-checks.json`` beside the coverage vectors):

* ``SUBSCRIPTIONS_REQUIRE_STANDARD_DIMENSIONS``: every offering publishes
  ``standard_dimensions`` (``core/reporting-delivery-offering.json``), so a
  buyer can resolve a selector against it.
* ``SUBSCRIPTIONS_REQUIRE_ACCOUNT_ID_KEY``: every managed-delivery offering
  (one with ``method``) keys its rows by account
  (``x-adcp-validation.subscription_rows``). Its
  ``reporting_profile.primary_keys`` include ``account_id`` (``missing:
  "primary_key"``), and its row schema (``schema_uri``), when the SDK has
  fetched it, lists ``account_id`` in ``required`` and declares it with
  ``type: "string"`` (``missing: "row_schema"``). Core offerings, which have
  no ``method``, are exempt.

:func:`check_subscription_offerings` reports every failure, in offering order
and, per offering, in that order. Both codes are seller configuration errors
(:class:`~adcp.reporting.ReliableReportingConfigurationError`), not wire codes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from adcp.reporting._errors import ReliableReportingConfigurationError

__all__ = [
    "SubscriptionOfferingFailure",
    "check_subscription_offerings",
]


@dataclass(frozen=True)
class SubscriptionOfferingFailure:
    """One offering that cannot serve subscriptions, and why.

    ``missing`` is set for ``SUBSCRIPTIONS_REQUIRE_ACCOUNT_ID_KEY`` only:
    ``primary_key`` or ``row_schema``.
    """

    code: Literal[
        "SUBSCRIPTIONS_REQUIRE_STANDARD_DIMENSIONS", "SUBSCRIPTIONS_REQUIRE_ACCOUNT_ID_KEY"
    ]
    offering_id: str
    missing: Literal["primary_key", "row_schema"] | None = None

    def as_dict(self) -> dict[str, str]:
        """The failure as ``offering-checks.json`` ``expected[]`` spells it."""
        result = {"code": self.code, "offering_id": self.offering_id}
        if self.missing is not None:
            result["missing"] = self.missing
        return result

    @property
    def message(self) -> str:
        """What is wrong, ending with the fix (``error-codes.json`` ``message_fix``)."""
        if self.code == "SUBSCRIPTIONS_REQUIRE_STANDARD_DIMENSIONS":
            return (
                f"offering {self.offering_id!r} has no standard_dimensions, so a buyer cannot "
                "resolve a subscription selector against it. Publish standard_dimensions "
                "(empty for totals only) in its capability_offerings entry."
            )
        where = (
            "its reporting_profile.primary_keys do not include account_id"
            if self.missing == "primary_key"
            else "its row schema does not declare account_id as a required string property"
        )
        return (
            f"offering {self.offering_id!r} is delivered to destinations, but {where}. Add "
            "account_id to the offering's reporting_profile.primary_keys and as a required "
            "string property of its row schema."
        )

    def error(self) -> ReliableReportingConfigurationError:
        """This failure as a :class:`~adcp.reporting.ReliableReportingConfigurationError`."""
        from adcp.reporting._errors import ReliableReportingConfigurationError

        return ReliableReportingConfigurationError(
            self.message,
            code=self.code,
            config_field="capability_offerings",
            context={key: value for key, value in self.as_dict().items() if key != "code"},
        )


def _get(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _plain(offering: Any) -> Any:
    if hasattr(offering, "model_dump"):
        return offering.model_dump(mode="json", exclude_none=True, by_alias=True)
    return offering


def _declares_required_string(schema: Any, name: str) -> bool:
    if not isinstance(schema, Mapping):
        return False
    properties = schema.get("properties")
    declared = properties.get(name) if isinstance(properties, Mapping) else None
    required = schema.get("required")
    return (
        isinstance(required, list)
        and name in required
        and isinstance(declared, Mapping)
        and declared.get("type") == "string"
    )


def check_subscription_offerings(
    offerings: Iterable[Any],
    row_schemas: Mapping[str, Any] | None = None,
) -> list[SubscriptionOfferingFailure]:
    """Every reason the offerings cannot serve subscriptions; empty when they can.

    :param offerings: the advertised ``core/reporting-delivery-offering.json``
        offerings, as models or mappings.
    :param row_schemas: the row schemas fetched so far, keyed by
        ``reporting_profile.schema_uri``. A schema not in it is not checked.
    """
    schemas = row_schemas or {}
    failures: list[SubscriptionOfferingFailure] = []
    for raw in offerings:
        offering = _plain(raw)
        offering_id = str(_get(offering, "offering_id"))
        if not isinstance(_get(offering, "standard_dimensions"), (list, tuple)):
            failures.append(
                SubscriptionOfferingFailure(
                    "SUBSCRIPTIONS_REQUIRE_STANDARD_DIMENSIONS", offering_id
                )
            )
        if _get(offering, "method") is None:
            continue  # Core offerings are exempt.
        profile = _get(offering, "reporting_profile") or {}
        if "account_id" not in (_get(profile, "primary_keys") or ()):
            failures.append(
                SubscriptionOfferingFailure(
                    "SUBSCRIPTIONS_REQUIRE_ACCOUNT_ID_KEY", offering_id, "primary_key"
                )
            )
        schema_uri = _get(profile, "schema_uri")
        if schema_uri is not None and str(schema_uri) in schemas:
            if not _declares_required_string(schemas[str(schema_uri)], "account_id"):
                failures.append(
                    SubscriptionOfferingFailure(
                        "SUBSCRIPTIONS_REQUIRE_ACCOUNT_ID_KEY", offering_id, "row_schema"
                    )
                )
    return failures
