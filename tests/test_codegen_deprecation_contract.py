"""Contract: JSON-Schema ``deprecated: true`` must propagate to Pydantic fields.

The codegen pipeline (datamodel-code-generator + post-generate fixes) is
expected to turn a JSON-Schema property marked ``"deprecated": true`` into a
Pydantic field declared with ``Field(..., deprecated=True)``. That propagation
works today but is otherwise unguarded: a codegen-version bump or a schema
refresh could silently drop the flag and no test would notice.

These tests pin a set of real, currently-deprecated properties from the pinned
schema bundle. Each case is checked from both sides:

1. The schema property genuinely carries ``deprecated: true`` (read straight
   from the cached JSON Schema, so the test stays anchored to the spec rather
   than to the generated code).
2. The corresponding field on the public ``adcp.types`` model exposes
   ``deprecated=True`` in its ``FieldInfo``.

A control assertion confirms non-deprecated fields report ``deprecated=None``,
so a regression that blanket-marks fields would still fail the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Each anchor maps a public ``adcp.types`` model + field to the schema file and
# the JSON-pointer-style property path that declares ``deprecated: true``.
#
# These are deliberately spread across multiple core schemas so a partial
# regression (e.g. deprecation surviving on scalars but not on arrays/objects,
# or on one schema but not another) still trips the contract.
#
# (public_type_name, field_name, schema_relpath, schema_property_path)
_DEPRECATED_ANCHORS: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "TargetingOverlay",
        "axe_include_segment",
        "core/targeting.json",
        ("properties", "axe_include_segment"),
    ),
    (
        "TargetingOverlay",
        "axe_exclude_segment",
        "core/targeting.json",
        ("properties", "axe_exclude_segment"),
    ),
    (
        "TargetingOverlay",
        "signal_targeting",
        "core/targeting.json",
        ("properties", "signal_targeting"),
    ),
    (
        "ProductFilters",
        "required_axe_integrations",
        "core/product-filters.json",
        ("properties", "required_axe_integrations"),
    ),
    (
        "LegacyFormat",
        "canonical_parameters",
        "core/format.json",
        ("properties", "canonical_parameters"),
    ),
    (
        "LegacyFormat",
        "input_format_ids",
        "core/format.json",
        ("properties", "input_format_ids"),
    ),
    (
        "SignalListing",
        "signal_id",
        "core/signal-listing.json",
        ("properties", "signal_id"),
    ),
    (
        "Product",
        "data_provider_signals",
        "core/product.json",
        ("properties", "data_provider_signals"),
    ),
]

# Public fields known NOT to be deprecated. Guards against a regression that
# would mark everything deprecated and make the positive cases pass vacuously.
_NON_DEPRECATED_CONTROLS: list[tuple[str, str]] = [
    ("LegacyFormat", "format_id"),
    ("Product", "product_id"),
]


def _bundle_dir() -> Path:
    """Resolve the cached schema bundle for the pinned ADCP version."""
    from adcp._version import _read_packaged_version
    from adcp.validation.version import resolve_bundle_key

    bundle_key = resolve_bundle_key(_read_packaged_version())
    return Path("schemas") / "cache" / bundle_key


def _resolve(schema: object, path: tuple[str, ...]) -> object:
    cur: object = schema
    for key in path:
        assert isinstance(cur, dict), f"expected dict at {key!r}, got {type(cur).__name__}"
        assert key in cur, f"missing key {key!r}"
        cur = cur[key]
    return cur


@pytest.mark.parametrize(
    ("type_name", "field_name", "schema_relpath", "schema_path"),
    _DEPRECATED_ANCHORS,
    ids=[f"{t}.{f}" for t, f, _, _ in _DEPRECATED_ANCHORS],
)
def test_schema_deprecated_property_propagates_to_field(
    type_name: str,
    field_name: str,
    schema_relpath: str,
    schema_path: tuple[str, ...],
) -> None:
    """A schema property marked deprecated must yield ``deprecated=True``."""
    import adcp.types as adcp_types

    # Spec side: the schema property genuinely declares deprecated: true.
    schema_file = _bundle_dir() / schema_relpath
    schema = json.loads(schema_file.read_text())
    prop = _resolve(schema, schema_path)
    assert isinstance(prop, dict)
    assert prop.get("deprecated") is True, (
        f"{schema_relpath}:{'/'.join(schema_path)} is no longer deprecated in the "
        f"schema bundle — update or remove this anchor"
    )

    # Codegen side: the generated public model carries the flag through.
    model = getattr(adcp_types, type_name)
    field = model.model_fields.get(field_name)
    assert field is not None, (
        f"{type_name}.{field_name} is missing from the generated model "
        f"(available: {sorted(model.model_fields)})"
    )
    assert field.deprecated is True, (
        f"{type_name}.{field_name} lost deprecated=True — the schema marks it "
        f"deprecated but the generated FieldInfo does not. Check the codegen "
        f"pin (datamodel-code-generator) and scripts/post_generate_fixes.py."
    )


@pytest.mark.parametrize(
    ("type_name", "field_name"),
    _NON_DEPRECATED_CONTROLS,
    ids=[f"{t}.{f}" for t, f in _NON_DEPRECATED_CONTROLS],
)
def test_non_deprecated_field_reports_not_deprecated(type_name: str, field_name: str) -> None:
    """Control: undeprecated fields must report ``deprecated is None``.

    Without this the positive assertions could pass vacuously if codegen ever
    started marking every field deprecated.
    """
    import adcp.types as adcp_types

    model = getattr(adcp_types, type_name)
    field = model.model_fields.get(field_name)
    assert field is not None, f"{type_name}.{field_name} unexpectedly missing"
    assert field.deprecated is None, (
        f"{type_name}.{field_name} reports deprecated={field.deprecated!r}; expected None. "
        f"A non-deprecated field is being flagged deprecated."
    )
