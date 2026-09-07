"""Tests for `AdcpErrorInfo` extraction and wire-recovery-driven retry decisioning.

Covers the AdCP 3.2 `error.buyer_reason` sub-object and `error.recovery` field
adoption in the client-facing exception surface.
"""

from __future__ import annotations

from adcp.exceptions import (
    AdcpErrorInfo,
    ADCPTaskError,
    BuyerReasonInfo,
    extract_adcp_error_info,
)


class TestExtractAdcpErrorInfo:
    """Extraction from every input shape the SDK sees in the wild."""

    def test_from_dict_with_buyer_reason(self) -> None:
        raw = {
            "code": "PRODUCT_NOT_FOUND",
            "message": "no such product",
            "recovery": "correctable",
            "buyer_reason": {
                "code": "PRODUCT_NOT_FOUND",
                "message": "The product you selected is no longer available.",
            },
            "field": "product_id",
            "suggestion": "call get_products to refresh the catalog",
            "retry_after": 5,
            "details": {"rejected_value": "prod_stale"},
        }
        info = extract_adcp_error_info(raw)
        assert info.code == "PRODUCT_NOT_FOUND"
        assert info.recovery == "correctable"
        assert info.buyer_reason == BuyerReasonInfo(
            code="PRODUCT_NOT_FOUND",
            message="The product you selected is no longer available.",
        )
        assert info.field == "product_id"
        assert info.suggestion == "call get_products to refresh the catalog"
        assert info.retry_after == 5.0
        assert info.details == {"rejected_value": "prod_stale"}

    def test_from_dict_without_buyer_reason(self) -> None:
        info = extract_adcp_error_info(
            {"code": "RATE_LIMITED", "message": "slow down", "recovery": "transient"}
        )
        assert info.buyer_reason is None
        assert info.recovery == "transient"

    def test_from_duck_typed_object(self) -> None:
        class Duck:
            code = "INVALID_REQUEST"
            message = "bad shape"

        info = extract_adcp_error_info(Duck())
        assert info.code == "INVALID_REQUEST"
        assert info.buyer_reason is None
        assert info.recovery is None

    def test_from_pydantic_error_model(self) -> None:
        from adcp.types._generated import BuyerReason as WireBuyerReason
        from adcp.types._generated import Recovery
        from adcp.types.generated_poc.core.error import Error as WireError

        wire = WireError(
            code="BUDGET_TOO_LOW",
            message="budget is below the minimum",
            recovery=Recovery.correctable,
            buyer_reason=WireBuyerReason(
                code="BUDGET_TOO_LOW",
                message="Your budget is below the minimum this publisher accepts.",
            ),
        )
        info = extract_adcp_error_info(wire)
        assert info.code == "BUDGET_TOO_LOW"
        assert info.recovery == "correctable"
        assert info.buyer_reason is not None
        assert info.buyer_reason.code == "BUDGET_TOO_LOW"
        assert "budget is below the minimum" in info.buyer_reason.message

    def test_unknown_recovery_normalizes_to_none(self) -> None:
        # Per the AdCP forward-compat rule, an unrecognized `recovery` string
        # normalizes to None so receivers don't branch on a fourth value.
        info = extract_adcp_error_info(
            {"code": "SOMETHING", "message": "x", "recovery": "future_value"}
        )
        assert info.recovery is None

    def test_malformed_buyer_reason_ignored(self) -> None:
        # Missing `message` on the buyer_reason payload — reject rather than
        # construct a half-typed value.
        info = extract_adcp_error_info(
            {
                "code": "X",
                "message": "y",
                "buyer_reason": {"code": "X"},  # no message
            }
        )
        assert info.buyer_reason is None

    def test_missing_fields_default_to_none(self) -> None:
        info = extract_adcp_error_info({})
        assert info == AdcpErrorInfo(
            code=None,
            message=None,
            recovery=None,
            buyer_reason=None,
        )

    def test_details_non_dict_normalizes_to_none(self) -> None:
        # A non-dict `details` (e.g. a string from a non-conforming producer)
        # gets dropped rather than crashing consumers that type it as dict.
        info = extract_adcp_error_info({"code": "X", "message": "y", "details": "bad"})
        assert info.details is None

    def test_retry_after_bool_rejected(self) -> None:
        # `bool` is an `int` subclass in Python — without an explicit guard a
        # `retry_after: true` payload from a non-conforming producer would
        # extract as `1.0` and schedule a real retry.
        info = extract_adcp_error_info(
            {"code": "X", "message": "y", "retry_after": True}
        )
        assert info.retry_after is None

    def test_retry_after_infinity_rejected(self) -> None:
        info = extract_adcp_error_info(
            {"code": "X", "message": "y", "retry_after": float("inf")}
        )
        assert info.retry_after is None

    def test_retry_after_and_details_from_pydantic_model(self) -> None:
        from adcp.types._generated import Recovery
        from adcp.types.generated_poc.core.error import Error as WireError

        wire = WireError(
            code="RATE_LIMITED",
            message="slow down",
            recovery=Recovery.transient,
            retry_after=42,
            details={"quota_reset_at": "2026-09-08T00:00:00Z"},
        )
        info = extract_adcp_error_info(wire)
        assert info.retry_after == 42.0
        assert info.details == {"quota_reset_at": "2026-09-08T00:00:00Z"}


class TestADCPTaskErrorInfoAccessors:
    """Typed accessors on `ADCPTaskError`."""

    def _err(self, **overrides):
        base = {"code": "INVALID_REQUEST", "message": "bad"}
        base.update(overrides)
        return base

    def test_error_info_cached(self) -> None:
        # `cached_property` contract — same tuple identity on repeat access.
        err = ADCPTaskError("create_media_buy", [self._err(code="A")])
        assert err.error_info is err.error_info

    def test_error_info_from_pydantic_models_end_to_end(self) -> None:
        # Pydantic-model plumbing through the exception accessor, not just via
        # `extract_adcp_error_info` in isolation.
        from adcp.types._generated import BuyerReason as WireBuyerReason
        from adcp.types._generated import Recovery
        from adcp.types.generated_poc.core.error import Error as WireError

        wire = WireError(
            code="BUDGET_TOO_LOW",
            message="budget below minimum",
            recovery=Recovery.correctable,
            buyer_reason=WireBuyerReason(
                code="BUDGET_TOO_LOW",
                message="Your budget is below the publisher's minimum.",
            ),
        )
        err = ADCPTaskError("create_media_buy", [wire])
        assert err.first_buyer_reason is not None
        assert err.first_buyer_reason.code == "BUDGET_TOO_LOW"
        assert err.wire_recoveries == ("correctable",)

    def test_empty_errors_list(self) -> None:
        err = ADCPTaskError("get_products", [])
        assert err.error_info == ()
        assert err.buyer_reasons == ()
        assert err.first_buyer_reason is None
        assert err.wire_recoveries == ()

    def test_error_info_preserves_order(self) -> None:
        err = ADCPTaskError(
            "create_media_buy",
            [
                self._err(code="A"),
                self._err(code="B"),
                self._err(code="C"),
            ],
        )
        assert [info.code for info in err.error_info] == ["A", "B", "C"]

    def test_buyer_reasons_collects_only_present(self) -> None:
        err = ADCPTaskError(
            "create_media_buy",
            [
                self._err(code="A"),  # no buyer_reason
                self._err(
                    code="B",
                    buyer_reason={"code": "BUDGET_TOO_LOW", "message": "raise the budget"},
                ),
                self._err(
                    code="C",
                    buyer_reason={
                        "code": "CREATIVE_REJECTED",
                        "message": "creative did not pass review",
                    },
                ),
            ],
        )
        assert len(err.buyer_reasons) == 2
        assert err.buyer_reasons[0].code == "BUDGET_TOO_LOW"
        assert err.first_buyer_reason == err.buyer_reasons[0]

    def test_first_buyer_reason_none_when_absent(self) -> None:
        err = ADCPTaskError("create_media_buy", [self._err(code="A")])
        assert err.first_buyer_reason is None
        assert err.buyer_reasons == ()

    def test_wire_recoveries_skip_absent(self) -> None:
        err = ADCPTaskError(
            "create_media_buy",
            [
                self._err(code="A"),  # no recovery
                self._err(code="B", recovery="transient"),
                self._err(code="C", recovery="correctable"),
            ],
        )
        assert err.wire_recoveries == ("transient", "correctable")


class TestRecoveryDrivenIsRetryable:
    """`is_retryable` prefers wire `recovery` and only falls back to codes."""

    def _err(self, **overrides):
        base = {"code": "RATE_LIMITED", "message": "slow"}
        base.update(overrides)
        return base

    def test_wire_terminal_blocks_retry_even_with_transient_code(self) -> None:
        # A response that carries `recovery: terminal` on one entry blocks the
        # retry regardless of whether another entry's code is in TRANSIENT_CODES.
        err = ADCPTaskError(
            "create_media_buy",
            [
                self._err(code="RATE_LIMITED", recovery="transient"),
                self._err(code="ACCOUNT_SUSPENDED", recovery="terminal"),
            ],
        )
        assert err.is_retryable is False

    def test_wire_transient_enables_retry_with_unknown_code(self) -> None:
        # An unrecognized code + `recovery: transient` on the wire — receiver
        # must trust the wire recovery per the forward-compat rule.
        err = ADCPTaskError(
            "create_media_buy",
            [self._err(code="X_VENDOR_HICCUP", recovery="transient")],
        )
        assert err.is_retryable is True

    def test_only_correctable_wire_recovery_blocks_retry(self) -> None:
        # `correctable` means "fix the request and resend" — not a retry.
        err = ADCPTaskError(
            "create_media_buy",
            [self._err(code="BUDGET_TOO_LOW", recovery="correctable")],
        )
        assert err.is_retryable is False

    def test_falls_back_to_code_table_when_no_wire_recovery(self) -> None:
        # Producer hasn't adopted `error.recovery`: legacy code-table behavior.
        # RATE_LIMITED is in TRANSIENT_CODES so it retries.
        err = ADCPTaskError("create_media_buy", [self._err(code="RATE_LIMITED")])
        assert err.is_retryable is True

    def test_falls_back_to_code_table_terminal(self) -> None:
        err = ADCPTaskError("create_media_buy", [self._err(code="ACCOUNT_NOT_FOUND")])
        assert err.is_retryable is False

    def test_correctable_wire_blocks_even_mixed_with_transient(self) -> None:
        # A batch with `[transient, correctable]` is NOT safely retryable as-is:
        # the correctable entry will re-trigger regardless of what the transient
        # entry does. Aligns with the docstring's "as-is without modification".
        err = ADCPTaskError(
            "create_media_buy",
            [
                self._err(code="RATE_LIMITED", recovery="transient"),
                self._err(code="BUDGET_TOO_LOW", recovery="correctable"),
            ],
        )
        assert err.is_retryable is False

    def test_unknown_code_without_wire_recovery_defaults_to_transient(self) -> None:
        # Per the AdCP forward-compat rule (`core/error.json` on `error.code`),
        # receivers MUST decode unknown codes and treat missing `recovery` as
        # `transient` — otherwise a producer that ships a new code before the
        # SDK learns about it silently becomes non-retryable.
        err = ADCPTaskError(
            "create_media_buy",
            [self._err(code="X_VENDOR_NEW_CODE_NOT_IN_TABLE")],
        )
        assert err.is_retryable is True

    def test_mixed_known_terminal_and_unknown_code_blocks(self) -> None:
        # An unknown code's transient default does not override a known
        # terminal in the same batch.
        err = ADCPTaskError(
            "create_media_buy",
            [
                self._err(code="X_VENDOR_HICCUP"),  # unknown → transient default
                self._err(code="ACCOUNT_NOT_FOUND"),  # known terminal
            ],
        )
        assert err.is_retryable is False

    def test_wire_transient_plus_no_wire_terminal_code_blocks(self) -> None:
        # Per-entry independence: one entry's wire `transient` does not
        # rescue another entry whose code-table classification is terminal.
        # Locks in that the effective-recovery loop treats each entry on its
        # own before batch aggregation.
        err = ADCPTaskError(
            "create_media_buy",
            [
                self._err(code="RATE_LIMITED", recovery="transient"),
                self._err(code="ACCOUNT_NOT_FOUND"),  # no wire → terminal via table
            ],
        )
        assert err.is_retryable is False
