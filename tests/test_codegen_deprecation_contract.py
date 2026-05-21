"""Lock that JSON Schema ``deprecated: true`` propagates through the
codegen pipeline to ``deprecated=True`` on Pydantic v2 field metadata.

The pinned ``datamodel-code-generator==0.56.1`` in ``pyproject.toml``
must emit ``deprecated=True`` on ``pydantic.Field(...)`` for every
JSON Schema property that carries ``"deprecated": true``.  This test
locks that contract so regressions are caught in CI rather than in
production.

Regression classes this test prevents:
  * An unreviewed bump to the ``datamodel-code-generator`` pin that
    silently drops deprecation semantics (the issue that prompted
    adcontextprotocol/adcp#4904's expert review flag).
  * A schema refresh that adds ``deprecated: true`` on a field whose
    generated Pydantic type misses the marker.
  * A toolchain swap that changes how the ``deprecated`` keyword
    surfaces in generated output.

Anchor cases (AdCP 3.0.7 — the current schema bundle):
  * ``TargetingOverlay.axe_include_segment`` / ``.axe_exclude_segment``
    across the create/update media-buy request and response bundles.
  * ``GetSignalsRequest.max_results``
  * ``ProductFilters.required_axe_integrations``

Pending anchor cases (active once AdCP 3.1 schema sync lands, #778):
  ``CreateMediaBuySuccessResponse.status`` and
  ``UpdateMediaBuySuccessResponse.status`` will carry ``deprecated: true``
  after adcontextprotocol/adcp#4904 is merged and the schema cache is
  refreshed.  See the commented-out block at the bottom of this file.
"""

from __future__ import annotations

from adcp.types import (
    GetSignalsRequest,
    ProductFilters,
    TargetingOverlay,
)


def _assert_field_deprecated(model_cls: type, field_name: str) -> None:
    """Assert that *field_name* on *model_cls* carries ``deprecated=True``.

    Pydantic v2 stores the ``deprecated`` marker directly on ``FieldInfo``
    as a bool or string (truthy when the field is deprecated).  This
    helper normalises the check and provides a consistent failure message
    that points back to the codegen pipeline.
    """
    assert field_name in model_cls.model_fields, (
        f"{model_cls.__name__} has no field {field_name!r} — "
        f"was the field removed or renamed in the schema?"
    )
    fi = model_cls.model_fields[field_name]
    assert fi.deprecated, (
        f"{model_cls.__name__}.{field_name} must carry deprecated=True.\n"
        f"  Got: deprecated={fi.deprecated!r}\n"
        f"  If datamodel-code-generator was bumped, verify that the new "
        f"version still emits 'deprecated=True' on Pydantic fields whose "
        f"JSON Schema property sets 'deprecated: true'.  "
        f"See pyproject.toml for the pinned version."
    )


# ---------------------------------------------------------------------------
# TargetingOverlay.axe_include_segment / axe_exclude_segment
#
# Source schema: bundled/media-buy/{create,update}-media-buy-{request,response}.json
# These are the canonical deprecated fields in the 3.0 schema bundle.
# The public TargetingOverlay re-exports the same generated class so
# one assertion here covers all four bundled schema occurrences.
# ---------------------------------------------------------------------------


def test_targeting_overlay_axe_include_segment_is_deprecated() -> None:
    _assert_field_deprecated(TargetingOverlay, "axe_include_segment")


def test_targeting_overlay_axe_exclude_segment_is_deprecated() -> None:
    _assert_field_deprecated(TargetingOverlay, "axe_exclude_segment")


# ---------------------------------------------------------------------------
# GetSignalsRequest.max_results
# Source schema: bundled/signals/get-signals-request.json
# ---------------------------------------------------------------------------


def test_get_signals_request_max_results_is_deprecated() -> None:
    _assert_field_deprecated(GetSignalsRequest, "max_results")


# ---------------------------------------------------------------------------
# ProductFilters.required_axe_integrations
# Source schema: bundled/media-buy/get-products-request.json
# ---------------------------------------------------------------------------


def test_product_filters_required_axe_integrations_is_deprecated() -> None:
    _assert_field_deprecated(ProductFilters, "required_axe_integrations")


# ---------------------------------------------------------------------------
# Pending anchor cases — AdCP 3.1 (issue #778 / adcp#4904)
# ---------------------------------------------------------------------------
# When the AdCP 3.1 schema bundle is synced and types are regenerated,
# uncomment the block below and remove this comment.  Both fields will
# carry ``deprecated: true`` in the 3.1 schema after adcp#4904 merges.
#
# from adcp.types.aliases import (
#     CreateMediaBuySuccessResponse,
#     UpdateMediaBuySuccessResponse,
# )
#
# def test_create_media_buy_success_status_is_deprecated_adcp_3_1() -> None:
#     """CreateMediaBuySuccess.status deprecated in AdCP 3.1 (adcp#4904)."""
#     _assert_field_deprecated(CreateMediaBuySuccessResponse, "status")
#
# def test_update_media_buy_success_status_is_deprecated_adcp_3_1() -> None:
#     """UpdateMediaBuySuccess.status deprecated in AdCP 3.1 (adcp#4904)."""
#     _assert_field_deprecated(UpdateMediaBuySuccessResponse, "status")
