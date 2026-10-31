"""Accept, validate and version the ``sync_principal`` ``reporting_subscriptions`` section.

The section is complete desired state, like the principal's other sections.
:func:`apply_reporting_subscriptions` turns the caller's desired list and the
previously stored states into the new ``core/reporting-subscription-state.json``
states, or raises :class:`ReportingSubscriptionValidationError` so the whole request
fails. It enforces what the generated model cannot express (the schema's
``allOf`` conditionals and ``x-adcp-validation`` rules):

* ``scope`` is exactly one of ``all_authorized_accounts: true`` or
  ``account_ids``; ``exclude_account_ids`` only with all-accounts scope;
* ``period_timezone`` only when ``account_ids`` names exactly one account, and
  it must be a recognized IANA zone;
* billing requires ``official`` finality, ``consumer_receipt`` and
  ``match: exact`` with ``dimensions`` (adcp#8619), and ``consumer_receipt``
  requires a destination;
* ``destination_id`` names a destination that exists after the same request
  applies; ``path_template`` uses only the four placeholders, contains
  ``{account_id}`` when the scope can cover several accounts or another
  active subscription shares the destination (and is then required for
  ``file_transfer``), and shared templates differ;
* ``subscription_id`` values are unique and never equal a ``delivery_config_id``
  the caller uses through ``sync_accounts``;
* two active subscriptions at the same scope level may not both apply to one
  account and ``feed_purpose``.

Versioning: a ``subscription_version`` is immutable. Resending it with
different semantic content is rejected; ``active`` and
``replace_account_configs`` change in place. A higher version records its own
``accepted_at`` and moves the old one to ``prior_versions`` with
``superseded_at``. Omitting a subscription, or ``active: false``, deactivates
it at ``deactivated_at``; it stays readable as ``inactive``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

from adcp.reporting.subscriptions._clock import iso_z, require_zone, utc_instant
from adcp.types.domains.core.reporting_subscription import ReportingSubscription
from adcp.types.domains.core.reporting_subscription_state import ReportingSubscriptionState

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_ALLOWED_PLACEHOLDERS = frozenset({"account_id", "subscription_id", "offering_id", "period_start"})
#: Fields that may change without a new ``subscription_version``.
_IN_PLACE_FIELDS = frozenset({"active", "replace_account_configs", "subscription_version"})
#: Inline readback caps (adcp#8619); each list carries its total count.
PRIOR_VERSIONS_INLINE = 16
ACCOUNTS_INLINE = 1000


class ReportingSubscriptionValidationError(ValueError):
    """The section is invalid; the whole ``sync_principal`` request fails."""

    def __init__(self, message: str, *, code: str = "INVALID_REQUEST") -> None:
        super().__init__(message)
        self.code = code


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", exclude_none=True)
        assert isinstance(dumped, dict)
        return dumped
    return dict(value)


def _semantic(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """The semantic content of a version, canonicalized.

    ``uniqueItems`` arrays compare as sets and an explicit default ``match``
    equals an omitted one, so a resend that only reorders or spells out a
    default is the same version.
    """
    payload = {key: value for key, value in configuration.items() if key not in _IN_PLACE_FIELDS}
    scope = dict(payload.get("scope") or {})
    if "account_ids" in scope:
        scope["account_ids"] = sorted(scope["account_ids"])
    payload["scope"] = scope
    for name in ("exclude_account_ids", "operator_domains"):
        if name in payload:
            payload[name] = sorted(payload[name])
    selector = dict(payload.get("selector") or {})
    if "dimensions" in selector:
        selector["dimensions"] = sorted(selector["dimensions"])
    selector.setdefault("match", "exact" if "dimensions" in selector else "finest")
    payload["selector"] = selector
    return payload


def _destinations(destinations: Sequence[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in destinations:
        payload = _dump(item)
        result[str(payload["destination_id"])] = payload
    return result


def _multi_account(configuration: Mapping[str, Any]) -> bool:
    scope = configuration.get("scope") or {}
    return scope.get("all_authorized_accounts") is True or len(scope.get("account_ids") or ()) > 1


def _validate_one(
    configuration: dict[str, Any],
    index: int,
    *,
    destinations: Mapping[str, Mapping[str, Any]],
    delivery_config_ids: Collection[str],
    replace_account_configs: bool = False,
) -> None:
    where = f"reporting_subscriptions[{index}]"
    subscription_id = configuration["subscription_id"]
    if subscription_id in delivery_config_ids:
        raise ReportingSubscriptionValidationError(
            f"{where}.subscription_id equals a delivery_config_id this caller uses; "
            "subscription_id and delivery_config_id share one namespace"
        )
    scope = configuration.get("scope") or {}
    has_all = "all_authorized_accounts" in scope
    has_ids = "account_ids" in scope
    if has_all == has_ids:
        raise ReportingSubscriptionValidationError(
            f"{where}.scope must be exactly one of all_authorized_accounts or account_ids"
        )
    if has_all and scope["all_authorized_accounts"] is not True:
        raise ReportingSubscriptionValidationError(
            f"{where}.scope.all_authorized_accounts must be true"
        )
    if configuration.get("exclude_account_ids") and not has_all:
        raise ReportingSubscriptionValidationError(
            f"{where}.exclude_account_ids is valid only with all_authorized_accounts scope"
        )
    if configuration.get("replace_account_configs") is True and not replace_account_configs:
        # Accepting it without a runtime that applies it would leave the
        # caller's account-level configurations in force while it believes
        # they were cut over.
        raise ReportingSubscriptionValidationError(
            f"{where}.replace_account_configs is not supported by this seller",
            code="UNSUPPORTED_FEATURE",
        )
    selector = configuration["selector"]
    period_timezone = selector.get("period_timezone")
    if period_timezone is not None:
        if not has_ids or len(scope["account_ids"]) != 1:
            raise ReportingSubscriptionValidationError(
                f"{where}.selector.period_timezone requires account_ids naming exactly one account"
            )
        try:
            require_zone(period_timezone, field="period_timezone")
        except ValueError as error:
            raise ReportingSubscriptionValidationError(f"{where}.selector.{error}") from None
    mode = configuration["reconciliation_mode"]
    if selector["feed_purpose"] == "billing" and (
        selector["required_finality"] != "official" or mode != "consumer_receipt"
    ):
        raise ReportingSubscriptionValidationError(
            f"{where}: billing subscriptions require official finality and consumer_receipt"
        )
    if selector["feed_purpose"] == "billing" and (
        not selector.get("dimensions") or selector.get("match", "exact") != "exact"
    ):
        # adcp#8619: a billing feed's breakdown never depends on which
        # offerings happen to exist.
        raise ReportingSubscriptionValidationError(
            f"{where}: billing subscriptions require match exact with dimensions"
        )
    destination = configuration.get("destination")
    if mode == "consumer_receipt" and destination is None:
        raise ReportingSubscriptionValidationError(
            f"{where}: consumer_receipt requires a destination"
        )
    if destination is None:
        return
    target = destinations.get(destination["destination_id"])
    if target is None:
        raise ReportingSubscriptionValidationError(
            f"{where}.destination.destination_id {destination['destination_id']!r} is not in "
            "reporting_destinations after this request"
        )
    template = destination.get("path_template")
    if template is not None:
        unknown = {name for name in _PLACEHOLDER.findall(template)} - _ALLOWED_PLACEHOLDERS
        if unknown:
            raise ReportingSubscriptionValidationError(
                f"{where}.destination.path_template uses unknown placeholders {sorted(unknown)}"
            )
        if "{" in _PLACEHOLDER.sub("", template) or "}" in _PLACEHOLDER.sub("", template):
            raise ReportingSubscriptionValidationError(
                f"{where}.destination.path_template has braces outside a placeholder"
            )
    if _multi_account(configuration):
        pattern = (target.get("configuration") or target).get("pattern")
        if template is None and pattern == "file_transfer":
            raise ReportingSubscriptionValidationError(
                f"{where}.destination.path_template is required for a multi-account "
                "file_transfer subscription"
            )
        if template is not None and "{account_id}" not in template:
            raise ReportingSubscriptionValidationError(
                f"{where}.destination.path_template must contain {{account_id}} when the "
                "scope can cover more than one account"
            )


def _validate_set(active: Sequence[dict[str, Any]], destinations: Mapping[str, Any]) -> None:
    by_destination: dict[str, list[dict[str, Any]]] = {}
    for configuration in active:
        destination = configuration.get("destination")
        if destination is not None:
            by_destination.setdefault(destination["destination_id"], []).append(configuration)
    for destination_id, sharing in by_destination.items():
        if len(sharing) < 2:
            continue
        templates = [(item.get("destination") or {}).get("path_template") for item in sharing]
        if any(template is None or "{account_id}" not in template for template in templates):
            raise ReportingSubscriptionValidationError(
                f"active subscriptions sharing destination {destination_id!r} must each have a "
                "path_template containing {account_id}"
            )
        if len(set(templates)) != len(templates):
            raise ReportingSubscriptionValidationError(
                f"active subscriptions sharing destination {destination_id!r} must use "
                "different path_template values"
            )
    for first_index, first in enumerate(active):
        for second in active[first_index + 1 :]:
            if first["selector"]["feed_purpose"] != second["selector"]["feed_purpose"]:
                continue
            if _same_level_overlap(first, second):
                raise ReportingSubscriptionValidationError(
                    f"active subscriptions {first['subscription_id']!r} and "
                    f"{second['subscription_id']!r} would both apply to one account and "
                    "feed_purpose at the same scope level"
                )


def _same_level_overlap(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    first_scope, second_scope = first["scope"], second["scope"]
    if "account_ids" in first_scope and "account_ids" in second_scope:
        shared = set(first_scope["account_ids"]) & set(second_scope["account_ids"])
        if not shared:
            return False
        return _operator_domains_overlap(first, second)
    if "all_authorized_accounts" in first_scope and "all_authorized_accounts" in second_scope:
        # exclude_account_ids cannot make two all-accounts scopes disjoint for
        # accounts granted later; disjoint operator_domains can.
        return _operator_domains_overlap(first, second)
    return False


def _operator_domains_overlap(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    first_domains = first.get("operator_domains")
    second_domains = second.get("operator_domains")
    if not first_domains or not second_domains:
        return True
    return bool(set(first_domains) & set(second_domains))


def apply_reporting_subscriptions(
    previous: Sequence[ReportingSubscriptionState | Mapping[str, Any]],
    desired: Sequence[ReportingSubscription | Mapping[str, Any]],
    *,
    destinations: Sequence[Any],
    now: datetime,
    delivery_config_ids: Collection[str] = (),
    replace_account_configs: bool = False,
) -> list[ReportingSubscriptionState]:
    """Return the new subscription states for a ``sync_principal`` request.

    ``destinations`` are the caller's ``reporting_destinations`` states after
    this request applies. ``delivery_config_ids`` are every
    ``delivery_config_id`` the caller uses through ``sync_accounts``.
    ``replace_account_configs`` accepts that flag; only a runtime that applies
    it passes ``True``, and otherwise it is refused with ``UNSUPPORTED_FEATURE``.
    Engine-derived fields (``accounts_with_exceptions``,
    ``accounts_unauthorized``, ``issues``) carry over from ``previous``.
    """
    now = utc_instant(now, field="now")
    stamp = iso_z(now)
    by_id = {str(_dump(item)["subscription_id"]): _dump(item) for item in previous}
    targets = _destinations(destinations)
    configurations = [_dump(item) for item in desired]
    seen: set[str] = set()
    for index, configuration in enumerate(configurations):
        subscription_id = configuration["subscription_id"]
        if subscription_id in seen:
            raise ReportingSubscriptionValidationError(
                f"reporting_subscriptions[{index}].subscription_id duplicates an earlier entry"
            )
        seen.add(subscription_id)
        _validate_one(
            configuration,
            index,
            destinations=targets,
            delivery_config_ids=delivery_config_ids,
            replace_account_configs=replace_account_configs,
        )
    _validate_set([item for item in configurations if item.get("active", True)], targets)

    states: list[ReportingSubscriptionState] = []
    for configuration in configurations:
        old = by_id.pop(configuration["subscription_id"], None)
        states.append(_next_state(old, configuration, stamp=stamp, destinations=targets))
    for old in by_id.values():
        # Omitted: deactivated by cutover now, retained as inactive for history.
        states.append(_deactivated(old, stamp=stamp))
    return [ReportingSubscriptionState.model_validate(item) for item in _sorted(states)]


def _sorted(states: Sequence[ReportingSubscriptionState]) -> list[dict[str, Any]]:
    return sorted((_dump(item) for item in states), key=lambda item: item["subscription_id"])


def _deactivated(old: dict[str, Any], *, stamp: str) -> ReportingSubscriptionState:
    payload = dict(old)
    if payload.get("state") != "inactive":
        configuration = dict(payload["configuration"])
        configuration["active"] = False
        payload.update(state="inactive", deactivated_at=stamp, configuration=configuration)
        payload.pop("issues", None)
        payload["inactive_intervals"] = _intervals(
            [*(old.get("inactive_intervals") or ()), {"deactivated_at": stamp}]
        )
    return ReportingSubscriptionState.model_validate(payload)


#: ``inactive_intervals`` holds at most this many entries (the schema's maxItems).
_MAX_INACTIVE_INTERVALS = 64


def _intervals(intervals: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every interval, or an error: dropping one would make its periods owed again."""
    if len(intervals) > _MAX_INACTIVE_INTERVALS:
        raise ReportingSubscriptionValidationError(
            f"the subscription has reached {_MAX_INACTIVE_INTERVALS} inactive intervals, the "
            "readback's limit; register a new subscription_id instead of deactivating again"
        )
    return [dict(item) for item in intervals]


def _next_state(
    old: dict[str, Any] | None,
    configuration: dict[str, Any],
    *,
    stamp: str,
    destinations: Mapping[str, Mapping[str, Any]],
) -> ReportingSubscriptionState:
    version = int(configuration["subscription_version"])
    active = bool(configuration.get("active", True))
    payload: dict[str, Any] = {
        "subscription_id": configuration["subscription_id"],
        "subscription_version": version,
        "configuration": configuration,
    }
    if old is None:
        payload["accepted_at"] = stamp
    else:
        old_version = int(old["subscription_version"])
        if version < old_version:
            raise ReportingSubscriptionValidationError(
                f"subscription {configuration['subscription_id']!r} version {version} is older "
                f"than the accepted version {old_version}"
            )
        if version == old_version:
            if _semantic(configuration) != _semantic(old["configuration"]):
                raise ReportingSubscriptionValidationError(
                    f"subscription {configuration['subscription_id']!r} reuses version "
                    f"{version} with different semantic content; increment subscription_version"
                )
            payload["accepted_at"] = old["accepted_at"]
            if old.get("prior_versions"):
                payload["prior_versions"] = old["prior_versions"]
                payload["prior_versions_count"] = old.get(
                    "prior_versions_count", len(old["prior_versions"])
                )
        else:
            payload["accepted_at"] = stamp
            prior = [
                {
                    "subscription_version": old_version,
                    "accepted_at": old["accepted_at"],
                    "superseded_at": stamp,
                    # adcp#8588: sellers MUST include the version's configuration.
                    "configuration": old["configuration"],
                },
                *(old.get("prior_versions") or ()),
            ]
            # adcp#8619: the 16 newest inline, with the total; the rest are
            # paged through get_principal reporting_subscription_page.
            total = int(old.get("prior_versions_count") or len(old.get("prior_versions") or ())) + 1
            payload["prior_versions"] = prior[:PRIOR_VERSIONS_INLINE]
            payload["prior_versions_count"] = max(total, len(prior))
        for carried in ("accounts_with_exceptions", "accounts_unauthorized"):
            if old.get(carried):
                payload[carried] = old[carried]
                payload[f"{carried}_count"] = old.get(f"{carried}_count", len(old[carried]))
    # inactive_intervals (adcp#8585) let a buyer recompute a reactivation in
    # place, which the readback otherwise loses with deactivated_at.
    intervals = [dict(item) for item in (old or {}).get("inactive_intervals") or ()]
    was_inactive = old is not None and old.get("state") == "inactive"
    if active:
        payload["state"] = "active"
        if was_inactive and intervals and "reactivated_at" not in intervals[-1]:
            intervals[-1]["reactivated_at"] = stamp
    else:
        payload["state"] = "inactive"
        if not was_inactive or not intervals or "reactivated_at" in intervals[-1]:
            start = old["deactivated_at"] if was_inactive and old is not None else stamp
            intervals.append({"deactivated_at": start})
        payload["deactivated_at"] = intervals[-1]["deactivated_at"]
    if intervals:
        payload["inactive_intervals"] = _intervals(intervals)
    destination = configuration.get("destination")
    if destination is not None:
        target = destinations[destination["destination_id"]]
        payload["destination_ref"] = target["destination_ref"]
        if active:
            issue = _destination_issue(target)
            if issue is not None:
                payload["state"] = "action_required"
                payload["issues"] = [issue]
    return ReportingSubscriptionState.model_validate(payload)


def _destination_issue(target: Mapping[str, Any]) -> dict[str, Any] | None:
    """A destination the caller must act on blocks delivery everywhere."""
    state = str(target.get("state"))
    if state in {"action_required", "inactive", "rejected"}:
        return {
            "code": "DESTINATION_NOT_READY",
            "message": f"destination {target['destination_id']!r} is {state}",
            "field": "destination.destination_id",
        }
    return None


def render_path_template(
    template: str,
    *,
    account_id: str,
    subscription_id: str,
    offering_id: str,
    period_start: datetime,
) -> str:
    """Expand a subscription ``path_template`` for one delivery.

    Each substituted value fills exactly one path segment: a value containing
    ``/`` (or ``\\``, for backslash-separated targets) is percent-encoded
    (RFC 3986), and a value equal to ``.`` or ``..`` is encoded too, so no
    value can add or escape a segment.
    ``period_start`` is written as ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    values = {
        "account_id": account_id,
        "subscription_id": subscription_id,
        "offering_id": offering_id,
        "period_start": iso_z(period_start),
    }

    def segment(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"unknown path_template placeholder {{{name}}}")
        value = values[name]
        if value in {".", ".."}:
            return value.replace(".", "%2E")
        if "/" in value or "\\" in value:
            # "/" per the spec; "\\" too, so a backslash-separated target
            # (SMB, UNC) cannot gain or escape a segment either.
            return quote(value, safe="")
        return value

    return _PLACEHOLDER.sub(segment, template)


def refresh_destination_states(
    states: Sequence[ReportingSubscriptionState | Mapping[str, Any]],
    destinations: Sequence[Any],
) -> list[ReportingSubscriptionState]:
    """Recompute ``state``, ``issues`` and ``destination_ref`` after a destination changes."""
    targets = _destinations(destinations)
    refreshed: list[ReportingSubscriptionState] = []
    for item in states:
        payload = _dump(item)
        destination = payload["configuration"].get("destination")
        if payload.get("state") != "inactive" and destination is not None:
            target = targets.get(destination["destination_id"])
            issue = (
                {
                    "code": "DESTINATION_NOT_READY",
                    "message": f"destination {destination['destination_id']!r} was removed",
                    "field": "destination.destination_id",
                }
                if target is None
                else _destination_issue(target)
            )
            if target is not None:
                payload["destination_ref"] = target["destination_ref"]
            if issue is None:
                payload["state"] = "active"
                payload.pop("issues", None)
            else:
                payload["state"] = "action_required"
                payload["issues"] = [issue]
        refreshed.append(ReportingSubscriptionState.model_validate(payload))
    return refreshed


__all__ = [
    "ReportingSubscriptionValidationError",
    "apply_reporting_subscriptions",
    "refresh_destination_states",
    "render_path_template",
]
