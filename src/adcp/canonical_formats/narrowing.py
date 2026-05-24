"""``FORMAT_DECLARATION_DIVERGENT`` narrowing check.

Per ``schemas/cache/<version>/core/product-format-declaration.json#v1_format_ref``
(normative):

    The v2 declaration's `params` MUST narrow (be compatible with) each
    referenced v1 format's `requirements` — see the 'Narrows — formal
    definition' section in canonical-formats.mdx. SDKs comparing
    dual-emitted shapes (`Product.format_ids[]` ⊇ entries from
    `v1_format_ref` AND `Product.format_options[]` carrying this
    declaration) treat the link as the authoritative pairing and run
    the narrowing check between this declaration and EACH referenced v1
    format file's `requirements`.

"Narrows" is structural — v2 params MUST be a subset of the constraints
v1 declared:

* **Numeric maxima** (``max_width``, ``max_height``, ``max_file_size_kb``,
  ``max_duration_ms``, …): v2's declared value MUST be ≤ v1's maximum.
* **Numeric minima** (``min_width``, ``min_height``, ``min_dpi``, …):
  v2's value MUST be ≥ v1's minimum.
* **Enum subsets** (``image_formats``, ``vast_versions``, …): v2's
  declared set MUST be a subset of v1's allowed set.
* **Exact-equal** scalars (``aspect_ratio``, ``vast_version`` when both
  declare a single value): MUST be equal.

The check is conservative — when v1 declares a constraint and v2 omits
the matching field, that's NOT a divergence (v2 silently inherits the
v1 cap, which is what "narrows" means). When v2 declares a constraint
v1 doesn't mention, that's also not a divergence (v2 narrows into
unconstrained space).

The check emits :class:`adcp.types.Error` advisories on
``FORMAT_DECLARATION_DIVERGENT`` when divergence is detected, with
``details`` enumerating each diverging field and the v1/v2 values so
the seller can reconcile.
"""

from __future__ import annotations

from typing import Any

from adcp.canonical_formats.advisory import _echo_identifier, make_sdk_advisory
from adcp.types import Error, ProductFormatDeclaration

# Field-name pairs declaring (v2 params field, v1 requirements field).
# When the names already match (the common case) the v2 lookup is the
# same key. The lists below cover the canonical format parameter sets
# documented under ``schemas/cache/<ver>/formats/canonical/*.json``;
# missing a field here means the SDK silently skips the narrowing
# check for that field, so additions are governed by spec changes.
_MAX_FIELDS: tuple[str, ...] = (
    "max_width",
    "max_height",
    "max_file_size_kb",
    "max_file_size_mb",  # video_hosted.json declares size in MB
    "max_initial_load_kb",
    "max_polite_load_kb",
    "max_duration_ms",
    "max_animation_duration_ms",
    "max_bitrate_kbps",  # video_hosted.json, audio_hosted.json
    "max_wrapper_depth",  # video_vast.json, audio_daast.json
    "max_dpi",
    "max_redirect_depth",
    "max_mention_length_chars",
    "max_mention_duration_ms",
    "max_cpu_load_percent",  # html5.json
    "max_response_time_ms",  # display_tag.json, html5.json
)
_MIN_FIELDS: tuple[str, ...] = (
    "min_width",
    "min_height",
    "min_dpi",
    "min_duration_ms",
    "min_bitrate_kbps",  # video_hosted.json, audio_hosted.json
)
_ENUM_SUBSET_FIELDS: tuple[str, ...] = (
    "image_formats",
    "formats",
    "supported_tag_types",
    "supported_catalog_types",
    "allowed_card_media_asset_types",
    "vast_versions",
    "daast_versions",
)
_EXACT_FIELDS: tuple[str, ...] = (
    "aspect_ratio",
    "orientation",
    "ssl_required",
    "vast_version",  # singular form on video_vast.json
    "daast_version",  # singular form on audio_daast.json
)

# Per-element cap when echoing seller-controlled allowed-set / declared-
# set lists into advisory ``details``. Bounded so a pathological seller
# list doesn't blow up the multi-hop ``errors[]`` payload.
_ECHO_SET_CAP = 32


def _as_dict(value: Any) -> dict[str, Any]:
    """Convert a Pydantic model or dict to a plain dict for field access.

    Tolerates ``None`` (returns empty), Pydantic models (via
    ``model_dump``), and anything dict-shaped. Anything else returns
    ``{}`` so the check fails open rather than raising on opaque inputs.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped: dict[str, Any] = value.model_dump(exclude_none=True)
        return dumped
    return {}


def _is_numeric(value: Any) -> bool:
    """Numeric per the narrowing check — ``int`` / ``float`` but NOT ``bool``.

    Python treats ``bool`` as an ``int`` subclass, so ``True > 5`` is
    valid; a seller declaring ``max_width: True`` would silently pass
    ``isinstance(_, (int, float))`` and corrupt the comparison.
    Excluding bools is the conservative narrowing semantic.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_subset(v2: Any, v1: Any) -> bool:
    """Return ``True`` iff ``v2`` is a subset of ``v1`` under set semantics.

    Tolerates unhashable elements (e.g., dicts in
    ``allowed_card_media_asset_types``) by falling back to ``in``-based
    containment. Returns ``True`` (no divergence) when the comparison
    can't be performed — better to silently pass than to crash on a
    fixture quirk; the narrowing check's job is to surface clear
    divergences, not to discover Pydantic edge cases.
    """
    if not isinstance(v2, (list, tuple, set)):
        v2 = [v2]
    if not isinstance(v1, (list, tuple, set)):
        v1 = [v1]
    try:
        return set(v2).issubset(set(v1))
    except TypeError:
        # Unhashable element somewhere — fall back to membership test.
        return all(any(item == allowed for allowed in v1) for item in v2)


def _echo_set(value: Any) -> list[Any]:
    """Cap a list/set value to ``_ECHO_SET_CAP`` items before echoing into details.

    Seller-controlled list lengths are unbounded at the v1 schema
    level; capping before echo keeps the multi-hop ``errors[]`` payload
    bounded.
    """
    items = list(value) if isinstance(value, (list, tuple, set)) else [value]
    if len(items) <= _ECHO_SET_CAP:
        return items
    return items[:_ECHO_SET_CAP] + [f"…{len(items) - _ECHO_SET_CAP} more"]


def check_narrows(
    v2_params: dict[str, Any] | Any,
    v1_requirements: dict[str, Any] | Any,
) -> list[dict[str, Any]]:
    """Compare ``v2_params`` against ``v1_requirements`` and return divergences.

    A divergence is one of:

    * v1 declares ``max_X = N`` and v2 declares an ``X`` (or ``max_X``,
      or ``X_max``) strictly greater than ``N``.
    * v1 declares ``min_X = N`` and v2 declares an ``X`` (or ``min_X``)
      strictly less than ``N``.
    * v1 declares an enum-typed allowed set and v2 declares a value
      outside that set.
    * Both sides declare a scalar with exact-equal semantics and they
      disagree.

    Returns an empty list when ``v2_params`` narrows ``v1_requirements``
    (the spec-conformant case). Returns a list of ``{field, v1_value,
    v2_value, kind}`` records when divergent — one record per diverging
    field, suitable for ``error.details["divergences"]``.
    """
    v2 = _as_dict(v2_params)
    v1 = _as_dict(v1_requirements)
    if not v2 or not v1:
        return []

    divergences: list[dict[str, Any]] = []

    for field_name in _MAX_FIELDS:
        v1_max = v1.get(field_name)
        if not _is_numeric(v1_max):
            continue
        # v2 may carry the cap directly OR the value being capped (e.g.,
        # v1 declares ``max_width`` and v2 declares ``width``).
        v2_value = v2.get(field_name)
        if v2_value is None:
            v2_value = v2.get(field_name.removeprefix("max_"))
        if v2_value is None:
            continue
        # The value-being-capped form: v2 ``width`` against v1 ``max_width``
        # is a "v2 value MUST be ≤ v1 cap" check.
        if _is_numeric(v2_value) and v2_value > v1_max:
            divergences.append(
                {
                    "field": field_name,
                    "kind": "exceeds_max",
                    "v1_max": v1_max,
                    "v2_value": v2_value,
                }
            )

    for field_name in _MIN_FIELDS:
        v1_min = v1.get(field_name)
        if not _is_numeric(v1_min):
            continue
        v2_value = v2.get(field_name)
        if v2_value is None:
            v2_value = v2.get(field_name.removeprefix("min_"))
        if v2_value is None:
            continue
        if _is_numeric(v2_value) and v2_value < v1_min:
            divergences.append(
                {
                    "field": field_name,
                    "kind": "below_min",
                    "v1_min": v1_min,
                    "v2_value": v2_value,
                }
            )

    for field_name in _ENUM_SUBSET_FIELDS:
        v1_set = v1.get(field_name)
        v2_set = v2.get(field_name)
        if v1_set is None or v2_set is None:
            continue
        if not _is_subset(v2_set, v1_set):
            divergences.append(
                {
                    "field": field_name,
                    "kind": "not_subset",
                    "v1_allowed": _echo_set(v1_set),
                    "v2_declared": _echo_set(v2_set),
                }
            )

    for field_name in _EXACT_FIELDS:
        v1_value = v1.get(field_name)
        v2_value = v2.get(field_name)
        if v1_value is None or v2_value is None:
            continue
        if v1_value != v2_value:
            divergences.append(
                {
                    "field": field_name,
                    "kind": "not_equal",
                    "v1_value": v1_value,
                    "v2_value": v2_value,
                }
            )

    return divergences


def narrowing_advisory(
    declaration: ProductFormatDeclaration,
    *,
    v1_requirements: dict[str, Any],
    v1_format_id: str,
    field_path: str = "format_options[]",
) -> Error | None:
    """Build the ``FORMAT_DECLARATION_DIVERGENT`` advisory for a single pairing.

    Returns ``None`` when ``declaration.params`` narrows ``v1_requirements``
    (no divergence to report). Returns an :class:`Error` with
    ``details.divergences`` listing the failing fields when divergent.

    Args:
        declaration: The v2 ``ProductFormatDeclaration`` carrying
            ``v1_format_ref[]``.
        v1_requirements: The referenced v1 format's ``requirements``
            object (dict or Pydantic model).
        v1_format_id: The v1 format identifier (``id`` portion of the
            ``FormatId``) — surfaced in advisory details so adopters can
            locate the divergent pair when a declaration carries many
            refs.
        field_path: JSONPath-lite pointer for the advisory's ``field``.
    """
    divs = check_narrows(declaration.params, v1_requirements)
    if not divs:
        return None
    safe_id = _echo_identifier(v1_format_id)
    return make_sdk_advisory(
        code="FORMAT_DECLARATION_DIVERGENT",
        message=(
            f"v2 declaration (format_kind={declaration.format_kind.value!r}) "
            f"params do not narrow v1 format {safe_id!r} requirements: "
            f"{len(divs)} divergence(s)."
        ),
        field=field_path,
        details={
            "format_kind": declaration.format_kind.value,
            "v1_format_id": safe_id,
            "divergences": divs,
        },
        suggestion=(
            "Reconcile the v2 params against the referenced v1 format's "
            "requirements: lower the v2 cap, expand the v1 allowed set, "
            "or drop the v1_format_ref entry if the formats genuinely "
            "differ in shape."
        ),
    )


__all__ = [
    "check_narrows",
    "narrowing_advisory",
]
