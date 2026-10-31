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

from jsonschema.exceptions import best_match
from jsonschema.validators import validator_for

from adcp._version import (
    get_supported_adcp_versions,
    normalize_to_release_precision,
    resolve_adcp_version_alias,
)
from adcp.exceptions import ADCPFeatureUnsupportedError
from adcp.validation.schema_loader import (
    Direction,
    _build_format_checker,
    get_portable_schema,
)

logger = logging.getLogger(__name__)
CONVERSION_METADATA_KEY = "adcp_version_conversion"
_OPAQUE_FIELDS = frozenset({"context", "ext"})
_ARRAY_INDEX = re.compile(r"/\d+(?=/|$)")
_SUMMARY_POINTER_LIMIT = 10
#: Compiled node validators keyed by schema identity; see :func:`_refusal`.
_NODE_VALIDATORS: dict[tuple[int, int, bool], tuple[Any, Any, Any]] = {}
_NODE_VALIDATOR_LIMIT = 4096
#: Response rows the older release cannot carry without a field it requires
#: there and the newer release made optional. The row is omitted, never
#: completed: an absent ``impressions`` means "not measured", never zero, and
#: a spot without ``aired_at`` (``scheduled`` / ``preempted``) has not aired.
_ROW_OMISSIONS = {
    "aired_at": "row_omitted_not_aired",
    "impressions": "row_omitted_impressions_not_measured",
}
#: Newer fields whose removal would change what the rest of the object says,
#: in a request or a response: refused, never dropped or rounded.
_REFUSED_FIELDS = {
    ("Daypart Target", field): (
        "a minute-level daypart window has no form in the older release; "
        "it is refused, never rounded to whole hours"
    )
    for field in ("start_time", "end_time")
}
#: Newer fields sent to older peers under the interim ``ext.adcp`` binding.
#: On upgrade a top-level value wins; the interim one fills only its absence.
_INTERIM_EXT_FIELDS = frozenset({"age_determination_bases"})
#: Objects meaningful only whole: a member the older release refuses makes
#: the object unrepresentable rather than shorter. A price breakdown without
#: its ``operator_fee`` rows would no longer walk list_price to fixed_price.
_WHOLE_OBJECTS = frozenset({"Price Breakdown"})


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


class _UnrepresentableError(Exception):
    """A projected node the target schema refuses; its parent decides what survives.

    ``pointer`` locates the refused value for the structured error; the
    parent drops the whole child it was projecting, not just that value.
    """

    def __init__(self, pointer: str, message: str) -> None:
        super().__init__(message)
        self.pointer = pointer
        self.message = message


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


_LINE = re.compile(r"^(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$")


def _line(version: str) -> tuple[int, int] | None:
    """``(major, minor)`` of a release-precision version, prerelease or not."""
    match = _LINE.match(version)
    return (int(match[1]), int(match[2])) if match else None


def _wire_for_line(major: int, minor: int) -> str:
    """The release-precision value this SDK serves for a minor line.

    A line whose packaged spec is a prerelease (``3.3-beta.3`` on the 3.3
    line) is spoken, converted and validated as that exact prerelease: its
    schemas are the only 3.3 contract this wheel ships. Every other line is
    its stable release.
    """
    stable = f"{major}.{minor}"
    for supported in get_supported_adcp_versions():
        release = _release(supported)
        if "-" in release and release.split("-", 1)[0] == stable:
            return release
    return stable


def _convertible(version: str) -> tuple[int, int] | None:
    """The line of ``version`` when a converter exists for it.

    Stable releases of the SDK's major convert. A prerelease converts only
    when it is the one this SDK serves for its line; any other prerelease
    keeps its own schema contract and is never inferred.
    """
    line = _line(version)
    if line is None:
        return None
    if "-" in version and version != _wire_for_line(*line):
        return None
    return line


def is_convertible_version(version: str) -> bool:
    """Whether minor-release translation applies to ``version``.

    Stable releases of the SDK's major convert, as does the exact prerelease
    the SDK serves for a line (``3.3-beta.3`` while the pin is that beta).
    Any other prerelease keeps its own schema contract and gates: the
    translators must not guess prerelease semantics.
    """
    try:
        release = _release(version)
    except ValueError:
        return False
    return _convertible(release) is not None


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
        raise ADCPFeatureUnsupportedError(
            ["No common AdCP version with this seller"], dispatched=False
        )
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
            segment = segment.replace("~1", "/").replace("~0", "~")
            resolved = resolved[int(segment) if isinstance(resolved, list) else segment]
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
                        else "number"
                        if isinstance(value, (int, float))
                        else "object"
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


#: Keywords a node's remaining shape can fail once a direct child is dropped or
#: coarsened, e.g. a required identity whose only newer spelling was removed.
_SHAPE_KEYWORDS = frozenset(
    {
        "anyOf",
        "contains",
        "dependencies",
        "if",
        "minItems",
        "minProperties",
        "not",
        "oneOf",
        "required",
    }
)


def _resolve_chain(schema: Any, root: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` until it reaches a node with its own keywords."""
    resolved = _resolve(schema, root)
    for _ in range(32):
        if "$ref" not in resolved:
            break
        resolved = _resolve(resolved, root)
    return resolved


def _shape_keywords(schema: Any, root: dict[str, Any]) -> frozenset[str]:
    """The node's own constraints that removing children could violate."""
    resolved = _resolve_chain(schema, root)
    found = _SHAPE_KEYWORDS & resolved.keys()
    for branch in resolved.get("allOf", []):
        found |= _shape_keywords(branch, root)
    return frozenset(found)


def _shape_schema(schema: Any, root: dict[str, Any]) -> dict[str, Any]:
    """The node's shape keywords alone, without ``properties`` or ``items``."""
    resolved = _resolve_chain(schema, root)
    own = {key: resolved[key] for key in _SHAPE_KEYWORDS if key in resolved}
    if "if" in own:
        own.update({key: resolved[key] for key in ("then", "else") if key in resolved})
    branches = [_shape_schema(branch, root) for branch in resolved.get("allOf", [])]
    return {**own, "allOf": [branch for branch in branches if branch]} if branches else own


def _refusal(
    schema: Any, root: dict[str, Any], value: Any, *, shape: bool = True
) -> tuple[str, str] | None:
    """Return where and why ``schema`` refuses ``value``, or ``None``.

    With ``shape`` only the node's own shape keywords run (see
    :func:`_shape_schema`); its children are checked where they were
    projected. Otherwise the whole subtree is validated. The pointer is
    relative to ``value``. Draft-07 ignores ``$ref`` siblings, so the
    wrapper validates only the node while every ``#/...`` pointer in it
    still resolves against the original document.
    """
    if not isinstance(schema, dict) or not schema:
        return None
    key = (id(schema), id(root), shape)
    cached = _NODE_VALIDATORS.get(key)
    if cached is None or cached[0] is not schema or cached[1] is not root:
        if len(_NODE_VALIDATORS) >= _NODE_VALIDATOR_LIMIT:
            _NODE_VALIDATORS.clear()
        document = {
            **root,
            "$ref": "#/$defs/__adcp_conversion_node",
            "$defs": {
                **root.get("$defs", {}),
                "__adcp_conversion_node": _shape_schema(schema, root) if shape else schema,
            },
        }
        # Formats as the SDK validators check them: unchecked, a ``date-time``
        # string also matches its ``const: asap`` sibling and fails a oneOf.
        validator = validator_for(root)(document, format_checker=_build_format_checker())
        cached = (schema, root, validator)
        _NODE_VALIDATORS[key] = cached
    try:
        error = best_match(cached[2].iter_errors(value))
    except Exception:  # noqa: BLE001 - an unresolvable node is checked whole later
        return None
    if error is None:
        return None
    pointer = ""
    for key in error.absolute_path:
        pointer = _pointer(pointer, key)
    return pointer, _describe(error)


_NO_SPELLING = "value has no spelling in the older release"


def _describe(error: Any) -> str:
    """A value-free reason: diagnostics never echo payload contents."""
    keyword = error.validator
    if keyword == "required":
        missing = [name for name in error.validator_value if name not in error.instance]
        return f"missing required field(s): {', '.join(map(str, missing))}"
    if keyword in {"minItems", "minProperties"}:
        unit = "item" if keyword == "minItems" else "field"
        return f"needs at least {error.validator_value} {unit}(s)"
    if keyword in {"enum", "const"}:
        return _NO_SPELLING
    if keyword is None:
        return "the older schema allows no value here"
    return f"fails the older schema's {keyword} constraint"


def _coarsens_to_boolean(source: Any, target: dict[str, Any], root: dict[str, Any]) -> bool:
    """A ``boolean | object`` capability whose older contract kept only the boolean.

    The union itself says how to map down: the boolean is the coarse spelling
    of the structured declaration (e.g. 3.2 ``geo_regions``).
    """
    types = target.get("type")
    if types != "boolean" and not (
        isinstance(types, list) and "boolean" in types and "object" not in types
    ):
        return False
    resolved = _resolve(source, root)
    arms = resolved.get("oneOf", resolved.get("anyOf", []))
    return any(_resolve(arm, root).get("type") == "boolean" for arm in arms) and any(
        _resolve(arm, root).get("type") == "object" or "properties" in _resolve(arm, root)
        for arm in arms
    )


def _declares_support(value: Any) -> bool:
    """Whether a structured capability declares anything supported."""
    if isinstance(value, dict):
        return any(_declares_support(child) for child in value.values())
    if isinstance(value, list):
        return any(_declares_support(child) for child in value)
    if isinstance(value, bool):
        return value
    return value is not None


def _parent_pointer(pointer: str) -> str:
    return pointer.rsplit("/", 1)[0]


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
    response: bool = False,
) -> Any:
    raw_source, raw_target = source, target
    first_loss = len(losses)
    source = _view(source, value, source_root)
    target = _view(target, value, target_root)
    if isinstance(value, dict) and _coarsens_to_boolean(raw_source, target, source_root):
        losses.append(ConversionLoss(path, source_version, target_version, "coarsened_to_boolean"))
        return _declares_support(value)
    if isinstance(value, dict) and source.get("title") in _WHOLE_OBJECTS:
        refusal = _refusal(raw_target, target_root, value, shape=False)
        if refusal is not None and _refusal(raw_source, source_root, value, shape=False) is None:
            if refusal[1] == _NO_SPELLING:
                losses.append(
                    ConversionLoss(
                        path + refusal[0], source_version, target_version, "enum_unavailable"
                    )
                )
            raise _UnrepresentableError(path + refusal[0], refusal[1])
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
        interim: dict[str, Any] = {}
        for key, child in value.items():
            child_path = _pointer(path, key)
            if key in _OPAQUE_FIELDS:
                projected[key] = child
                continue
            source_child = _property(source, key)
            target_child = _property(target, key)
            if source_child is not None and target_child is None:
                refused = _REFUSED_FIELDS.get((source.get("title"), key))
                if refused is not None:
                    raise ADCPVersionConversionError(
                        pointer=child_path,
                        source_version=source_version,
                        target_version=target_version,
                        reason=refused,
                    )
                if (
                    key in _INTERIM_EXT_FIELDS
                    and isinstance(value.get("ext", {}), dict)
                    and ("ext" in target["properties"] or _is_open(target))
                ):
                    interim[key] = child
                    losses.append(
                        ConversionLoss(
                            child_path, source_version, target_version, "moved_to_ext_adcp"
                        )
                    )
                    continue
                if response and not path and key == "message" and _is_open(target):
                    # Human-readable MCP summaries predate the protocol
                    # envelope; older open response objects can retain them.
                    projected[key] = child
                    continue
                if (
                    not response
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
            if (
                key in source["properties"]
                and key not in target["properties"]
                and target_child is target.get("additionalProperties")
                and _refusal(target_child, target_root, child, shape=False) is not None
            ):
                # A field the newer release declares is not an application
                # key of the older open map, which refuses its value: 3.3's
                # ``catalog_ingestion`` object in 3.2's boolean-only features.
                losses.append(ConversionLoss(child_path, source_version, target_version))
                continue
            if source_child is None or target_child is None:
                projected[key] = child
                continue
            source_view = _view(source_child, child, source_root)
            target_view = _view(target_child, child, target_root)
            if (
                (not response or key in target["required"])
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
                        if not response
                        else "the older contract requires a value unavailable in this response"
                    ),
                )
            if (
                response
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
            # be sent to the old peer. A required value is never replaced by
            # a different meaning; the target check fails it structurally.
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
            try:
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
                    response=response,
                )
            except _UnrepresentableError:
                if key in target["required"] or not response:
                    # Omitting a request field changes its meaning, e.g. an
                    # absent filter matches everything; the caller decides.
                    raise
                losses.append(
                    ConversionLoss(
                        child_path, source_version, target_version, "unrepresentable_in_target"
                    )
                )
        if interim:
            # The top-level value wins over an interim one already present.
            ext = projected.get("ext", {})
            adcp = ext.get("adcp") if isinstance(ext.get("adcp"), dict) else {}
            projected["ext"] = {**ext, "adcp": {**adcp, **interim}}
        _check_representable(
            projected,
            value,
            raw_source,
            raw_target,
            source_root,
            target_root,
            path,
            losses,
            first_loss,
        )
        return projected
    if isinstance(value, list):
        projected_items = []
        for index, child in enumerate(value):
            source_item = _view(source.get("items", {}), child, source_root)
            target_item = _view(target.get("items", {}), child, target_root)
            omitted = _omitted_row(child, source_item, target_item) if response else None
            if omitted is not None:
                losses.append(
                    ConversionLoss(_pointer(path, index), source_version, target_version, omitted)
                )
                continue
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
            try:
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
                        response=response,
                    )
                )
            except _UnrepresentableError:
                if not response:
                    # A shortened request list can read as removal, e.g. a
                    # sync with delete_missing.
                    raise
                losses.append(
                    ConversionLoss(
                        _pointer(path, index),
                        source_version,
                        target_version,
                        "unrepresentable_in_target",
                    )
                )
        _check_representable(
            projected_items,
            value,
            raw_source,
            raw_target,
            source_root,
            target_root,
            path,
            losses,
            first_loss,
        )
        return projected_items
    return value


def _is_open(schema: dict[str, Any]) -> bool:
    return schema.get("additionalProperties", True) is not False


def _omitted_row(row: Any, source_item: dict[str, Any], target_item: dict[str, Any]) -> str | None:
    """The loss reason when a response row lacks a field only the older release requires."""
    if not isinstance(row, dict):
        return None
    for field, reason in _ROW_OMISSIONS.items():
        if (
            field not in row
            and field in target_item["required"]
            and field not in source_item["required"]
        ):
            return reason
    return None


def _check_representable(
    projected: Any,
    value: Any,
    source: Any,
    target: Any,
    source_root: dict[str, Any],
    target_root: dict[str, Any],
    path: str,
    losses: list[ConversionLoss],
    first_loss: int,
) -> None:
    """Refuse a node whose own projection left a shape the target rejects.

    Only nodes that lost or coarsened a direct child are checked, and only
    when the drop caused the refusal: the source accepted the original and
    the target accepts the node with those children put back. A refusal
    the drop did not cause, such as a field the older release requires and
    the payload never had, is left to :func:`_require_target_valid`.
    Projection must not turn valid data invalid, but it is not a validator
    for data that arrived invalid.
    """
    dropped = [
        loss.pointer.rsplit("/", 1)[1].replace("~1", "/").replace("~0", "~")
        for loss in losses[first_loss:]
        if _parent_pointer(loss.pointer) == path
    ]
    if not dropped or not _shape_keywords(target, target_root):
        return
    refusal = _refusal(target, target_root, projected)
    if refusal is None or _refusal(source, source_root, value) is not None:
        return
    if isinstance(value, dict):
        restored: Any = {**projected, **{key: value[key] for key in dropped if key in value}}
    else:
        # Only removed items are restored; a coarsened item is still present.
        removed = {
            int(loss.pointer.rsplit("/", 1)[1])
            for loss in losses[first_loss:]
            if _parent_pointer(loss.pointer) == path and loss.reason != "coarsened_to_boolean"
        }
        restored = [*projected, *(value[index] for index in sorted(removed))]
    if _refusal(target, target_root, restored) is None:
        raise _UnrepresentableError(path + refusal[0], refusal[1])


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


def _carries_interim_ext(value: Any) -> bool:
    """Whether any object carries an ``ext.adcp`` interim field (cheap pre-check)."""
    if isinstance(value, list):
        return any(_carries_interim_ext(child) for child in value)
    if not isinstance(value, dict):
        return False
    ext = value.get("ext")
    adcp = ext.get("adcp") if isinstance(ext, dict) else None
    if isinstance(adcp, dict) and not _INTERIM_EXT_FIELDS.isdisjoint(adcp):
        return True
    return any(
        _carries_interim_ext(child) for key, child in value.items() if key not in _OPAQUE_FIELDS
    )


def _hoist_interim_ext(value: Any, schema: Any, root: dict[str, Any]) -> None:
    """Spell an interim ``ext.adcp`` field top-level where the newer schema declares it.

    A top-level value already present wins. The extension bag stays opaque
    and unchanged.
    """
    if not isinstance(value, (dict, list)):
        return
    view = _view(schema, value, root)
    if isinstance(value, list):
        for child in value:
            _hoist_interim_ext(child, view.get("items", {}), root)
        return
    for key, child in value.items():
        child_schema = _property(view, key)
        if key not in _OPAQUE_FIELDS and child_schema is not None:
            _hoist_interim_ext(child, child_schema, root)
    ext = value.get("ext")
    adcp = ext.get("adcp") if isinstance(ext, dict) else None
    if isinstance(adcp, dict):
        for key in _INTERIM_EXT_FIELDS & adcp.keys() & view["properties"].keys():
            value.setdefault(key, deepcopy(adcp[key]))


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
    require_valid_target: bool,
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
            converted = _convert_payload(
                task_type,
                result,
                direction=direction,
                source_version=source_version,
                target_version=target_version,
                require_valid_target=require_valid_target,
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
    loss_log_level: int = logging.WARNING,
    require_valid_target: bool = True,
) -> ConversionResult:
    """Chain adjacent minor conversions without modifying the caller's data.

    The 3.1/3.0 lifecycle envelope is translated semantically. Additive
    changes use the bundled schemas, including nested arrays and unions.
    New tasks without an older equivalent fail before transport.

    Every loss is logged at ``loss_log_level``. ``direction`` says nothing
    about which side of the wire is converting, so callers choose the level:
    a buyer losing a seller's data keeps ``WARNING``, while a seller dropping
    newer fields for an older buyer expects those losses and logs them at
    ``DEBUG`` (see :func:`log_conversion_summary`).

    A downgrade raises :class:`ADCPVersionConversionError` rather than return
    a payload the older schema refuses. ``require_valid_target=False`` skips
    only that final check, for callers reconstructing a peer's own evidence
    rather than sending a payload; projection repairs still apply.
    """
    converted = _convert_payload(
        tool,
        payload,
        direction=direction,
        source_version=source_version,
        target_version=target_version,
        request=request,
        require_valid_target=require_valid_target,
    )
    for loss in converted.losses:
        logger.log(
            loss_log_level,
            "AdCP conversion %s -> %s dropped %s (%s)",
            loss.source_version,
            loss.target_version,
            loss.pointer,
            loss.reason,
        )
    return converted


def log_conversion_summary(
    tool: str,
    losses: tuple[ConversionLoss, ...],
    *,
    source_version: str,
    target_version: str,
    level: int = logging.INFO,
) -> None:
    """Log one line summarizing a conversion's losses, if it had any.

    Array indexes collapse to ``*`` so a response repeating the same drop
    on every item lists the pointer once, e.g.
    ``/products/*/included_signals/*/demographic_predicate``.
    """
    if not losses:
        return
    pointers = list(dict.fromkeys(_ARRAY_INDEX.sub("/*", loss.pointer) for loss in losses))
    shown = ", ".join(pointers[:_SUMMARY_POINTER_LIMIT])
    if len(pointers) > _SUMMARY_POINTER_LIMIT:
        shown += f", ... (+{len(pointers) - _SUMMARY_POINTER_LIMIT} more)"
    logger.log(
        level,
        "AdCP conversion of %s %s -> %s dropped %d field(s): %s",
        tool,
        source_version,
        target_version,
        len(losses),
        shown,
    )


def _convert_payload(
    tool: str,
    payload: dict[str, Any],
    *,
    direction: Direction,
    source_version: str,
    target_version: str,
    request: dict[str, Any] | None = None,
    require_valid_target: bool = True,
) -> ConversionResult:
    """Convert without logging; :func:`convert_payload` logs the final losses once."""
    source_version, target_version = _release(source_version), _release(target_version)
    result = deepcopy(payload)
    losses: list[ConversionLoss] = []
    if source_version == target_version:
        if direction != "request" and "adcp_version" in result:
            result["adcp_version"] = target_version
        return ConversionResult(result)
    source_line, target_line = _convertible(source_version), _convertible(target_version)
    if source_line is None or target_line is None or source_line[0] != target_line[0]:
        raise ADCPFeatureUnsupportedError(
            [f"No AdCP converter from {source_version} to {target_version}"]
        )
    major, minor = source_line
    end = target_line[1]
    step = 1 if end > minor else -1
    while minor != end:
        source = _wire_for_line(major, minor)
        target = _wire_for_line(major, minor + step)
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
            elif step > 0 and uses_task_status_envelope(tool, target):
                result.setdefault("status", "completed")
            if step > 0 and tool in {"get_products", "get_signals"}:
                # Old responses have no cache declaration. An account-scoped
                # result must never be upgraded into a globally cacheable one.
                scope = (
                    "account" if request is None or request.get("account") is not None else "public"
                )
                result.setdefault("cache_scope", scope)
        if step > 0 and _carries_interim_ext(result):
            _hoist_interim_ext(result, target_schema, target_schema)
        if step < 0:
            try:
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
                    response=direction != "request",
                )
            except _UnrepresentableError as exc:
                raise ADCPVersionConversionError(
                    pointer=exc.pointer,
                    source_version=source,
                    target_version=target,
                    reason=(
                        f"the older contract cannot represent the remaining value: {exc.message}"
                    ),
                ) from None
        minor += step
    # The version envelope describes the boundary's contract, including
    # upgrades before application code. 3.0 has only the major envelope.
    if target_version == "3.0":
        result.pop("adcp_version", None)
        if direction == "request":
            result["adcp_major_version"] = 3
    elif direction != "request" or "adcp_version" in payload:
        if direction == "request" or _admits_version_envelope(
            tool, direction, target_version, result
        ):
            result["adcp_version"] = target_version
        else:
            # A 3.2 response that composes no version envelope (list_products)
            # refuses the member outright; 3.3 added it there.
            result.pop("adcp_version", None)
    if direction != "request":
        _convert_embedded_results(
            tool, result, source_version, target_version, losses, require_valid_target
        )
    if step < 0 and require_valid_target:
        _require_target_valid(tool, direction, payload, result, source_version, target_version)
    return ConversionResult(result, tuple(losses))


def _admits_version_envelope(
    tool: str, direction: Direction, version: str, payload: dict[str, Any]
) -> bool:
    """Whether ``version``'s contract for this message admits ``adcp_version``."""
    schema = _schema(tool, direction, version)
    view = _view(schema, payload, schema)
    if "adcp_version" in view["properties"]:
        return True
    return view.get("additionalProperties", True) is not False


def _require_target_valid(
    tool: str,
    direction: Direction,
    payload: dict[str, Any],
    result: dict[str, Any],
    source_version: str,
    target_version: str,
) -> None:
    """A downgrade never emits a payload the older schema refuses.

    Projection repairs what it removed. Anything else the older contract
    requires and the payload cannot supply, such as a newly optional field
    or a required enum member it lacks, fails as a structured error rather
    than reaching the peer invalid. Payloads that arrived invalid are left
    to the caller's validation policy. Validation uses the same bundled
    schemas the projection used, with no payload size cap.
    """
    from adcp.validation._response_envelope import uses_task_status_envelope

    if direction != "request":
        # Match the SDK validators, which default the status envelope.
        if uses_task_status_envelope(tool, source_version):
            payload = {"status": "completed", **payload}
        if uses_task_status_envelope(tool, target_version):
            result = {"status": "completed", **result}
    target_schema = _schema(tool, direction, target_version)
    refusal = _refusal(target_schema, target_schema, result, shape=False)
    if refusal is None:
        return
    source_schema = _schema(tool, direction, source_version)
    if _refusal(source_schema, source_schema, payload, shape=False) is not None:
        return
    raise ADCPVersionConversionError(
        pointer=refusal[0],
        source_version=source_version,
        target_version=target_version,
        reason=f"the older contract refuses the converted payload at that pointer: {refusal[1]}",
    )
