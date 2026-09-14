"""Preserve explicit ``null`` in request payloads, where null is a command.

AdCP 3.2 request schemas use a three-state convention on mutation inputs:

* **omitted** — leave the stored value unchanged (or inherit the product
  default on create);
* **``null``** — *clear* the stored value (or suppress the product default);
* **a value** — replace it.

Every client request in this SDK serializes with
``model_dump(mode="json", exclude_none=True)``, which collapses the first two
states into one. That is correct for the overwhelming majority of optional
fields — an unset optional must not appear on the wire — but it silently
destroys the middle state. A buyer that writes
``frequency_cap=None`` to remove a cap, or
``targeting_overlay=TargetingOverlayInput(geo_countries=None)`` to clear a
targeting dimension, gets "leave unchanged" and no error. The request succeeds,
the cap stays on, and nothing anywhere says so.

Pydantic already records the difference: ``model_fields_set`` contains a field
the caller supplied, even when the value is ``None``. What this module adds is
the *other* half of the decision — whether ``null`` is actually meaningful at
that path, which only the schema knows.

Why the schema and not the annotation
-------------------------------------

Every optional generated field is annotated ``T | None = None``, so the
annotation cannot distinguish "nullable on the wire" from "optional in Python".
``canceled: Literal[True] | None`` is the cautionary case: the schema permits
only ``true``, so emitting ``"canceled": null`` because a caller passed
``canceled=None`` would turn a meaningless keyword argument into a request the
seller must reject. So the nullable paths are computed from the bundled request
schema — ``{"type": ["string", "null"]}`` or an ``anyOf`` containing
``{"type": "null"}`` — and cached per task.

This generalizes the three fields hard-coded in #1161
(``ControlMediaBuyRequest.daily_budget_cap`` / ``budget_cap_timezone`` and
``packages[].daily_budget_cap``) to every path the spec marks nullable,
including the AdCP 3.2.0-rc.3 additions: the MediaBuy-level ``frequency_cap``
on ``update_media_buy`` and ``control_media_buy``, and every dimension of
``core/targeting-input.json`` on ``packages[]`` and ``new_packages[]``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

__all__ = ["nullable_request_paths", "preserve_explicit_nulls"]

#: Guard against a pathological ``$ref`` cycle in a hand-edited bundle. The
#: deepest real request nesting is well under this.
_MAX_DEPTH = 12


def _definitions(root: Mapping[str, Any]) -> Mapping[str, Any]:
    out: dict[str, Any] = {}
    for key in ("definitions", "$defs"):
        block = root.get(key)
        if isinstance(block, Mapping):
            out.update(block)
    return out


def _resolve(node: Any, root: Mapping[str, Any]) -> Any:
    """Follow a local ``$ref`` one hop.

    Bundled schemas are self-contained, so only local pointers appear. A
    remote pointer is left unresolved rather than fetched: this runs on the
    request path and must never do I/O.
    """
    if not isinstance(node, Mapping):
        return node
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    target: Any = root
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(target, Mapping) or part not in target:
            return node
        target = target[part]
    return target if isinstance(target, Mapping) else node


def _branches(node: Mapping[str, Any]) -> list[Any]:
    """Every subschema that could describe the same instance."""
    out: list[Any] = []
    for key in ("anyOf", "oneOf", "allOf"):
        block = node.get(key)
        if isinstance(block, Sequence) and not isinstance(block, str | bytes):
            out.extend(block)
    return out


def _permits_null(node: Any, root: Mapping[str, Any], depth: int = 0) -> bool:
    node = _resolve(node, root)
    if not isinstance(node, Mapping) or depth > _MAX_DEPTH:
        return False
    declared = node.get("type")
    if declared == "null":
        return True
    if isinstance(declared, Sequence) and not isinstance(declared, str) and "null" in declared:
        return True
    return any(_permits_null(branch, root, depth + 1) for branch in _branches(node))


def _walk(
    node: Any,
    root: Mapping[str, Any],
    prefix: tuple[str, ...],
    found: set[tuple[str, ...]],
    depth: int = 0,
) -> None:
    """Collect every property path at which the schema permits ``null``.

    Array items descend under the same prefix rather than an indexed one, so
    ``packages[].daily_budget_cap`` is recorded as
    ``("packages", "daily_budget_cap")`` and matches any index at apply time.
    """
    node = _resolve(node, root)
    if not isinstance(node, Mapping) or depth > _MAX_DEPTH:
        return

    properties = node.get("properties")
    if isinstance(properties, Mapping):
        for name, subschema in properties.items():
            path = (*prefix, str(name))
            if _permits_null(subschema, root):
                found.add(path)
            _walk(subschema, root, path, found, depth + 1)

    items = node.get("items")
    if items is not None:
        _walk(items, root, prefix, found, depth + 1)

    for branch in _branches(node):
        _walk(branch, root, prefix, found, depth + 1)


@lru_cache(maxsize=256)
def nullable_request_paths(
    task_name: str, version: str | None = None
) -> frozenset[tuple[str, ...]]:
    """Property paths in ``task_name``'s request schema where ``null`` is legal.

    Cached because it is a pure function of the bundled schema, which is
    immutable for the life of the process. Returns an empty set when the
    schema is unavailable (an older pin, or a task this bundle does not
    define), so an unknown task degrades to today's ``exclude_none`` behavior
    rather than failing the request.
    """
    from adcp.validation.schema_loader import get_schema

    schema = get_schema(task_name, "request", version=version)
    if schema is None:
        return frozenset()
    found: set[tuple[str, ...]] = set()
    _walk(schema, schema, (), found)
    # A top-level ``$ref``-only document (the unwrapped-union request shapes)
    # needs its definitions walked too, or every path would be missed.
    if not found:
        for definition in _definitions(schema).values():
            _walk(definition, schema, (), found)
    return frozenset(found)


def preserve_explicit_nulls(
    request: BaseModel,
    params: dict[str, Any],
    *,
    task_name: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Re-insert the nulls ``exclude_none=True`` dropped, where they are commands.

    Mutates and returns ``params``. A field is restored only when **both** hold:

    1. the caller explicitly supplied it (it is in ``model_fields_set``) and its
       value is ``None`` — so an unset optional stays absent; and
    2. the bundled request schema permits ``null`` at that path — so
       ``canceled=None`` on a ``Literal[True]`` field stays dropped instead of
       becoming a request no seller can accept.

    Nested models and lists of models are walked in lockstep with the dumped
    payload, so a cleared dimension inside ``packages[3].targeting_overlay``
    survives.
    """
    nullable = nullable_request_paths(task_name, version)
    if not nullable:
        return params
    _restore(request, params, (), nullable)
    return params


def _restore(
    model: BaseModel,
    payload: dict[str, Any],
    prefix: tuple[str, ...],
    nullable: frozenset[tuple[str, ...]],
) -> None:
    for name in model.model_fields_set:
        value = getattr(model, name, None)
        path = (*prefix, name)
        if value is None:
            if path in nullable:
                payload[name] = None
            continue
        if isinstance(value, BaseModel):
            nested = payload.get(name)
            if isinstance(nested, dict):
                _restore(value, nested, path, nullable)
            continue
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            dumped = payload.get(name)
            if not isinstance(dumped, list):
                continue
            # zip() stops at the shorter sequence, which is what we want: a
            # value that serialized away has no payload slot to patch.
            for item, slot in zip(value, dumped, strict=False):
                if isinstance(item, BaseModel) and isinstance(slot, dict):
                    _restore(item, slot, path, nullable)
