"""Helpers for interpreting ``update_media_buy`` patch requests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, TypeAlias

UpdateMutationResolution: TypeAlias = Literal["fine", "coarse", "unknown"]

UNKNOWN_UPDATE_ACTION = "unknown"


@dataclass(frozen=True, slots=True)
class UpdateMediaBuyMutation:
    """A single logical mutation requested by an ``update_media_buy`` patch.

    ``action`` is the most specific action inferred from the patch. When the
    patch does not carry enough information to choose a fine-grained action,
    the helper emits the closest coarse action and marks ``resolution`` as
    ``"coarse"``.

    ``allowed_action_candidates`` are ordered from specific to broad. A caller
    can use them to match a mutation against either fine-grained capabilities
    such as ``increase_budget`` or coarse capabilities such as
    ``update_budget`` / ``update_packages``.
    """

    action: str
    field_paths: tuple[str, ...]
    package_id: str | None = None
    before: Any = None
    after: Any = None
    raw: Any = None
    resolution: UpdateMutationResolution = "fine"
    allowed_action_candidates: tuple[str, ...] = field(default_factory=tuple)

    def is_allowed_by(self, allowed_actions: Iterable[Any] | None) -> bool:
        """Return whether ``allowed_actions`` covers this mutation.

        ``allowed_actions`` may be a simple string list or the wire
        ``available_actions[]`` shape returned by ``get_media_buys``.
        """

        allowed = set(normalize_update_media_buy_allowed_actions(allowed_actions))
        return any(candidate in allowed for candidate in self.allowed_action_candidates)


_TOP_LEVEL_METADATA_FIELDS = {
    "account",
    "adcp_major_version",
    "adcp_version",
    "context",
    "ext",
    "idempotency_key",
    "media_buy_id",
    "revision",
}

_TOP_LEVEL_KNOWN_MUTATION_FIELDS = {
    "canceled",
    "cancellation_reason",
    "end_time",
    "invoice_recipient",
    "new_packages",
    "packages",
    "paused",
    "push_notification_config",
    "reporting_webhook",
    "start_time",
}

_TOP_LEVEL_UNMAPPED_MUTATION_FIELDS = (
    "invoice_recipient",
    "push_notification_config",
    "reporting_webhook",
)

_PACKAGE_METADATA_FIELDS = {"context", "ext", "package_id"}

_PACKAGE_KNOWN_MUTATION_FIELDS = {
    "bid_price",
    "budget",
    "canceled",
    "cancellation_reason",
    "catalogs",
    "creative_assignments",
    "creatives",
    "end_time",
    "impressions",
    "keyword_targets_add",
    "keyword_targets_remove",
    "negative_keywords_add",
    "negative_keywords_remove",
    "optimization_goals",
    "pacing",
    "paused",
    "start_time",
    "targeting_overlay",
}

_PACKAGE_COARSE_FIELDS = {
    "bid_price",
    "catalogs",
    "impressions",
    "optimization_goals",
}

_ACTION_CANDIDATES: dict[str, tuple[str, ...]] = {
    "add_packages": ("add_packages", "update_packages"),
    "cancel": ("cancel",),
    "decrease_budget": ("decrease_budget", "update_budget", "update_packages"),
    "extend_flight": ("extend_flight", "update_dates", "update_packages"),
    "increase_budget": ("increase_budget", "update_budget", "update_packages"),
    "pause": ("pause",),
    "reallocate_budget": ("reallocate_budget", "update_budget", "update_packages"),
    "remove_creative": ("remove_creative", "sync_creatives", "update_packages"),
    "remove_packages": ("remove_packages", "update_packages"),
    "replace_creative": ("replace_creative", "sync_creatives", "update_packages"),
    "resume": ("resume",),
    "shorten_flight": ("shorten_flight", "update_dates", "update_packages"),
    "sync_creatives": ("sync_creatives", "update_packages"),
    "update_budget": ("update_budget", "update_packages"),
    "update_creative_assignments": (
        "update_creative_assignments",
        "sync_creatives",
        "update_packages",
    ),
    "update_dates": ("update_dates", "update_packages"),
    "update_flight_dates": ("update_flight_dates", "update_dates", "update_packages"),
    "update_frequency_caps": (
        "update_frequency_caps",
        "update_targeting",
        "update_packages",
    ),
    "update_packages": ("update_packages",),
    "update_pacing": ("update_pacing", "update_packages"),
    "update_targeting": ("update_targeting", "update_packages"),
}


def decompose_update_media_buy(
    patch: Any,
    current_media_buy: Any | None = None,
) -> list[UpdateMediaBuyMutation]:
    """Split an ``update_media_buy`` patch into ordered logical mutations.

    ``patch`` may be a generated Pydantic ``UpdateMediaBuyRequest`` or a plain
    mapping. ``current_media_buy`` is optional; when supplied, the helper can
    promote coarse mutations into specific actions such as ``increase_budget``,
    ``decrease_budget``, ``extend_flight``, ``shorten_flight``, and
    ``reallocate_budget``.
    """

    patch_dict = _to_plain_mapping(patch)
    current_dict = _to_plain_mapping(current_media_buy) if current_media_buy is not None else {}
    current_packages = _index_packages(current_dict.get("packages"))

    mutations: list[UpdateMediaBuyMutation] = []

    if patch_dict.get("paused") is True:
        mutations.append(
            _mutation(
                "pause",
                ("paused",),
                before=_current_paused(current_dict),
                after=True,
            )
        )
    elif patch_dict.get("paused") is False:
        mutations.append(
            _mutation(
                "resume",
                ("paused",),
                before=_current_paused(current_dict),
                after=False,
            )
        )

    if patch_dict.get("canceled") is True:
        fields = ["canceled"]
        after: dict[str, Any] = {"canceled": True}
        if "cancellation_reason" in patch_dict:
            fields.append("cancellation_reason")
            after["cancellation_reason"] = patch_dict["cancellation_reason"]
        mutations.append(
            _mutation(
                "cancel",
                tuple(fields),
                before=_current_status(current_dict),
                after=after,
                raw=after,
            )
        )
    elif "cancellation_reason" in patch_dict:
        mutations.append(
            _unknown_mutation(
                ("cancellation_reason",),
                after=patch_dict["cancellation_reason"],
            )
        )

    date_fields = tuple(
        field_name for field_name in ("start_time", "end_time") if field_name in patch_dict
    )
    if date_fields:
        before = {
            field_name: current_dict[field_name]
            for field_name in date_fields
            if field_name in current_dict
        }
        after = {field_name: patch_dict[field_name] for field_name in date_fields}
        action, resolution = _date_action(date_fields, before, after)
        mutations.append(
            _mutation(
                action,
                date_fields,
                before=before or None,
                after=after,
                raw=after,
                resolution=resolution,
            )
        )

    if "new_packages" in patch_dict:
        mutations.append(
            _mutation(
                "add_packages",
                ("new_packages",),
                after=patch_dict["new_packages"],
                raw=patch_dict["new_packages"],
            )
        )

    packages = _as_sequence_of_mappings(patch_dict.get("packages"))
    reallocation = _package_budget_reallocation(packages, current_packages)
    if reallocation is not None:
        mutations.append(reallocation)
        reallocated_package_ids = set(reallocation.after)
    else:
        reallocated_package_ids = set()

    for index, package_patch in enumerate(packages):
        package_id = _package_id(package_patch)
        current_package = current_packages.get(package_id or "")
        mutations.extend(
            _decompose_package_patch(
                package_patch,
                index=index,
                package_id=package_id,
                current_package=current_package,
                skip_budget=package_id in reallocated_package_ids,
            )
        )

    for field_name in _TOP_LEVEL_UNMAPPED_MUTATION_FIELDS:
        if field_name in patch_dict:
            mutations.append(
                _unknown_mutation(
                    (field_name,),
                    after=patch_dict[field_name],
                    raw=patch_dict[field_name],
                )
            )

    for field_name in _unknown_top_level_fields(patch_dict):
        mutations.append(
            _unknown_mutation(
                (field_name,),
                after=patch_dict[field_name],
                raw=patch_dict[field_name],
            )
        )

    return mutations


def requested_update_media_buy_actions(
    patch: Any,
    current_media_buy: Any | None = None,
) -> tuple[str, ...]:
    """Return ordered, de-duplicated actions requested by a patch."""

    return _dedupe(
        mutation.action for mutation in decompose_update_media_buy(patch, current_media_buy)
    )


def normalize_update_media_buy_allowed_actions(
    allowed_actions: Iterable[Any] | None,
) -> tuple[str, ...]:
    """Normalize action declarations to ordered action identifiers.

    Accepts any mix of action strings, generated enum values, wire dictionaries
    like ``{"action": "pause", "mode": "self_serve"}``, and generated
    ``MediaBuyAvailableAction`` models.
    """

    if allowed_actions is None:
        return ()
    return _dedupe(
        action_name
        for action in allowed_actions
        if (action_name := _action_name(action)) is not None
    )


def is_update_media_buy_mutation_allowed(
    mutation: UpdateMediaBuyMutation,
    allowed_actions: Iterable[Any] | None,
) -> bool:
    """Return whether ``allowed_actions`` contains a capability covering ``mutation``."""

    return mutation.is_allowed_by(allowed_actions)


def disallowed_update_media_buy_mutations(
    patch: Any,
    allowed_actions: Iterable[Any] | None,
    current_media_buy: Any | None = None,
) -> list[UpdateMediaBuyMutation]:
    """Return requested mutations not covered by the supplied allowed actions."""

    return [
        mutation
        for mutation in decompose_update_media_buy(patch, current_media_buy)
        if not is_update_media_buy_mutation_allowed(mutation, allowed_actions)
    ]


def _decompose_package_patch(
    package_patch: Mapping[str, Any],
    *,
    index: int,
    package_id: str | None,
    current_package: Mapping[str, Any] | None,
    skip_budget: bool,
) -> list[UpdateMediaBuyMutation]:
    mutations: list[UpdateMediaBuyMutation] = []

    if package_patch.get("paused") is True:
        mutations.append(
            _mutation(
                "pause",
                (_package_path(index, "paused"),),
                package_id=package_id,
                before=_current_paused(current_package or {}),
                after=True,
            )
        )
    elif package_patch.get("paused") is False:
        mutations.append(
            _mutation(
                "resume",
                (_package_path(index, "paused"),),
                package_id=package_id,
                before=_current_paused(current_package or {}),
                after=False,
            )
        )

    if package_patch.get("canceled") is True:
        fields = [_package_path(index, "canceled")]
        after: dict[str, Any] = {"canceled": True}
        if "cancellation_reason" in package_patch:
            fields.append(_package_path(index, "cancellation_reason"))
            after["cancellation_reason"] = package_patch["cancellation_reason"]
        mutations.append(
            _mutation(
                "remove_packages",
                tuple(fields),
                package_id=package_id,
                before=_current_status(current_package or {}),
                after=after,
                raw=after,
            )
        )
    elif "cancellation_reason" in package_patch:
        mutations.append(
            _unknown_mutation(
                (_package_path(index, "cancellation_reason"),),
                package_id=package_id,
                after=package_patch["cancellation_reason"],
            )
        )

    if "budget" in package_patch and not skip_budget:
        before = current_package.get("budget") if current_package else None
        action, resolution = _budget_action(before, package_patch["budget"])
        mutations.append(
            _mutation(
                action,
                (_package_path(index, "budget"),),
                package_id=package_id,
                before=before,
                after=package_patch["budget"],
                resolution=resolution,
            )
        )

    date_fields = tuple(
        field_name for field_name in ("start_time", "end_time") if field_name in package_patch
    )
    if date_fields:
        before = {
            field_name: current_package[field_name]
            for field_name in date_fields
            if current_package and field_name in current_package
        }
        after = {field_name: package_patch[field_name] for field_name in date_fields}
        action, resolution = _date_action(date_fields, before, after)
        mutations.append(
            _mutation(
                action,
                tuple(_package_path(index, field_name) for field_name in date_fields),
                package_id=package_id,
                before=before or None,
                after=after,
                raw=after,
                resolution=resolution,
            )
        )

    if "pacing" in package_patch:
        mutations.append(
            _mutation(
                "update_pacing",
                (_package_path(index, "pacing"),),
                package_id=package_id,
                before=current_package.get("pacing") if current_package else None,
                after=package_patch["pacing"],
            )
        )

    mutations.extend(
        _decompose_package_targeting(
            package_patch,
            index=index,
            package_id=package_id,
            current_package=current_package,
        )
    )

    if "creative_assignments" in package_patch:
        mutations.append(
            _mutation(
                "update_creative_assignments",
                (_package_path(index, "creative_assignments"),),
                package_id=package_id,
                before=current_package.get("creative_assignments") if current_package else None,
                after=package_patch["creative_assignments"],
                raw=package_patch["creative_assignments"],
            )
        )

    if "creatives" in package_patch:
        action = "remove_creative" if package_patch["creatives"] == [] else "replace_creative"
        mutations.append(
            _mutation(
                action,
                (_package_path(index, "creatives"),),
                package_id=package_id,
                before=current_package.get("creatives") if current_package else None,
                after=package_patch["creatives"],
                raw=package_patch["creatives"],
            )
        )

    for field_name in _PACKAGE_COARSE_FIELDS:
        if field_name in package_patch:
            mutations.append(
                _mutation(
                    "update_packages",
                    (_package_path(index, field_name),),
                    package_id=package_id,
                    before=current_package.get(field_name) if current_package else None,
                    after=package_patch[field_name],
                    raw=package_patch[field_name],
                    resolution="coarse",
                )
            )

    for field_name in _unknown_package_fields(package_patch):
        mutations.append(
            _unknown_mutation(
                (_package_path(index, field_name),),
                package_id=package_id,
                after=package_patch[field_name],
                raw=package_patch[field_name],
            )
        )

    return mutations


def _decompose_package_targeting(
    package_patch: Mapping[str, Any],
    *,
    index: int,
    package_id: str | None,
    current_package: Mapping[str, Any] | None,
) -> list[UpdateMediaBuyMutation]:
    mutations: list[UpdateMediaBuyMutation] = []
    incremental_fields = tuple(
        field_name
        for field_name in (
            "keyword_targets_add",
            "keyword_targets_remove",
            "negative_keywords_add",
            "negative_keywords_remove",
        )
        if field_name in package_patch
    )
    if incremental_fields:
        after = {field_name: package_patch[field_name] for field_name in incremental_fields}
        mutations.append(
            _mutation(
                "update_targeting",
                tuple(_package_path(index, field_name) for field_name in incremental_fields),
                package_id=package_id,
                after=after,
                raw=after,
            )
        )

    if "targeting_overlay" not in package_patch:
        return mutations

    overlay = _to_plain_mapping(package_patch["targeting_overlay"])
    if set(overlay) == {"frequency_cap"}:
        mutations.append(
            _mutation(
                "update_frequency_caps",
                (_package_path(index, "targeting_overlay.frequency_cap"),),
                package_id=package_id,
                before=_nested_value(current_package, ("targeting_overlay", "frequency_cap")),
                after=overlay["frequency_cap"],
                raw=overlay["frequency_cap"],
            )
        )
        return mutations

    targeting_paths = [_package_path(index, "targeting_overlay")]
    if overlay:
        targeting_paths = [
            _package_path(index, f"targeting_overlay.{field_name}")
            for field_name in overlay
            if field_name != "frequency_cap"
        ]

    if targeting_paths:
        mutations.append(
            _mutation(
                "update_targeting",
                tuple(targeting_paths),
                package_id=package_id,
                before=current_package.get("targeting_overlay") if current_package else None,
                after=package_patch["targeting_overlay"],
                raw=package_patch["targeting_overlay"],
            )
        )

    if "frequency_cap" in overlay:
        mutations.append(
            _mutation(
                "update_frequency_caps",
                (_package_path(index, "targeting_overlay.frequency_cap"),),
                package_id=package_id,
                before=_nested_value(current_package, ("targeting_overlay", "frequency_cap")),
                after=overlay["frequency_cap"],
                raw=overlay["frequency_cap"],
            )
        )

    return mutations


def _package_budget_reallocation(
    packages: Sequence[Mapping[str, Any]],
    current_packages: Mapping[str, Mapping[str, Any]],
) -> UpdateMediaBuyMutation | None:
    budget_updates = [
        (index, package_patch, _package_id(package_patch))
        for index, package_patch in enumerate(packages)
        if "budget" in package_patch
    ]
    if len(budget_updates) < 2:
        return None

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    paths: list[str] = []
    for index, package_patch, package_id in budget_updates:
        if package_id is None:
            return None
        current_package = current_packages.get(package_id)
        if current_package is None or "budget" not in current_package:
            return None
        before[package_id] = current_package["budget"]
        after[package_id] = package_patch["budget"]
        paths.append(_package_path(index, "budget"))

    before_total = _numeric_sum(before.values())
    after_total = _numeric_sum(after.values())
    if before_total is None or after_total is None or before_total != after_total:
        return None

    return _mutation(
        "reallocate_budget",
        tuple(paths),
        before=before,
        after=after,
        raw=after,
    )


def _date_action(
    fields: tuple[str, ...],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> tuple[str, UpdateMutationResolution]:
    if "start_time" in fields:
        return "update_flight_dates", "fine"
    if "end_time" not in fields:
        return "update_dates", "coarse"
    comparison = _compare_ordered(before.get("end_time"), after.get("end_time"))
    if comparison is None:
        return "update_dates", "coarse"
    if comparison < 0:
        return "extend_flight", "fine"
    if comparison > 0:
        return "shorten_flight", "fine"
    return "update_flight_dates", "fine"


def _budget_action(before: Any, after: Any) -> tuple[str, UpdateMutationResolution]:
    comparison = _compare_ordered(before, after)
    if comparison is None:
        return "update_budget", "coarse"
    if comparison < 0:
        return "increase_budget", "fine"
    if comparison > 0:
        return "decrease_budget", "fine"
    return "update_budget", "coarse"


def _mutation(
    action: str,
    field_paths: tuple[str, ...],
    *,
    package_id: str | None = None,
    before: Any = None,
    after: Any = None,
    raw: Any = None,
    resolution: UpdateMutationResolution = "fine",
) -> UpdateMediaBuyMutation:
    return UpdateMediaBuyMutation(
        action=action,
        field_paths=field_paths,
        package_id=package_id,
        before=before,
        after=after,
        raw=raw if raw is not None else after,
        resolution=resolution,
        allowed_action_candidates=_ACTION_CANDIDATES.get(action, (action,)),
    )


def _unknown_mutation(
    field_paths: tuple[str, ...],
    *,
    package_id: str | None = None,
    after: Any = None,
    raw: Any = None,
) -> UpdateMediaBuyMutation:
    return UpdateMediaBuyMutation(
        action=UNKNOWN_UPDATE_ACTION,
        field_paths=field_paths,
        package_id=package_id,
        after=after,
        raw=raw if raw is not None else after,
        resolution="unknown",
        allowed_action_candidates=(),
    )


def _to_plain_mapping(value: Any) -> dict[str, Any]:
    normalized = _normalize_value(value)
    if isinstance(normalized, Mapping):
        return {str(key): item for key, item in normalized.items()}
    return {}


def _normalize_value(value: Any) -> Any:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _normalize_value(model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return {str(key): _normalize_value(item) for key, item in value.items() if item is not None}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_value(item) for item in value]
    return value


def _as_sequence_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [_to_plain_mapping(item) for item in value]


def _index_packages(value: Any) -> dict[str, Mapping[str, Any]]:
    packages = _as_sequence_of_mappings(value)
    return {
        package_id: package
        for package in packages
        if (package_id := _package_id(package)) is not None
    }


def _package_id(package: Mapping[str, Any]) -> str | None:
    value = package.get("package_id") or package.get("id")
    if value is None:
        return None
    return str(value)


def _current_status(value: Mapping[str, Any]) -> str | None:
    status = value.get("status") or value.get("media_buy_status")
    return str(status) if status is not None else None


def _current_paused(value: Mapping[str, Any]) -> bool | None:
    paused = value.get("paused")
    if isinstance(paused, bool):
        return paused
    status = _current_status(value)
    if status == "paused":
        return True
    if status in {"active", "pending_start", "pending_creatives", "pending_activation"}:
        return False
    return None


def _action_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        value = value.get("action")
    else:
        value = getattr(value, "action", value)
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    if isinstance(value, str):
        return value
    return None


def _nested_value(value: Mapping[str, Any] | None, path: tuple[str, ...]) -> Any:
    current: Any = value
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _unknown_top_level_fields(value: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        field_name
        for field_name in value
        if field_name not in _TOP_LEVEL_METADATA_FIELDS
        and field_name not in _TOP_LEVEL_KNOWN_MUTATION_FIELDS
    )


def _unknown_package_fields(value: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        field_name
        for field_name in value
        if field_name not in _PACKAGE_METADATA_FIELDS
        and field_name not in _PACKAGE_KNOWN_MUTATION_FIELDS
    )


def _compare_ordered(before: Any, after: Any) -> int | None:
    before_value = _ordered_value(before)
    after_value = _ordered_value(after)
    if before_value is None or after_value is None:
        return None
    if isinstance(before_value, Decimal) and isinstance(after_value, Decimal):
        if before_value < after_value:
            return -1
        if before_value > after_value:
            return 1
        return 0
    if isinstance(before_value, datetime) and isinstance(after_value, datetime):
        try:
            if before_value < after_value:
                return -1
            if before_value > after_value:
                return 1
        except TypeError:
            return None
        return 0
    return None


def _ordered_value(value: Any) -> Decimal | datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                return Decimal(value)
            except ArithmeticError:
                return None
    return None


def _numeric_sum(values: Iterable[Any]) -> Decimal | None:
    total = Decimal("0")
    for value in values:
        ordered = _ordered_value(value)
        if not isinstance(ordered, Decimal):
            return None
        total += ordered
    return total


def _package_path(index: int, field_name: str) -> str:
    return f"packages[{index}].{field_name}"


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)
