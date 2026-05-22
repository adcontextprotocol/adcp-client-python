"""Unit tests for the schema-driven validator (issue #249).

Exercises the real bundled schemas shipped with the SDK — no mocking,
so a schema regeneration that breaks the validator's assumptions
surfaces here rather than at storyboard time.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from adcp.validation import (
    SchemaValidationError,
    ValidationHookConfig,
    build_adcp_validation_error_payload,
    build_validation_error,
    format_issues,
    list_validator_keys,
    resolve_validation_modes,
    validate_incoming_response,
    validate_outgoing_request,
    validate_request,
    validate_response,
)
from adcp.validation.schema_validator import ValidationIssue


class TestValidateRequest:
    def test_flags_missing_required_fields_with_json_pointer(self) -> None:
        outcome = validate_request("get_products", {})
        assert outcome.valid is False
        pointers = [i.pointer for i in outcome.issues]
        assert "/buying_mode" in pointers, f"expected /buying_mode in {pointers}"

    def test_returns_skipped_for_tools_outside_adcp_catalog(self) -> None:
        outcome = validate_request("custom_seller_extension", {"anything": True})
        assert outcome.valid is True
        assert outcome.variant == "skipped"

    def test_accepts_extension_fields_without_error(self) -> None:
        outcome = validate_request(
            "get_products",
            {
                "brief": "campaign brief",
                "promoted_offering": "product",
                "buying_mode": "brief",
                # ext + unknown vendor field — additionalProperties must tolerate both
                "ext": {"gam": {"custom_field": 1}},
                "unknown_vendor_field": {"ok": True},
            },
        )
        # Schema may require other fields; only assert unknown extensions
        # themselves don't appear as failures.
        for issue in outcome.issues:
            assert issue.pointer != "/unknown_vendor_field"
            assert not issue.pointer.startswith("/ext")


class TestValidateResponse:
    def test_selects_submitted_variant_on_status(self) -> None:
        outcome = validate_response("create_media_buy", {"status": "submitted", "task_id": "t_1"})
        assert outcome.valid is True
        assert outcome.variant == "submitted"

    def test_selects_working_variant(self) -> None:
        outcome = validate_response("create_media_buy", {"status": "working", "task_id": "t_2"})
        assert outcome.valid is True
        assert outcome.variant == "working"

    def test_selects_input_required_variant(self) -> None:
        outcome = validate_response(
            "create_media_buy", {"status": "input-required", "task_id": "t_3"}
        )
        assert outcome.valid is True
        assert outcome.variant == "input-required"

    def test_falls_back_to_sync_without_status(self) -> None:
        outcome = validate_response("create_media_buy", {"media_buy_id": "mb_1"})
        assert outcome.variant == "sync"

    def test_surfaces_errors_with_pointer_keyword_schema_path(self) -> None:
        outcome = validate_response(
            "get_products", {"products": "not-an-array", "cache_scope": "public"}
        )
        assert outcome.valid is False
        products_issue = next((i for i in outcome.issues if i.pointer == "/products"), None)
        assert products_issue is not None, "expected an issue at /products"
        assert products_issue.keyword == "type"
        assert products_issue.schema_path
        # Sanitized message MUST NOT echo the offending value — the
        # whole point of the sanitizer is keeping tokens/PII out of the
        # wire envelope and logs.
        assert "not-an-array" not in products_issue.message
        assert "expected type" in products_issue.message

    def test_sanitizes_offending_value_out_of_message(self) -> None:
        """Hostile or buggy payloads can carry secrets in the wrong slot.
        The error message the caller sees must not echo them back."""
        secret = "Bearer sk-should-never-appear-in-any-error"
        outcome = validate_response("get_products", {"products": secret, "cache_scope": "public"})
        assert outcome.valid is False
        for issue in outcome.issues:
            assert secret not in issue.message
            assert secret not in issue.schema_path


class TestFormatIssues:
    def test_caps_verbose_failures_and_notes_overflow(self) -> None:
        issues = [
            ValidationIssue(
                pointer=f"/{c}",
                message="oops",
                keyword="required",
                schema_path="#/required",
            )
            for c in "abcd"
        ]
        summary = format_issues(issues, limit=2)
        assert "/a" in summary
        assert "/b" in summary
        assert "(+2 more)" in summary


class TestErrorBuilders:
    def test_build_validation_error_carries_details(self) -> None:
        issues = [
            ValidationIssue(
                pointer="/foo/bar",
                message="bad",
                keyword="type",
                schema_path="#/properties/foo/bar",
            )
        ]
        err = build_validation_error("get_products", "request", issues)
        assert isinstance(err, SchemaValidationError)
        assert err.code == "VALIDATION_ERROR"
        assert err.tool == "get_products"
        assert err.side == "request"
        assert err.issues == issues
        # ``details`` is a declared attribute on the class — not a bolt-on.
        assert err.details["tool"] == "get_products"
        assert err.details["side"] == "request"
        assert err.details["issues"][0]["pointer"] == "/foo/bar"

    def test_build_adcp_validation_error_payload(self) -> None:
        issues = [
            ValidationIssue(
                pointer="/media_buy_id",
                message="is required",
                keyword="required",
                schema_path="#/required",
            )
        ]
        payload = build_adcp_validation_error_payload("create_media_buy", "response", issues)
        assert payload["code"] == "VALIDATION_ERROR"
        assert "/media_buy_id" in payload["message"]
        assert payload["field"] == "/media_buy_id"
        assert payload["details"]["tool"] == "create_media_buy"
        assert payload["details"]["side"] == "response"
        assert payload["details"]["issues"][0]["pointer"] == "/media_buy_id"


class TestListValidatorKeys:
    def test_exposes_every_shipped_pair(self) -> None:
        keys = list_validator_keys()
        assert len(keys) > 0
        for expected in (
            "get_products::request",
            "get_products::sync",
            "create_media_buy::submitted",
            "create_media_buy::working",
            "create_media_buy::input-required",
        ):
            assert expected in keys, f"missing {expected}"


class TestClientHooks:
    def test_outgoing_strict_raises(self) -> None:
        with pytest.raises(SchemaValidationError) as info:
            validate_outgoing_request("create_media_buy", {}, "strict")
        assert info.value.code == "VALIDATION_ERROR"

    def test_outgoing_warn_logs_and_returns_invalid(self) -> None:
        logs: list = []
        outcome = validate_outgoing_request("create_media_buy", {}, "warn", logs)
        assert outcome is not None
        assert outcome.valid is False
        assert len(logs) == 1
        assert logs[0]["type"] == "warning"

    def test_outgoing_off_short_circuits(self) -> None:
        outcome = validate_outgoing_request("create_media_buy", {}, "off")
        assert outcome is None

    def test_incoming_off_is_noop_valid(self) -> None:
        outcome = validate_incoming_response("get_products", {"products": "not-array"}, "off")
        assert outcome.valid is True

    def test_incoming_warn_logs_and_returns_invalid(self) -> None:
        logs: list = []
        outcome = validate_incoming_response(
            "get_products", {"products": "not-array"}, "warn", logs
        )
        assert outcome.valid is False
        assert len(logs) == 1


class TestResolveValidationModes:
    def test_requests_default_to_warn(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            req, _ = resolve_validation_modes()
        assert req == "warn"

    def test_responses_default_to_strict(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            _, resp = resolve_validation_modes()
        assert resp == "strict"

    def test_responses_flip_warn_when_adcp_env_is_production(self) -> None:
        with patch.dict(os.environ, {"ADCP_ENV": "production"}, clear=True):
            _, resp = resolve_validation_modes()
        assert resp == "warn"

    def test_responses_flip_warn_when_adcp_env_is_prod_shorthand(self) -> None:
        with patch.dict(os.environ, {"ADCP_ENV": "prod"}, clear=True):
            _, resp = resolve_validation_modes()
        assert resp == "warn"

    def test_generic_env_vars_do_not_flip_default(self) -> None:
        """Only ``ADCP_ENV`` is consulted. Generic ``ENV`` / ``ENVIRONMENT``
        are set by unrelated tooling (rails, postgres, 12-factor) and
        must not silently flip the SDK's default — that's a footgun."""
        for name in ("ENV", "ENVIRONMENT", "PYTHON_ENV"):
            with patch.dict(os.environ, {name: "production"}, clear=True):
                _, resp = resolve_validation_modes()
            assert resp == "strict", f"{name} should not flip the default"

    def test_explicit_config_overrides_defaults(self) -> None:
        req, resp = resolve_validation_modes(
            ValidationHookConfig(requests="strict", responses="off")
        )
        assert (req, resp) == ("strict", "off")
