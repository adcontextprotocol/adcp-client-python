"""Translation between consecutive AdCP minor releases.

Application payloads use the packaged release. Only protocol boundaries use
older schemas. Unknown extension data is opaque; known newer fields are
projected using both schemas, with a value-free record for every loss.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any

from adcp._version import (
    get_supported_adcp_versions,
    normalize_to_release_precision,
    resolve_adcp_version_alias,
)
from adcp.exceptions import ADCPFeatureUnsupportedError
from adcp.validation.schema_loader import Direction, get_portable_schema

logger = logging.getLogger(__name__)
CONVERSION_METADATA_KEY = "adcp_version_conversion"
_OPAQUE_FIELDS = frozenset({"context", "ext"})


@dataclass(frozen=True)
class ConversionLoss:
    """One discarded field/item; values are deliberately never retained."""

    pointer: str
    source_version: str
    target_version: str
    reason: str = "field_unavailable"


@dataclass(frozen=True)
class ConversionResult:
    payload: dict[str, Any]
    losses: tuple[ConversionLoss, ...] = ()
    creative_dialect: str | None = None
    identity_migrations: tuple[dict[str, Any], ...] = ()

    def metadata(self) -> list[dict[str, Any]]:
        return [asdict(loss) for loss in self.losses]


class ADCPVersionConversionError(ADCPFeatureUnsupportedError):
    """A payload cannot be represented without inventing state or changing a clear."""

    def __init__(
        self, *, pointer: str, source_version: str, target_version: str, reason: str
    ) -> None:
        self.pointer = pointer
        self.source_version = source_version
        self.target_version = target_version
        self.reason = reason
        super().__init__(
            [f"Cannot convert AdCP {source_version} to {target_version} at {pointer}: {reason}"]
        )


def _release(version: str) -> str:
    return resolve_adcp_version_alias(normalize_to_release_precision(version))


def negotiate_version(supported: list[str], *, maximum: str) -> str:
    """Select the highest common release, bounded by this call's pin.

    Prereleases match exactly (except explicitly supported aliases). Never
    infer compatibility with an unadvertised release or another major.
    """
    from adcp._version import is_adcp_version_at_least

    maximum = _release(maximum)
    available = {_release(version) for version in get_supported_adcp_versions()}
    common = [
        version
        for version in supported
        if isinstance(version, str)
        and _release(version) in available
        and is_adcp_version_at_least(maximum, _release(version))
    ]
    if not common:
        raise ADCPFeatureUnsupportedError(["No common AdCP version with this seller"])
    selected = common[0]
    for candidate in common[1:]:
        candidate_release, selected_release = _release(candidate), _release(selected)
        if (
            candidate_release != selected_release
            and is_adcp_version_at_least(candidate_release, selected_release)
        ) or (candidate_release == selected_release and candidate == candidate_release):
            selected = candidate
    # Retain a seller's advertised alias for old sellers with exact matching.
    return selected


@lru_cache(maxsize=512)
def _schema(tool: str, direction: Direction, version: str) -> dict[str, Any]:
    if version == "3.0" and direction == "submitted" and tool == "create_media_buy":
        # The main 3.0 response includes the submitted wire envelope. Its
        # separate async schema describes webhook data without that envelope.
        direction = "sync"
    schema = get_portable_schema(tool, direction, version=version)
    if schema is None:
        raise ADCPFeatureUnsupportedError([f"{tool} is unavailable in AdCP {version}"])
    if version == "3.0" and direction in {"submitted", "working", "input-required"}:
        # Older async schemas describe data beneath the transport envelope.
        # Preserve that envelope when projecting a flat tool response.
        schema["properties"] = {
            **schema.get("properties", {}),
            "status": {"type": "string", "const": direction},
            "task_id": {"type": "string"},
        }
    return schema


def _pointer(path: str, key: str | int) -> str:
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _resolve(schema: Any, root: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        resolved: Any = root
        for segment in ref[2:].split("/"):
            resolved = resolved[segment.replace("~1", "/").replace("~0", "~")]
        return {**resolved, **{key: value for key, value in schema.items() if key != "$ref"}}
    return schema


def _score(schema: dict[str, Any], value: Any, root: dict[str, Any]) -> int:
    schema = _resolve(schema, root)
    if "const" in schema:
        return 100 if value == schema["const"] else -1000
    if "enum" in schema:
        return 20 if value in schema["enum"] else -100
    if isinstance(value, dict):
        props = schema.get("properties", {})
        score = len(props.keys() & value.keys()) * 2
        # A malformed discriminator must not redirect a structurally complete
        # success into an errors-only arm and discard its financial artifact.
        score -= len(set(schema.get("required", [])) - value.keys()) * 10_000
        for key in props.keys() & value.keys():
            child = _resolve(props[key], root)
            if "const" in child or "enum" in child:
                score += _score(child, value[key], root)
        return score
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        actual = (
            "null"
            if value is None
            else (
                "array"
                if isinstance(value, list)
                else (
                    "boolean"
                    if isinstance(value, bool)
                    else (
                        "string"
                        if isinstance(value, str)
                        else "number" if isinstance(value, (int, float)) else "object"
                    )
                )
            )
        )
        return 5 if actual in types or actual == "number" and "integer" in types else -1000
    return 0


def _view(schema: Any, value: Any, root: dict[str, Any]) -> dict[str, Any]:
    """Collect properties across allOf and the matching union arm."""
    schema = _resolve(schema, root)
    result = dict(schema)
    properties = dict(schema.get("properties", {}))
    required = set(schema.get("required", []))
    branches = list(schema.get("allOf", []))
    choices = schema.get("oneOf", schema.get("anyOf", []))
    if choices:
        branches.append(max(choices, key=lambda arm: _score(_resolve(arm, root), value, root)))
    for branch in branches:
        child = _view(branch, value, root)
        properties.update(child.get("properties", {}))
        required.update(child.get("required", []))
        for key in ("type", "items", "additionalProperties", "patternProperties", "enum", "const"):
            if key in child:
                result[key] = child[key]
    result["properties"] = properties
    result["required"] = required
    return result


def _property(schema: dict[str, Any], key: str) -> Any:
    if key in schema["properties"]:
        return schema["properties"][key]
    for pattern, child in schema.get("patternProperties", {}).items():
        if re.search(pattern, key):
            return child
    additional = schema.get("additionalProperties")
    return additional if isinstance(additional, dict) else None


def _project(
    value: Any,
    source: Any,
    target: Any,
    source_root: dict[str, Any],
    target_root: dict[str, Any],
    path: str,
    source_version: str,
    target_version: str,
    losses: list[ConversionLoss],
    *,
    drop_unavailable_nulls: bool = False,
) -> Any:
    source = _view(source, value, source_root)
    target = _view(target, value, target_root)
    if isinstance(value, dict):
        if target_version == "3.0" and source.get("title") in {
            "Postal Area",
            "Postal Area Support",
        }:
            from adcp.types.projections import project_geo_postal_areas

            if source["title"] == "Postal Area Support":
                for country, systems in value.items():
                    if isinstance(systems, list):
                        for index, system in enumerate(systems):
                            if not project_geo_postal_areas({country: [system]}, target_version):
                                losses.append(
                                    ConversionLoss(
                                        _pointer(_pointer(path, country), index),
                                        source_version,
                                        target_version,
                                        "postal_system_unavailable",
                                    )
                                )
                return project_geo_postal_areas(value, target_version)
            if "country" in value:
                aliases = project_geo_postal_areas(
                    {value["country"]: [value.get("system")]}, target_version
                )
                if not aliases:
                    raise ADCPFeatureUnsupportedError(
                        [f"Postal area at {path} has no AdCP 3.0 representation"]
                    )
                value = {key: child for key, child in value.items() if key != "country"}
                value["system"] = next(iter(aliases))
                source = _view(source, value, source_root)
        projected = {}
        for key, child in value.items():
            child_path = _pointer(path, key)
            if key in _OPAQUE_FIELDS:
                projected[key] = child
                continue
            source_child = _property(source, key)
            target_child = _property(target, key)
            if source_child is not None and target_child is None:
                if (
                    drop_unavailable_nulls
                    and not path
                    and key == "message"
                    and target.get("additionalProperties", True) is not False
                ):
                    # Human-readable MCP summaries predate the protocol
                    # envelope; older open response objects can retain them.
                    projected[key] = child
                    continue
                if (
                    not drop_unavailable_nulls
                    and child is None
                    and _allows_null(_view(source_child, child, source_root))
                ):
                    raise ADCPVersionConversionError(
                        pointer=child_path,
                        source_version=source_version,
                        target_version=target_version,
                        reason="the older contract cannot express an explicit null clear",
                    )
                # Schema maps (e.g. asset IDs) are application-owned keys.
                additional = target.get("additionalProperties")
                if isinstance(additional, dict) or target.get("patternProperties"):
                    target_child = additional or {}
                else:
                    losses.append(ConversionLoss(child_path, source_version, target_version))
                    continue
            if source_child is None or target_child is None:
                projected[key] = child
                continue
            source_view = _view(source_child, child, source_root)
            target_view = _view(target_child, child, target_root)
            if (
                (not drop_unavailable_nulls or key in target["required"])
                and child is None
                and _allows_null(source_view)
                and _rejects_null(target_view)
            ):
                raise ADCPVersionConversionError(
                    pointer=child_path,
                    source_version=source_version,
                    target_version=target_version,
                    reason=(
                        "the older contract cannot express an explicit null clear"
                        if not drop_unavailable_nulls
                        else "the older contract requires a value unavailable in this response"
                    ),
                )
            if (
                drop_unavailable_nulls
                and child is None
                and key not in target["required"]
                and _allows_null(source_view)
                and _rejects_null(target_view)
            ):
                losses.append(
                    ConversionLoss(child_path, source_version, target_version, "null_unavailable")
                )
                continue
            # An optional enum member introduced by the newer release cannot
            # be sent to the old peer. Required values remain for validation
            # to reject rather than manufacturing a different meaning.
            if (
                not isinstance(child, (dict, list))
                and "enum" in source_view
                and child in source_view["enum"]
                and "enum" in target_view
                and child not in target_view["enum"]
                and key not in target["required"]
            ):
                losses.append(
                    ConversionLoss(child_path, source_version, target_version, "enum_unavailable")
                )
                continue
            projected[key] = _project(
                child,
                source_child,
                target_child,
                source_root,
                target_root,
                child_path,
                source_version,
                target_version,
                losses,
                drop_unavailable_nulls=drop_unavailable_nulls,
            )
        return projected
    if isinstance(value, list):
        projected_items = []
        for index, child in enumerate(value):
            source_item = _view(source.get("items", {}), child, source_root)
            target_item = _view(target.get("items", {}), child, target_root)
            if (
                not isinstance(child, (dict, list))
                and "enum" in source_item
                and child in source_item["enum"]
                and "enum" in target_item
                and child not in target_item["enum"]
            ):
                losses.append(
                    ConversionLoss(
                        _pointer(path, index), source_version, target_version, "enum_unavailable"
                    )
                )
                continue
            projected_items.append(
                _project(
                    child,
                    source.get("items", {}),
                    target.get("items", {}),
                    source_root,
                    target_root,
                    _pointer(path, index),
                    source_version,
                    target_version,
                    losses,
                    drop_unavailable_nulls=drop_unavailable_nulls,
                )
            )
        return projected_items
    return value


def _allows_null(schema: dict[str, Any]) -> bool:
    types = schema.get("type", [])
    return (
        "const" in schema
        and schema["const"] is None
        or "enum" in schema
        and None in schema["enum"]
        or types == "null"
        or isinstance(types, list)
        and "null" in types
    )


def _rejects_null(schema: dict[str, Any]) -> bool:
    types = schema.get("type")
    return (
        "const" in schema
        and schema["const"] is not None
        or "enum" in schema
        and None not in schema["enum"]
        or types is not None
        and types != "null"
        and (not isinstance(types, list) or "null" not in types)
    )


def _upgrade_media_buy_metadata(tool: str, payload: dict[str, Any]) -> None:
    """Represent absent confirmation honestly; concurrency tokens cannot be inferred."""
    entries: list[tuple[dict[str, Any], str]] = []
    if tool in {"create_media_buy", "update_media_buy"}:
        if "media_buy_id" in payload and "errors" not in payload and "task_id" not in payload:
            entries.append((payload, ""))
    elif tool == "get_media_buys":
        entries.extend(
            (row, _pointer("/media_buys", index))
            for index, row in enumerate(payload.get("media_buys", []))
            if isinstance(row, dict)
        )
    for entry, path in entries:
        if "revision" not in entry:
            raise ADCPVersionConversionError(
                pointer=_pointer(path, "revision"),
                source_version="3.0",
                target_version="3.1",
                reason="the seller omitted the required optimistic concurrency token",
            )
        if tool == "update_media_buy" or "confirmed_at" in entry:
            continue
        if entry.get("media_buy_status", entry.get("status")) == "active" or any(
            isinstance(package, dict) and "committed_metrics" in package
            for package in entry.get("packages", [])
        ):
            raise ADCPVersionConversionError(
                pointer=_pointer(path, "confirmed_at"),
                source_version="3.0",
                target_version="3.1",
                reason="the seller omitted the confirmation timestamp for committed delivery",
            )
        entry["confirmed_at"] = None


def _upgrade_governance_registration(payload: dict[str, Any]) -> None:
    """A category-scoped authority cannot become the full lifecycle owner."""
    for account_index, account in enumerate(payload.get("accounts", [])):
        if not isinstance(account, dict):
            continue
        agents = account.get("governance_agents", [])
        if not isinstance(agents, list):
            continue
        path = _pointer(_pointer("/accounts", account_index), "governance_agents")
        if len(agents) > 1:
            raise ADCPVersionConversionError(
                pointer=path,
                source_version="3.0",
                target_version="3.1",
                reason="multiple governance authorities cannot become one lifecycle owner",
            )
        for agent_index, agent in enumerate(agents):
            if isinstance(agent, dict) and "categories" in agent:
                raise ADCPVersionConversionError(
                    pointer=_pointer(_pointer(path, agent_index), "categories"),
                    source_version="3.0",
                    target_version="3.1",
                    reason="category-scoped governance cannot become full lifecycle authority",
                )


def _upgrade_product_reporting_metrics(payload: dict[str, Any]) -> None:
    """A removed historical metric has no certified equivalent in the newer vocabulary."""
    products = payload.get("products", [])
    if not isinstance(products, list):
        return
    for product_index, product in enumerate(products):
        if not isinstance(product, dict):
            continue
        capabilities = product.get("reporting_capabilities")
        if not isinstance(capabilities, dict):
            continue
        metrics = capabilities.get("available_metrics", [])
        if not isinstance(metrics, list):
            continue
        for metric_index, metric in enumerate(metrics):
            if metric == "video_completions":
                raise ADCPVersionConversionError(
                    pointer=f"/products/{product_index}/reporting_capabilities/available_metrics/{metric_index}",
                    source_version="3.0",
                    target_version="3.1",
                    reason="the historical reporting metric has no verified newer equivalent",
                )


def _convert_embedded_results(
    tool: str,
    payload: dict[str, Any],
    source_version: str,
    target_version: str,
    losses: list[ConversionLoss],
) -> None:
    """Use task identity for opaque polling artifacts, bounded to one wrapper level."""
    if tool == "get_task_status":
        entries = [(payload, "")]
    elif tool == "list_tasks":
        entries = [
            (row, _pointer("/tasks", index))
            for index, row in enumerate(payload.get("tasks", []))
            if isinstance(row, dict)
        ]
    else:
        return
    for entry, path in entries:
        task_type = entry.get("task_type")
        result = entry.get("result")
        if (
            not isinstance(task_type, str)
            or task_type in {"get_task_status", "list_tasks"}
            or not isinstance(result, dict)
        ):
            continue
        status = result.get("status")
        direction: Direction = (
            status if status in {"submitted", "working", "input-required"} else "sync"
        )
        try:
            _schema(task_type, direction, source_version)
        except ADCPFeatureUnsupportedError:
            # Vendor task names have no SDK-owned contract to project.
            continue
        prefix = _pointer(path, "result")
        try:
            converted = convert_payload(
                task_type,
                result,
                direction=direction,
                source_version=source_version,
                target_version=target_version,
            )
        except ADCPVersionConversionError as exc:
            raise ADCPVersionConversionError(
                pointer=prefix + exc.pointer,
                source_version=exc.source_version,
                target_version=exc.target_version,
                reason=exc.reason,
            ) from exc
        entry["result"] = converted.payload
        losses.extend(
            ConversionLoss(
                prefix + loss.pointer, loss.source_version, loss.target_version, loss.reason
            )
            for loss in converted.losses
        )


def convert_payload(
    tool: str,
    payload: dict[str, Any],
    *,
    direction: Direction,
    source_version: str,
    target_version: str,
    request: dict[str, Any] | None = None,
) -> ConversionResult:
    """Chain adjacent minor conversions without modifying the caller's data.

    The 3.1/3.0 lifecycle envelope is translated semantically. Additive
    changes use the bundled schemas, including nested arrays and unions.
    New tasks without an older equivalent fail before transport.
    """
    source_version, target_version = _release(source_version), _release(target_version)
    result = deepcopy(payload)
    losses: list[ConversionLoss] = []
    if source_version == target_version:
        if direction != "request" and "adcp_version" in result:
            result["adcp_version"] = target_version
        return ConversionResult(result)
    source_parts = source_version.split(".")
    target_parts = target_version.split(".")
    if source_parts[0] != target_parts[0] or "-" in source_version or "-" in target_version:
        raise ADCPFeatureUnsupportedError(
            [f"No AdCP converter from {source_version} to {target_version}"]
        )
    minor, end = int(source_parts[1]), int(target_parts[1])
    step = 1 if end > minor else -1
    while minor != end:
        source = f"{source_parts[0]}.{minor}"
        target = f"{source_parts[0]}.{minor + step}"
        source_schema = _schema(tool, direction, source)
        target_schema = _schema(tool, direction, target)
        from adcp.compat.governance import convert_governance_fields

        convert_governance_fields(tool, result, source, target, direction)
        if (
            tool == "sync_governance"
            and direction == "request"
            and (source, target) == ("3.0", "3.1")
        ):
            _upgrade_governance_registration(result)
        if tool == "get_products" and direction != "request" and (source, target) == ("3.0", "3.1"):
            _upgrade_product_reporting_metrics(result)
        if direction != "request" and {source, target} == {"3.0", "3.1"}:
            from adcp.validation._response_envelope import uses_task_status_envelope

            if step > 0:
                _upgrade_media_buy_metadata(tool, result)
            if (
                tool in {"create_media_buy", "update_media_buy"}
                and "media_buy_id" in result
                and "errors" not in result
                and "task_id" not in result
            ):
                if step > 0:
                    if result.get("status") not in {None, "completed"}:
                        result["media_buy_status"] = result["status"]
                    result["status"] = "completed"
                elif step < 0:
                    if "media_buy_status" in result:
                        result["status"] = result.pop("media_buy_status")
                    elif result.get("status") == "completed":
                        result.pop("status")
                        losses.append(ConversionLoss("/status", source, target))
            elif step > 0 and uses_task_status_envelope(tool):
                result.setdefault("status", "completed")
            if step > 0 and tool in {"get_products", "get_signals"}:
                # Old responses have no cache declaration. An account-scoped
                # result must never be upgraded into a globally cacheable one.
                scope = (
                    "account" if request is None or request.get("account") is not None else "public"
                )
                result.setdefault("cache_scope", scope)
        if step < 0:
            result = _project(
                result,
                source_schema,
                target_schema,
                source_schema,
                target_schema,
                "",
                source,
                target,
                losses,
                drop_unavailable_nulls=direction != "request",
            )
        minor += step
    # The version envelope describes the boundary's contract, including
    # upgrades before application code. 3.0 has only the major envelope.
    if target_version == "3.0":
        result.pop("adcp_version", None)
        if direction == "request":
            result["adcp_major_version"] = 3
    elif direction != "request" or "adcp_version" in payload:
        result["adcp_version"] = target_version
    if direction != "request":
        _convert_embedded_results(tool, result, source_version, target_version, losses)
    for loss in losses:
        logger.warning(
            "AdCP conversion %s -> %s dropped %s (%s)",
            loss.source_version,
            loss.target_version,
            loss.pointer,
            loss.reason,
        )
    return ConversionResult(result, tuple(losses))
