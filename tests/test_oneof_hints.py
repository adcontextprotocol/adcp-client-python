"""Tests for the ``oneOf`` near-miss validator hint.

When a discriminated-union shape fails validation because the caller used
the wrong key as the discriminator (the v3 reference-seller
``pricing_options`` regression: ``{"type": "cpm"}`` instead of
``{"pricing_model": "cpm"}``), an additive ``hint`` field on the
``VALIDATION_ERROR`` issue names the closest matching variant and the
wrong / expected discriminator keys.
"""

from __future__ import annotations

from typing import Any

from adcp.validation.oneof_hints import compute_oneof_hint
from adcp.validation.schema_errors import build_adcp_validation_error_payload
from adcp.validation.schema_validator import (
    SchemaValidationError,
    ValidationIssue,
    _issue_to_wire,
    validate_response,
)

# Mirrors the AdCP `pricing-option` oneOf shape — three variants pinning
# `pricing_model` via `const`. Kept inline so the test isn't coupled to
# the bundled schema's exact variant count.
_PRICING_LIKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pricing_options": {
            "type": "array",
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "pricing_option_id": {"type": "string"},
                            "pricing_model": {"type": "string", "const": "cpm"},
                            "currency": {"type": "string"},
                        },
                        "required": ["pricing_option_id", "pricing_model", "currency"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "pricing_option_id": {"type": "string"},
                            "pricing_model": {"type": "string", "const": "cpc"},
                            "currency": {"type": "string"},
                        },
                        "required": ["pricing_option_id", "pricing_model", "currency"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "pricing_option_id": {"type": "string"},
                            "pricing_model": {"type": "string", "const": "flat_rate"},
                            "currency": {"type": "string"},
                        },
                        "required": ["pricing_option_id", "pricing_model", "currency"],
                    },
                ]
            },
        }
    },
}


class TestComputeOneofHint:
    def test_pricing_options_type_vs_pricing_model_regression(self) -> None:
        """The v3 ref-seller regression: caller used ``type`` instead of
        ``pricing_model`` as the discriminator. Hint must name the ``cpm``
        variant and call out ``pricing_model`` vs ``type``."""
        payload = {
            "pricing_options": [{"pricing_option_id": "po1", "type": "cpm", "currency": "USD"}]
        }
        hint = compute_oneof_hint(
            _PRICING_LIKE_SCHEMA,
            ["properties", "pricing_options", "items", "oneOf"],
            ["pricing_options", 0],
            payload,
        )
        assert hint is not None
        assert "'cpm'" in hint
        assert "'pricing_model'" in hint
        assert "'type'" in hint
        assert "instead of" in hint

    def test_no_hint_when_discriminator_present_but_value_invalid(self) -> None:
        """Caller used the right discriminator key but an invalid value —
        the standard enum-like message is more accurate than a hint."""
        payload = {
            "pricing_options": [
                {
                    "pricing_option_id": "po1",
                    "pricing_model": "not_a_real_model",
                    "currency": "USD",
                }
            ]
        }
        hint = compute_oneof_hint(
            _PRICING_LIKE_SCHEMA,
            ["properties", "pricing_options", "items", "oneOf"],
            ["pricing_options", 0],
            payload,
        )
        assert hint is None

    def test_no_hint_when_no_clear_winner(self) -> None:
        """When the caller's payload doesn't carry any variant's
        discriminator value and shapes tie, the heuristic stays silent."""
        payload = {"pricing_options": [{"pricing_option_id": "po1", "currency": "USD"}]}
        hint = compute_oneof_hint(
            _PRICING_LIKE_SCHEMA,
            ["properties", "pricing_options", "items", "oneOf"],
            ["pricing_options", 0],
            payload,
        )
        assert hint is None

    def test_no_hint_when_no_discriminator_in_schema(self) -> None:
        """Schemas whose ``oneOf`` variants don't pin a ``const``
        discriminator field are out of scope for this heuristic."""
        non_disc_schema: dict[str, Any] = {
            "oneOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"type": "object", "properties": {"b": {"type": "string"}}, "required": ["b"]},
            ]
        }
        hint = compute_oneof_hint(non_disc_schema, ["oneOf"], [], {"c": "x"})
        assert hint is None

    def test_no_hint_when_value_is_not_object(self) -> None:
        """oneOf failures on scalars (e.g., string-or-array unions) don't
        carry a discriminator — hint stays silent."""
        scalar_oneof = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        hint = compute_oneof_hint(scalar_oneof, ["oneOf"], [], True)
        assert hint is None

    def test_picks_correct_variant_among_many(self) -> None:
        """With nine pricing-model variants, the hint names the one whose
        ``const`` matches the value the caller supplied."""
        payload = {
            "pricing_options": [
                {"pricing_option_id": "po1", "type": "flat_rate", "currency": "USD"}
            ]
        }
        hint = compute_oneof_hint(
            _PRICING_LIKE_SCHEMA,
            ["properties", "pricing_options", "items", "oneOf"],
            ["pricing_options", 0],
            payload,
        )
        assert hint is not None
        assert "'flat_rate'" in hint

    def test_seen_key_is_the_one_carrying_winning_variant_const(self) -> None:
        """When multiple top-level keys carry values matching different
        variants' consts, the hint's ``seen_key`` must be the one tied
        to the *winning* variant — not whichever key the old fallback
        scan happened to find first.

        Payload: ``alias: "cpc"``, ``flavor: "cpm"``, plus shape-fields
        only present in the ``cpc`` variant. The cpc variant wins on
        required_present, so the hint must say "use ``pricing_model``
        instead of ``alias``" (the key carrying ``"cpc"``), not
        ``flavor``.
        """
        schema: dict[str, Any] = {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "pricing_model": {"type": "string", "const": "cpm"},
                        "cpm_only": {"type": "string"},
                    },
                    "required": ["pricing_model", "cpm_only"],
                },
                {
                    "type": "object",
                    "properties": {
                        "pricing_model": {"type": "string", "const": "cpc"},
                        "cpc_only": {"type": "string"},
                    },
                    "required": ["pricing_model", "cpc_only"],
                },
            ]
        }
        # cpc-shape (cpc_only present) — but BOTH "cpm" and "cpc" appear
        # as values under unrelated keys. The winner is cpc on shape; the
        # seen_key reported in the hint must be the one carrying "cpc".
        payload = {"alias": "cpc", "flavor": "cpm", "cpc_only": "x"}
        hint = compute_oneof_hint(schema, ["oneOf"], [], payload)
        assert hint is not None
        assert "'cpc'" in hint
        assert "'pricing_model'" in hint
        assert "'alias'" in hint
        assert "'flavor'" not in hint

    def test_no_hint_on_tie_without_const_match(self) -> None:
        """Two variants score identically on shape (no const_match for
        either) — the heuristic must not promote one over the other."""
        schema: dict[str, Any] = {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "const": "alpha"},
                        "shared": {"type": "string"},
                    },
                    "required": ["kind", "shared"],
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "const": "beta"},
                        "shared": {"type": "string"},
                    },
                    "required": ["kind", "shared"],
                },
            ]
        }
        # Payload supplies `shared` (in both variants' required + properties)
        # but no `kind` and no value matching either const. Both variants
        # score (0, 1, 1, None) — a tie.
        payload = {"shared": "x"}
        hint = compute_oneof_hint(schema, ["oneOf"], [], payload)
        assert hint is None

    def test_runner_up_with_extra_unmatched_property_does_not_win(self) -> None:
        """Variant A is a true near-miss (caller supplied A's const value
        via the wrong key). Variant B has more declared properties but
        the payload doesn't carry them. const_match must dominate so A
        wins, even though B has higher ``total`` declared properties."""
        schema: dict[str, Any] = {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "const": "alpha"},
                        "value": {"type": "string"},
                    },
                    "required": ["kind", "value"],
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "const": "beta"},
                        "value": {"type": "string"},
                        "extra1": {"type": "string"},
                        "extra2": {"type": "string"},
                        "extra3": {"type": "string"},
                    },
                    "required": ["kind", "value"],
                },
            ]
        }
        payload = {"category": "alpha", "value": "v"}
        hint = compute_oneof_hint(schema, ["oneOf"], [], payload)
        assert hint is not None
        assert "'alpha'" in hint
        assert "'kind'" in hint
        assert "'category'" in hint

    def test_nested_oneof_failures_each_get_their_own_hint(self) -> None:
        """``oneOf`` inside ``oneOf``: outer variants choose by ``mode``,
        and one outer variant has a nested ``oneOf`` keyed by
        ``pricing_model``. Both layers must produce independent hints
        when each is queried with its own schema/instance path."""
        nested_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "config": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "mode": {"type": "string", "const": "auto"},
                                "pricing": {
                                    "oneOf": [
                                        {
                                            "type": "object",
                                            "properties": {
                                                "pricing_model": {
                                                    "type": "string",
                                                    "const": "cpm",
                                                },
                                                "currency": {"type": "string"},
                                            },
                                            "required": ["pricing_model", "currency"],
                                        },
                                        {
                                            "type": "object",
                                            "properties": {
                                                "pricing_model": {
                                                    "type": "string",
                                                    "const": "cpc",
                                                },
                                                "currency": {"type": "string"},
                                            },
                                            "required": ["pricing_model", "currency"],
                                        },
                                    ]
                                },
                            },
                            "required": ["mode", "pricing"],
                        },
                        {
                            "type": "object",
                            "properties": {
                                "mode": {"type": "string", "const": "manual"},
                                "knob": {"type": "string"},
                            },
                            "required": ["mode", "knob"],
                        },
                    ]
                }
            },
        }
        payload = {
            "config": {
                "type": "auto",
                "pricing": {"type": "cpm", "currency": "USD"},
            }
        }
        # Outer hint: caller used `type` instead of `mode` to pick `auto`.
        outer_hint = compute_oneof_hint(
            nested_schema,
            ["properties", "config", "oneOf"],
            ["config"],
            payload,
        )
        assert outer_hint is not None
        assert "'auto'" in outer_hint
        assert "'mode'" in outer_hint
        assert "'type'" in outer_hint

        # Inner hint: caller used `type` instead of `pricing_model` to pick `cpm`.
        inner_hint = compute_oneof_hint(
            nested_schema,
            ["properties", "config", "oneOf", 0, "properties", "pricing", "oneOf"],
            ["config", "pricing"],
            payload,
        )
        assert inner_hint is not None
        assert "'cpm'" in inner_hint
        assert "'pricing_model'" in inner_hint
        assert "'type'" in inner_hint

    def test_two_independent_oneof_failures_each_get_their_own_hint(self) -> None:
        """Two array entries each fail their own ``oneOf`` for different
        reasons. Each instance path must be hinted independently."""
        payload = {
            "pricing_options": [
                {"pricing_option_id": "po1", "type": "cpm", "currency": "USD"},
                {"pricing_option_id": "po2", "type": "cpc", "currency": "USD"},
            ]
        }
        hint_0 = compute_oneof_hint(
            _PRICING_LIKE_SCHEMA,
            ["properties", "pricing_options", "items", "oneOf"],
            ["pricing_options", 0],
            payload,
        )
        hint_1 = compute_oneof_hint(
            _PRICING_LIKE_SCHEMA,
            ["properties", "pricing_options", "items", "oneOf"],
            ["pricing_options", 1],
            payload,
        )
        assert hint_0 is not None
        assert "'cpm'" in hint_0
        assert hint_1 is not None
        assert "'cpc'" in hint_1
        # Hints must not cross-contaminate.
        assert "'cpc'" not in hint_0
        assert "'cpm'" not in hint_1


class TestValidationIntegration:
    def test_clean_validation_passes_silently(self) -> None:
        """A valid payload yields no issues at all — no hint plumbing
        side-effect on the success path. The bundled ``get_products``
        response schema must be present (we assert ``variant != skipped``
        so this stays a real signal rather than vacuous)."""
        payload: dict[str, Any] = {"products": [], "cache_scope": "public"}
        outcome = validate_response("get_products", payload)
        assert (
            outcome.variant != "skipped"
        ), "expected the bundled get_products schema to be loaded; got skipped"
        assert outcome.issues == []

    def test_pricing_options_regression_against_real_schema(self) -> None:
        """End-to-end against the bundled ``get_products`` schema. The
        wrong-discriminator failure on ``pricing_options[0]`` carries a
        hint naming ``cpm`` and ``pricing_model``."""
        payload: dict[str, Any] = {
            "cache_scope": "public",
            "products": [
                {
                    "product_id": "p1",
                    "name": "P",
                    "description": "d",
                    "format_ids": [{"format_id": "display_300x250"}],
                    "delivery_type": "guaranteed",
                    "pricing_options": [
                        {"pricing_option_id": "po1", "type": "cpm", "currency": "USD"}
                    ],
                    "reporting_capabilities": {"available_metrics": []},
                    "publisher_properties": {"property_list_id": "pl1"},
                }
            ],
        }
        outcome = validate_response("get_products", payload)
        oneof_issues = [i for i in outcome.issues if i.keyword == "oneOf"]
        assert oneof_issues, (
            "expected at least one oneOf issue against the bundled schema; "
            f"got keywords={[i.keyword for i in outcome.issues]}"
        )
        pricing_issue = next((i for i in oneof_issues if "pricing_options" in i.pointer), None)
        assert pricing_issue is not None
        assert pricing_issue.hint is not None
        assert "'cpm'" in pricing_issue.hint
        assert "'pricing_model'" in pricing_issue.hint
        assert "'type'" in pricing_issue.hint


class TestWireEnvelope:
    def test_hint_is_optional_in_wire_payload(self) -> None:
        """Issues without hints serialize without the field — clients
        that ignore the new key see exactly the pre-hint envelope."""
        issue = ValidationIssue(
            pointer="/foo",
            message="expected type 'string'",
            keyword="type",
            schema_path="#/properties/foo/type",
        )
        wire = _issue_to_wire(issue)
        assert "hint" not in wire
        assert wire == {
            "pointer": "/foo",
            "message": "expected type 'string'",
            "keyword": "type",
            "schema_path": "#/properties/foo/type",
        }

    def test_hint_included_when_present(self) -> None:
        issue = ValidationIssue(
            pointer="/pricing_options/0",
            message="oneOf composition failed",
            keyword="oneOf",
            schema_path="#/properties/pricing_options/items/oneOf",
            hint="Looks like you may have meant the 'cpm' variant. "
            "Use 'pricing_model' instead of 'type' as the discriminator.",
        )
        wire = _issue_to_wire(issue)
        assert wire.get("hint") == issue.hint

    def test_build_adcp_validation_error_payload_carries_hint(self) -> None:
        """The wire envelope produced by ``build_adcp_validation_error_payload``
        — the canonical projection point for ``VALIDATION_ERROR`` — surfaces
        ``hint`` per-issue when set."""
        issues = [
            ValidationIssue(
                pointer="/pricing_options/0",
                message="oneOf composition failed",
                keyword="oneOf",
                schema_path="#/properties/pricing_options/items/oneOf",
                hint="Looks like you may have meant the 'cpm' variant. "
                "Use 'pricing_model' instead of 'type' as the discriminator.",
            ),
        ]
        payload = build_adcp_validation_error_payload("get_products", "response", issues)
        assert payload["code"] == "VALIDATION_ERROR"
        wire_issues = payload["details"]["issues"]
        assert wire_issues[0]["hint"].startswith("Looks like")

    def test_schema_validation_error_details_carries_hint(self) -> None:
        issues = [
            ValidationIssue(
                pointer="/pricing_options/0",
                message="oneOf composition failed",
                keyword="oneOf",
                schema_path="#/properties/pricing_options/items/oneOf",
                hint="Looks like you may have meant the 'cpm' variant. "
                "Use 'pricing_model' instead of 'type' as the discriminator.",
            ),
        ]
        err = SchemaValidationError("get_products", "response", issues)
        wire_issues = err.details["issues"]
        assert wire_issues[0]["hint"].startswith("Looks like")
